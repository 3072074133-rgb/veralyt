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
    monkeypatch.setattr("app.workflow.repository.add_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.workflow.start_node", lambda *args, **kwargs: _reflection_tracker())
    from app.models import ReflectionDecision
    reviewer = ReflectionDecision.model_validate({
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
