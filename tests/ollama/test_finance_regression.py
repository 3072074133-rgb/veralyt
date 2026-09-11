from __future__ import annotations

import pytest

from app.config import settings
from app.llm import LLMUnavailableError, llm
from app.models import AnalysisPlan, DatasetQuerySpec, IntentDecision, QueryRequest


DATASET = {
    "id": "golden-monthly-revenue",
    "display_name": "月度经营数据",
    "row_count": 3,
    "columns": [
        {"name": "月份", "display_name": "月份", "data_type": "String", "semantic_type": "date", "role": "dimension", "sample_values": ["2026-01", "2026-02"]},
        {"name": "收入", "display_name": "收入", "data_type": "Int64", "semantic_type": "amount", "role": "measure", "default_aggregation": "sum", "sample_values": [100, 120]},
    ],
}


def _require_model() -> str:
    try:
        response = llm.client.list()
    except Exception as exc:
        pytest.skip(f"Ollama unavailable: {exc}")
    models = list(getattr(response, "models", []) or [])
    selected = next(
        (item for item in models if getattr(item, "model", None) == settings.ollama_model),
        None,
    )
    if selected is None:
        pytest.skip(f"model {settings.ollama_model} is not installed")
    return str(getattr(selected, "digest", "unknown"))


@pytest.mark.ollama
def test_local_model_preserves_finance_intent_plan_and_query(record_property) -> None:
    digest = _require_model()
    record_property("model", settings.ollama_model)
    record_property("model_digest", digest)

    try:
        intent = llm.structured(
            "intent_classifier",
            {
                "user_question": "按月份汇总收入，并按时间升序展示",
                "has_uploaded_data": True,
                "conversation_summary": None,
            },
            IntentDecision,
            thinking=False,
        )
        plan = llm.structured(
            "analysis_planner",
            {
                "user_question": "按月份汇总收入，并按时间升序展示",
                "conversation_context": {},
                "dataset_catalog": [DATASET],
                "knowledge_context": [],
                "confirmed_policies": {},
                "available_tools": ["auto_analyze", "profile_table", "query_data"],
                "current_plan": None,
                "completed_step_ids": [],
                "revision_feedback": None,
            },
            AnalysisPlan,
            thinking=True,
        )
        query = llm.structured(
            "tool_orchestrator",
            {
                "user_question": "按月份汇总收入，并按时间升序展示",
                "analysis_plan": plan.model_dump(mode="json"),
                "current_step": plan.steps[0].model_dump(mode="json"),
                "selected_dataset": DATASET,
                "available_results": [],
                "knowledge_context": [],
                "latest_validation_failure": None,
            },
            QueryRequest,
            thinking=True,
        )
    except LLMUnavailableError as exc:
        pytest.fail(str(exc))

    assert intent.is_analysis is True
    assert plan.can_execute is True and plan.steps
    assert query.query == DatasetQuerySpec(
        dimensions=["月份"],
        measures=query.query.measures,
        filters=query.query.filters,
        order_by=query.query.order_by,
        descending=query.query.descending,
        limit=query.query.limit,
    )
    assert query.query.dimensions == ["月份"]
    assert any(item.field == "收入" and item.aggregation == "sum" for item in query.query.measures)
    assert query.query.descending is False
