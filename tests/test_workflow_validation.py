from types import SimpleNamespace

import pytest

from app.models import (
    AnalysisState,
    ValidationIssue,
    ValidationReport,
    Severity,
)
from app.llm import LLMStructuredOutputError
from app.workflow import (
    _query_spec_from_arguments,
    route_intent,
    route_plan,
    route_reflection,
    reflect_node,
)


def _reflection_tracker() -> SimpleNamespace:
    return SimpleNamespace(
        prompt=SimpleNamespace(content="reflection prompt"),
        record_diagnostics=lambda diagnostics: None,
        complete=lambda output: output,
        fail=lambda error: None,
    )


def test_failed_validation_is_reviewed_by_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.workflow.repository.update_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.workflow.start_node", lambda *args, **kwargs: _reflection_tracker())
    reviewer = SimpleNamespace(model_dump=lambda **kwargs: {
        "verdict": "revise",
        "route": "rewrite",
        "reason": "修复证据引用",
        "issues": [],
    })
    monkeypatch.setattr("app.workflow.llm.structured", lambda *args, **kwargs: reviewer)
    validation = ValidationReport(
        passed=False,
        issues=[ValidationIssue(
            code="invalid_evidence_pointer",
            message="证据定位无效",
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


def test_reflection_schema_error_is_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.workflow.repository.update_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.workflow.start_node", lambda *args, **kwargs: _reflection_tracker())
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

    with pytest.raises(LLMStructuredOutputError):
        reflect_node(state)


def test_query_arguments_preserve_model_query() -> None:
    spec = _query_spec_from_arguments(
        {"query": {
            "dataset_id": "dataset",
            "dimensions": ["部门"],
            "measures": [{"field": "营业收入", "aggregation": "sum", "alias": "营业收入合计"}],
            "limit": 100,
        }},
    )
    assert spec.dataset_id == "dataset"
    assert spec.measures[0].field == "营业收入"
    assert spec.measures[0].aggregation == "sum"


def test_query_arguments_preserve_model_order_expression() -> None:
    spec = _query_spec_from_arguments(
        {"query": {
            "dataset_id": "dataset",
            "dimensions": ["Department"],
            "measures": [{"field": "Sum", "aggregation": "sum", "alias": "盈亏总额"}],
            "order_by": "Sum",
            "limit": 100,
        }},
    )

    assert spec.order_by == "Sum"


def test_query_arguments_preserve_model_choice_not_to_sort() -> None:
    spec = _query_spec_from_arguments(
        {"query": {
            "dataset_id": "dataset",
            "dimensions": [],
            "measures": [{"field": "金额", "aggregation": "sum", "alias": "金额合计"}],
            "order_by": None,
            "descending": False,
            "filters": [],
            "joins": [],
            "limit": 100,
        }},
    )

    assert spec.order_by is None


def test_workflow_routes_cover_terminal_and_revision_branches() -> None:
    state = AnalysisState(
        task_id="task", run_id="run", user_question="hello",
        intent={"route": "conversation", "reply": "hello"},
    )
    assert route_intent(state) == "off_topic"
    state.intent = {"route": "analysis", "reply": None}
    state.plan = {"goal": "分析", "can_execute": False}
    assert route_plan(state) == "clarify"
    state.plan = {"goal": "分析", "can_execute": True}
    assert route_plan(state) == "execute"
    state.reflection = {"verdict": "revise", "route": "rewrite", "reason": "缺少证据"}
    state.revision_round = 1
    assert route_reflection(state) == "rewrite"
    state.revision_round = 3
    assert route_reflection(state) == "rewrite"
    state.revision_round = 4
    assert route_reflection(state) == "finish"
