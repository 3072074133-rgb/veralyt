from unittest.mock import Mock

import pytest

from app import workflow
from app.models import IntentDecision
from app.repository import repository


@pytest.mark.parametrize('decision', [IntentDecision(route='conversation', reply='hello'),
                                     IntentDecision(route='clarification', reply='which period?'),
                                     IntentDecision(route='explanation', reply='建议优先关注现金流。')])
def test_short_routes_never_prepare_or_retrieve(tmp_path, monkeypatch, decision):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'routing.sqlite')
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, 'hello', status='queued')
    monkeypatch.setattr(workflow.llm, 'structured', lambda *args, **kwargs: decision)
    prepare = Mock(side_effect=AssertionError('summary should not run'))
    retrieve = Mock(side_effect=AssertionError('embedding should not run'))
    monkeypatch.setattr(workflow.context_manager, 'prepare', prepare)
    monkeypatch.setattr(workflow, 'retrieve_for_run', retrieve)
    workflow.run_analysis(run)
    assert repository.get_run_by_id(run).status in {'off_topic', 'needs_clarification'}
    prepare.assert_not_called()
    retrieve.assert_not_called()


def test_explanation_route_ends_at_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'explanation.sqlite')
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, '有什么建议吗', status='queued')
    monkeypatch.setattr(workflow.llm, 'structured', lambda *args, **kwargs: IntentDecision(
        route='explanation', reply='建议优先核查回款和费用结构。'))
    prepare = Mock(side_effect=AssertionError('explanation must not prepare strict context'))
    monkeypatch.setattr(workflow.context_manager, 'prepare', prepare)
    workflow.run_analysis(run)
    assert repository.get_run_by_id(run).status == 'off_topic'
    prepare.assert_not_called()


def test_missing_direct_reply_is_filled_by_text_model(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'direct-answer.sqlite')
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, '有什么建议吗', status='queued')
    monkeypatch.setattr(workflow.llm, 'structured', lambda *args, **kwargs: IntentDecision(route='explanation'))
    monkeypatch.setattr(workflow.llm, 'text', lambda *args, **kwargs: '建议先核查费用占比最高的项目，再确认是否为一次性支出。')

    workflow.run_analysis(run)

    messages = repository.get_task(task).messages
    assert messages[-1].content.startswith('建议先核查费用')


def test_legacy_intent_does_not_prepare_context_for_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'legacy-chat.sqlite')
    repository.initialize()
    task = repository.create_task()
    run = repository.start_execution(task, '你好', status='queued')
    monkeypatch.setattr(workflow.settings, 'model_intent_enabled', False)
    monkeypatch.setattr(workflow.context_manager, 'prepare', Mock(side_effect=AssertionError('chat must not prepare context')))
    monkeypatch.setattr(workflow.llm, 'text', lambda *args, **kwargs: '你好，我可以帮你分析上传的报表。')

    workflow.run_analysis(run)

    assert repository.get_run_by_id(run).status == 'off_topic'
