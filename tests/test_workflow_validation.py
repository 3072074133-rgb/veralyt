from pathlib import Path
from types import SimpleNamespace

import pytest

from app import analysis_tools
from app.analysis_tools import _choose_measure
from app.models import (
    AnalysisDraft,
    AnalysisPlan,
    AnalysisState,
    DatasetColumn,
    DatasetInfo,
    EvidenceRecord,
    Metric,
    ToolExecutionResult,
    ValidationIssue,
    ValidationReport,
    Severity,
)
from app.llm import LLMStructuredOutputError
from app.repository import repository
from app.workflow import (
    _as_decimal,
    _generic_overview_draft,
    _normalize_optional_metric_changes,
    _normalize_plan_for_request,
    _numeric_tokens,
    _resolve_evidence_pointers,
    _evidence_row_count_supports,
    _compact_draft,
    _department_profit_draft,
    _is_sectioned_department_profit_request,
    _query_spec_from_arguments,
    _unsupported_numbers,
    route_intent,
    route_plan,
    route_reflection,
    reflect_node,
)


@pytest.fixture()
def evidence_repo(tmp_path: Path):
    old_path = repository.db_path
    repository.db_path = tmp_path / "validation.sqlite"
    repository.initialize()
    task_id = repository.create_task()
    repository.add_evidence(
        EvidenceRecord(
            id="ev_current",
            task_id=task_id,
            title="同比证据",
            source="query_data",
            columns=["期间", "收入", "同比百分比", "年度范围合计", "异常阈值"],
            rows=[
                {
                    "期间": "2026-03",
                    "收入": "174000",
                    "同比百分比": "-25",
                    "年度范围合计": "631000",
                    "异常阈值": "1.5",
                }
            ],
        )
    )
    yield task_id
    repository.db_path = old_path


def test_numeric_evidence_requires_exact_value(evidence_repo: str) -> None:
    assert _unsupported_numbers("2026年3月收入174000，同比下降25%", ["ev_current"], evidence_repo) == []
    assert _unsupported_numbers("2026年1-3月合计631000", ["ev_current"], evidence_repo) == []
    assert _unsupported_numbers("收入174001", ["ev_current"], evidence_repo) == ["174001"]


def test_cell_pointer_requires_unique_row(evidence_repo: str) -> None:
    pointers = _resolve_evidence_pointers("收入为174000", ["ev_current"], evidence_repo)
    assert len(pointers) == 1
    assert pointers[0].field == "收入"
    evidence = repository.get_evidence(evidence_repo, "ev_current")
    assert evidence is not None
    evidence.rows.append(dict(evidence.rows[0]))
    repository.add_evidence(evidence)
    assert _resolve_evidence_pointers("收入为174000", ["ev_current"], evidence_repo) == []


def test_decimal_parser_rejects_date_labels() -> None:
    assert _as_decimal("1,234.50%") == _as_decimal("1234.5")
    assert _as_decimal("2026-03") is None


def test_date_tokens_are_ignored_next_to_chinese_text() -> None:
    assert _numeric_tokens("期间为2025-01时，收入为1530000。") == ["1530000"]


def test_numeric_suffix_in_known_field_name_is_not_a_business_value() -> None:
    assert _numeric_tokens(
        "未命名列3数据概览",
        ignored_terms={"未命名列3"},
    ) == []
    assert _numeric_tokens(
        "未命名列3合计1530000",
        ignored_terms={"未命名列3"},
    ) == ["1530000"]


def _reflection_tracker() -> SimpleNamespace:
    return SimpleNamespace(
        prompt=SimpleNamespace(content="reflection prompt"),
        record_diagnostics=lambda diagnostics: None,
        complete=lambda output: output,
        fail=lambda error: pytest.fail(f"reflection unexpectedly failed: {error}"),
    )


