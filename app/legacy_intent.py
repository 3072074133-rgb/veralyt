"""Compatibility classifier for ANALYSE_AGENT_MODEL_INTENT_ENABLED=false."""
import re
from typing import Any

from .config import settings
from .followups import clearly_off_topic, requested_metric
from .llm import LLMStructuredOutputError, llm
from .models import AnalysisState, IntentDecision, TaskStatus
from .node_runtime import begin_node
from .repository import repository


def classify_legacy(state: AnalysisState) -> dict[str, Any]:
    from .workflow import _has_clear_analysis_intent, _is_generic_analysis_request, _is_retry_analysis_request, _previous_analysis_question

    repository.update_task(state.task_id, status=TaskStatus.CLASSIFYING, progress=20, status_message="正在判断分析需求")
    tracker = begin_node(state, "classify")
    try:
        question = state.user_question
        if settings.followup_enabled and clearly_off_topic(question):
            return tracker.complete({'intent': IntentDecision(is_analysis=False, route='off_topic',
                reason='当前请求属于非数据功能', confidence=1).model_dump(mode='json')})
        metric = requested_metric(question) if settings.followup_enabled else None
        if settings.followup_enabled and not metric and re.search(r'为什么|解释|说明', question) and re.search(r'利润|现金|收入|费用|报表|结果', question):
            return tracker.complete({'intent': IntentDecision(is_analysis=True, route='explanation',
                reason='解释已有结果，先检查证据', confidence=1).model_dump(mode='json')})
        if metric and state.datasets:
            return tracker.complete({'intent': IntentDecision(is_analysis=True,
                route='clarification' if metric == 'ambiguous' else 'derived_metric', metric=metric,
                reason='财务指标追问，先检查有效证据', confidence=1).model_dump(mode='json')})
        if _is_retry_analysis_request(question) and state.datasets:
            resolved = _previous_analysis_question(state) or question
            decision = IntentDecision(
                is_analysis=True,
                reason="用户要求基于已上传数据重新执行分析。",
                confidence=1,
            )
            tracker.record_diagnostics({
                "classification_source": "deterministic_retry",
                "original_user_question": question,
                "resolved_user_question": resolved,
            })
            return tracker.complete({
                "intent": decision.model_dump(mode="json"),
                "user_question": resolved,
            })

        if _has_clear_analysis_intent(question, bool(state.datasets)):
            resolved = _previous_analysis_question(state) if _is_generic_analysis_request(question) else None
            decision = IntentDecision(
                is_analysis=True,
                reason="请求包含明确的数据分析动作和业务数据对象。",
                confidence=1,
            )
            tracker.record_diagnostics({
                "classification_source": "deterministic_analysis",
                "original_user_question": question,
                "resolved_user_question": resolved or question,
            })
            update: dict[str, Any] = {"intent": decision.model_dump(mode="json")}
            if resolved:
                update["user_question"] = resolved
            return tracker.complete(update)

        try:
            decision = llm.structured(
                "intent_classifier",
                {"user_question": question, "has_uploaded_data": bool(state.datasets), "conversation_summary": state.conversation_summary},
                IntentDecision,
                thinking=False,
                prompt_override=tracker.prompt.content,
                diagnostics=tracker.record_diagnostics,
            )
        except LLMStructuredOutputError as exc:
            if not _has_clear_analysis_intent(question, bool(state.datasets)):
                return tracker.complete({'intent': IntentDecision(is_analysis=True, route='clarification',
                    reason='本次问题未能可靠识别，请明确要查询的指标或问题', confidence=0,
                    suggested_response='请说明要查询的指标，或明确这是一般知识问题。').model_dump(mode='json')})
            decision = IntentDecision(
                is_analysis=True,
                reason="请求包含明确的数据分析动作和业务数据对象。",
                confidence=0.8,
            )
            tracker.record_diagnostics({
                "classification_source": "deterministic_fallback",
                "structured_output_error": str(exc),
            })
        return tracker.complete({"intent": decision.model_dump(mode="json")})
    except Exception as exc:
        tracker.fail(exc)
        raise

