"""Pure routing decisions for the analysis workflow."""
from __future__ import annotations
from ..models import AnalysisState, IntentDecision, AnalysisPlan, ReflectionDecision
from ..config import settings

def route_intent(state: AnalysisState) -> str:
    # Only explicit report analysis/calculation enters the strict workflow.
    # Explanations, recommendations, clarifications and ordinary chat end
    # after the intent node with the model's user-facing reply.
    route = IntentDecision.model_validate(state.intent).route if state.intent else "conversation"
    return "plan" if route in {"analysis", "derived_metric"} else "off_topic"

def route_plan(state: AnalysisState) -> str:
    plan = AnalysisPlan.model_validate(state.plan)
    return "execute" if plan.can_execute else "clarify"

def route_execute(state: AnalysisState) -> str:
    plan = AnalysisPlan.model_validate(state.plan)
    if any(step.id not in state.completed_step_ids for step in plan.steps):
        return "execute"
    if state.reflection and state.reflection.get("route") == "execute":
        revision_step_id = f"revision_{state.revision_round}"
        if revision_step_id not in state.completed_step_ids:
            return "execute"
    return "draft"

def route_reflection(state: AnalysisState) -> str:
    decision = ReflectionDecision.model_validate(state.reflection)
    if decision.route == "finish":
        return "finish"
    if decision.verdict == "pass" or state.revision_round >= settings.max_revision_rounds:
        return "finish"
    return decision.route if decision.route in {"replan", "execute", "rewrite"} else "rewrite"


def route_validation(state: AnalysisState) -> str:
    """Use one repair pass only for model-generated drafts with real errors."""
    validation = state.validation or {}
    if validation.get("passed", False):
        return "finish"
    if state.draft_execution_mode == "model" and state.revision_round == 0:
        return "reflect"
    return "finish"