def test_failed_validation_skips_model_reflection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.workflow.repository.update_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.workflow.begin_node", lambda *args, **kwargs: _reflection_tracker())
    monkeypatch.setattr(
        "app.workflow.llm.structured",
        lambda *args, **kwargs: pytest.fail("model should not run after deterministic validation failed"),
    )
    validation = ValidationReport(
        passed=False,
        issues=[ValidationIssue(
            code="unreferenced_title_number",
            message="字段名编号被误判",
            severity=Severity.ERROR,
            target="title",
        )],
    )
    state = AnalysisState(
        task_id="task",
        run_id="run",
        user_question="分析报表",
        validation=validation.model_dump(mode="json"),
    )

    result = reflect_node(state)

    assert result["reflection"]["verdict"] == "revise"
    assert result["reflection"]["route"] == "rewrite"


def test_reflection_schema_error_falls_back_to_validated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.workflow.repository.update_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.workflow.begin_node", lambda *args, **kwargs: _reflection_tracker())
    monkeypatch.setattr(
        "app.workflow.llm.structured",
        lambda *args, **kwargs: (_ for _ in ()).throw(LLMStructuredOutputError("invalid schema")),
    )
    state = AnalysisState(
        task_id="task",
        run_id="run",
        user_question="比较不同部门的利润变化",
        validation=ValidationReport(passed=True).model_dump(mode="json"),
    )

    result = reflect_node(state)

    assert result["reflection"]["verdict"] == "pass"
    assert result["reflection"]["route"] == "finish"


def test_query_arguments_normalize_local_model_shape() -> None:
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="收入",
        row_count=1,
        columns=[
            DatasetColumn(name="部门", display_name="部门", data_type="String", null_count=0),
            DatasetColumn(name="营业收入", display_name="营业收入", data_type="Int64", null_count=0),
        ],
    )
    spec = _query_spec_from_arguments(
        {"query": {
            "dimensions": ["部门"],
            "metrics": ["营业收入"],
            "aggregations": {"营业收入": "sum"},
        }},
        [dataset],
    )
    assert spec.dataset_id == "dataset"
    assert spec.measures[0].field == "营业收入"
    assert spec.measures[0].aggregation == "sum"


def test_query_arguments_split_order_direction_from_alias() -> None:
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="盈亏",
        row_count=1,
        columns=[
            DatasetColumn(name="Department", display_name="Department", data_type="String", null_count=0),
            DatasetColumn(name="Sum", display_name="Sum", data_type="Float64", null_count=0),
        ],
    )

    spec = _query_spec_from_arguments(
        {"query": {
            "dataset_id": "dataset",
            "dimensions": ["Department"],
            "measures": [{"field": "Sum", "aggregation": "sum", "alias": "盈亏"}],
            "order_by": "盈亏 DESC",
        }},
        [dataset],
    )

    assert spec.order_by == "盈亏"
    assert spec.descending is True


def test_query_arguments_map_measure_field_order_to_output_alias() -> None:
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="盈亏",
        row_count=1,
        columns=[
            DatasetColumn(name="Department", display_name="Department", data_type="String", null_count=0),
            DatasetColumn(name="Sum", display_name="Sum", data_type="Float64", null_count=0),
        ],
    )

    spec = _query_spec_from_arguments(
        {"query": {
            "dataset_id": "dataset",
            "dimensions": ["Department"],
            "measures": [{"field": "Sum", "aggregation": "sum", "alias": "盈亏总额"}],
            "order_by": "Sum",
        }},
        [dataset],
    )

    assert spec.order_by == "盈亏总额"


def test_sectioned_department_profit_strategy_requires_matching_question_and_samples() -> None:
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="KZE Total",
        row_count=6,
        columns=[
            DatasetColumn(
                name="Department", display_name="Department", data_type="String", null_count=0,
                sample_values=["Ebiz Revenue (Kimmy&Carl)", "Gross Profit", "Net Profit"],
            ),
            DatasetColumn(name="Sum", display_name="Sum", data_type="Float64", null_count=0),
        ],
    )

    assert _is_sectioned_department_profit_request("统计每个部门的盈亏", dataset) is True
    assert _is_sectioned_department_profit_request("统计各项收入", dataset) is False


