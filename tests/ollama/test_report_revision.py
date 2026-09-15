from pathlib import Path
import uuid

import pytest

from app.config import settings
from app.ingestion import ingest_file
from app.models import AnalysisDraft, AnalysisState, UploadedFile
from app.repository import repository
from app.workflow import build_graph, referenced_evidence_ids
from tests.ollama.test_finance_regression import _require_model


FIXTURE = Path(__file__).parents[1] / "fixtures" / "monthly_financial_report.xlsx"


def _invoke(task_id: str, question: str, previous: AnalysisDraft | None = None) -> dict:
    snapshot = repository.get_task(task_id)
    run_id = repository.start_execution(task_id, question)
    result = build_graph().invoke(
        AnalysisState(
            task_id=task_id,
            run_id=run_id,
            user_question=question,
            previous_result=previous.model_dump(mode="json") if previous else None,
            inherited_evidence_ids=sorted(referenced_evidence_ids(
                previous.model_dump(mode="json") if previous else {}
            )),
            data_revision=snapshot.data_revision,
            datasets=[item.model_dump(mode="json") for item in snapshot.datasets],
        ),
        config={"configurable": {"thread_id": run_id}, "recursion_limit": 100},
    )
    status = result["final_status"]
    draft = AnalysisDraft.model_validate(result["draft"])
    repository.finish_execution(run_id, status, result.get("error"), draft,
                                activate=status in {"completed", "completed_with_warnings"})
    return result


@pytest.mark.ollama
def test_real_model_generates_then_revises_report(tmp_path, monkeypatch, record_property) -> None:
    digest = _require_model()
    record_property("model", settings.ollama_model)
    record_property("model_digest", digest)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(repository, "db_path", tmp_path / "app.sqlite")
    repository.initialize()
    task_id = repository.create_task()
    uploaded = UploadedFile(
        id=str(uuid.uuid4()), original_name=FIXTURE.name,
        size=FIXTURE.stat().st_size, status="ready",
    )
    repository.add_file(task_id, uploaded, str(FIXTURE))
    ingest_file(task_id, uploaded, FIXTURE)
    repository.publish_data_revision(task_id)

    first = _invoke(task_id, "分析报表数据并生成完整报告，列出关键财务指标。")
    assert first["final_status"] in {"completed", "completed_with_warnings"}, first.get("error")
    assert first["reflection"]["verdict"] == "pass"
    first_draft = AnalysisDraft.model_validate(first["draft"])
    first_refs = referenced_evidence_ids(first["draft"])
    assert first_refs

    second = _invoke(
        task_id,
        "保留关键指标，重点分析应收账款逾期风险，增加具体改进建议，缩短其他部分，重新生成完整报告并替换当前报告。",
        first_draft,
    )
    assert second["final_status"] in {"completed", "completed_with_warnings"}, second.get("error")
    assert second["reflection"]["verdict"] == "pass"
    revised = AnalysisDraft.model_validate(second["draft"])
    combined = " ".join([
        revised.summary,
        *(item.detail for item in revised.findings),
        *(item.conclusion + " " + (item.action or "") for item in revised.insights),
    ])
    assert "应收" in combined and ("逾期" in combined or "回款" in combined)
    assert any(item.action for item in revised.insights) or revised.suggested_questions
