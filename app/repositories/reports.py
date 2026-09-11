from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

from ..config import settings
from ..models import ReportDetail, ReportJob, ReportSummary, TaskStatus, utc_now


class ReportRepositoryMixin:
    def publish_report(self, task_id: str, run_id: str, html_path: Path | None = None) -> ReportDetail:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM execution_runs WHERE id=? AND task_id=?", (run_id, task_id)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in {"completed", "completed_with_warnings"} or not run["result_json"]:
                raise ValueError("只有已完成并通过验证的分析可以发布报告")
            task = connection.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
            existing = connection.execute(
                "SELECT * FROM reports WHERE task_id=? AND status<>'archived' ORDER BY created_at LIMIT 1",
                (task_id,),
            ).fetchone()
            if existing:
                report_id = existing["id"]
                version_number = int(existing["latest_version"]) + 1
            else:
                report_id = str(uuid.uuid4())
                version_number = 1
                connection.execute(
                    """INSERT INTO reports(id,task_id,title,status,latest_version,created_at,updated_at)
                    VALUES(?,?,?,'draft',0,?,?)""",
                    (report_id, task_id, task["title"], now, now),
                )
            snapshot = json.loads(run["input_snapshot_json"] or "{}")
            source_revision_ids = list(snapshot.get("revision_ids", []))
            knowledge_revision_ids = list(snapshot.get("knowledge_revision_ids", []))
            content = {
                "analysis": json.loads(run["result_json"]),
                "provenance": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "data_revision": run["data_revision"],
                    "source_revision_ids": source_revision_ids,
                    "knowledge_revision_ids": knowledge_revision_ids,
                    "published_at": now,
                },
            }
            serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
            content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            version_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO report_versions(
                id,report_id,version_number,run_id,status,content_json,source_revision_ids_json,
                content_hash,html_path,created_at) VALUES(?,?,?,?,'ready',?,?,?,?,?)""",
                (
                    version_id,
                    report_id,
                    version_number,
                    run_id,
                    serialized,
                    json.dumps(source_revision_ids),
                    content_hash,
                    str(html_path) if html_path else None,
                    now,
                ),
            )
            if html_path:
                report_dir = settings.data_dir / "reports" / report_id
                report_dir.mkdir(parents=True, exist_ok=True)
                persistent_path = report_dir / f"v{version_number}.html"
                shutil.copy2(html_path, persistent_path)
                connection.execute(
                    "UPDATE report_versions SET html_path=? WHERE id=?",
                    (str(persistent_path), version_id),
                )
            connection.execute(
                "UPDATE reports SET status='ready',latest_version=?,updated_at=? WHERE id=?",
                (version_number, now, report_id),
            )
        return self.get_report(report_id)

    def queue_report(self, task_id: str, run_id: str) -> ReportJob:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            run = connection.execute(
                "SELECT status,result_json FROM execution_runs WHERE id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] not in {"completed", "completed_with_warnings"} or not run["result_json"]:
                raise ValueError("只有已完成并通过验证的分析可以发布报告")
            pending = connection.execute(
                "SELECT * FROM report_jobs WHERE run_id=? AND status IN ('queued','generating')",
                (run_id,),
            ).fetchone()
            if pending:
                return self._report_job(pending)
            connection.execute(
                """INSERT INTO report_jobs(
                id,task_id,run_id,status,report_id,report_version_id,error,attempt_count,
                claimed_at,created_at,updated_at)
                VALUES(?,?,?,'queued',NULL,NULL,NULL,0,NULL,?,?)""",
                (job_id, task_id, run_id, now, now),
            )
        self.add_event(
            task_id,
            "report.queued",
            TaskStatus(self._task_row(task_id)["status"]),
            100,
            "正式报告已进入生成队列",
            {"job_id": job_id, "run_id": run_id},
        )
        return self.get_report_job(job_id)

    def get_report_job(self, job_id: str) -> ReportJob:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM report_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._report_job(row)

    def unfinished_report_jobs(self) -> list[ReportJob]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM report_jobs WHERE status='queued' ORDER BY created_at"
            ).fetchall()
        return [self._report_job(row) for row in rows]

    def requeue_interrupted_report_jobs(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE report_jobs SET status='queued',claimed_at=NULL,error=?,updated_at=?
                WHERE status='generating'""",
                ("进程重启后自动恢复", utc_now()),
            )

    def claim_report_job(self, job_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE report_jobs SET status='generating',claimed_at=?,updated_at=?,
                attempt_count=attempt_count+1,error=NULL WHERE id=? AND status='queued'""",
                (now, now, job_id),
            )
        return cursor.rowcount == 1

    def finish_report_job(self, job_id: str, report: ReportDetail) -> ReportJob:
        version_id = report.versions[0].id
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE report_jobs SET status='ready',report_id=?,report_version_id=?,
                error=NULL,updated_at=? WHERE id=?""",
                (report.id, version_id, now, job_id),
            )
            row = connection.execute("SELECT * FROM report_jobs WHERE id=?", (job_id,)).fetchone()
        job = self._report_job(row)
        task = self._task_row(job.task_id)
        self.add_event(
            job.task_id,
            "report.ready",
            TaskStatus(task["status"]),
            100,
            f"报告 v{report.latest_version} 已发布",
            {"job_id": job.id, "report_id": report.id, "version_id": version_id},
        )
        return job

    def fail_report_job(self, job_id: str, error: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE report_jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                (error, now, job_id),
            )
            row = connection.execute("SELECT task_id FROM report_jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            task = self._task_row(row["task_id"])
            self.add_event(
                row["task_id"],
                "report.failed",
                TaskStatus(task["status"]),
                100,
                "报告生成失败",
                {"job_id": job_id, "error": error},
            )

    def list_reports(self) -> list[ReportSummary]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE status<>'archived' ORDER BY updated_at DESC"
            ).fetchall()
        return [self._report_summary(row) for row in rows]

    def get_report(self, report_id: str) -> ReportDetail:
        with self.connect() as connection:
            report = connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                raise KeyError(report_id)
            versions = connection.execute(
                "SELECT * FROM report_versions WHERE report_id=? ORDER BY version_number DESC",
                (report_id,),
            ).fetchall()
        return ReportDetail(
            **self._report_summary(report).model_dump(),
            versions=[self._report_version(row) for row in versions],
        )