def test_department_profit_draft_is_built_from_verified_rows() -> None:
    state = AnalysisState(
        task_id="task",
        run_id="run",
        user_question="统计各部门盈亏",
        tool_results=[{
            "result_id": "result",
            "tool_name": "query_data",
            "status": "success",
            "summary": "各部门净利润",
            "arguments": {"sql": "SELECT regexp_matches(profit.label, 'Net Profit')"},
            "rows": [
                {"部门": "Ebiz", "净利润": "8464748.80"},
                {"部门": "MCN", "净利润": "-47798.38"},
            ],
            "evidence_ids": ["ev_profit"],
        }],
    )

    draft = _department_profit_draft(state)

    assert draft is not None
    assert draft.title == "各部门净利润分析"
    assert draft.metrics[0].value == "8464748.80"
    assert draft.metrics[1].value == "-47798.38"
    assert draft.findings[-1].severity == "warning"
    assert draft.charts[0].dataset_ref == "ev_profit"


def test_evidence_row_count_supports_explicit_result_count(evidence_repo: str) -> None:
    assert _evidence_row_count_supports("1", "共 1 行结果", ["ev_current"], evidence_repo) is True
    assert _evidence_row_count_supports("2", "共 2 行结果", ["ev_current"], evidence_repo) is False
    assert _evidence_row_count_supports("1", "增长 1 元", ["ev_current"], evidence_repo) is False


def test_compact_draft_omits_repeated_pointers_and_calculation_sql() -> None:
    compact = _compact_draft({
        "title": "部门盈亏",
        "summary": "汇总完成",
        "summary_evidence_refs": ["ev_1"],
        "summary_evidence_pointers": [{"evidence_id": "ev_1"}],
        "metrics": [{
            "label": "Ebiz", "value": "10", "change": None, "direction": "neutral",
            "evidence_refs": ["ev_1"], "evidence_pointers": [{"evidence_id": "ev_1"}],
        }],
        "findings": [], "charts": [], "assumptions": [], "warnings": [],
        "suggested_questions": [], "calculation_details": [{"query": "SELECT 1"}],
    })

    assert compact is not None
    assert "summary_evidence_pointers" not in compact
    assert "calculation_details" not in compact
    assert "evidence_pointers" not in compact["metrics"][0]


def test_unsupported_optional_change_is_removed_without_weakening_value_check(evidence_repo: str) -> None:
    draft = AnalysisDraft(
        title="测试",
        summary="测试",
        metrics=[
            Metric(
                label="收入",
                value="174000",
                change="0",
                direction="flat",
                evidence_refs=["ev_current"],
            )
        ],
    )

    normalized = _normalize_optional_metric_changes(draft, evidence_repo)

    assert normalized.metrics[0].value == "174000"
    assert normalized.metrics[0].change is None
    assert normalized.metrics[0].direction == "neutral"
    assert _unsupported_numbers(normalized.metrics[0].value, ["ev_current"], evidence_repo) == []


def test_default_measure_prefers_amount_over_numeric_month() -> None:
    columns = [
        DatasetColumn(name="Month", display_name="Month", data_type="Int64", null_count=0),
        DatasetColumn(name="Amount", display_name="Amount", data_type="Float64", null_count=0),
    ]

    assert _choose_measure(columns, "分析数据").name == "Amount"


def test_semantic_amount_is_preferred_over_unlabelled_numeric_metric() -> None:
    columns = [
        DatasetColumn(
            name="score", display_name="score", data_type="Float64", null_count=0,
            semantic_type="metric", role="measure", default_aggregation="sum",
        ),
        DatasetColumn(
            name="cash", display_name="cash", data_type="Float64", null_count=0,
            semantic_type="amount", role="measure", default_aggregation="sum",
        ),
    ]

    assert _choose_measure(columns, "分析数据").name == "cash"


