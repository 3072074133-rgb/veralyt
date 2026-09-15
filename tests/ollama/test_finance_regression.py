from __future__ import annotations

import pytest

from app.config import settings
from app.llm import LLMUnavailableError, llm
from app.analysis_tools import _validate_sql
from app.models import IntentDecision, PlanDecision


DATASET = {
    "dataset_id": "golden-monthly-revenue",
    "table_name": "monthly_revenue",
    "display_name": "月度经营数据",
    "row_count": 3,
    "fields": [
        {"name": "月份", "type": "VARCHAR"},
        {"name": "收入", "type": "BIGINT"},
    ],
    "sample_rows": [{"月份": "2026-01", "收入": 100}, {"月份": "2026-02", "收入": 120}],
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
                "confirmed_policies": {},
                "previous_result": {},
                "evidence_catalog": [],
                "query_failures": [],
                "result_decision": None,
                "remaining_tool_calls": 8,
                "current_plan": None,
                "completed_step_ids": [],
                "revision_feedback": None,
            },
            PlanDecision,
            thinking=True,
        )
    except LLMUnavailableError as exc:
        pytest.fail(str(exc))

    assert intent.route == "analysis"
    assert plan.action == "analyze" and plan.steps
    step = plan.steps[0]
    assert step.dataset_ids == ["golden-monthly-revenue"]
    _validate_sql(step.sql, {"monthly_revenue"})
    lowered = step.sql.lower()
    assert "sum" in lowered and "order by" in lowered
    assert "月份" in step.sql and "收入" in step.sql
