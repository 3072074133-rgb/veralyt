from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.knowledge_service import _fit_context_budget, embedding_gateway, retrieve_for_run
from app.main import app
from app.models import AnalysisDraft
from app.repository import repository


def _fake_embeddings(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = text.casefold()
        vectors.append(
            [
                1.0 if "ebiz" in lowered or "电商" in text else 0.0,
                1.0 if "profit" in lowered or "利润" in text else 0.0,
                0.25,
            ]
        )
    return vectors


def test_vector_knowledge_versions_bindings_and_run_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    old_path = repository.db_path
    repository.db_path = tmp_path / "knowledge.sqlite"
    monkeypatch.setattr(embedding_gateway, "embed", _fake_embeddings)
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/knowledge-bases",
                json={
                    "name": "集团财务口径",
                    "description": "中英文业务定义",
                    "documents": [
                        {
                            "title": "业务线说明",
                            "content": "Ebiz refers to the E-Commerce Business Unit. Net Profit means 净利润。",
                        }
                    ],
                },
            )
            assert created.status_code == 201, created.text
            base = created.json()
            assert base["latest_revision"] == 1
            assert base["document_count"] == 1
            assert base["chunk_count"] == 1

            revised = client.post(
                f"/api/v1/knowledge-bases/{base['id']}/revisions",
                json={
                    "change_summary": "补充适用范围",
                    "documents": [
                        {
                            "title": "业务线说明",
                            "content": "Ebiz is the E-Commerce Business Unit. Net Profit means 净利润，适用于月报。",
                        }
                    ],
                },
            )
            assert revised.status_code == 200, revised.text
            latest = revised.json()["revisions"][0]
            assert latest["revision_number"] == 2
            assert revised.json()["revisions"][1]["documents"][0]["content"].startswith("Ebiz refers")

            task_id = client.post("/api/v1/tasks").json()["id"]
            repository.update_task(
                task_id,
                status="completed",
                progress=100,
                status_message="分析完成",
                result=AnalysisDraft(summary="旧结果"),
            )
            bound = client.post(
                f"/api/v1/tasks/{task_id}/knowledge-bases",
                json={
                    "bindings": [
                        {"knowledge_base_id": base["id"], "revision_id": latest["id"]}
                    ]
                },
            )
            assert bound.status_code == 200, bound.text
            assert bound.json()[0]["revision_number"] == 2
            refreshed_task = client.get(f"/api/v1/tasks/{task_id}").json()
            assert refreshed_task["knowledge_bases"][0]["knowledge_base_name"] == "集团财务口径"
            assert refreshed_task["result"] is None
            assert refreshed_task["status_message"] == "知识库已更新，可以重新分析"

            run_id = repository.start_execution(task_id, "分析 Ebiz 利润")
            snapshot = repository.get_run_input_snapshot(run_id)
            assert snapshot["knowledge_revision_ids"] == [latest["id"]]
            matches = retrieve_for_run(run_id, "分析电商业务利润")
            assert matches
            assert matches[0].revision_id == latest["id"]
            listed = client.get(f"/api/v1/tasks/{task_id}/runs/{run_id}/knowledge")
            assert listed.status_code == 200
            assert listed.json()[0]["document_title"] == "业务线说明"
            assert sum(len(item["content"]) for item in listed.json()) <= settings.knowledge_context_max_chars

            completed = AnalysisDraft(summary="使用 v2 知识生成的结果")
            with repository.connect() as connection:
                connection.execute(
                    """UPDATE execution_runs SET status='completed',result_json=?,is_active=1,
                    finished_at=started_at WHERE id=?""",
                    (completed.model_dump_json(), run_id),
                )
                connection.execute(
                    """UPDATE tasks SET status='completed',progress=100,status_message='分析完成',
                    result_json=?,active_run_id=? WHERE id=?""",
                    (completed.model_dump_json(), run_id, task_id),
                )
            unbound = client.post(
                f"/api/v1/tasks/{task_id}/knowledge-bases", json={"bindings": []}
            )
            assert unbound.status_code == 200
            assert client.get(f"/api/v1/tasks/{task_id}").json()["result"] is None
            with pytest.raises(ValueError, match="旧知识库版本"):
                repository.start_execution(
                    task_id,
                    "基于旧节点重跑",
                    status="queued",
                    data_revision=0,
                    input_snapshot=snapshot,
                )
            with pytest.raises(ValueError, match="旧知识库版本"):
                repository.finish_execution(
                    run_id,
                    "completed",
                    result=AnalysisDraft(summary="不应写回的新结果"),
                    activate=True,
                )
            activate = client.post(f"/api/v1/tasks/{task_id}/runs/{run_id}/activate")
            assert activate.status_code == 409
            assert "旧知识库版本" in activate.json()["detail"]
    finally:
        repository.db_path = old_path


def test_context_budget_keeps_complete_chunks_within_limit() -> None:
    candidates = [
        (0.9, {"content": "a" * 100}),
        (0.8, {"content": "b" * 100}),
        (0.7, {"content": "c" * 100}),
    ]
    selected = _fit_context_budget(candidates, 220)
    assert len(selected) == 2
    assert sum(len(row["content"]) for _, row in selected) == 200