def test_default_measure_skips_generated_placeholder_headers() -> None:
    columns = [
        DatasetColumn(
            name="未命名列3", display_name="未命名列3", data_type="Float64", null_count=0,
            semantic_type="metric", role="measure", default_aggregation="sum",
        ),
        DatasetColumn(
            name="收入", display_name="收入", data_type="Float64", null_count=0,
            semantic_type="amount", role="measure", default_aggregation="sum",
        ),
    ]

    assert _choose_measure(columns, "分析数据").name == "收入"


def test_default_measure_returns_none_when_only_placeholders_exist() -> None:
    columns = [
        DatasetColumn(
            name="未命名列3", display_name="未命名列3", data_type="Float64", null_count=0,
            semantic_type="metric", role="measure", default_aggregation="sum",
        ),
    ]

    assert _choose_measure(columns, "分析数据") is None


def test_generic_analysis_request_replaces_unrelated_model_plan() -> None:
    plan = AnalysisPlan(goal="验证JSON格式正确性", can_execute=True)

    normalized = _normalize_plan_for_request(plan, "分析数据")

    assert normalized.goal == "概览主要金额指标及其业务分布"
    assert normalized.steps[0].tool == "auto_analyze"


def test_categorical_overview_uses_absolute_contribution(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_query(task_id, datasets, sql, title, run_id):
        captured["sql"] = sql
        return ToolExecutionResult(
            result_id="result", tool_name="query_data", status="success", summary=title
        )

    monkeypatch.setattr(analysis_tools, "query_data", fake_query)
    dataset = DatasetInfo(
        id="dataset",
        file_id="file",
        table_name="data_table",
        display_name="费用表",
        row_count=2,
        columns=[
            DatasetColumn(name="From", display_name="From", data_type="String", null_count=0),
            DatasetColumn(name="Amount", display_name="Amount", data_type="Float64", null_count=0),
        ],
    )

    analysis_tools.auto_analyze("task", [dataset], "分析数据")

    assert 'SUM(ABS("Amount")) OVER ()' in captured["sql"]
    assert 'ORDER BY ABS("Amount") DESC' in captured["sql"]
    assert "绝对金额贡献百分比" in captured["sql"]


def test_generic_draft_keeps_raw_amount_as_primary_measure() -> None:
    state = AnalysisState(
        task_id="task",
        run_id="run",
        user_question="分析数据",
        tool_results=[
            {
                "status": "success",
                "evidence_ids": ["ev_amount"],
                "rows": [
                    {
                        "From": "WF",
                        "Amount": "-1707172.4",
                        "记录数": 121,
                        "绝对金额贡献百分比": "57.75",
                        "绝对影响排名": 1,
                    }
                ],
            }
        ],
    )

    draft = _generic_overview_draft(state)

    assert draft is not None
    assert draft.title == "Amount数据概览"
    assert draft.metrics[0].value == "-1707172.4"
    assert draft.charts[0].series[0].field == "Amount"


def test_workflow_routes_cover_terminal_and_revision_branches() -> None:
    state = AnalysisState(task_id="task", run_id="run", user_question="hello", intent={"is_analysis": False})
    assert route_intent(state) == "off_topic"
    state.intent = {"is_analysis": True}
    state.plan = {"goal": "分析", "can_execute": False}
    assert route_plan(state) == "clarify"
    state.plan = {"goal": "分析", "can_execute": True}
    assert route_plan(state) == "execute"
    state.reflection = {"verdict": "revise", "route": "rewrite", "reason": "缺少证据"}
    state.revision_round = 1
    assert route_reflection(state) == "rewrite"
    state.revision_round = 3
    assert route_reflection(state) == "finish"
