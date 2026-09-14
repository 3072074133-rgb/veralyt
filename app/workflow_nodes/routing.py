"""Pure routing decisions for the analysis workflow."""
from __future__ import annotations
from ..models import AnalysisState, IntentDecision, AnalysisPlan, ReflectionDecision
from ..config import settings

def route_intent(state: AnalysisState) -> str:
    # Only explicit report analysis/calculation enters the strict workflow.
    # Explanations, recommendations, clarifications and ordinary chat end
    # after the intent node with the model's user-facing reply.
    route = IntentDecision.model_validate(state.intent).route
    return "plan" if route == "analysis" else "off_topic"

def route_plan(state: AnalysisState) -> str:
    plan = AnalysisPlan.model_validate(state.plan)
    return "execute" if plan.can_execute else "clarify"

def route_execute(state: AnalysisState) -> str:
    plan = AnalysisPlan.model_validate(state.plan)
    if any(step.id not in state.completed_step_ids for step in plan.steps):
        return "execute"
    return "draft"

def route_reflection(state: AnalysisState) -> str:
    decision = ReflectionDecision.model_validate(state.reflection)
    if state.revision_round > settings.max_revision_rounds:
        return "finish"
    return decision.route


def route_validation(state: AnalysisState) -> str:
    """Send revised drafts back to the model reviewer until accepted or capped."""
    validation = state.validation or {}
    if validation.get("passed", False) and (not state.reflection or state.reflection.get("verdict") == "pass"):
        return "finish"
    if validation.get("passed", False):
        return "reflect"
    if state.revision_round < settings.max_revision_rounds:
        return "reflect"
    return "finish"

