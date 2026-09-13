from __future__ import annotations

import asyncio
import logging
import time

from .observability import bind_log_context, duration_ms, log_event
from .exports import export_html
from .repository import repository
from .workflow import run_analysis


logger = logging.getLogger(__name__)


class AnalysisWorker:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, str]] | None = None
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.task is None:
            self.queue = asyncio.Queue()
            self.task = asyncio.create_task(self._run())
        repository.requeue_interrupted_executions()
        repository.requeue_interrupted_report_jobs()
        pending_runs = repository.unfinished_runs()
        pending_jobs = repository.unfinished_report_jobs()
        log_event(
            logger,
            "worker.started",
            recovered_runs=len(pending_runs),
            recovered_report_jobs=len(pending_jobs),
        )
        for run in pending_runs:
            await self.enqueue_run(run.id)
        for job in pending_jobs:
            await self.enqueue_report(job.id)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def enqueue_run(self, run_id: str) -> None:
        if self.queue is None:
            raise RuntimeError("分析队列尚未启动")
        await self.queue.put(("analysis", run_id))

    async def enqueue_report(self, job_id: str) -> None:
        if self.queue is None:
            raise RuntimeError("报告队列尚未启动")
        await self.queue.put(("report", job_id))

    async def _run(self) -> None:
        if self.queue is None:
            return
        while True:
            kind, identifier = await self.queue.get()
            started_at = time.perf_counter()
            try:
                if kind == "report":
                    await asyncio.to_thread(run_report_job, identifier)
                    continue
                run = repository.get_run_by_id(identifier)
                if run.status != "queued":
                    continue
                with bind_log_context(task_id=run.task_id, run_id=run.id):
                    log_event(logger, "worker.job.started", kind=kind)
                    await asyncio.to_thread(run_analysis, identifier)
                    log_event(
                        logger,
                        "worker.job.completed",
                        kind=kind,
                        duration_ms=duration_ms(started_at),
                    )
            except Exception as exc:
                logger.exception(
                    "worker.job.failed",
                    extra={
                        "event_fields": {
                            "event": "worker.job.failed",
                            "kind": kind,
                            "identifier": identifier,
                            "duration_ms": duration_ms(started_at),
                        }
                    },
                )
                if kind == "report":
                    try:
                        repository.fail_report_job(identifier, str(exc))
                    except Exception:
                        logger.exception("worker.failure_state_update_failed")
                elif kind == "replay":
                    try:
                        run = repository.get_run_by_id(identifier)
                        repository.finish_execution(run.id, "failed", str(exc))
                        repository.restore_active_run_after_failure(run.task_id, run.id, str(exc))
                    except Exception:
                        logger.exception("worker.failure_state_update_failed")
                else:
                    try:
                        run = repository.get_run_by_id(identifier)
                        repository.finish_execution(run.id, "failed", str(exc))
                        repository.restore_active_run_after_failure(run.task_id, run.id, str(exc))
                    except Exception:
                        logger.exception("worker.failure_state_update_failed")
            finally:
                self.queue.task_done()


def run_report_job(job_id: str) -> None:
    started_at = time.perf_counter()
    job = repository.get_report_job(job_id)
    if not repository.claim_report_job(job_id):
        return
    log_event(logger, "report.started", task_id=job.task_id, run_id=job.run_id, report_job_id=job_id)
    try:
        snapshot = repository.get_task(job.task_id)
        if snapshot.active_run_id != job.run_id or snapshot.result is None:
            raise ValueError("当前活动结果已变化，报告任务已停止")
        run = repository.get_run(job.task_id, job.run_id)
        if run.data_revision != snapshot.data_revision:
            raise ValueError("数据版本已变化，报告任务已停止")
        path = export_html(snapshot)
        report = repository.publish_report(job.task_id, job.run_id, path)
        repository.record_export(job.task_id, "published_report", path, job.run_id)
        repository.finish_report_job(job_id, report)
        log_event(
            logger,
            "report.completed",
            task_id=job.task_id,
            run_id=job.run_id,
            report_job_id=job_id,
            report_id=report.id,
            duration_ms=duration_ms(started_at),
        )
    except Exception as exc:
        repository.fail_report_job(job_id, str(exc))
        logger.exception(
            "report.failed",
            extra={"event_fields": {"event": "report.failed", "task_id": job.task_id, "run_id": job.run_id, "report_job_id": job_id, "error_type": type(exc).__name__, "duration_ms": duration_ms(started_at)}},
        )


worker = AnalysisWorker()
