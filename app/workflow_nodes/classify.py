"""Intent classification workflow nodes."""
from __future__ import annotations
import re
from typing import Any
from ..config import settings
from ..legacy_intent import classify_legacy
from ..llm import LLMContextOverflowError, LLMStructuredOutputError, llm
from ..models import AnalysisState, IntentDecision
from ..node_logging import start_node
from ..repository import repository
from ..followups import FORMULAS
from .rules import is_contextual_followup


def _strict_report_request(question: str, has_data: bool) -> bool:
    """Only explicit operations on uploaded data enter the report workflow."""
    if not has_data:
        return False
    return bool(
        re.search(r"分析|查询|统计|汇总|筛选|排序|比较|对比|同比|环比|趋势|异常|对账|预测|计算", question)
        and re.search(r"数据|报表|表格|部门|区域|客户|产品|收入|利润|盈亏|销售|成本|费用|金额|预算|回款|账龄|字段|记录|现金|应收|应付", question)
    )

def classify_node(state: AnalysisState) -> dict[str, Any]:
    if not settings.model_intent_enabled:
        return classify_legacy(state)
    repository.update_task(state.task_id, status="classifying", progress=20, status_message="正在理解本轮问题")
    tracker = start_node(state, "classify")
    context = state.conversation_summary or {}
    recent = context.get("recent_messages") or []
    followup = is_contextual_followup(state.user_question, bool(state.previous_result))
    previous_result = state.previous_result or {}
    compact = {
        "user_question": state.user_question,
        "has_uploaded_data": bool(state.datasets),
        "available_metrics": list(FORMULAS),
        "recent_messages": [
            {"role": item.get("role"), "content": str(item.get("content", ""))[:400]}
            for item in recent[-4:] if isinstance(item, dict)
        ],
        "dataset_names": [str(item.get("display_name", ""))[:80] for item in state.datasets[:8]],
        "report_topic": str((context.get("memory") or {}).get("task_goal", ""))[:200],
        "previous_result": {
            "title": str(previous_result.get("title", ""))[:120],
            "summary": str(previous_result.get("summary", ""))[:500],
            "metrics": [
                {"label": str(item.get("label", "")), "value": str(item.get("value", ""))}
                for item in (previous_result.get("metrics") or [])[:8] if isinstance(item, dict)
            ],
            "insights": [
                {"title": str(item.get("title", "")), "conclusion": str(item.get("conclusion", ""))[:240]}
                for item in (previous_result.get("insights") or [])[:8] if isinstance(item, dict)
            ],
        },
    }
    minimal = {**compact, "recent_messages": compact["recent_messages"][-2:], "dataset_names": []}
    try:
        try:
            decision = llm.structured(
                "intent_classifier", compact, IntentDecision, thinking=False,
                prompt_override=tracker.prompt.content, fallback_contexts=[minimal],
                diagnostics=tracker.record_diagnostics,
            )
        except (LLMStructuredOutputError, LLMContextOverflowError) as exc:
            fallback_route = "explanation" if followup else "clarification"
            tracker.record_diagnostics({"classification_error": str(exc), "fallback": fallback_route})
            decision = IntentDecision(
                route=fallback_route,
                reason="模型输出异常，尝试直接回答当前问题。",
                reply=("暂时无法生成本轮回答，请换一种方式描述问题。" if followup
                       else "暂时无法可靠理解本轮问题，请补充具体需求或分析目标。"),
            )
        if decision.route == "derived_metric" and decision.metric not in FORMULAS:
            decision = IntentDecision(route="clarification", reply="请明确指标口径：毛利率、营业利润率、净利润率或资产负债率。")
        if followup and decision.route == "clarification":
            decision = decision.model_copy(update={
                "route": "explanation",
                "reason": "基于上一轮结果继续回答当前追问。",
            })
        if decision.route == "analysis" and not _strict_report_request(state.user_question, bool(state.datasets)):
            decision = decision.model_copy(update={
                "route": "explanation" if (state.previous_result or followup) else "conversation",
                "reason": "当前问题不是明确的报表分析请求，直接回答用户。",
            })
        return tracker.complete({"intent": decision.model_dump(mode="json")})
    except Exception as exc:
        tracker.fail(exc)
        raise

def respond_node(state: AnalysisState) -> dict[str, Any]:
    decision = IntentDecision.model_validate(state.intent)
    message = (decision.reply or "").strip()
    if not message:
        # The classifier is asked to answer directly, but a small local model
        # may return only the route.  Fill the missing answer with one normal
        # text completion instead of exposing an internal fallback question.
        context = {
            "user_question": state.user_question,
            "recent_messages": (state.conversation_summary or {}).get("recent_messages", [])[-6:],
            "previous_result": state.previous_result or {},
        }
        try:
            message = llm.text("direct_answer", context, thinking=False)
        except Exception:
            message = "我暂时无法完整回答这个问题，请稍后再试。"
    if not message:
        message = "我暂时无法完整回答这个问题，请稍后再试。"
    if not state.is_replay:
        repository.add_message(state.task_id, "assistant", message)
    repository.update_task(state.task_id, status="off_topic", progress=100, status_message="本轮已回复")
    return {"final_status": "off_topic"}


# Compatibility alias for callers from older integrations.
off_topic_node = respond_node
