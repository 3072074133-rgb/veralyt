"""Intent classification workflow nodes."""
from __future__ import annotations
from typing import Any
from ..llm import llm, _select_context, LLMContextOverflowError
from ..context_manager import context_manager
from ..model_settings import model_settings
from ..models import AnalysisState, IntentDecision, MessageRecord
from ..node_logging import start_node
from ..repository import repository


def classify_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status="classifying", progress=20, status_message="正在理解本轮问题")
    tracker = start_node(state, "classify")
    context = state.conversation_summary or {}
    model_context = {
        "user_question": state.user_question,
        "has_uploaded_data": bool(state.datasets),
        "conversation_context": context,
        "dataset_names": [item.get("display_name", "") for item in state.datasets],
        "previous_result": state.previous_result,
    }
    try:
        if state.conversation_messages:
            def fits(conversation):
                try:
                    _select_context('intent_classifier', tracker.prompt.content, {
                        **model_context, 'conversation_context': conversation.model_dump(mode='json'),
                    })
                    return True
                except LLMContextOverflowError:
                    return False

            runtime = model_settings.get()
            cloud_dynamic_context = None
            fit_check = fits
            if runtime.mode == "openai_compatible":
                fit_check = None
                cloud_dynamic_context = {
                    "system_prompt": tracker.prompt.content,
                    **{key: value for key, value in model_context.items() if key != "conversation_context"},
                }
            conversation, _ = context_manager.prepare(
                state.task_id,
                [MessageRecord.model_validate(item) for item in state.conversation_messages],
                current_question=state.user_question,
                dynamic_context=cloud_dynamic_context,
                fits_context=fit_check,
            )
            model_context['conversation_context'] = conversation.model_dump(mode='json')
        decision = llm.structured(
            "intent_classifier", model_context, IntentDecision, thinking=False,
            prompt_override=tracker.prompt.content,
            diagnostics=tracker.record_diagnostics,
        )
        return tracker.complete({"intent": decision.model_dump(mode="json"),
                                 "conversation_summary": model_context['conversation_context']})
    except Exception as exc:
        tracker.fail(exc)
        raise

def respond_node(state: AnalysisState) -> dict[str, Any]:
    decision = IntentDecision.model_validate(state.intent)
    message = (decision.reply or "").strip()
    if not state.is_replay:
        repository.add_message(state.task_id, "assistant", message)
    repository.update_task(state.task_id, status="off_topic", progress=100, status_message="本轮已回复")
    return {"final_status": "off_topic"}
