from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import AnalysisDraft
from app.repository import repository


@pytest.fixture()
def client(tmp_path: Path):
    old_path = repository.db_path
    repository.db_path = tmp_path / "test.sqlite"
    with TestClient(app) as test_client:
        yield test_client
    repository.db_path = old_path


def test_create_and_list_task(client: TestClient) -> None:
    created = client.post("/api/v1/tasks")
    assert created.status_code == 201
    task_id = created.json()["id"]
    snapshot = client.get(f"/api/v1/tasks/{task_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "ready"
    listing = client.get("/api/v1/tasks")
    assert listing.json()["total"] == 0


def test_message_requires_dataset(client: TestClient) -> None:
    task_id = client.post("/api/v1/tasks").json()["id"]
    response = client.post(f"/api/v1/tasks/{task_id}/messages", json={"content": "分析收入"})
    assert response.status_code == 400


def test_unsupported_upload_preserves_ready_state(client: TestClient) -> None:
    task_id = client.post("/api/v1/tasks").json()["id"]
    response = client.post(
        f"/api/v1/tasks/{task_id}/files",
        files={"files": ("notes.txt", b"not a spreadsheet", "text/plain")},
    )
    assert response.status_code == 415
    assert response.json()["outcome"] == "failed"
    assert response.json()["rejected_files"][0]["code"] == "unsupported_type"
    assert response.json()["task"]["status"] == "ready"


def test_successful_upload_invalidates_active_result(client: TestClient) -> None:
    task_id = client.post("/api/v1/tasks").json()["id"]
    first = client.post(
        f"/api/v1/tasks/{task_id}/files",
        files={"files": ("first.csv", "月份,收入\n1月,100\n".encode(), "text/csv")},
    )
    assert first.status_code == 200
    source_run = repository.start_execution(task_id, "分析收入")
    result = AnalysisDraft(summary="收入为100", summary_evidence_refs=[])
    repository.finish_execution(source_run, "completed", result=result, activate=True)
    repository.update_task(task_id, status="completed", progress=100, result=result)

    second = client.post(
        f"/api/v1/tasks/{task_id}/files",
        files={"files": ("second.csv", "月份,收入\n2月,200\n".encode(), "text/csv")},
    )
    payload = second.json()["task"]
    assert payload["data_revision"] == 2
    assert payload["result"] is None
    assert payload["active_run_id"] is None


def test_node_record_endpoints_are_removed(client: TestClient) -> None:
    task_id = client.post("/api/v1/tasks").json()["id"]
    assert client.get(f"/api/v1/tasks/{task_id}/runs/missing/nodes").status_code == 404
    assert client.get(f"/api/v1/tasks/{task_id}/nodes/missing").status_code == 404
    assert client.post(f"/api/v1/tasks/{task_id}/nodes/missing/replay", json={"prompt_content": "x" * 20}).status_code in {404, 405}
    with repository.connect() as connection:
        names = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    assert "node_executions" not in names
    assert "prompt_versions" not in names
