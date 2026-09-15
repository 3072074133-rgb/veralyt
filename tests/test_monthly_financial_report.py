from pathlib import Path
import uuid

import pytest

from app.analysis_tools import execute_sql
from app.config import settings
from app.ingestion import _read_xlsx, ingest_file
from app.models import UploadedFile, AnalysisState
from app.repository import repository

FIXTURE = Path(__file__).parent / "fixtures" / "monthly_financial_report.xlsx"


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def test_statement_headers_and_original_rows():
    result = _read_xlsx(FIXTURE)
    assert len(result.regions) == 6
    assert all(r.header_start_row == r.header_end_row == 4 for r in result.regions)
    expense = next(r.frame for r in result.regions if r.sheet_name == "费用明细")
    assert expense.height == 8
    assert "借款利息" in set(expense["费用项目"])
    balance = next(r.frame for r in result.regions if r.sheet_name == "资产负债表")
    assert {"短期借款", "实收资本", "未分配利润"} <= set(balance["项目"])
    assert "报表行类型" not in balance.columns and "来源行号" not in balance.columns


def test_native_table_header_wins_over_numeric_business_row(tmp_path):
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table

    path = tmp_path / "native.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["费用项目", "归属科目", "金额", "凭证"])
    sheet.append(["租金", "管理费用", 5000, "FY-001"])
    sheet.append(["利息", "财务费用", 2000, "FY-002"])
    sheet.add_table(Table(displayName="Expenses", ref="A1:D3"))
    workbook.save(path)
    region = _read_xlsx(path).regions[0]
    assert region.header_end_row == 1
    assert region.frame.height == 2


@pytest.fixture
def imported_report(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(repository, "db_path", tmp_path / "app.sqlite")
    repository.initialize()
    task_id = repository.create_task()
    file = UploadedFile(id=str(uuid.uuid4()), original_name=FIXTURE.name,
                        size=FIXTURE.stat().st_size, status="ready")
    repository.add_file(task_id, file, str(FIXTURE))
    datasets = ingest_file(task_id, file, FIXTURE)
    yield task_id, datasets


def _six_table_union(datasets) -> str:
    statements = []
    for dataset in datasets:
        label = dataset.display_name.replace("'", "''")
        item = _quote(dataset.columns[0].name)
        value = _quote(next(column.name for column in dataset.columns[1:]
                            if "Int" in column.data_type or "Float" in column.data_type))
        statements.append(
            f"SELECT '{label}' AS {_quote('报表')}, CAST({item} AS VARCHAR) AS {_quote('项目')}, "
            f"TRY_CAST({value} AS DOUBLE) AS {_quote('金额')} FROM {_quote(dataset.table_name)}"
        )
    return " UNION ALL ".join(statements)


def test_generic_sql_workflow_end_to_end_preserves_all_tables(imported_report, monkeypatch):
    from app.models import (EvidencePointer, ChapterAnalysisDraft, ReportOutline, ReportSection, ReportBlock, IntentDecision, Metric,
                            PlanDecision, PlanStep, ReflectionDecision, ResultDecision)

    task_id, datasets = imported_report
    sql = _six_table_union(datasets)

    def model(prompt, context, *args, **kwargs):
        if prompt == "intent_classifier":
            return IntentDecision(route="analysis", reply=None)
        if prompt == "analysis_planner":
            assert len(context["dataset_catalog"]) == 6
            assert all(item["sample_rows"] for item in context["dataset_catalog"])
            return PlanDecision(action="analyze", goal="分析整套报表", clarification=None,
                clarification_options=[], steps=[PlanStep(
                    id="all_tables", purpose="汇总全部工作表", dataset_ids=[d.id for d in datasets], sql=sql)])
        if prompt == "result_reviewer":
            return ResultDecision(action="draft", reason="查询结果足以生成报告")
        if prompt == "final_reviewer":
            return ReflectionDecision(verdict="pass", route="finish", reason="报告可交付")
        assert prompt == "draft_writer"
        if args[0] is ReportOutline:
            return ReportOutline(sections=[{'id': 'finance', 'title': '关键数据', 'purpose': '分析数据'}])
        evidence = context["evidence_catalog"][0]
        pointers = evidence["rows"][0][1]
        amount_index = evidence["columns"].index("金额")
        return ChapterAnalysisDraft(
            title="月度报表分析", summary="已分析全部工作表。",
            summary_evidence_pointers=[EvidencePointer(citation_id=pointers[amount_index])],
            sections=[ReportSection(id='finance', title='关键数据', blocks=[ReportBlock(
                id='finance-metrics', kind='metrics', metrics=[Metric(
                    label="首项金额", value=str(evidence["rows"][0][0][amount_index]),
                    evidence_pointers=[EvidencePointer(citation_id=pointers[amount_index])])])])],
        )

    monkeypatch.setattr("app.workflow.llm.structured", model)
    from app.workflow import build_graph
    run = repository.start_execution(task_id, "分析报表")
    result = build_graph().invoke(AnalysisState(
        task_id=task_id, run_id=run, user_question="分析报表",
        datasets=[d.model_dump(mode="json") for d in datasets]),
        config={"configurable": {"thread_id": run}})

    assert result["final_status"] in {"completed", "completed_with_warnings"}
    assert result["reflection"]["verdict"] == "pass"
    assert len(result["plan"]["steps"][0]["dataset_ids"]) == 6
    assert {row["报表"] for row in result["tool_results"][0]["rows"]} == {
        dataset.display_name for dataset in datasets
    }


def test_claim_validation_accepts_generic_sql_evidence(imported_report):
    from app.evidence_validation import validate_claim
    from app.models import EvidencePointer

    task_id, datasets = imported_report
    dataset = datasets[0]
    field = dataset.columns[1].name
    result = execute_sql(task_id, [dataset],
        f"SELECT {_quote(field)} AS value FROM {_quote(dataset.table_name)} LIMIT 1")
    state = AnalysisState(task_id=task_id, run_id="test", user_question="分析报表",
                          tool_results=[result.model_dump(mode="json")])
    assert validate_claim(state, "metric", result.evidence_ids, [EvidencePointer(
        evidence_id=result.evidence_ids[0], row_index=0, field="value",
        raw_value=str(result.rows[0]["value"]))]) == []


def test_backend_executes_model_sql_without_business_rewrite(imported_report):
    task_id, datasets = imported_report
    dataset = next(item for item in datasets if item.display_name == "应收账款")
    result = execute_sql(task_id, [dataset],
        f"SELECT SUM({_quote('逾期余额')}) AS {_quote('模型定义结果')} "
        f"FROM {_quote(dataset.table_name)}")
    # The source includes detail and a displayed total row.  The executor
    # returns the model's literal SUM result and does not apply a hidden rule.
    assert result.rows == [{"模型定义结果": 100000}]
    assert result.warnings == []
