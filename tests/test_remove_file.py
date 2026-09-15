import pytest

from app.models import AnalysisDraft, TaskStatus, UploadedFile
from app.repository import repository


def test_remove_file_scope_and_running_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'files.sqlite')
    repository.initialize()
    task = repository.create_task()
    other = repository.create_task()
    repository.add_file(task, UploadedFile(id='file1', original_name='a.xlsx', size=10, status='ready'), 'a.xlsx')
    with pytest.raises(KeyError):
        repository.remove_task_file(other, 'file1')
    run = repository.queue_analysis(task, 'analyze')
    with pytest.raises(ValueError):
        repository.remove_task_file(task, 'file1')
    repository.finish_execution(run, 'failed', 'test')
    repository.update_task(task, status=TaskStatus.FAILED)
    repository.remove_task_file(task, 'file1')
    snapshot = repository.get_task(task)
    assert snapshot.files == []
    assert snapshot.data_revision == 1
    assert repository.get_run_by_id(run).status == 'failed'


def test_remove_file_preserves_displayed_report(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'report.sqlite')
    repository.initialize()
    task = repository.create_task()
    repository.add_file(task, UploadedFile(id='file1', original_name='a.xlsx', size=10, status='ready'), 'a.xlsx')
    run = repository.queue_analysis(task, 'report')
    repository.finish_execution(run, 'completed')
    draft = AnalysisDraft(title='Existing report', summary='Keep this report')
    with repository.connect() as connection:
        connection.execute("UPDATE tasks SET status='completed', result_json=?, active_run_id=? WHERE id=?", (draft.model_dump_json(), run, task))
        connection.execute("UPDATE execution_runs SET result_json=?, is_active=1 WHERE id=?", (draft.model_dump_json(), run))
    repository.remove_task_file(task, 'file1')
    snapshot = repository.get_task(task)
    assert snapshot.files == []
    assert snapshot.result == draft
    assert snapshot.active_run_id == run
    assert repository.get_run_by_id(run).is_active
    following = repository.queue_analysis(task, 'new analysis')
    repository.finish_execution(following, 'failed', 'test failure')
    assert repository.get_task(task).result == draft
