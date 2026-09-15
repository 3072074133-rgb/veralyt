from __future__ import annotations

from pathlib import Path
import time

import duckdb
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.ingestion import task_dir
from app.main import app
from app.models import AnalysisDraft
from app.repository import repository


def _workbook_bytes(path: Path) -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "收入"
    first.append(["月份", "金额"])
    first.append(["1月", 100])
    second = workbook.create_sheet("成本")
    second.append(["月份", "金额"])
    second.append(["1月", 40])
    workbook.save(path)
    return path.read_bytes()


def test_multisheet_workbook_is_one_asset_with_multiple_tables(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "multisheet.sqlite"
    try:
        payload = _workbook_bytes(tmp_path / "经营数据.xlsx")
        with TestClient(app) as client:
            task_id = client.post("/api/v1/tasks").json()["id"]
            uploaded = client.post(
                f"/api/v1/tasks/{task_id}/files?publish_to_library=true",
                files={"files": ("经营数据.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
            assert uploaded.status_code == 200, uploaded.text
            assert len(uploaded.json()["task"]["datasets"]) == 2

            assets = client.get("/api/v1/datasets").json()
            assert assets["total"] == 1
            assert assets["items"][0]["name"] == "经营数据"
            detail = client.get(f"/api/v1/datasets/{assets['items'][0]['id']}").json()
            assert {table["display_name"] for table in detail["revisions"][0]["tables"]} == {"收入", "成本"}
            editor = client.post(f"/api/v1/datasets/{detail['id']}/revisions/{detail['revisions'][0]['id']}/tasks?editing=true")
            assert editor.status_code == 201, editor.text
            editor_id = editor.json()['id']
            assert repository.get_task(editor_id).datasets
            assert editor_id not in {item.id for item in repository.list_tasks('', None, 1, 100).items}
            assert client.delete(f'/api/v1/tasks/{editor_id}').status_code == 204
            assert repository.get_data_asset(detail['id']).revisions

            income = next(
                table for table in uploaded.json()["task"]["datasets"] if table["display_name"] == "收入"
            )
            corrected = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{income['id']}/corrections",
                json={
                    "expected_data_revision": 1,
                    "cell_updates": [{"row_id": 1, "column": "金额", "value": 120}],
                    "change_summary": "修正收入",
                },
            )
            assert corrected.status_code == 200, corrected.text
            revised = client.get(f"/api/v1/datasets/{assets['items'][0]['id']}").json()
            assert len(revised["revisions"][0]["tables"]) == 2
            assert {table["display_name"] for table in revised["revisions"][0]["tables"]} == {"收入", "成本"}
            asset_id = assets['items'][0]['id']
            removed_id = revised['revisions'][0]['id']
            assert client.delete(f'/api/v1/datasets/{asset_id}/revisions/{removed_id}').status_code == 204
            remaining = client.get(f'/api/v1/datasets/{asset_id}').json()
            assert remaining['latest_revision'] == 1
            assert len(remaining['revisions']) == 1
            assert repository.revision_table_records(asset_id, removed_id)
            assert client.delete(f"/api/v1/datasets/{asset_id}/revisions/{remaining['revisions'][0]['id']}").status_code == 409
            next_revision = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{income['id']}/corrections",
                json={'expected_data_revision': 2, 'cell_updates': [{'row_id': 1, 'column': '金额', 'value': 140}]},
            )
            assert next_revision.status_code == 200, next_revision.text
            assert next_revision.json()['revision_number'] == 3
    finally:
        repository.db_path = old_path


def test_duplicate_workbook_upload_reuses_existing_asset(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "deduplicated.sqlite"
    try:
        payload = _workbook_bytes(tmp_path / "经营数据.xlsx")
        with TestClient(app) as client:
            task_ids = [client.post("/api/v1/tasks").json()["id"] for _ in range(2)]
            for task_id in task_ids:
                uploaded = client.post(
                    f"/api/v1/tasks/{task_id}/files?publish_to_library=true",
                    files={"files": ("经营数据.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                assert uploaded.status_code == 200, uploaded.text

            assets = client.get("/api/v1/datasets").json()
            assert assets["total"] == 1
            with repository.connect() as connection:
                bindings = connection.execute(
                    "SELECT task_id,dataset_id,revision_id FROM task_dataset_bindings ORDER BY task_id"
                ).fetchall()
            assert len(bindings) == 2
            assert len({row["dataset_id"] for row in bindings}) == 1
            assert len({row["revision_id"] for row in bindings}) == 1
    finally:
        repository.db_path = old_path


def test_preview_profile_correction_and_asset_revision(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "datasets.sqlite"
    try:
        with TestClient(app) as client:
            task_id = client.post("/api/v1/tasks").json()["id"]
            upload = client.post(
                f"/api/v1/tasks/{task_id}/files",
                files={"files": ("sales.csv", "客户,收入\n甲,100\n乙,200\n".encode(), "text/csv")},
            )
            dataset_id = upload.json()["task"]["datasets"][0]["id"]
            preview = client.get(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/preview?page_size=1"
            )
            assert preview.status_code == 200
            assert preview.json()["rows"][0]["__row_id"] == 1
            profile = client.get(f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/profile")
            assert profile.json()["row_count"] == 2
            assert profile.json()["duplicate_count"] == 0

            correction = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 1,
                    "cell_updates": [{"row_id": 1, "column": "收入", "value": 150}],
                    "metadata_updates": [{"column": "收入", "unit": "元"}],
                    "change_summary": "修正甲客户收入",
                },
            )
            assert correction.status_code == 200, correction.text
            assert correction.json()["data_revision"] == 2
            assert correction.json()["revision_number"] == 2
            revised = client.get(f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/preview").json()
            assert revised["rows"][0]["收入"] == 150
            assert revised["columns"][1]["unit"] == "元"

            conflict = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 1,
                    "cell_updates": [{"row_id": 1, "column": "收入", "value": 999}],
                },
            )
            assert conflict.status_code == 409
            assets = client.get("/api/v1/datasets").json()
            assert assets["total"] == 0
            detail = client.get(f"/api/v1/datasets/{correction.json()['dataset_asset_id']}").json()
            assert [item["revision_number"] for item in detail["revisions"]] == [2, 1]
            revision_table = repository.revision_table_records(
                correction.json()["dataset_asset_id"], correction.json()["dataset_revision_id"]
            )[0]
            assert Path(revision_table["parquet_path"]).is_file()

            new_task = client.post(
                f"/api/v1/datasets/{correction.json()['dataset_asset_id']}/revisions/"
                f"{detail['revisions'][1]['id']}/tasks"
            )
            assert new_task.status_code == 201, new_task.text
            old_version_task = client.get(f"/api/v1/tasks/{new_task.json()['id']}").json()
            assert old_version_task["data_revision"] == 1
            old_preview = client.get(
                f"/api/v1/tasks/{new_task.json()['id']}/datasets/"
                f"{old_version_task['datasets'][0]['id']}/preview"
            ).json()
            assert old_preview["rows"][0]["收入"] == 100
    finally:
        repository.db_path = old_path


def test_row_ids_remain_stable_across_sort_delete_add_and_correction(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "stable-row-ids.sqlite"
    try:
        with TestClient(app) as client:
            task_id = client.post("/api/v1/tasks").json()["id"]
            upload = client.post(
                f"/api/v1/tasks/{task_id}/files",
                files={
                    "files": (
                        "sales.csv",
                        "客户,收入\n甲,100\n乙,300\n丙,200\n".encode(),
                        "text/csv",
                    )
                },
            )
            dataset = upload.json()["task"]["datasets"][0]
            dataset_id = dataset["id"]
            assert "__aa_row_id" not in {column["name"] for column in dataset["columns"]}

            sorted_preview = client.get(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/preview",
                params={"sort_by": "收入", "sort_direction": "desc"},
            ).json()
            assert [row["客户"] for row in sorted_preview["rows"]] == ["乙", "丙", "甲"]
            assert [row["__row_id"] for row in sorted_preview["rows"]] == [2, 3, 1]

            corrected = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 1,
                    "cell_updates": [{"row_id": 2, "column": "收入", "value": 350}],
                },
            )
            assert corrected.status_code == 200, corrected.text

            deleted = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 2,
                    "deleted_row_ids": [3],
                },
            )
            assert deleted.status_code == 200, deleted.text

            added = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 3,
                    "added_rows": [{"客户": "丁", "收入": 400}],
                },
            )
            assert added.status_code == 200, added.text
            preview = client.get(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/preview"
            ).json()
            assert [(row["__row_id"], row["客户"], row["收入"]) for row in preview["rows"]] == [
                (1, "甲", 100),
                (2, "乙", 350),
                (4, "丁", 400),
            ]

            second_correction = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/corrections",
                json={
                    "expected_data_revision": 4,
                    "cell_updates": [{"row_id": 4, "column": "收入", "value": 450}],
                },
            )
            assert second_correction.status_code == 200, second_correction.text
            final_rows = client.get(
                f"/api/v1/tasks/{task_id}/datasets/{dataset_id}/preview"
            ).json()["rows"]
            assert next(row for row in final_rows if row["__row_id"] == 4)["收入"] == 450
    finally:
        repository.db_path = old_path


