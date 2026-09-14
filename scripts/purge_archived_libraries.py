"""Explicit maintenance command for deleting archived libraries and linked tasks."""
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.repository import repository


def main():
    with repository.connect() as connection:
        assets = [r[0] for r in connection.execute("SELECT id FROM data_assets WHERE status='archived'")]
        tasks = [r[0] for r in connection.execute(
            """SELECT DISTINCT t.id FROM tasks t LEFT JOIN task_dataset_bindings b ON b.task_id=t.id
            LEFT JOIN data_assets a ON a.id=b.dataset_id
            WHERE a.status='archived' OR t.archived_at IS NOT NULL"""
        )]
        reports = {r[0] for r in connection.execute("SELECT id FROM reports WHERE status='archived'")}
        runs = []
        for task_id in tasks:
            if connection.execute("SELECT 1 FROM execution_runs WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchone():
                raise RuntimeError('Cannot delete a running task')
            reports.update(r[0] for r in connection.execute('SELECT id FROM reports WHERE task_id=?', (task_id,)))
            runs.extend(r[0] for r in connection.execute('SELECT id FROM execution_runs WHERE task_id=?', (task_id,)))
    directories = [(settings.data_dir / 'tasks' / task_id).resolve() for task_id in tasks]
    root = (settings.data_dir / 'tasks').resolve()
    if any(directory.parent != root for directory in directories):
        raise ValueError('Task directory outside data root')
    for report_id in reports:
        repository.delete_report(report_id)
    for task_id, directory in zip(tasks, directories, strict=True):
        repository.delete_task(task_id)
        if directory.exists():
            shutil.rmtree(directory)
    for asset_id in assets:
        repository.delete_data_asset(asset_id)
    if settings.checkpoint_db.exists():
        with sqlite3.connect(settings.checkpoint_db) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('checkpoints', 'writes'):
                if table in tables:
                    connection.executemany(f'DELETE FROM {table} WHERE thread_id=?', [(run_id,) for run_id in runs])
    with repository.connect() as connection:
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute("SELECT COUNT(*) FROM data_assets WHERE status='archived'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reports WHERE status='archived'").fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM tasks WHERE archived_at IS NOT NULL').fetchone()[0] == 0
    print({'datasets_deleted': len(assets), 'tasks_deleted': len(tasks), 'reports_deleted': len(reports), 'runs_deleted': len(runs)})


if __name__ == '__main__':
    main()
