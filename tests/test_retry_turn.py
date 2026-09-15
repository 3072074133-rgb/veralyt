import pytest

from app.repository import repository


def test_retry_reuses_message_and_keeps_failed_run(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'retry.sqlite')
    repository.initialize()
    task = repository.create_task()
    original = repository.queue_analysis(task, 'analyze all tables')
    repository.finish_execution(original, 'failed', 'interrupted')
    before = repository.get_task(task).messages
    retried = repository.queue_analysis(task, '', retry_run_id=original)
    assert repository.get_task(task).messages == before
    old = repository.get_run_by_id(original)
    new = repository.get_run_by_id(retried)
    assert old.status == 'failed'
    assert new.status == 'queued'
    assert new.message_sequence == old.message_sequence
    assert new.question == old.question
    assert new.parent_run_id == original
    with pytest.raises(RuntimeError):
        repository.queue_analysis(task, '', retry_run_id=original)


def test_retry_cannot_reference_another_task(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'retry.sqlite')
    repository.initialize()
    task = repository.create_task()
    other = repository.create_task()
    original = repository.queue_analysis(task, 'analyze')
    repository.finish_execution(original, 'failed', 'interrupted')
    with pytest.raises(KeyError):
        repository.queue_analysis(other, '', retry_run_id=original)
    assert repository.get_task(other).messages == []