def test_legacy_table_gets_a_row_id_high_water_mark_before_deletion(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "legacy-row-ids.sqlite"
    try:
        with TestClient(app) as client:
            task_id = client.post("/api/v1/tasks").json()["id"]
            upload = client.post(
                f"/api/v1/tasks/{task_id}/files",
                files={
                    "files": (
                        "legacy.csv",
                        "客户,收入\n甲,100\n乙,200\n丙,300\n".encode(),
                        "text/csv",
                    )
                },
            )
            dataset = upload.json()["task"]["datasets"][0]
            db_path = task_dir(task_id) / "work" / "analysis.duckdb"
            with duckdb.connect(str(db_path)) as connection:
                connection.execute(
                    f'ALTER TABLE "{dataset["table_name"]}" DROP COLUMN "__aa_row_id"'
                )
                connection.execute(
                    'DELETE FROM "__aa_row_sequences" WHERE dataset_id=?',
                    [dataset["id"]],
                )

            deleted = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset['id']}/corrections",
                json={"expected_data_revision": 1, "deleted_row_ids": [3]},
            )
            assert deleted.status_code == 200, deleted.text
            added = client.post(
                f"/api/v1/tasks/{task_id}/datasets/{dataset['id']}/corrections",
                json={
                    "expected_data_revision": 2,
                    "added_rows": [{"客户": "丁", "收入": 400}],
                },
            )
            assert added.status_code == 200, added.text
            rows = client.get(
                f"/api/v1/tasks/{task_id}/datasets/{dataset['id']}/preview"
            ).json()["rows"]
            assert [row["__row_id"] for row in rows] == [1, 2, 4]
    finally:
        repository.db_path = old_path


