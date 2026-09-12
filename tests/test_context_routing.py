from unittest.mock import Mock

import pytest

from app import workflow
from app.models import IntentDecision
from app.repository import repository


@pytest.mark.parametrize('decision', [IntentDecision(route='conversation', reply='hello'),
                                     IntentDecision(route='clarification', reply='which period?')])
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
