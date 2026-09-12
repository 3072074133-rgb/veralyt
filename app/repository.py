from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import settings
from .migrations import apply_migrations
from .repositories.reports import ReportRepositoryMixin
from .models import (
    AnalysisDraft,
    ConversationMemory,
    ConversationMemoryRecord,
    DatasetAsset,
    DatasetAssetDetail,
    DatasetAssetList,
    DatasetInfo,
    DatasetRevision,
    DatasetRelationship,
    EvidenceRecord,
    MessageRecord,
    NodeExecutionDetail,
    NodeExecutionSummary,
    PromptVersion,
    ReportJob,
    ReportSummary,
    ReportVersion,
    RunArtifact,
    TaskEvent,
    TaskListItem,
    TaskListResponse,
    TaskSnapshot,
    TaskStatus,
    UploadedFile,
    WorkflowRun,
    utc_now,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    status_message TEXT NOT NULL,
    result_json TEXT,
    clarification_question TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active_run_id TEXT,
    data_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    sheet_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    detected_sheet_count INTEGER NOT NULL DEFAULT 0,
    skipped_sheet_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    catalog_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_memories (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    memory_json TEXT NOT NULL,
    covered_until_sequence INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT,
    data_revision INTEGER NOT NULL DEFAULT 0,
    source_dataset_ids_json TEXT NOT NULL DEFAULT '[]',
    query TEXT,
    query_hash TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    parent_run_id TEXT,
    forked_from_node_execution_id TEXT,
    entry_node TEXT NOT NULL DEFAULT 'classify',
    prompt_version_id TEXT,
    result_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    data_revision INTEGER NOT NULL DEFAULT 0,
    message_sequence INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id TEXT PRIMARY KEY,
    node_name TEXT NOT NULL,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parent_version_id TEXT REFERENCES prompt_versions(id),
    created_at TEXT NOT NULL,
    UNIQUE(node_name, content_hash)
);
CREATE TABLE IF NOT EXISTS node_executions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES execution_runs(id) ON DELETE CASCADE,
    node_name TEXT NOT NULL,
    occurrence INTEGER NOT NULL,
    input_state_json TEXT NOT NULL,
    output_state_json TEXT,
    state_schema_version INTEGER NOT NULL,
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(id),
    model_config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, node_name, occurrence)
);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_task_id ON events(task_id, id);
CREATE INDEX IF NOT EXISTS ix_node_executions_run ON node_executions(run_id, started_at);
"""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_evidence_cache: ContextVar[tuple[str, str, dict[str, EvidenceRecord | None]] | None] = ContextVar(
    'evidence_cache', default=None
)


@lru_cache(maxsize=128)
def _cached_relationships(database: str, task_id: str, catalog: tuple[str, ...]) -> tuple[str, ...]:
    from .dataset_retrieval import detect_dataset_relationships
    return tuple(DatasetRelationship.model_validate(item).model_dump_json()
                 for item in detect_dataset_relationships([DatasetInfo.model_validate_json(item) for item in catalog]))


class Repository(ReportRepositoryMixin):
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or settings.metadata_db

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            apply_migrations(connection, self.db_path)
            self._ensure_column(connection, "tasks", "active_run_id", "TEXT")
            self._ensure_column(connection, "tasks", "data_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "files", "detected_sheet_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "files", "skipped_sheet_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "evidence", "run_id", "TEXT")
            self._ensure_column(connection, "evidence", "data_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "evidence", "source_dataset_ids_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "evidence", "query", "TEXT")
            self._ensure_column(connection, "evidence", "query_hash", "TEXT")
            self._ensure_column(connection, "execution_runs", "parent_run_id", "TEXT")
            self._ensure_column(connection, "execution_runs", "forked_from_node_execution_id", "TEXT")
            self._ensure_column(connection, "execution_runs", "entry_node", "TEXT NOT NULL DEFAULT 'classify'")
            self._ensure_column(connection, "execution_runs", "prompt_version_id", "TEXT")
            self._ensure_column(connection, "execution_runs", "result_json", "TEXT")
            self._ensure_column(connection, "execution_runs", "is_active", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "execution_runs", "data_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "execution_runs", "message_sequence", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "execution_runs", "claimed_at", "TEXT")
            self._ensure_column(connection, "execution_runs", "heartbeat_at", "TEXT")
            self._ensure_column(connection, "execution_runs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "node_executions", "diagnostics_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "exports", "run_id", "TEXT")
            self._ensure_column(connection, "data_assets", "library_visible", "INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS ix_evidence_run ON evidence(task_id, run_id)")
            connection.execute(
                """UPDATE execution_runs SET status='failed',error='迁移时清理重复的待执行分支',
                finished_at=COALESCE(finished_at, ?)
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY task_id ORDER BY started_at, id
                        ) AS position
                        FROM execution_runs WHERE status IN ('queued','running')
                    ) duplicates WHERE position > 1
                )""",
                (utc_now(),),
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_runs_pending_task
                ON execution_runs(task_id) WHERE status IN ('queued','running')"""
            )
            self._migrate_legacy_results(connection)
            self._materialize_asset_files(connection)
            self._backfill_revision_table_bindings(connection)

    def create_task(self) -> str:
        task_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO tasks(
                id,title,status,progress,status_message,result_json,clarification_question,error,
                created_at,updated_at,active_run_id,data_revision)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, 0)""",
                (task_id, "新建分析", TaskStatus.READY, 0, "等待上传数据", now, now),
            )
        self.add_event(task_id, "task.created", TaskStatus.READY, 0, "分析任务已创建")
        return task_id

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        progress: int | None = None,
        status_message: str | None = None,
        title: str | None = None,
        result: AnalysisDraft | None = None,
        clear_result: bool = False,
        clarification_question: str | None = None,
        clear_clarification: bool = False,
        error: str | None = None,
        clear_error: bool = False,
        event_type: str = "task.updated",
        payload: dict[str, Any] | None = None,
    ) -> None:
        current = self._task_row(task_id)
        values = {
            "status": str(status or current["status"]),
            "progress": progress if progress is not None else current["progress"],
            "status_message": status_message or current["status_message"],
            "title": title or current["title"],
            "result_json": None if clear_result else result.model_dump_json() if result else current["result_json"],
            "clarification_question": (
                None if clear_clarification else clarification_question or current["clarification_question"]
            ),
            "error": None if clear_error else error or current["error"],
            "updated_at": utc_now(),
            "id": task_id,
        }
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET title=:title, status=:status, progress=:progress,
                status_message=:status_message, result_json=:result_json,
                clarification_question=:clarification_question, error=:error,
                updated_at=:updated_at WHERE id=:id""",
                values,
            )
        if status or progress is not None or status_message:
            self.add_event(
                task_id,
                event_type,
                TaskStatus(values["status"]),
                values["progress"],
                values["status_message"],
                payload or {},
            )

    def begin_ingestion(self, task_id: str) -> bool:
        """Atomically claim a task for upload processing."""
        terminal = (
            TaskStatus.READY.value,
            TaskStatus.OFF_TOPIC.value,
            TaskStatus.NEEDS_CLARIFICATION.value,
            TaskStatus.NEEDS_REVIEW.value,
            TaskStatus.COMPLETED_WITH_WARNINGS.value,
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in terminal)
            cursor = connection.execute(
                f"""UPDATE tasks SET status=?,progress=5,status_message=?,updated_at=?
                WHERE id=? AND status IN ({placeholders})
                AND NOT EXISTS(
                    SELECT 1 FROM execution_runs
                    WHERE task_id=? AND status IN ('queued','running')
                )""",
                (
                    TaskStatus.INGESTING.value, "正在校验并解析文件", utc_now(), task_id,
                    *terminal, task_id,
                ),
            )
        claimed = cursor.rowcount == 1
        if claimed:
            self.add_event(
                task_id, "upload.started", TaskStatus.INGESTING, 5,
                "正在校验并解析文件",
            )
        return claimed

    def add_file(self, task_id: str, item: UploadedFile, stored_name: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO files(
                id, task_id, original_name, stored_name, size, status, sheet_count,
                row_count, error, detected_sheet_count, skipped_sheet_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id,
                    task_id,
                    item.original_name,
                    stored_name,
                    item.size,
                    item.status,
                    item.sheet_count,
                    item.row_count,
                    item.error,
                    item.detected_sheet_count,
                    item.skipped_sheet_count,
                ),
            )

    def update_file(self, item: UploadedFile) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE files SET status=?, sheet_count=?, row_count=?, error=?,
                detected_sheet_count=?, skipped_sheet_count=?
                WHERE id=?""",
                (
                    item.status,
                    item.sheet_count,
                    item.row_count,
                    item.error,
                    item.detected_sheet_count,
                    item.skipped_sheet_count,
                    item.id,
                ),
            )

    def add_dataset(self, task_id: str, dataset: DatasetInfo) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset.id,
                    task_id,
                    dataset.file_id,
                    dataset.table_name,
                    dataset.display_name,
                    dataset.row_count,
                    dataset.model_dump_json(),
                ),
            )

    def publish_ingestion(
        self,
        task_id: str,
        file_record: UploadedFile,
        datasets: list[DatasetInfo],
        *,
        publish_to_library: bool = False,
    ) -> None:
        """Publish file metadata and its complete dataset catalog atomically."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE files SET status=?, sheet_count=?, row_count=?, error=?,
                detected_sheet_count=?, skipped_sheet_count=? WHERE id=? AND task_id=?""",
                (
                    file_record.status, file_record.sheet_count, file_record.row_count,
                    file_record.error, file_record.detected_sheet_count,
                    file_record.skipped_sheet_count, file_record.id, task_id,
                ),
            )
            for dataset in datasets:
                connection.execute(
                    """INSERT INTO datasets(id,task_id,file_id,table_name,display_name,row_count,catalog_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dataset.id, task_id, dataset.file_id, dataset.table_name,
                        dataset.display_name, dataset.row_count, dataset.model_dump_json(),
                    ),
                )
            self._publish_initial_asset(
                connection,
                task_id,
                file_record,
                datasets,
                "upload",
                publish_to_library=publish_to_library,
            )

    def add_message(self, task_id: str, role: str, content: str) -> MessageRecord:
        message = MessageRecord(id=str(uuid.uuid4()), role=role, content=content, created_at=utc_now())
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                (message.id, task_id, role, content, message.created_at),
            )
            sequence = cursor.lastrowid
        return message.model_copy(update={"sequence": sequence})

    def queue_analysis(self, task_id: str, content: str) -> str:
        """Atomically persist the user message and its queued workflow run."""
        run_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if connection.execute(
                "SELECT 1 FROM execution_runs WHERE task_id=? AND status IN ('queued','running')",
                (task_id,),
            ).fetchone():
                raise RuntimeError("该任务已有分析正在运行")
            if not connection.execute("SELECT 1 FROM datasets WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
                raise ValueError("请先上传可分析的 Excel 或 CSV 文件")
            cursor = connection.execute(
                "INSERT INTO messages VALUES (?, ?, 'user', ?, ?)",
                (message_id, task_id, content, now),
            )
            message_sequence = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO execution_runs(
                id,task_id,question,status,error,started_at,finished_at,parent_run_id,
                forked_from_node_execution_id,entry_node,prompt_version_id,result_json,is_active,
                data_revision,message_sequence,claimed_at,heartbeat_at,attempt_count,input_snapshot_json)
                VALUES (?, ?, ?, 'queued', NULL, ?, NULL, NULL, NULL, 'classify', NULL, NULL, 0,
                ?, ?, NULL, NULL, 0, ?)""",
                (
                    run_id, task_id, content, now, task["data_revision"], message_sequence,
                    json.dumps(self._task_input_snapshot(connection, task_id), ensure_ascii=False),
                ),
            )
            title = content[:36] if task["title"] == "新建分析" else task["title"]
            connection.execute(
                """UPDATE tasks SET title=?,status=?,progress=18,status_message=?,
                clarification_question=NULL,error=NULL,updated_at=? WHERE id=?""",
                (title, TaskStatus.CLASSIFYING, "分析请求已进入队列", now, task_id),
            )
            connection.execute(
                """INSERT INTO events(task_id,event_type,status,progress,message,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    task_id, "run.queued", TaskStatus.CLASSIFYING, 18, "分析请求已进入队列",
                    json.dumps({"run_id": run_id}, ensure_ascii=False), now,
                ),
            )
        return run_id

    def publish_data_revision(self, task_id: str) -> int:
        """Invalidate active results after a successful data import."""
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT data_revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            revision = int(task["data_revision"]) + 1
            connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=?", (task_id,))
            connection.execute(
                """UPDATE tasks SET data_revision=?,active_run_id=NULL,result_json=NULL,
                clarification_question=NULL,error=NULL,updated_at=? WHERE id=?""",
                (revision, now, task_id),
            )
        return revision

    def get_conversation_memory(self, task_id: str) -> ConversationMemoryRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_memories WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return ConversationMemoryRecord(
            task_id=row["task_id"],
            memory=ConversationMemory.model_validate_json(row["memory_json"]),
            covered_until_sequence=row["covered_until_sequence"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_conversation_memory(
        self,
        task_id: str,
        memory: ConversationMemory,
        covered_until_sequence: int,
        *,
        expected_version: int | None,
    ) -> ConversationMemoryRecord:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT version, created_at FROM conversation_memories WHERE task_id=?", (task_id,)
            ).fetchone()
            if current is None:
                if expected_version not in (None, 0):
                    raise RuntimeError("会话记忆版本冲突")
                version = 1
                created_at = now
                connection.execute(
                    "INSERT INTO conversation_memories VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, memory.model_dump_json(), covered_until_sequence, version, created_at, now),
                )
            else:
                if expected_version != current["version"]:
                    raise RuntimeError("会话记忆版本冲突")
                version = current["version"] + 1
                created_at = current["created_at"]
                connection.execute(
                    """UPDATE conversation_memories
                    SET memory_json=?, covered_until_sequence=?, version=?, updated_at=?
                    WHERE task_id=?""",
                    (memory.model_dump_json(), covered_until_sequence, version, now, task_id),
                )
        return ConversationMemoryRecord(
            task_id=task_id,
            memory=memory,
            covered_until_sequence=covered_until_sequence,
            version=version,
            created_at=created_at,
            updated_at=now,
        )

    def start_execution(
        self,
        task_id: str,
        question: str,
        *,
        run_id: str | None = None,
        status: str = "running",
        parent_run_id: str | None = None,
        forked_from_node_execution_id: str | None = None,
        entry_node: str = "classify",
        prompt_version_id: str | None = None,
        data_revision: int | None = None,
        message_sequence: int | None = None,
        input_snapshot: dict[str, Any] | None = None,
    ) -> str:
        run_id = run_id or str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT data_revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if data_revision is None:
                data_revision = int(task["data_revision"])
            if message_sequence is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(rowid),0) FROM messages WHERE task_id=?", (task_id,)
                ).fetchone()
                message_sequence = int(row[0])
            captured_input = (
                input_snapshot
                if input_snapshot is not None
                else self._task_input_snapshot(connection, task_id)
            )
            if input_snapshot is not None:
                self._assert_snapshot_current(
                    connection, task_id, data_revision, captured_input
                )
            connection.execute(
                """INSERT INTO execution_runs(
                id,task_id,question,status,error,started_at,finished_at,parent_run_id,
                forked_from_node_execution_id,entry_node,prompt_version_id,result_json,is_active,
                data_revision,message_sequence,claimed_at,heartbeat_at,attempt_count,input_snapshot_json)
                VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, 0, ?, ?, NULL, NULL, 0, ?)""",
                (
                    run_id, task_id, question, status, utc_now(), parent_run_id,
                    forked_from_node_execution_id, entry_node, prompt_version_id,
                    data_revision, message_sequence,
                    json.dumps(captured_input, ensure_ascii=False),
                ),
            )
        return run_id

    def mark_execution_running(self, run_id: str) -> bool:
        with self.connect() as connection:
            now = utc_now()
            cursor = connection.execute(
                """UPDATE execution_runs SET status='running',error=NULL,claimed_at=?,heartbeat_at=?,
                attempt_count=attempt_count+1 WHERE id=? AND status='queued'""",
                (now, now, run_id),
            )
        return cursor.rowcount == 1

    def requeue_interrupted_executions(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE execution_runs SET status='queued',claimed_at=NULL,heartbeat_at=NULL,
                error='进程重启后自动恢复' WHERE status='running'"""
            )

    def cancel_queued_execution(self, task_id: str, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM execution_runs WHERE task_id=? AND id=?",
                (task_id, run_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "queued":
                raise ValueError("只能取消尚未开始执行的分析")
            now = utc_now()
            connection.execute(
                "UPDATE execution_runs SET status='cancelled',error=?,finished_at=? WHERE id=?",
                ("用户已取消", now, run_id),
            )
            active = connection.execute(
                """SELECT status FROM execution_runs WHERE task_id=? AND is_active=1
                AND result_json IS NOT NULL AND data_revision=(SELECT data_revision FROM tasks WHERE id=?)""",
                (task_id, task_id),
            ).fetchone()
            restored = (
                TaskStatus.COMPLETED_WITH_WARNINGS
                if active and active["status"] == TaskStatus.COMPLETED_WITH_WARNINGS.value
                else TaskStatus.COMPLETED if active else TaskStatus.READY
            )
            connection.execute(
                """UPDATE tasks SET status=?,progress=?,status_message=?,error=NULL,updated_at=?
                WHERE id=?""",
                (
                    restored,
                    100 if active else 15,
                    "已取消重跑，保留原分析结果" if active else "分析已取消，可以重新提问",
                    now,
                    task_id,
                ),
            )
        self.add_event(
            task_id, "run.cancelled", restored, 100 if active else 15,
            "已取消重跑，保留原分析结果" if active else "分析已取消",
            {"run_id": run_id},
        )

    def queue_position(self, run_id: str) -> int | None:
        with self.connect() as connection:
            run = connection.execute(
                "SELECT status,started_at FROM execution_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] not in {"queued", "running"}:
                return None
            if run["status"] == "running":
                return 0
            earlier = connection.execute(
                """SELECT COUNT(*) FROM execution_runs
                WHERE status IN ('queued','running')
                AND (started_at < ? OR (started_at = ? AND id < ?))""",
                (run["started_at"], run["started_at"], run_id),
            ).fetchone()[0]
        return int(earlier) + 1

    def touch_execution(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE execution_runs SET heartbeat_at=? WHERE id=? AND status='running'",
                (utc_now(), run_id),
            )

    def finish_execution(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        result: AnalysisDraft | None = None,
        *,
        activate: bool = False,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT task_id,data_revision,input_snapshot_json
                FROM execution_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if result is not None or activate:
                self._assert_run_input_current(connection, row)
            connection.execute(
                """UPDATE execution_runs SET status=?, error=?, finished_at=?,
                result_json=COALESCE(?, result_json), is_active=? WHERE id=?""",
                (status, error, utc_now(), result.model_dump_json() if result else None, int(activate), run_id),
            )
            if activate:
                connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=? AND id<>?", (row["task_id"], run_id))
                connection.execute(
                    "UPDATE tasks SET active_run_id=?, result_json=?, updated_at=? WHERE id=?",
                    (run_id, result.model_dump_json() if result else None, utc_now(), row["task_id"]),
                )

    def get_run(self, task_id: str, run_id: str) -> WorkflowRun:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_runs WHERE task_id=? AND id=?", (task_id, run_id)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_model(row)

    def get_run_by_id(self, run_id: str) -> WorkflowRun:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_model(row)

    def get_run_input_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT input_snapshot_json FROM execution_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["input_snapshot_json"] or "{}")

    def assert_run_context_current(self, task_id: str, run_id: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT task_id,data_revision,input_snapshot_json
                FROM execution_runs WHERE task_id=? AND id=?""",
                (task_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            self._assert_run_input_current(connection, row)

    def list_runs(self, task_id: str) -> list[WorkflowRun]:
        self._task_row(task_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_runs WHERE task_id=? ORDER BY started_at DESC", (task_id,)
            ).fetchall()
        return [self._run_model(row) for row in rows]

    def run_lineage_ids(self, task_id: str, run_id: str) -> set[str]:
        lineage: set[str] = set()
        current: str | None = run_id
        with self.connect() as connection:
            while current and current not in lineage:
                row = connection.execute(
                    "SELECT id,parent_run_id FROM execution_runs WHERE task_id=? AND id=?",
                    (task_id, current),
                ).fetchone()
                if row is None:
                    break
                lineage.add(row["id"])
                current = row["parent_run_id"]
        return lineage

    def unfinished_runs(self) -> list[WorkflowRun]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_runs WHERE status IN ('queued','running') ORDER BY started_at"
            ).fetchall()
        return [self._run_model(row) for row in rows]

    def has_pending_run(self, task_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM execution_runs WHERE task_id=? AND status IN ('queued','running') LIMIT 1",
                (task_id,),
            ).fetchone() is not None

    def activate_run(self, task_id: str, run_id: str) -> WorkflowRun:
        run = self.get_run(task_id, run_id)
        if run.status not in {"completed", "completed_with_warnings"} or run.result is None:
            raise ValueError("只能切换到已完成且包含结果的分析分支")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            context_row = connection.execute(
                """SELECT task_id,data_revision,input_snapshot_json
                FROM execution_runs WHERE task_id=? AND id=?""",
                (task_id, run_id),
            ).fetchone()
            if context_row is None:
                raise KeyError(run_id)
            self._assert_run_input_current(connection, context_row)
            activated_status = TaskStatus(run.status)
            connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=?", (task_id,))
            connection.execute("UPDATE execution_runs SET is_active=1 WHERE id=?", (run_id,))
            connection.execute(
                """UPDATE tasks SET active_run_id=?, result_json=?, status=?, progress=100,
                status_message=?, error=NULL, updated_at=? WHERE id=?""",
                (run_id, run.result.model_dump_json(), activated_status, "已切换分析分支", utc_now(), task_id),
            )
        self.add_event(task_id, "run.activated", activated_status, 100, "已切换分析分支", {"run_id": run_id})
        return self.get_run(task_id, run_id)

    def restore_active_run_after_failure(self, task_id: str, failed_run_id: str, error: str) -> None:
        with self.connect() as connection:
            active = connection.execute(
                """SELECT id,result_json,status FROM execution_runs
                WHERE task_id=? AND is_active=1 AND status IN ('completed','completed_with_warnings')
                AND result_json IS NOT NULL
                AND data_revision=(SELECT data_revision FROM tasks WHERE id=?)""",
                (task_id, task_id),
            ).fetchone()
            if active:
                restored_status = TaskStatus(active["status"])
                connection.execute(
                    """UPDATE tasks SET active_run_id=?, result_json=?, status=?, progress=100,
                    status_message=?, error=NULL, updated_at=? WHERE id=?""",
                    (
                        active["id"], active["result_json"], restored_status,
                        "本次请求未完成，上次报告未受影响", utc_now(), task_id,
                    ),
                )
                status, progress = restored_status, 100
            else:
                failed = connection.execute(
                    "SELECT status,parent_run_id FROM execution_runs WHERE task_id=? AND id=?",
                    (task_id, failed_run_id),
                ).fetchone()
                fallback_status = (
                    TaskStatus.NEEDS_REVIEW
                    if failed and failed["status"] == TaskStatus.NEEDS_REVIEW.value
                    else TaskStatus.FAILED
                )
                is_replay = bool(failed and failed["parent_run_id"])
                failure_message = "分析分支运行失败" if is_replay else "分析运行失败"
                connection.execute(
                    """UPDATE tasks SET status=?, status_message=?, error=?, updated_at=? WHERE id=?""",
                    (
                        fallback_status,
                        "结果需要人工复核" if fallback_status == TaskStatus.NEEDS_REVIEW else failure_message,
                        error, utc_now(), task_id,
                    ),
                )
                status, progress = fallback_status, 95 if fallback_status == TaskStatus.NEEDS_REVIEW else 0
        is_replay = bool(failed and failed["parent_run_id"]) if not active else True
        self.add_event(
            task_id,
            "run.replay_failed" if is_replay else "run.failed",
            status,
            progress,
            "新分支未通过，已保留原分析结果" if active else (
                "结果需要人工复核" if status == TaskStatus.NEEDS_REVIEW else (
                    "分析分支运行失败" if is_replay else "分析运行失败"
                )
            ),
            {"run_id": failed_run_id, "error": error},
        )

    def ensure_prompt_version(
        self,
        node_name: str,
        version: str,
        content: str,
        parent_version_id: str | None = None,
    ) -> PromptVersion:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM prompt_versions WHERE node_name=? AND content_hash=?",
                (node_name, content_hash),
            ).fetchone()
            if row is None:
                prompt_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO prompt_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (prompt_id, node_name, version, content, content_hash, parent_version_id, utc_now()),
                )
                row = connection.execute("SELECT * FROM prompt_versions WHERE id=?", (prompt_id,)).fetchone()
        return PromptVersion(**dict(row))

    def get_prompt_version(self, prompt_id: str) -> PromptVersion:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM prompt_versions WHERE id=?", (prompt_id,)).fetchone()
        if row is None:
            raise KeyError(prompt_id)
        return PromptVersion(**dict(row))

    def start_node_execution(
        self,
        run_id: str,
        node_name: str,
        input_state: dict[str, Any],
        prompt_version_id: str,
        model_config: dict[str, Any],
        state_schema_version: int,
    ) -> str:
        execution_id = str(uuid.uuid4())
        with self.connect() as connection:
            occurrence = connection.execute(
                "SELECT COUNT(*) + 1 FROM node_executions WHERE run_id=? AND node_name=?",
                (run_id, node_name),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO node_executions(
                id,run_id,node_name,occurrence,input_state_json,output_state_json,
                state_schema_version,prompt_version_id,model_config_json,status,error,
                started_at,finished_at,diagnostics_json)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'running', NULL, ?, NULL, '{}')""",
                (
                    execution_id, run_id, node_name, occurrence,
                    json.dumps(input_state, ensure_ascii=False, default=str), state_schema_version,
                    prompt_version_id, json.dumps(model_config, ensure_ascii=False), utc_now(),
                ),
            )
        return execution_id

    def record_node_diagnostics(self, execution_id: str, diagnostics: dict[str, Any]) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT diagnostics_json FROM node_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                return
            current = json.loads(row["diagnostics_json"] or "{}")
            current.update(diagnostics)
            connection.execute(
                "UPDATE node_executions SET diagnostics_json=? WHERE id=?",
                (json.dumps(current, ensure_ascii=False, default=str), execution_id),
            )

    def finish_node_execution(
        self,
        execution_id: str,
        output_state: dict[str, Any] | None,
        *,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE node_executions SET output_state_json=?, status=?, error=?, finished_at=?
                WHERE id=?""",
                (
                    json.dumps(output_state, ensure_ascii=False, default=str) if output_state is not None else None,
                    "failed" if error else "completed", error, utc_now(), execution_id,
                ),
            )

    def list_node_executions(self, task_id: str, run_id: str) -> list[NodeExecutionSummary]:
        self.get_run(task_id, run_id)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT n.*, p.id AS p_id, p.node_name AS p_node_name, p.version AS p_version,
                p.content AS p_content, p.content_hash AS p_content_hash,
                p.parent_version_id AS p_parent_version_id, p.created_at AS p_created_at
                FROM node_executions n JOIN prompt_versions p ON p.id=n.prompt_version_id
                WHERE n.run_id=? ORDER BY n.started_at, n.occurrence""",
                (run_id,),
            ).fetchall()
        return [self._node_summary(row) for row in rows]

    def get_node_execution(self, task_id: str, execution_id: str) -> NodeExecutionDetail:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT n.*, r.task_id, p.id AS p_id, p.node_name AS p_node_name,
                p.version AS p_version, p.content AS p_content, p.content_hash AS p_content_hash,
                p.parent_version_id AS p_parent_version_id, p.created_at AS p_created_at
                FROM node_executions n JOIN execution_runs r ON r.id=n.run_id
                JOIN prompt_versions p ON p.id=n.prompt_version_id
                WHERE r.task_id=? AND n.id=?""",
                (task_id, execution_id),
            ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        summary = self._node_summary(row)
        return NodeExecutionDetail(
            **summary.model_dump(),
            input_state=json.loads(row["input_state_json"]),
            output_state=json.loads(row["output_state_json"]) if row["output_state_json"] else None,
            state_schema_version=row["state_schema_version"],
            model_parameters=json.loads(row["model_config_json"]),
            diagnostics=json.loads(row["diagnostics_json"] or "{}"),
        )

    def prompt_versions_for_run(self, run_id: str) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT node_name, prompt_version_id FROM node_executions
                WHERE run_id=? AND status='completed' ORDER BY started_at""",
                (run_id,),
            ).fetchall()
        return {row["node_name"]: row["prompt_version_id"] for row in rows}

    def record_export(self, task_id: str, kind: str, path: Path, run_id: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO exports(id,task_id,kind,path,created_at,run_id)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), task_id, kind, str(path), utc_now(), run_id),
            )

    def add_evidence(self, evidence: EvidenceRecord) -> None:
        with self.connect() as connection:
            source_revision_ids = evidence.source_revision_ids
            if not source_revision_ids and evidence.source_dataset_ids:
                placeholders = ",".join("?" for _ in evidence.source_dataset_ids)
                rows = connection.execute(
                    f"""SELECT DISTINCT b.revision_id FROM task_dataset_bindings b
                    JOIN dataset_revision_tables t ON t.revision_id=b.revision_id
                    WHERE b.task_id=? AND t.source_dataset_id IN ({placeholders})""",
                    (evidence.task_id, *evidence.source_dataset_ids),
                ).fetchall()
                source_revision_ids = [row[0] for row in rows]
            connection.execute(
                """INSERT OR REPLACE INTO evidence(
                id,task_id,title,source,columns_json,rows_json,created_at,run_id,
                data_revision,source_dataset_ids_json,query,query_hash,source_revision_ids_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.id,
                    evidence.task_id,
                    evidence.title,
                    evidence.source,
                    json.dumps(evidence.columns, ensure_ascii=False),
                    json.dumps(evidence.rows, ensure_ascii=False, default=str),
                    evidence.created_at,
                    evidence.run_id,
                    evidence.data_revision,
                    json.dumps(evidence.source_dataset_ids, ensure_ascii=False),
                    evidence.query,
                    evidence.query_hash,
                    json.dumps(source_revision_ids, ensure_ascii=False),
                ),
            )

    def get_evidence(self, task_id: str, evidence_id: str) -> EvidenceRecord | None:
        cached = _evidence_cache.get()
        if cached and cached[:2] == (str(self.db_path.resolve()), task_id):
            if evidence_id not in cached[2]:
                self.list_evidence_by_ids(task_id, [evidence_id])
            return cached[2].get(evidence_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence WHERE task_id=? AND id=?", (task_id, evidence_id)
            ).fetchone()
        if not row:
            return None
        return EvidenceRecord(
            id=row["id"], task_id=row["task_id"], run_id=row["run_id"], title=row["title"], source=row["source"],
            columns=json.loads(row["columns_json"]), rows=json.loads(row["rows_json"]),
            created_at=row["created_at"], data_revision=row["data_revision"],
            source_dataset_ids=json.loads(row["source_dataset_ids_json"] or "[]"),
            source_revision_ids=json.loads(row["source_revision_ids_json"] or "[]"),
            query=row["query"], query_hash=row["query_hash"],
        )

    @contextmanager
    def evidence_scope(self, task_id: str):
        token = _evidence_cache.set((str(self.db_path.resolve()), task_id, {}))
        try:
            yield
        finally:
            _evidence_cache.reset(token)

    def list_evidence_by_ids(self, task_id: str, evidence_ids) -> list[EvidenceRecord]:
        identifiers = sorted(set(evidence_ids))
        if not identifiers:
            return []
        scoped = _evidence_cache.get()
        cache = scoped[2] if scoped and scoped[:2] == (str(self.db_path.resolve()), task_id) else {}
        missing = [identifier for identifier in identifiers if identifier not in cache]
        if missing:
            with self.connect() as connection:
                for offset in range(0, len(missing), 400):
                    batch = missing[offset:offset + 400]
                    placeholders = ','.join('?' for _ in batch)
                    rows = connection.execute(
                        f'SELECT * FROM evidence WHERE task_id=? AND id IN ({placeholders})',
                        [task_id, *batch],
                    ).fetchall()
                    cache.update({identifier: None for identifier in batch})
                    cache.update({row['id']: self._evidence_record(row) for row in rows})
        return sorted((cache[identifier] for identifier in identifiers if cache[identifier] is not None),
                      key=lambda item: (item.created_at, item.id))

    def evidence_ids(self, task_id: str) -> set[str]:
        with self.connect() as connection:
            return {row[0] for row in connection.execute('SELECT id FROM evidence WHERE task_id=?', (task_id,))}

    def iter_current_evidence(self, task_id: str, revision: int, run_id: str):
        cursor = None
        while True:
            params: list[Any] = [task_id, revision, run_id]
            clause = ''
            if cursor:
                clause = ' AND (e.created_at,e.id) < (?,?)'
                params.extend(cursor)
            with self.connect() as connection:
                rows = connection.execute(
                    '''SELECT e.* FROM evidence e LEFT JOIN execution_runs r ON r.id=e.run_id
                    WHERE e.task_id=? AND e.data_revision=?
                    AND (e.run_id=? OR r.status IN ('completed','completed_with_warnings'))'''
                    + clause + ' ORDER BY e.created_at DESC,e.id DESC LIMIT 32', params,
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._evidence_record(row)
            cursor = (rows[-1]['created_at'], rows[-1]['id'])

    @staticmethod
    def _evidence_record(row) -> EvidenceRecord:
        return EvidenceRecord(
            id=row['id'], task_id=row['task_id'], run_id=row['run_id'], title=row['title'], source=row['source'],
            columns=json.loads(row['columns_json']), rows=json.loads(row['rows_json']),
            created_at=row['created_at'], data_revision=row['data_revision'],
            source_dataset_ids=json.loads(row['source_dataset_ids_json'] or '[]'),
            source_revision_ids=json.loads(row['source_revision_ids_json'] or '[]'),
            query=row['query'], query_hash=row['query_hash'],
        )

    def list_evidence(self, task_id: str) -> list[EvidenceRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [
            EvidenceRecord(
                id=row["id"], task_id=row["task_id"], run_id=row["run_id"], title=row["title"], source=row["source"],
                columns=json.loads(row["columns_json"]), rows=json.loads(row["rows_json"]),
                created_at=row["created_at"], data_revision=row["data_revision"],
                source_dataset_ids=json.loads(row["source_dataset_ids_json"] or "[]"),
                source_revision_ids=json.loads(row["source_revision_ids_json"] or "[]"),
                query=row["query"], query_hash=row["query_hash"],
            )
            for row in rows
        ]

    def add_event(
        self,
        task_id: str,
        event_type: str,
        status: TaskStatus,
        progress: int,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(task_id,event_type,status,progress,message,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, event_type, status, progress, message, json.dumps(payload or {}, ensure_ascii=False), utc_now()),
            )

    def events_after(self, task_id: str, after_id: int) -> list[TaskEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, after_id)
            ).fetchall()
        return [
            TaskEvent(
                id=row["id"], task_id=row["task_id"], event_type=row["event_type"],
                status=TaskStatus(row["status"]), progress=row["progress"], message=row["message"],
                payload=json.loads(row["payload_json"]), created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_task_relationships(self, task_id: str) -> list[DatasetRelationship]:
        """Return persisted relationship decisions for a task."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_relationships WHERE task_id=? ORDER BY confidence DESC, relation_key",
                (task_id,),
            ).fetchall()
        return [DatasetRelationship(
            left_dataset_id=row["left_dataset_id"], left_field=row["left_field"],
            right_dataset_id=row["right_dataset_id"], right_field=row["right_field"],
            confidence=row["confidence"], reason=row["reason"], status=row["status"],
        ) for row in rows]

    def save_task_relationships(self, task_id: str, relationships: list[DatasetRelationship]) -> list[DatasetRelationship]:
        now = utc_now()
        with self.connect() as connection:
            for relation in relationships:
                key = "|".join((relation.left_dataset_id, relation.left_field, relation.right_dataset_id, relation.right_field))
                connection.execute(
                    """INSERT INTO task_relationships(
                    task_id,relation_key,left_dataset_id,left_field,right_dataset_id,right_field,
                    confidence,reason,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(task_id,relation_key) DO UPDATE SET
                    confidence=excluded.confidence,reason=excluded.reason,status=excluded.status,updated_at=excluded.updated_at""",
                    (task_id, key, relation.left_dataset_id, relation.left_field,
                     relation.right_dataset_id, relation.right_field, relation.confidence,
                     relation.reason, relation.status, now, now),
                )
        self.add_event(task_id, "task.relationships_updated", TaskStatus.READY, 15, "表间关系已更新", {"count": len(relationships)})
        return self.list_task_relationships(task_id)

    def get_task(self, task_id: str) -> TaskSnapshot:
        task = self._task_row(task_id)
        with self.connect() as connection:
            files = connection.execute("SELECT * FROM files WHERE task_id=?", (task_id,)).fetchall()
            datasets = connection.execute("SELECT catalog_json FROM datasets WHERE task_id=?", (task_id,)).fetchall()
            messages = connection.execute(
                "SELECT rowid AS sequence,id,role,content,created_at FROM messages WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
        with self.connect() as connection:
            pending = connection.execute(
                """SELECT id FROM execution_runs WHERE task_id=? AND status IN ('queued','running')
                ORDER BY started_at LIMIT 1""",
                (task_id,),
            ).fetchone()
        pending_run_id = pending["id"] if pending else None
        parsed_datasets = [DatasetInfo.model_validate_json(d['catalog_json']) for d in datasets]
        saved_relationships = self.list_task_relationships(task_id)
        try:
            detected = [DatasetRelationship.model_validate_json(item) for item in _cached_relationships(
                str(self.db_path.resolve()), task_id, tuple(d['catalog_json'] for d in datasets)
            )]
        except Exception:
            detected = []
        saved_by_key = {
            (item.left_dataset_id, item.left_field, item.right_dataset_id, item.right_field): item
            for item in saved_relationships
        }
        relationships = []
        seen = set()
        for item in detected + saved_relationships:
            key = (item.left_dataset_id, item.left_field, item.right_dataset_id, item.right_field)
            if key not in seen:
                relationships.append(saved_by_key.get(key, item))
                seen.add(key)
        return TaskSnapshot(
            id=task["id"], title=task["title"], status=TaskStatus(task["status"]),
            progress=task["progress"], status_message=task["status_message"],
            files=[UploadedFile(
                id=f["id"], original_name=f["original_name"], size=f["size"], status=f["status"],
                sheet_count=f["sheet_count"], detected_sheet_count=f["detected_sheet_count"],
                skipped_sheet_count=f["skipped_sheet_count"], row_count=f["row_count"], error=f["error"],
            ) for f in files],
            datasets=parsed_datasets,
            relationships=relationships,
            messages=[MessageRecord(**dict(m)) for m in messages],
            result=AnalysisDraft.model_validate_json(task["result_json"]) if task["result_json"] else None,
            clarification_question=task["clarification_question"], error=task["error"],
            active_run_id=task["active_run_id"],
            pending_run_id=pending_run_id,
            queue_position=self.queue_position(pending_run_id) if pending_run_id else None,
            data_revision=task["data_revision"],
            created_at=task["created_at"], updated_at=task["updated_at"],
        )

    def list_tasks(self, query: str, status: str | None, page: int, page_size: int) -> TaskListResponse:
        clauses, params = [
            "t.archived_at IS NULL",
            "(EXISTS(SELECT 1 FROM files f0 WHERE f0.task_id=t.id) "
            "OR EXISTS(SELECT 1 FROM messages m0 WHERE m0.task_id=t.id) "
            "OR EXISTS(SELECT 1 FROM execution_runs r0 WHERE r0.task_id=t.id))"
        ], []
        if query:
            clauses.append("(t.title LIKE ? OR EXISTS(SELECT 1 FROM files f WHERE f.task_id=t.id AND f.original_name LIKE ?))")
            params.extend([f"%{query}%", f"%{query}%"])
        if status:
            clauses.append("t.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}"
        with self.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM tasks t {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT t.* FROM tasks t {where} ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for row in rows:
                names = [r[0] for r in connection.execute("SELECT original_name FROM files WHERE task_id=?", (row["id"],))]
                items.append(TaskListItem(id=row["id"], title=row["title"], status=TaskStatus(row["status"]), status_message=row["status_message"], file_names=names, created_at=row["created_at"], updated_at=row["updated_at"]))
        return TaskListResponse(items=items, total=total, page=page, page_size=page_size)

    def delete_task(self, task_id: str) -> bool:
        self._task_row(task_id)
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM reports WHERE task_id=? AND status='ready'", (task_id,)
            ).fetchone():
                now = utc_now()
                connection.execute(
                    "UPDATE tasks SET archived_at=?,updated_at=? WHERE id=?",
                    (now, now, task_id),
                )
                return False
            connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return True

    def unfinished_task_ids(self) -> list[str]:
        terminal = (
            TaskStatus.READY, TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_WARNINGS,
            TaskStatus.NEEDS_REVIEW, TaskStatus.FAILED, TaskStatus.OFF_TOPIC,
            TaskStatus.NEEDS_CLARIFICATION,
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE status NOT IN ({','.join('?' for _ in terminal)})", tuple(map(str, terminal))
            ).fetchall()
        return [row[0] for row in rows]

    def get_task_dataset(self, task_id: str, dataset_id: str) -> DatasetInfo:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT catalog_json FROM datasets WHERE task_id=? AND id=?",
                (task_id, dataset_id),
            ).fetchone()
        if row is None:
            raise KeyError(dataset_id)
        return DatasetInfo.model_validate_json(row["catalog_json"])

    def current_dataset_revision(self, task_id: str, source_dataset_id: str) -> DatasetRevision:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.* FROM task_dataset_table_bindings tb
                JOIN dataset_revision_tables t ON t.id=tb.revision_table_id
                JOIN dataset_revisions r ON r.id=t.revision_id
                WHERE tb.task_id=? AND tb.task_dataset_id=?""",
                (task_id, source_dataset_id),
            ).fetchone()
            if row is None:
                raise KeyError(source_dataset_id)
            tables = connection.execute(
                "SELECT catalog_json FROM dataset_revision_tables WHERE revision_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
        return self._revision_model(row, tables)

    def source_revision_provenance(
        self, task_id: str, source_dataset_ids: list[str]
    ) -> list[tuple[str, str]]:
        if not source_dataset_ids:
            return []
        placeholders = ",".join("?" for _ in source_dataset_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT r.id,r.content_hash FROM task_dataset_table_bindings tb
                JOIN dataset_revision_tables t ON t.id=tb.revision_table_id
                JOIN dataset_revisions r ON r.id=t.revision_id
                WHERE tb.task_id=? AND tb.task_dataset_id IN ({placeholders}) ORDER BY r.id""",
                (task_id, *source_dataset_ids),
            ).fetchall()
        return [(row["id"], row["content_hash"]) for row in rows]

    def publish_dataset_correction(
        self,
        task_id: str,
        source_dataset_id: str,
        dataset: DatasetInfo,
        parquet_path: Path,
        content_hash: str,
        change_summary: str,
        expected_data_revision: int,
    ) -> tuple[int, str, str, int]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT data_revision FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if int(task["data_revision"]) != expected_data_revision:
                raise RuntimeError("数据已被其他操作更新，请刷新预览后重试")
            current = connection.execute(
                """SELECT b.dataset_id,b.revision_id,a.latest_revision,
                t.id AS revision_table_id
                FROM task_dataset_bindings b JOIN data_assets a ON a.id=b.dataset_id
                JOIN task_dataset_table_bindings tb ON tb.task_id=b.task_id
                JOIN dataset_revision_tables t ON t.id=tb.revision_table_id
                WHERE b.task_id=? AND tb.task_dataset_id=?""",
                (task_id, source_dataset_id),
            ).fetchone()
            if current is None:
                raise KeyError(source_dataset_id)
            revision_id = str(uuid.uuid4())
            table_revision_id = str(uuid.uuid4())
            revision_number = int(current["latest_revision"]) + 1
            asset_dir = settings.data_dir / "assets" / current["dataset_id"] / f"v{revision_number}"
            asset_dir.mkdir(parents=True, exist_ok=True)
            persistent_path = asset_dir / f"{source_dataset_id}.parquet"
            if parquet_path.resolve() != persistent_path.resolve():
                shutil.copy2(parquet_path, persistent_path)
            previous_tables = connection.execute(
                "SELECT * FROM dataset_revision_tables WHERE revision_id=? ORDER BY id",
                (current["revision_id"],),
            ).fetchall()
            revision_members: list[tuple[DatasetInfo, str]] = []
            for previous in previous_tables:
                member = (
                    dataset
                    if previous["id"] == current["revision_table_id"]
                    else DatasetInfo.model_validate_json(previous["catalog_json"])
                )
                member_hash = content_hash if previous["id"] == current["revision_table_id"] else previous["content_hash"]
                revision_members.append((member, member_hash))
            revision_hash = self._dataset_collection_hash(revision_members)
            connection.execute(
                """INSERT INTO dataset_revisions(
                id,dataset_id,revision_number,parent_revision_id,source_type,status,
                content_hash,change_summary,created_at) VALUES(?,?,?,?,?,'published',?,?,?)""",
                (
                    revision_id, current["dataset_id"], revision_number, current["revision_id"],
                    "correction", revision_hash, change_summary, now,
                ),
            )
            replacement_ids: dict[str, str] = {}
            for previous in previous_tables:
                is_target = previous["id"] == current["revision_table_id"]
                member = dataset if is_target else DatasetInfo.model_validate_json(previous["catalog_json"])
                member_hash = content_hash if is_target else previous["content_hash"]
                new_table_id = table_revision_id if is_target else str(uuid.uuid4())
                member_path = persistent_path if is_target else asset_dir / f"{previous['source_dataset_id']}.parquet"
                if not is_target:
                    source = Path(previous["parquet_path"])
                    if source.exists() and source.resolve() != member_path.resolve():
                        shutil.copy2(source, member_path)
                connection.execute(
                    """INSERT INTO dataset_revision_tables(
                    id,revision_id,source_dataset_id,table_name,parquet_path,catalog_json,content_hash)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        new_table_id, revision_id, previous["source_dataset_id"], member.table_name,
                        str(member_path), member.model_dump_json(), member_hash,
                    ),
                )
                replacement_ids[previous["id"]] = new_table_id
            connection.execute(
                "UPDATE data_assets SET latest_revision=?,updated_at=? WHERE id=?",
                (revision_number, now, current["dataset_id"]),
            )
            connection.execute(
                "UPDATE task_dataset_bindings SET revision_id=?,bound_at=? WHERE task_id=? AND dataset_id=?",
                (revision_id, now, task_id, current["dataset_id"]),
            )
            connection.execute(
                """UPDATE datasets SET table_name=?,display_name=?,row_count=?,catalog_json=?
                WHERE task_id=? AND id=?""",
                (
                    dataset.table_name, dataset.display_name, dataset.row_count,
                    dataset.model_dump_json(), task_id, source_dataset_id,
                ),
            )
            task_tables = connection.execute(
                "SELECT task_dataset_id,revision_table_id FROM task_dataset_table_bindings WHERE task_id=?",
                (task_id,),
            ).fetchall()
            for task_table in task_tables:
                replacement = replacement_ids.get(task_table["revision_table_id"])
                if replacement:
                    connection.execute(
                        """UPDATE task_dataset_table_bindings SET revision_table_id=?
                        WHERE task_id=? AND task_dataset_id=?""",
                        (replacement, task_id, task_table["task_dataset_id"]),
                    )
            data_revision = expected_data_revision + 1
            connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=?", (task_id,))
            connection.execute(
                """UPDATE tasks SET data_revision=?,active_run_id=NULL,result_json=NULL,
                clarification_question=NULL,error=NULL,status=?,progress=15,status_message=?,updated_at=?
                WHERE id=?""",
                (
                    data_revision, TaskStatus.READY, "数据纠错已发布，可以重新分析", now, task_id,
                ),
            )
        self.add_event(
            task_id, "dataset.revision_published", TaskStatus.READY, 15,
            "数据纠错已发布，可以重新分析",
            {
                "dataset_id": current["dataset_id"], "source_dataset_id": source_dataset_id,
                "revision_id": revision_id, "revision_number": revision_number,
                "data_revision": data_revision,
            },
        )
        return data_revision, current["dataset_id"], revision_id, revision_number

    def list_data_assets(self, include_archived: bool = False) -> DatasetAssetList:
        where = "WHERE library_visible=1"
        if not include_archived:
            where += " AND status='active'"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM data_assets {where} ORDER BY updated_at DESC"
            ).fetchall()
        return DatasetAssetList(items=[self._asset_model(row) for row in rows], total=len(rows))

    def get_data_asset(self, dataset_id: str) -> DatasetAssetDetail:
        with self.connect() as connection:
            asset = connection.execute("SELECT * FROM data_assets WHERE id=?", (dataset_id,)).fetchone()
            if asset is None:
                raise KeyError(dataset_id)
            permission = connection.execute(
                """SELECT role FROM resource_permissions
                WHERE resource_type='dataset' AND resource_id=? AND principal_id='local'""",
                (dataset_id,),
            ).fetchone()
            revisions = connection.execute(
                "SELECT * FROM dataset_revisions WHERE dataset_id=? ORDER BY revision_number DESC",
                (dataset_id,),
            ).fetchall()
            revision_models = []
            for revision in revisions:
                tables = connection.execute(
                    "SELECT catalog_json FROM dataset_revision_tables WHERE revision_id=? ORDER BY id",
                    (revision["id"],),
                ).fetchall()
                revision_models.append(self._revision_model(revision, tables))
        return DatasetAssetDetail(
            **self._asset_model(asset).model_dump(), revisions=revision_models,
            permission=permission["role"] if permission else "viewer",
        )

    def revision_table_records(self, dataset_id: str, revision_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            revision = connection.execute(
                "SELECT 1 FROM dataset_revisions WHERE id=? AND dataset_id=?",
                (revision_id, dataset_id),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            rows = connection.execute(
                "SELECT * FROM dataset_revision_tables WHERE revision_id=? ORDER BY id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def attach_revision_to_task(
        self,
        task_id: str,
        asset: DatasetAsset,
        revision_id: str,
        revision_number: int,
        file_record: UploadedFile,
        datasets: list[DatasetInfo],
        revision_table_ids: list[str],
    ) -> None:
        if len(datasets) != len(revision_table_ids):
            raise ValueError("数据表与版本映射数量不一致")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM task_dataset_bindings WHERE task_id=?", (task_id,)
            ).fetchone():
                raise ValueError("任务已经绑定数据集")
            connection.execute(
                """INSERT INTO files(
                id,task_id,original_name,stored_name,size,status,sheet_count,row_count,error,
                detected_sheet_count,skipped_sheet_count) VALUES(?,?,?,?,?,'ready',?,?,NULL,?,0)""",
                (
                    file_record.id, task_id, file_record.original_name,
                    f"dataset:{asset.id}:{revision_id}", file_record.size,
                    file_record.sheet_count, file_record.row_count, file_record.detected_sheet_count,
                ),
            )
            for dataset, revision_table_id in zip(datasets, revision_table_ids):
                connection.execute(
                    """INSERT INTO datasets(id,task_id,file_id,table_name,display_name,row_count,catalog_json)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        dataset.id, task_id, file_record.id, dataset.table_name,
                        dataset.display_name, dataset.row_count, dataset.model_dump_json(),
                    ),
                )
                connection.execute(
                    """INSERT INTO task_dataset_table_bindings(
                    task_id,task_dataset_id,revision_table_id) VALUES(?,?,?)""",
                    (task_id, dataset.id, revision_table_id),
                )
            connection.execute(
                "INSERT INTO task_dataset_bindings(task_id,dataset_id,revision_id,bound_at) VALUES(?,?,?,?)",
                (task_id, asset.id, revision_id, now),
            )
            connection.execute(
                """UPDATE tasks SET title=?,status=?,progress=15,status_message=?,data_revision=1,
                updated_at=? WHERE id=?""",
                (f"{asset.name} v{revision_number} 分析", TaskStatus.READY, "指定数据版本已就绪", now, task_id),
            )
        self.add_event(
            task_id, "dataset.bound", TaskStatus.READY, 15, "指定数据版本已就绪",
            {"dataset_id": asset.id, "revision_id": revision_id},
        )

    def archive_data_asset(self, dataset_id: str) -> None:
        with self.connect() as connection:
            role = connection.execute(
                """SELECT role FROM resource_permissions
                WHERE resource_type='dataset' AND resource_id=? AND principal_id='local'""",
                (dataset_id,),
            ).fetchone()
            if role is None:
                raise KeyError(dataset_id)
            if role["role"] != "owner":
                raise PermissionError("只有所有者可以归档数据集")
            connection.execute(
                "UPDATE data_assets SET status='archived',updated_at=? WHERE id=?",
                (utc_now(), dataset_id),
            )

    def add_artifact(
        self,
        task_id: str,
        run_id: str,
        artifact_type: str,
        title: str,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        status: str = "ready",
        message_id: str | None = None,
    ) -> RunArtifact:
        artifact_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT task_id FROM execution_runs WHERE id=? AND task_id=?", (run_id, task_id)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM run_artifacts WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            connection.execute(
                """INSERT INTO run_artifacts(
                id,task_id,run_id,message_id,sequence,artifact_type,status,title,payload_json,
                evidence_refs_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id, task_id, run_id, message_id, sequence, artifact_type, status,
                    title, json.dumps(payload or {}, ensure_ascii=False, default=str),
                    json.dumps(evidence_refs or [], ensure_ascii=False), now, now,
                ),
            )
        artifact = RunArtifact(
            id=artifact_id, task_id=task_id, run_id=run_id, message_id=message_id,
            sequence=sequence, artifact_type=artifact_type, status=status, title=title,
            payload=payload or {}, evidence_refs=evidence_refs or [], created_at=now, updated_at=now,
        )
        task = self._task_row(task_id)
        self.add_event(
            task_id, "artifact.created", TaskStatus(task["status"]), task["progress"], title,
            {"run_id": run_id, "artifact": artifact.model_dump(mode="json")},
        )
        return artifact

    def list_artifacts(self, task_id: str, run_id: str) -> list[RunArtifact]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_artifacts WHERE task_id=? AND run_id=? ORDER BY sequence",
                (task_id, run_id),
            ).fetchall()
        return [self._artifact_model(row) for row in rows]

    def _publish_initial_asset(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        file_record: UploadedFile,
        datasets: list[DatasetInfo],
        source_type: str,
        *,
        publish_to_library: bool = False,
    ) -> None:
        """Publish one file as one asset whose revision can contain multiple tables."""
        if not datasets:
            return
        now = utc_now()
        prepared: list[tuple[DatasetInfo, Path, str]] = []
        for dataset in datasets:
            source_path = settings.data_dir / "tasks" / task_id / "work" / f"{dataset.id}.parquet"
            prepared.append((dataset, source_path, self._content_hash(source_path)))
        content_hash = self._dataset_collection_hash(
            [(dataset, table_hash) for dataset, _, table_hash in prepared]
        )

        existing = connection.execute(
            """SELECT r.id,r.dataset_id FROM dataset_revisions r
            JOIN data_assets a ON a.id=r.dataset_id
            WHERE r.content_hash=? AND r.status='published' AND a.status='active'
            ORDER BY r.created_at LIMIT 1""",
            (content_hash,),
        ).fetchone()
        if existing is not None:
            revision_tables = connection.execute(
                "SELECT * FROM dataset_revision_tables WHERE revision_id=?",
                (existing["id"],),
            ).fetchall()
            matches = self._match_revision_tables(prepared, revision_tables)
            if matches is not None:
                if publish_to_library:
                    connection.execute(
                        """UPDATE data_assets SET library_visible=1,name=?,updated_at=?
                        WHERE id=?""",
                        (Path(file_record.original_name).stem, now, existing["dataset_id"]),
                    )
                connection.execute(
                    """INSERT OR REPLACE INTO task_dataset_bindings(
                    task_id,dataset_id,revision_id,bound_at) VALUES(?,?,?,?)""",
                    (task_id, existing["dataset_id"], existing["id"], now),
                )
                for dataset, revision_table_id in matches:
                    connection.execute(
                        """INSERT OR REPLACE INTO task_dataset_table_bindings(
                        task_id,task_dataset_id,revision_table_id) VALUES(?,?,?)""",
                        (task_id, dataset.id, revision_table_id),
                    )
                return

        asset_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        asset_dir = settings.data_dir / "assets" / asset_id / "v1"
        asset_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(
            """INSERT INTO data_assets(
            id,owner_id,name,description,status,latest_revision,created_at,updated_at,library_visible)
            VALUES(?,'local',?,'','active',1,?,?,?)""",
            (
                asset_id, Path(file_record.original_name).stem, now, now,
                int(publish_to_library),
            ),
        )
        connection.execute(
            """INSERT INTO dataset_revisions(
            id,dataset_id,revision_number,parent_revision_id,source_type,status,
            content_hash,change_summary,created_at) VALUES(?,?,1,NULL,?,'published',?,?,?)""",
            (revision_id, asset_id, source_type, content_hash, "初始导入", now),
        )
        connection.execute(
            "INSERT INTO task_dataset_bindings(task_id,dataset_id,revision_id,bound_at) VALUES(?,?,?,?)",
            (task_id, asset_id, revision_id, now),
        )
        for dataset, source_path, table_hash in prepared:
            revision_table_id = str(uuid.uuid4())
            parquet_path = asset_dir / f"{dataset.id}.parquet"
            if source_path.exists() and not parquet_path.exists():
                shutil.copy2(source_path, parquet_path)
            connection.execute(
                """INSERT INTO dataset_revision_tables(
                id,revision_id,source_dataset_id,table_name,parquet_path,catalog_json,content_hash)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    revision_table_id, revision_id, dataset.id, dataset.table_name,
                    str(parquet_path), dataset.model_dump_json(), table_hash,
                ),
            )
            connection.execute(
                """INSERT OR REPLACE INTO task_dataset_table_bindings(
                task_id,task_dataset_id,revision_table_id) VALUES(?,?,?)""",
                (task_id, dataset.id, revision_table_id),
            )
        connection.execute(
            """INSERT OR IGNORE INTO resource_permissions(
            resource_type,resource_id,principal_id,role,created_at) VALUES('dataset',?,'local','owner',?)""",
            (asset_id, now),
        )

    def _backfill_data_assets(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT d.task_id,d.file_id,f.original_name,f.size,f.status,f.sheet_count,
            f.detected_sheet_count,f.skipped_sheet_count,f.row_count
            FROM datasets d JOIN files f ON f.id=d.file_id
            LEFT JOIN dataset_revision_tables rt ON rt.source_dataset_id=d.id
            WHERE rt.id IS NULL GROUP BY d.task_id,d.file_id"""
        ).fetchall()
        for row in rows:
            tables = connection.execute(
                "SELECT catalog_json FROM datasets WHERE task_id=? AND file_id=? ORDER BY rowid",
                (row["task_id"], row["file_id"]),
            ).fetchall()
            file_record = UploadedFile(
                id=row["file_id"], original_name=row["original_name"], size=row["size"],
                status=row["status"], sheet_count=row["sheet_count"], row_count=row["row_count"],
                detected_sheet_count=row["detected_sheet_count"],
                skipped_sheet_count=row["skipped_sheet_count"],
            )
            self._publish_initial_asset(
                connection,
                row["task_id"],
                file_record,
                [DatasetInfo.model_validate_json(item["catalog_json"]) for item in tables],
                "migration",
            )

    def _consolidate_legacy_file_assets(self, connection: sqlite3.Connection) -> None:
        """Collapse legacy per-sheet assets into one workbook-level asset."""
        files = connection.execute(
            """SELECT f.* FROM files f
            WHERE f.status='ready' AND (
                SELECT COUNT(*) FROM datasets d WHERE d.file_id=f.id
            ) > 1"""
        ).fetchall()
        for file_record in files:
            rows = connection.execute(
                """SELECT d.catalog_json,rt.id AS revision_table_id,rt.parquet_path,
                rt.content_hash,r.id AS revision_id,r.dataset_id AS asset_id
                FROM datasets d
                JOIN task_dataset_table_bindings tb
                  ON tb.task_id=d.task_id AND tb.task_dataset_id=d.id
                JOIN dataset_revision_tables rt ON rt.id=tb.revision_table_id
                JOIN dataset_revisions r ON r.id=rt.revision_id
                JOIN task_dataset_bindings b
                  ON b.task_id=d.task_id AND b.dataset_id=r.dataset_id AND b.revision_id=r.id
                WHERE d.task_id=? AND d.file_id=? ORDER BY d.rowid""",
                (file_record["task_id"], file_record["id"]),
            ).fetchall()
            old_asset_ids = {row["asset_id"] for row in rows}
            if len(rows) < 2 or len(old_asset_ids) <= 1:
                continue
            prepared = [
                (
                    DatasetInfo.model_validate_json(row["catalog_json"]),
                    Path(row["parquet_path"]),
                    row["content_hash"],
                )
                for row in rows
            ]
            collection_hash = self._dataset_collection_hash(
                [(dataset, table_hash) for dataset, _, table_hash in prepared]
            )
            now = utc_now()
            asset_id = str(uuid.uuid4())
            revision_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO data_assets(
                id,owner_id,name,description,status,latest_revision,created_at,updated_at)
                VALUES(?,'local',?,'','active',1,?,?)""",
                (asset_id, Path(file_record["original_name"]).stem, now, now),
            )
            connection.execute(
                """INSERT INTO dataset_revisions(
                id,dataset_id,revision_number,parent_revision_id,source_type,status,
                content_hash,change_summary,created_at) VALUES(?,?,1,NULL,'migration','published',?,?,?)""",
                (revision_id, asset_id, collection_hash, "合并旧版工作表资产", now),
            )
            identities: set[str] = set()
            for row, (dataset, source_path, table_hash) in zip(rows, prepared):
                revision_table_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO dataset_revision_tables(
                    id,revision_id,source_dataset_id,table_name,parquet_path,catalog_json,content_hash)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        revision_table_id, revision_id, dataset.id, dataset.table_name,
                        str(source_path), dataset.model_dump_json(), table_hash,
                    ),
                )
                connection.execute(
                    """UPDATE task_dataset_table_bindings SET revision_table_id=?
                    WHERE task_id=? AND task_dataset_id=?""",
                    (revision_table_id, file_record["task_id"], dataset.id),
                )
                identities.add(self._dataset_identity(dataset, table_hash))
            placeholders = ",".join("?" for _ in old_asset_ids)
            connection.execute(
                f"DELETE FROM task_dataset_bindings WHERE task_id=? AND dataset_id IN ({placeholders})",
                (file_record["task_id"], *old_asset_ids),
            )
            connection.execute(
                "INSERT INTO task_dataset_bindings(task_id,dataset_id,revision_id,bound_at) VALUES(?,?,?,?)",
                (file_record["task_id"], asset_id, revision_id, now),
            )
            connection.execute(
                """INSERT OR IGNORE INTO resource_permissions(
                resource_type,resource_id,principal_id,role,created_at)
                VALUES('dataset',?,'local','owner',?)""",
                (asset_id, now),
            )
            self._archive_unbound_single_table_duplicates(connection, identities, asset_id, now)

    @classmethod
    def _archive_unbound_single_table_duplicates(
        cls,
        connection: sqlite3.Connection,
        identities: set[str],
        keep_asset_id: str,
        now: str,
    ) -> None:
        candidates = connection.execute(
            """SELECT a.id,rt.catalog_json,rt.content_hash
            FROM data_assets a
            JOIN dataset_revisions r
              ON r.dataset_id=a.id AND r.revision_number=a.latest_revision
            JOIN dataset_revision_tables rt ON rt.revision_id=r.id
            WHERE a.status='active' AND a.id<>?
              AND (SELECT COUNT(*) FROM dataset_revision_tables x WHERE x.revision_id=r.id)=1
              AND NOT EXISTS(SELECT 1 FROM task_dataset_bindings b WHERE b.dataset_id=a.id)""",
            (keep_asset_id,),
        ).fetchall()
        duplicate_ids = []
        for row in candidates:
            dataset = DatasetInfo.model_validate_json(row["catalog_json"])
            if cls._dataset_identity(dataset, row["content_hash"]) in identities:
                duplicate_ids.append(row["id"])
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            connection.execute(
                f"UPDATE data_assets SET status='archived',updated_at=? WHERE id IN ({placeholders})",
                (now, *duplicate_ids),
            )

    @staticmethod
    def _archive_legacy_orphan_sheet_assets(connection: sqlite3.Connection) -> None:
        """Hide unbound legacy assets whose names are now represented inside a workbook asset."""
        workbook_tables = connection.execute(
            """SELECT rt.catalog_json FROM data_assets a
            JOIN dataset_revisions r
              ON r.dataset_id=a.id AND r.revision_number=a.latest_revision
            JOIN dataset_revision_tables rt ON rt.revision_id=r.id
            WHERE a.status='active' AND (
                SELECT COUNT(*) FROM dataset_revision_tables x WHERE x.revision_id=r.id
            ) > 1"""
        ).fetchall()
        names: set[str] = set()
        for row in workbook_tables:
            dataset = DatasetInfo.model_validate_json(row["catalog_json"])
            names.add(dataset.display_name.casefold().strip())
            if dataset.source_region:
                names.add(dataset.source_region.sheet_name.casefold().strip())
        if not names:
            return
        candidates = connection.execute(
            """SELECT a.id,a.name,rt.catalog_json FROM data_assets a
            JOIN dataset_revisions r
              ON r.dataset_id=a.id AND r.revision_number=a.latest_revision
            JOIN dataset_revision_tables rt ON rt.revision_id=r.id
            WHERE a.status='active'
              AND (SELECT COUNT(*) FROM dataset_revision_tables x WHERE x.revision_id=r.id)=1
              AND NOT EXISTS(SELECT 1 FROM task_dataset_bindings b WHERE b.dataset_id=a.id)"""
        ).fetchall()
        duplicate_ids = []
        for row in candidates:
            dataset = DatasetInfo.model_validate_json(row["catalog_json"])
            candidate_names = {row["name"].casefold().strip(), dataset.display_name.casefold().strip()}
            has_broken_name = any("\ufffd" in name for name in candidate_names)
            if candidate_names & names or has_broken_name:
                duplicate_ids.append(row["id"])
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            connection.execute(
                f"UPDATE data_assets SET status='archived',updated_at=? WHERE id IN ({placeholders})",
                (utc_now(), *duplicate_ids),
            )

    @staticmethod
    def _dataset_identity(dataset: DatasetInfo, content_hash: str) -> str:
        source = dataset.source_region.model_dump(mode="json") if dataset.source_region else None
        return json.dumps(
            {"display_name": dataset.display_name, "source_region": source, "content_hash": content_hash},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _dataset_collection_hash(cls, tables: list[tuple[DatasetInfo, str]]) -> str:
        identities = sorted(cls._dataset_identity(dataset, content_hash) for dataset, content_hash in tables)
        payload = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _match_revision_tables(
        cls,
        prepared: list[tuple[DatasetInfo, Path, str]],
        revision_tables: list[sqlite3.Row],
    ) -> list[tuple[DatasetInfo, str]] | None:
        available: dict[str, list[str]] = {}
        for row in revision_tables:
            catalog = DatasetInfo.model_validate_json(row["catalog_json"])
            identity = cls._dataset_identity(catalog, row["content_hash"])
            available.setdefault(identity, []).append(row["id"])
        matches: list[tuple[DatasetInfo, str]] = []
        for dataset, _, table_hash in prepared:
            candidates = available.get(cls._dataset_identity(dataset, table_hash), [])
            if not candidates:
                return None
            matches.append((dataset, candidates.pop()))
        if any(available.values()):
            return None
        return matches

    def _materialize_asset_files(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT rt.id,rt.parquet_path,rt.source_dataset_id,r.dataset_id,r.revision_number
            FROM dataset_revision_tables rt JOIN dataset_revisions r ON r.id=rt.revision_id"""
        ).fetchall()
        for row in rows:
            source = Path(row["parquet_path"])
            target_dir = settings.data_dir / "assets" / row["dataset_id"] / f"v{row['revision_number']}"
            target = target_dir / f"{row['source_dataset_id']}.parquet"
            if source.resolve() == target.resolve():
                continue
            if source.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(source, target)
                connection.execute(
                    "UPDATE dataset_revision_tables SET parquet_path=? WHERE id=?",
                    (str(target), row["id"]),
                )

    @staticmethod
    def _backfill_revision_table_bindings(connection: sqlite3.Connection) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO task_dataset_table_bindings(
            task_id,task_dataset_id,revision_table_id)
            SELECT d.task_id,d.id,rt.id FROM datasets d
            JOIN task_dataset_bindings b ON b.task_id=d.task_id
            JOIN dataset_revision_tables rt ON rt.revision_id=b.revision_id
            AND rt.source_dataset_id=d.id"""
        )

    @staticmethod
    def _task_input_snapshot(connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """SELECT b.dataset_id,b.revision_id,r.revision_number,r.content_hash
            FROM task_dataset_bindings b JOIN dataset_revisions r ON r.id=b.revision_id
            WHERE b.task_id=? ORDER BY b.dataset_id""",
            (task_id,),
        ).fetchall()
        knowledge_rows = connection.execute(
            """SELECT b.knowledge_base_id,b.revision_id,r.revision_number,r.content_hash,
            r.embedding_model FROM task_knowledge_bindings b
            JOIN knowledge_base_revisions r ON r.id=b.revision_id
            WHERE b.task_id=? ORDER BY b.knowledge_base_id""",
            (task_id,),
        ).fetchall()
        return {
            "dataset_ids": [row["dataset_id"] for row in rows],
            "revision_ids": [row["revision_id"] for row in rows],
            "revisions": [dict(row) for row in rows],
            "knowledge_base_ids": [row["knowledge_base_id"] for row in knowledge_rows],
            "knowledge_revision_ids": [row["revision_id"] for row in knowledge_rows],
            "knowledge_revisions": [dict(row) for row in knowledge_rows],
        }

    @staticmethod
    def _input_fingerprint(snapshot: dict[str, Any]) -> str:
        canonical = {
            "revision_ids": sorted(str(value) for value in snapshot.get("revision_ids", [])),
            "knowledge_revision_ids": sorted(
                str(value) for value in snapshot.get("knowledge_revision_ids", [])
            ),
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _assert_run_input_current(
        cls, connection: sqlite3.Connection, run: sqlite3.Row
    ) -> None:
        cls._assert_snapshot_current(
            connection,
            run["task_id"],
            int(run["data_revision"]),
            json.loads(run["input_snapshot_json"] or "{}"),
        )

    @classmethod
    def _assert_snapshot_current(
        cls,
        connection: sqlite3.Connection,
        task_id: str,
        data_revision: int,
        run_snapshot: dict[str, Any],
    ) -> None:
        task = connection.execute(
            "SELECT data_revision FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        if int(task["data_revision"]) != int(data_revision):
            raise ValueError("分析分支基于旧数据版本，请基于最新数据重新分析")

        current_snapshot = cls._task_input_snapshot(connection, task_id)
        if not run_snapshot:
            if current_snapshot.get("knowledge_revision_ids"):
                raise ValueError("分析分支缺少知识库版本信息，请基于当前知识库重新分析")
            return
        if cls._input_fingerprint(run_snapshot) == cls._input_fingerprint(current_snapshot):
            return
        if sorted(run_snapshot.get("knowledge_revision_ids", [])) != sorted(
            current_snapshot.get("knowledge_revision_ids", [])
        ):
            raise ValueError("分析分支基于旧知识库版本，请基于当前知识库重新分析")
        raise ValueError("分析分支的输入数据版本已变化，请重新分析")

    @staticmethod
    def _content_hash(path: Path, fallback: str = "") -> str:
        digest = hashlib.sha256()
        if path.exists():
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(fallback.encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _asset_model(row: sqlite3.Row) -> DatasetAsset:
        return DatasetAsset(
            id=row["id"], owner_id=row["owner_id"], name=row["name"],
            description=row["description"], status=row["status"],
            latest_revision=row["latest_revision"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _revision_model(row: sqlite3.Row, tables: list[sqlite3.Row]) -> DatasetRevision:
        return DatasetRevision(
            id=row["id"], dataset_id=row["dataset_id"], revision_number=row["revision_number"],
            parent_revision_id=row["parent_revision_id"], source_type=row["source_type"],
            status=row["status"], content_hash=row["content_hash"],
            change_summary=row["change_summary"],
            tables=[DatasetInfo.model_validate_json(table["catalog_json"]) for table in tables],
            created_at=row["created_at"],
        )

    @staticmethod
    def _artifact_model(row: sqlite3.Row) -> RunArtifact:
        return RunArtifact(
            id=row["id"], task_id=row["task_id"], run_id=row["run_id"],
            message_id=row["message_id"], sequence=row["sequence"],
            artifact_type=row["artifact_type"], status=row["status"], title=row["title"],
            payload=json.loads(row["payload_json"]),
            evidence_refs=json.loads(row["evidence_refs_json"] or "[]"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _report_summary(row: sqlite3.Row) -> ReportSummary:
        return ReportSummary(
            id=row["id"], task_id=row["task_id"], title=row["title"], status=row["status"],
            latest_version=row["latest_version"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _report_version(row: sqlite3.Row) -> ReportVersion:
        return ReportVersion(
            id=row["id"], report_id=row["report_id"], version_number=row["version_number"],
            run_id=row["run_id"], status=row["status"], content=json.loads(row["content_json"]),
            source_revision_ids=json.loads(row["source_revision_ids_json"] or "[]"),
            content_hash=row["content_hash"], html_path=row["html_path"], created_at=row["created_at"],
        )

    @staticmethod
    def _report_job(row: sqlite3.Row) -> ReportJob:
        return ReportJob(
            id=row["id"], task_id=row["task_id"], run_id=row["run_id"], status=row["status"],
            report_id=row["report_id"], report_version_id=row["report_version_id"],
            error=row["error"], attempt_count=row["attempt_count"], claimed_at=row["claimed_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _task_row(self, task_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_legacy_results(connection: sqlite3.Connection) -> None:
        tasks = connection.execute(
            """SELECT id,result_json,created_at,updated_at FROM tasks
            WHERE active_run_id IS NULL AND result_json IS NOT NULL"""
        ).fetchall()
        for task in tasks:
            run = connection.execute(
                """SELECT id FROM execution_runs
                WHERE task_id=? AND status='completed' ORDER BY started_at DESC LIMIT 1""",
                (task["id"],),
            ).fetchone()
            if run:
                connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=?", (task["id"],))
                connection.execute(
                    "UPDATE execution_runs SET is_active=1, result_json=? WHERE id=?",
                    (task["result_json"], run["id"]),
                )
                connection.execute("UPDATE tasks SET active_run_id=? WHERE id=?", (run["id"], task["id"]))
            else:
                run_id = str(uuid.uuid4())
                message = connection.execute(
                    """SELECT content FROM messages WHERE task_id=? AND role='user'
                    ORDER BY rowid DESC LIMIT 1""",
                    (task["id"],),
                ).fetchone()
                connection.execute(
                    """INSERT INTO execution_runs(
                    id,task_id,question,status,error,started_at,finished_at,parent_run_id,
                    forked_from_node_execution_id,entry_node,prompt_version_id,result_json,is_active)
                    VALUES (?, ?, ?, 'completed', NULL, ?, ?, NULL, NULL, 'legacy', NULL, ?, 1)""",
                    (
                        run_id, task["id"], message["content"] if message else "旧版分析",
                        task["created_at"], task["updated_at"], task["result_json"],
                    ),
                )
                connection.execute("UPDATE tasks SET active_run_id=? WHERE id=?", (run_id, task["id"]))

    @staticmethod
    def _run_model(row: sqlite3.Row) -> WorkflowRun:
        return WorkflowRun(
            id=row["id"], task_id=row["task_id"], question=row["question"], status=row["status"],
            parent_run_id=row["parent_run_id"],
            forked_from_node_execution_id=row["forked_from_node_execution_id"],
            entry_node=row["entry_node"] or "classify", prompt_version_id=row["prompt_version_id"],
            result=AnalysisDraft.model_validate_json(row["result_json"]) if row["result_json"] else None,
            is_active=bool(row["is_active"]), error=row["error"], started_at=row["started_at"],
            finished_at=row["finished_at"], data_revision=row["data_revision"],
            message_sequence=row["message_sequence"], claimed_at=row["claimed_at"],
            heartbeat_at=row["heartbeat_at"], attempt_count=row["attempt_count"],
        )

    @staticmethod
    def _node_summary(row: sqlite3.Row) -> NodeExecutionSummary:
        diagnostics = json.loads(row['diagnostics_json'] or '{}')
        mode = diagnostics.get('execution_mode', 'unknown')
        if mode == 'unknown' and (diagnostics.get('model_request_attempt') or diagnostics.get('attempt')) and (
            'prompt_eval_count' in diagnostics or 'response_content_chars' in diagnostics
        ):
            mode = 'model'
        if mode not in {'model', 'deterministic', 'unknown'}:
            mode = 'unknown'
        supported = mode == 'model' and row['status'] == 'completed'
        reason = None if supported else (
            '纯程序节点不使用提示词，请重新发起分析以重新计算。' if mode == 'deterministic' else
            '历史记录无法确认模型调用，节点仅供查看。' if mode == 'unknown' else '只能重跑已完成的模型节点。'
        )
        prompt = PromptVersion(
            id=row["p_id"], node_name=row["p_node_name"], version=row["p_version"],
            content=row["p_content"], content_hash=row["p_content_hash"],
            parent_version_id=row["p_parent_version_id"], created_at=row["p_created_at"],
        )
        return NodeExecutionSummary(
            execution_mode=mode, prompt_replay_supported=supported, replay_unavailable_reason=reason,
            id=row["id"], run_id=row["run_id"], node_name=row["node_name"],
            occurrence=row["occurrence"], prompt_version=prompt, status=row["status"],
            error=row["error"], started_at=row["started_at"], finished_at=row["finished_at"],
        )


repository = Repository()
