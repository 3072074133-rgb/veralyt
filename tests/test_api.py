from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import AnalysisDraft, AnalysisState
from app.repository import repository
from app.worker import worker


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


def test_completed_node_can_create_replay_branch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = client.post("/api/v1/tasks").json()["id"]
    source_run = repository.start_execution(task_id, "分析收入")
    result = AnalysisDraft(summary="原分析结果")
    repository.finish_execution(source_run, "completed", result=result, activate=True)
    repository.update_task(task_id, status="completed", progress=100, result=result)
    prompt = repository.ensure_prompt_version("draft", "1.0.0", "这是原始草稿节点提示词，要求返回严格的结构化分析结果。")
    state = AnalysisState(task_id=task_id, run_id=source_run, user_question="分析收入")
    execution_id = repository.start_node_execution(
        source_run,
        "draft",
        state.model_dump(mode="json"),
        prompt.id,
        {"model": "qwen3.5:4b"},
        state.schema_version,
    )
    repository.finish_node_execution(execution_id, {"draft": result.model_dump(mode="json")})

    async def enqueue_noop(_run_id: str) -> None:
        return None

    monkeypatch.setattr(worker, "enqueue_replay", enqueue_noop)
    response = client.post(
        f"/api/v1/tasks/{task_id}/nodes/{execution_id}/replay",
        json={"prompt_content": "这是修改后的草稿节点提示词，要求结论简洁并严格引用有效证据。"},
    )

    assert response.status_code == 202
    replay = repository.get_run(task_id, response.json()["run_id"])
    assert replay.status == "queued"
    assert replay.parent_run_id == source_run
    assert replay.forked_from_node_execution_id == execution_id
    assert client.get(f"/api/v1/tasks/{task_id}/runs/{source_run}/nodes").json()[0]["node_name"] == "draft"
