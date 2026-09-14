from unittest.mock import Mock

import pytest

from app import workflow
from app.models import IntentDecision
from app.repository import repository


@pytest.mark.parametrize(
    "decision",
    [
        IntentDecision(route="conversation", reply="hello"),
        IntentDecision(route="clarification", reply="which period?"),
        IntentDecision(route="explanation", reply="建议优先关注现金流。"),
    ],
)
def test_short_routes_never_prepare_context(tmp_path, monkeypatch, decision):
    monkeypatch.setattr(repository, "db_path", tmp_path / "routing.sqlite")
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, "hello", status="queued")
    monkeypatch.setattr(workflow.llm, "structured", lambda *args, **kwargs: decision)
    prepare = Mock(side_effect=AssertionError("summary should not run"))
    monkeypatch.setattr(workflow.context_manager, "prepare", prepare)
    workflow.run_analysis(run)
    assert repository.get_run_by_id(run).status in {"off_topic", "needs_clarification"}
    prepare.assert_not_called()


def test_explanation_route_ends_at_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "db_path", tmp_path / "explanation.sqlite")
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, "有什么建议吗", status="queued")
    monkeypatch.setattr(
        workflow.llm,
        "structured",
        lambda *args, **kwargs: IntentDecision(route="explanation", reply="建议优先核查回款和费用结构。"),
    )
    prepare = Mock(side_effect=AssertionError("explanation must not prepare strict context"))
    monkeypatch.setattr(workflow.context_manager, "prepare", prepare)
    workflow.run_analysis(run)
    assert repository.get_run_by_id(run).status == "off_topic"
    prepare.assert_not_called()


def test_missing_direct_reply_fails_model_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "db_path", tmp_path / "direct-answer.sqlite")
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, "有什么建议吗", status="queued")
    monkeypatch.setattr(workflow.llm, "structured", lambda *args, **kwargs: IntentDecision(route="explanation"))

    workflow.run_analysis(run)

    assert repository.get_run_by_id(run).status == "failed"
