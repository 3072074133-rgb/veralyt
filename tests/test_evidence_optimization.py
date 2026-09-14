from unittest.mock import Mock

import pytest

from app import workflow
from app.models import AnalysisState, EvidenceRecord
from app.repository import repository


@pytest.fixture
def evidence_task(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'evidence.sqlite')
    repository.initialize()
    task = repository.create_task()
    for index in range(30):
        repository.add_evidence(EvidenceRecord(id=f'ev_{index}', task_id=task, title='test', source='query_data',
            columns=['amount'], rows=[{'amount': index}]))
    return task


def test_batch_reads_ignore_other_tasks_and_empty_ids(evidence_task, monkeypatch):
    other = repository.create_task()
    assert repository.list_evidence_by_ids(other, ['ev_0']) == []
    monkeypatch.setattr(repository, 'connect', Mock(side_effect=AssertionError('empty query')))
    assert repository.list_evidence_by_ids(evidence_task, []) == []


def test_evidence_context_batch_ignores_other_evidence(evidence_task, monkeypatch):
    parser = Mock(wraps=repository._evidence_record)
    monkeypatch.setattr(repository, '_evidence_record', parser)
    state = AnalysisState(task_id=evidence_task, run_id='run', user_question='question', tool_results=[
        {'status': 'success', 'evidence_ids': ['ev_0']},
    ])
    with repository.evidence_scope(evidence_task):
        assert len(workflow._draft_context(state)['evidence_catalog']) == 1
        assert repository.get_evidence(evidence_task, 'ev_0').rows == [{'amount': 0}]
    assert parser.call_count == 1
    with repository.evidence_scope(evidence_task):
        repository.get_evidence(evidence_task, 'ev_0')
    assert parser.call_count == 2


def test_validation_deserializes_each_evidence_once(evidence_task, monkeypatch):
    parser = Mock(wraps=repository._evidence_record)
    monkeypatch.setattr(repository, '_evidence_record', parser)
    run = repository.start_execution(evidence_task, 'question')
    pointer = {'evidence_id': 'ev_1', 'row_index': 0, 'field': 'amount', 'raw_value': '1'}
    state = AnalysisState(task_id=evidence_task, run_id=run, user_question='question', tool_results=[
        {'status': 'success', 'evidence_ids': ['ev_1']},
    ], draft={'summary': 'amount 1', 'summary_evidence_refs': ['ev_1'], 'summary_evidence_pointers': [pointer],
              'metrics': [{'label': 'amount', 'value': '1', 'evidence_refs': ['ev_1'], 'evidence_pointers': [pointer]}]})
    assert workflow.validate_node(state)['validation']['passed']
    assert parser.call_count == 1
