from pathlib import Path

import pytest

from app.models import AnalysisDraft, DatasetColumn, DatasetInfo, TaskStatus, UploadedFile
from app.repository import repository


@pytest.fixture()
def queued_task(tmp_path: Path):
    old_path = repository.db_path
    repository.db_path = tmp_path / "queue.sqlite"
    repository.initialize()
    task_id = repository.create_task()
    file_record = UploadedFile(
        id="file", original_name="data.csv", size=20, status="ready", sheet_count=1, row_count=1
    )
    repository.add_file(task_id, file_record, "file.csv")
    repository.add_dataset(
        task_id,
        DatasetInfo(
            id="dataset", file_id="file", table_name="data_1", display_name="data",
            row_count=1,
            columns=[DatasetColumn(name="收入", display_name="收入", data_type="Int64", null_count=0)],
        ),
    )
    yield task_id
    repository.db_path = old_path


def test_only_one_pending_run_per_task_and_queued_run_can_cancel(queued_task: str) -> None:
    run_id = repository.queue_analysis(queued_task, "分析收入")
    with pytest.raises(RuntimeError, match="已有分析"):
        repository.queue_analysis(queued_task, "再次分析")
    snapshot = repository.get_task(queued_task)
    assert snapshot.pending_run_id == run_id
    assert snapshot.queue_position is not None and snapshot.queue_position >= 1

    repository.cancel_queued_execution(queued_task, run_id)
    assert repository.get_run_by_id(run_id).status == "cancelled"
    assert repository.get_task(queued_task).status.value == "ready"


def test_interrupted_run_is_requeued(queued_task: str) -> None:
    run_id = repository.queue_analysis(queued_task, "分析收入")
    assert repository.mark_execution_running(run_id) is True
    assert repository.mark_execution_running(run_id) is False
    repository.requeue_interrupted_executions()
    run = repository.get_run_by_id(run_id)
    assert run.status == "queued"
    assert run.attempt_count == 1


def test_running_run_can_be_cancelled_and_task_becomes_terminal(queued_task: str) -> None:
    run_id = repository.queue_analysis(queued_task, "分析收入")
    assert repository.mark_execution_running(run_id) is True
    repository.cancel_queued_execution(queued_task, run_id)

    assert repository.get_run_by_id(run_id).status == "cancelled"
    snapshot = repository.get_task(queued_task)
    assert snapshot.status == TaskStatus.CANCELLED
    assert snapshot.pending_run_id is None
    assert repository.is_execution_cancelled(run_id) is True


def test_failed_follow_up_restores_the_previous_active_result(queued_task: str) -> None:
    result = AnalysisDraft(title="原结果", summary="已验证的原结果")
    source_run_id = repository.start_execution(queued_task, "原分析")
    repository.finish_execution(
        source_run_id,
        TaskStatus.COMPLETED_WITH_WARNINGS.value,
        result=result,
        activate=True,
    )

    failed_run_id = repository.queue_analysis(queued_task, "调整分析口径")
    repository.finish_execution(failed_run_id, TaskStatus.FAILED.value, "模型不可用")
    repository.restore_active_run_after_failure(queued_task, failed_run_id, "模型不可用")

    snapshot = repository.get_task(queued_task)
    assert snapshot.active_run_id == source_run_id
    assert snapshot.status == TaskStatus.COMPLETED_WITH_WARNINGS
    assert snapshot.result is not None
    assert snapshot.result.summary == "已验证的原结果"


def test_initial_failure_is_not_described_as_branch_failure(queued_task: str) -> None:
    run_id = repository.queue_analysis(queued_task, "分析收入")
    repository.finish_execution(run_id, TaskStatus.FAILED.value, "模型不可用")

    repository.restore_active_run_after_failure(queued_task, run_id, "模型不可用")

    snapshot = repository.get_task(queued_task)
    assert snapshot.status == TaskStatus.FAILED
    assert snapshot.status_message == "分析运行失败"
