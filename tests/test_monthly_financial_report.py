from pathlib import Path
import uuid

import pytest

from app.config import settings
from app.ingestion import _read_xlsx, ingest_file
from app.models import UploadedFile, AnalysisState
from app.repository import repository

FIXTURE = Path(__file__).parent / "fixtures" / "monthly_financial_report.xlsx"


def test_statement_headers_and_original_rows():
    result = _read_xlsx(FIXTURE)
    assert len(result.regions) == 6
    assert all(r.header_start_row == r.header_end_row == 4 for r in result.regions)
    expense = next(r.frame for r in result.regions if r.sheet_name == "费用明细")
    assert expense.filter(expense["报表行类型"] == "detail").height == 7
    assert expense.filter(expense["费用项目"] == "借款利息")["来源行号"][0] == 11
    balance = next(r.frame for r in result.regions if r.sheet_name == "资产负债表")
    assert {"短期借款", "实收资本", "未分配利润"} <= set(balance["项目"])


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
    file = UploadedFile(id=str(uuid.uuid4()), original_name=FIXTURE.name, size=FIXTURE.stat().st_size, status="ready")
    repository.add_file(task_id, file, str(FIXTURE))
    datasets = ingest_file(task_id, file, FIXTURE)
    yield task_id, datasets


def test_financial_workflow_end_to_end(imported_report, monkeypatch):
    from app.models import EvidencePointer, GeneratedAnalysisDraft, IntentDecision, Metric, PlanDecision, PlanStep

    task_id, datasets = imported_report

    def model(prompt, context, *args, **kwargs):
        if prompt == "intent_classifier":
            return IntentDecision(route="analysis", reply=None)
        if prompt == "analysis_planner":
            return PlanDecision(
                action="analyze",
                goal="分析整套财务报表",
                clarification=None,
                clarification_options=[],
                steps=[
                    PlanStep(
                        id="financial_report",
                        purpose="核对六张报表",
                        tool="query_financial_report",
                        dataset_id=datasets[0].id,
                        dataset_ids=[d.id for d in datasets[1:]],
                    )
                ],
            )
        assert prompt == "draft_writer"
        evidence = context["evidence_catalog"][0]
        selected = [(row['row_index'], row['cells']) for row in evidence["rows"][:2]]
        return GeneratedAnalysisDraft(
            title="月度财务报表分析",
            summary="模型已分析整套财务报表并核对报表项目。",
            summary_evidence_refs=[evidence["id"]],
            summary_evidence_pointers=[EvidencePointer(**evidence['rows'][0]['value_pointers']['金额'])],
            metrics=[
                Metric(
                    label=f"{row['项目']}（{row['口径']}）",
                    value=str(row["金额"]),
                    evidence_refs=[evidence["id"]],
                    evidence_pointers=[
                        EvidencePointer(
                            evidence_id=evidence["id"],
                            row_index=index,
                            field="金额",
                            raw_value=str(row["金额"]),
                        )
                    ],
                )
                for index, row in selected
            ],
        )

    monkeypatch.setattr("app.workflow.llm.structured", model)
    from app.workflow import build_graph

    run = repository.start_execution(task_id, "分析报表")
    graph = build_graph()
    result = graph.invoke(
        AnalysisState(
            task_id=task_id,
            run_id=run,
            user_question="分析报表",
            datasets=[d.model_dump(mode="json") for d in datasets],
        ),
        config={"configurable": {"thread_id": run}},
    )
    assert result["final_status"] in {"completed", "completed_with_warnings"}, result.get("error")
    assert result["validation"]["passed"], result["validation"]
    # A structurally valid draft does not need a repair round.
    assert result["revision_round"] == 0
    assert len(result["plan"]["steps"][0]["dataset_ids"]) == 5
    rows = result["tool_results"][0]["rows"]

    def amount(sheet, item, field):
        return float(next(r["金额"] for r in rows if (r["报表"], r["项目"], r["口径"]) == (sheet, item, field)))

    assert amount("利润表", "营业收入", "本月金额") == 300000
    assert amount("利润表", "营业成本", "本月金额") == 120000
    assert amount("利润表", "净利润", "本月金额") == 81000
    assert amount("资产负债表", "资产总计", "月末余额") == 638000
    assert amount("现金流量表", "月末现金及现金等价物余额", "本月金额") == 323000
    assert amount("应收账款", "明细汇总", "月初余额") == 120000
    assert amount("应付账款", "明细汇总", "月初余额") == 80000
    assert all(float(r["金额"]) == 0 for r in rows if r["报表"] == "勾稽核对")
    assert len(result["draft"]["metrics"]) == 2
    assert result["draft"]["delivery"]["status"] in {"completed", "completed_with_warnings"}


def test_claim_validation_only_checks_explicit_pointer_integrity(imported_report):
    from app.financial_reports import query_financial_report
    from app.evidence_validation import validate_claim
    from app.models import EvidencePointer

    task_id, datasets = imported_report
    result = query_financial_report(task_id, datasets)
    row = result.rows[0]
    state = AnalysisState(
        task_id=task_id, run_id="test", user_question="分析报表", tool_results=[result.model_dump(mode="json")]
    )
    assert validate_claim(
        state,
        "metric",
        result.evidence_ids,
        [EvidencePointer(evidence_id=result.evidence_ids[0], row_index=0, field="金额", raw_value=str(row["金额"]))],
    ) == []


def test_reconciliation_differences_are_exposed_without_backend_judgment(imported_report):
    import duckdb
    from app.financial_reports import query_financial_report
    from app.ingestion import task_dir

    task_id, datasets = imported_report
    ar = next(d for d in datasets if d.display_name == "应收账款")
    with duckdb.connect(str(task_dir(task_id) / "work" / "analysis.duckdb")) as connection:
        connection.execute(f'UPDATE "{ar.table_name}" SET "月初余额"=50001 WHERE "来源行号"=5')
    result = query_financial_report(task_id, datasets)
    checks = [row for row in result.rows if row["报表"] == "勾稽核对"]
    assert any("明细与合计" in row["项目"] and float(row["金额"]) != 0 for row in checks)
    assert any("与主表" in row["项目"] and float(row["金额"]) != 0 for row in checks)
    assert result.warnings == []
