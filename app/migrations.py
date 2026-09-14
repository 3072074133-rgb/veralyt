from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import utc_now


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "dataset_revision_and_report_foundation",
        """
        CREATE TABLE IF NOT EXISTS data_assets (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL DEFAULT 'local',
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            latest_revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dataset_revisions (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES data_assets(id),
            revision_number INTEGER NOT NULL,
            parent_revision_id TEXT REFERENCES dataset_revisions(id),
            source_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'published',
            content_hash TEXT NOT NULL,
            change_summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(dataset_id, revision_number)
        );
        CREATE TABLE IF NOT EXISTS dataset_revision_tables (
            id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES dataset_revisions(id) ON DELETE CASCADE,
            source_dataset_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            parquet_path TEXT NOT NULL,
            catalog_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(revision_id, source_dataset_id)
        );
        CREATE TABLE IF NOT EXISTS task_dataset_bindings (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            dataset_id TEXT NOT NULL REFERENCES data_assets(id),
            revision_id TEXT NOT NULL REFERENCES dataset_revisions(id),
            bound_at TEXT NOT NULL,
            PRIMARY KEY(task_id, dataset_id)
        );
        CREATE TABLE IF NOT EXISTS resource_permissions (
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(resource_type, resource_id, principal_id)
        );
        CREATE TABLE IF NOT EXISTS run_artifacts (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES execution_runs(id) ON DELETE CASCADE,
            message_id TEXT,
            sequence INTEGER NOT NULL,
            artifact_type TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            latest_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_versions (
            id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL,
            run_id TEXT NOT NULL REFERENCES execution_runs(id),
            status TEXT NOT NULL,
            content_json TEXT NOT NULL,
            source_revision_ids_json TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            html_path TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(report_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS ix_data_assets_updated ON data_assets(updated_at DESC);
        CREATE INDEX IF NOT EXISTS ix_dataset_revisions_asset ON dataset_revisions(dataset_id, revision_number DESC);
        CREATE INDEX IF NOT EXISTS ix_task_dataset_bindings_task ON task_dataset_bindings(task_id);
        CREATE INDEX IF NOT EXISTS ix_run_artifacts_run ON run_artifacts(run_id, sequence);
        CREATE INDEX IF NOT EXISTS ix_reports_updated ON reports(updated_at DESC);
        """,
    ),
    (
        2,
        "run_input_snapshots_and_evidence_revisions",
        "",
    ),
    (
        3,
        "task_revision_table_bindings",
        """
        CREATE TABLE IF NOT EXISTS task_dataset_table_bindings (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            task_dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            revision_table_id TEXT NOT NULL REFERENCES dataset_revision_tables(id),
            PRIMARY KEY(task_id, task_dataset_id)
        );
        CREATE INDEX IF NOT EXISTS ix_task_dataset_table_bindings_task
        ON task_dataset_table_bindings(task_id);
        """,
    ),
    (
        4,
        "soft_archive_report_source_tasks",
        "",
    ),
    (
        5,
        "persistent_report_jobs",
        """
        CREATE TABLE IF NOT EXISTS report_jobs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES execution_runs(id),
            status TEXT NOT NULL,
            report_id TEXT REFERENCES reports(id),
            report_version_id TEXT REFERENCES report_versions(id),
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_report_jobs_status ON report_jobs(status, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_report_jobs_pending_run
        ON report_jobs(run_id) WHERE status IN ('queued','generating');
        """,
    ),
    (
        6,
        "node_execution_diagnostics",
        "",
    ),
    (
        8,
        "workbook_level_dataset_assets",
        "",
    ),
    (
        9,
        "explicit_dataset_library_visibility",
        "",
    ),
    (
        10,
        "task_dataset_relationship_confirmations",
        """
        CREATE TABLE IF NOT EXISTS task_relationships (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            relation_key TEXT NOT NULL,
            left_dataset_id TEXT NOT NULL,
            left_field TEXT NOT NULL,
            right_dataset_id TEXT NOT NULL,
            right_field TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(task_id, relation_key)
        );
        CREATE INDEX IF NOT EXISTS ix_task_relationships_task
        ON task_relationships(task_id, status);
        """,
    ),
    (
        11,
        "remove_node_execution_history",
        """
        DROP TABLE IF EXISTS node_executions;
        DROP TABLE IF EXISTS prompt_versions;
        DROP INDEX IF EXISTS ix_node_executions_run;
        """,
    ),
    (
        12,
        "remove_optional_context_library",
        """
        DROP TABLE IF EXISTS run_knowledge_matches;
        DROP TABLE IF EXISTS task_knowledge_bindings;
        DROP TABLE IF EXISTS knowledge_chunks;
        DROP TABLE IF EXISTS knowledge_documents;
        DROP TABLE IF EXISTS knowledge_base_revisions;
        DROP TABLE IF EXISTS knowledge_bases;
        DELETE FROM resource_permissions WHERE resource_type='knowledge_base';
        UPDATE execution_runs
        SET input_snapshot_json = json_remove(
            input_snapshot_json,
            '$.knowledge_base_ids',
            '$.knowledge_revision_ids',
            '$.knowledge_revisions'
        )
        WHERE json_valid(input_snapshot_json);
        UPDATE report_versions
        SET content_json = json_remove(content_json, '$.provenance.knowledge_revision_ids')
        WHERE json_valid(content_json);
        DELETE FROM schema_migrations WHERE version=7;
        """,
    ),
)


def apply_migrations(connection: sqlite3.Connection, db_path: Path) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
        )"""
    )
    applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
    pending = [migration for migration in MIGRATIONS if migration[0] not in applied]
    if not pending:
        return
    if db_path.exists() and db_path.stat().st_size:
        backup = db_path.with_suffix(f"{db_path.suffix}.pre-v{pending[0][0]}.bak")
        if not backup.exists():
            connection.commit()
            backup_connection = sqlite3.connect(backup)
            try:
                connection.backup(backup_connection)
            finally:
                backup_connection.close()
    for version, name, sql in pending:
        if version == 2:
            _ensure_column(connection, "execution_runs", "input_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(connection, "evidence", "source_revision_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        if version == 4:
            _ensure_column(connection, "tasks", "archived_at", "TEXT")
        if version == 9:
            _ensure_column(connection, "data_assets", "library_visible", "INTEGER NOT NULL DEFAULT 0")
        if version == 11:
            columns = {row[1] for row in connection.execute('PRAGMA table_info("execution_runs")')}
            for column in ("forked_from_node_execution_id", "prompt_version_id"):
                if column in columns:
                    connection.execute(f'ALTER TABLE execution_runs DROP COLUMN "{column}"')
        connection.executescript(sql)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, utc_now()),
        )


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in existing:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