def test_artifacts_and_persisted_report(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "reports.sqlite"
    try:
        with TestClient(app) as client:
            task_id = client.post("/api/v1/tasks").json()["id"]
            client.post(
                f"/api/v1/tasks/{task_id}/files",
                files={"files": ("sales.csv", "客户,收入\n甲,100\n".encode(), "text/csv")},
            )
            run_id = repository.start_execution(task_id, "汇总收入")
            result = AnalysisDraft(summary="收入已汇总")
            repository.finish_execution(run_id, "completed", result=result, activate=True)
            repository.update_task(
                task_id, status="completed", progress=100, result=result, clear_error=True
            )
            artifact = repository.add_artifact(
                task_id, run_id, "table", "收入汇总", {"row_count": 1}
            )
            listed = client.get(f"/api/v1/tasks/{task_id}/runs/{run_id}/artifacts")
            assert listed.json()[0]["id"] == artifact.id
            artifact_events = [
                event for event in repository.events_after(task_id, 0)
                if event.event_type == "artifact.created"
            ]
            assert artifact_events[-1].schema_version == 1
            assert artifact_events[-1].payload["artifact"]["id"] == artifact.id

            published = client.post(f"/api/v1/tasks/{task_id}/reports")
            assert published.status_code == 202, published.text
            job = published.json()
            for _ in range(100):
                job = client.get(f"/api/v1/report-jobs/{job['id']}").json()
                if job["status"] in {"ready", "failed"}:
                    break
                time.sleep(0.02)
            assert job["status"] == "ready", job
            report = client.get(f"/api/v1/reports/{job['report_id']}").json()
            assert report["latest_version"] == 1
            assert report["versions"][0]["content"]["provenance"]["run_id"] == run_id
            assert Path(report["versions"][0]["html_path"]).is_file()
            assert client.get("/api/v1/reports").json()[0]["id"] == report["id"]
            assert client.delete(f"/api/v1/tasks/{task_id}").status_code == 409
            assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200
            assert client.delete(f"/api/v1/reports/{report['id']}").status_code == 204
            assert client.get(f"/api/v1/reports/{report['id']}").status_code == 404
            assert not Path(report["versions"][0]["html_path"]).exists()
            assert client.delete(f"/api/v1/tasks/{task_id}").status_code == 204
            assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
    finally:
        repository.db_path = old_path


def test_interrupted_report_job_is_requeued_on_restart(tmp_path: Path) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "report-recovery.sqlite"
    try:
        repository.initialize()
        task_id = repository.create_task()
        run_id = repository.start_execution(task_id, "生成报告")
        result = AnalysisDraft(summary="已验证结果")
        repository.finish_execution(run_id, "completed", result=result, activate=True)
        repository.update_task(task_id, status="completed", progress=100, result=result)
        job = repository.queue_report(task_id, run_id)
        assert repository.claim_report_job(job.id)
        assert repository.get_report_job(job.id).status == "generating"

        repository.requeue_interrupted_report_jobs()

        recovered = repository.get_report_job(job.id)
        assert recovered.status == "queued"
        assert recovered.error == "进程重启后自动恢复"
    finally:
        repository.db_path = old_path
