from __future__ import annotations

import atexit
import sqlite3
import logging
import time
from functools import wraps
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .analysis_tools import ToolError, execute_sql
from .config import settings
from .context_manager import context_manager
from .dataset_retrieval import planner_catalog
from .evidence_validation import (
    current_evidence_ids as _current_evidence_ids,
    validate_claim as _validate_claim,
)
from .llm import LLMContextOverflowError, LLMError, LLMUnavailableError, llm
from .report_generation import compact_evidence, generate_report, resolve_citations
from .report_document import report_charts
from .review_generation import generate_review
from .models import ReflectionDecision as ReflectionDecision
from .models import (
    AnalysisDraft,
    AnalysisPlan, PlanDecision,
    AnalysisState,
    DatasetInfo,
    ChapterAnalysisDraft,
    CalculationDetail,
    DeliveryCheck,
    DeliveryGate,
    ResultDecision,
    ConvergenceDecision,
    Severity,
    TaskStatus,
    ValidationIssue,
    ValidationReport,
    MessageRecord,
)
from .node_logging import start_node
from .observability import duration_ms, log_event
from .repository import repository
from .workflow_nodes.classify import classify_node as classify_node_impl, respond_node as respond_node_impl
from .workflow_nodes.routing import (
    route_execute as route_execute_impl,
    route_intent as route_intent_impl,
    route_plan as route_plan_impl,
    route_reflection as route_reflection_impl,
    route_validation as route_validation_impl,
)


logger = logging.getLogger(__name__)


class AnalysisCancelled(Exception):
    """Raised at workflow node boundaries after a user cancellation."""
__all__ = ["_validate_claim"]


def classify_node(state: AnalysisState) -> dict[str, Any]:
    return classify_node_impl(state)




def route_intent(state: AnalysisState) -> str:
    return route_intent_impl(state)


def respond_node(state: AnalysisState) -> dict[str, Any]:
    return respond_node_impl(state)


def _format_clarification(question: str | None, options: list[str] | None = None) -> str:
    """Render structured planner options as a user-facing business question."""
    text = (question or "").strip()
    if not text:
        raise ValueError("模型未提供澄清问题")
    rendered_options = [item.strip() for item in (options or [])]
    if not rendered_options:
        return text
    lines = "\n".join(f"{index}. {item}" for index, item in enumerate(rendered_options, start=1))
    return f"{text}\n请从下面选择，或在输入框补充说明：\n{lines}"


def _last_clarification(state: AnalysisState) -> str | None:
    for item in reversed(state.conversation_messages):
        if item.get("role") == "assistant":
            content = str(item.get("content", "")).strip()
            if content:
                return content
    return None


def plan_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status=TaskStatus.PLANNING, progress=35, status_message="正在理解指标和分析口径")
    tracker = start_node(state, "plan")
    try:
        analysis_question = state.analysis_question or state.user_question
        planning_question = analysis_question
        if state.clarification_answer:
            planning_question = (
                f"{analysis_question}\n"
                f"用户对上一轮确认问题的回答：{state.clarification_answer}"
            )
        datasets = [DatasetInfo.model_validate(item) for item in state.datasets]
        catalog_datasets = datasets
        compact_catalog = planner_catalog(catalog_datasets, state.task_id)
        tracker.record_diagnostics({
            "dataset_count": len(datasets),
            "candidate_count": len(catalog_datasets),
            "dataset_candidates": [
                {"dataset_id": item.id, "display_name": item.display_name}
                for item in catalog_datasets
            ],
        })
        plan_context = {
                "user_question": planning_question,
                "clarification_answer": state.clarification_answer,
                "result_decision": state.result_decision,
                "remaining_tool_calls": settings.max_tool_calls - state.tool_call_count,
                "dataset_catalog": compact_catalog,
                "conversation_context": state.conversation_summary or {},
                "intent_decision": state.intent or {},
                "previous_result": state.previous_result or {},
                "evidence_catalog": _evidence_catalog(state),
                "data_revision": state.data_revision,
                "confirmed_policies": {},
                "confirmed_relationships": state.confirmed_relationships,
                "current_plan": state.plan,
                "completed_step_ids": state.completed_step_ids,
                "query_failures": state.query_failures,
                "revision_feedback": _revision_feedback(state),
                "last_clarification": _last_clarification(state),
        }
        plan = llm.structured(
            "analysis_planner",
            plan_context,
            PlanDecision,
            thinking=False,
            prompt_override=tracker.prompt.content,
            diagnostics=tracker.record_diagnostics,
        )
        if isinstance(plan, PlanDecision):
            plan = AnalysisPlan(
                goal=plan.goal,
                can_execute=plan.action == "analyze",
                steps=plan.steps,
                clarification_question=plan.clarification,
                clarification_options=plan.clarification_options,
            )
        update: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "dataset_candidates": [
                {"dataset_id": item.id, "display_name": item.display_name}
                for item in catalog_datasets
            ],
        }
        if (state.reflection and state.reflection.get("route") == "replan") or state.result_decision:
            update["completed_step_ids"] = []
        return tracker.complete(update)
    except Exception as exc:
        tracker.fail(exc)
        raise


def route_plan(state: AnalysisState) -> str:
    return route_plan_impl(state)


def clarify_node(state: AnalysisState) -> dict[str, Any]:
    plan = AnalysisPlan.model_validate(state.plan)
    question = _format_clarification(plan.clarification_question, plan.clarification_options)
    if not state.is_replay:
        repository.add_message(state.task_id, "assistant", question)
    repository.update_task(
        state.task_id, status=TaskStatus.NEEDS_CLARIFICATION, progress=40,
        status_message="需要确认分析口径", clarification_question=question,
    )
    return {"final_status": "needs_clarification"}


def execute_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(
        state.task_id, status=TaskStatus.EXECUTING, progress=55,
        status_message="正在执行计算并提取证据", clear_clarification=True,
    )
    tracker = start_node(state, "execute")
    try:
        datasets = [DatasetInfo.model_validate(item) for item in state.datasets]
        plan = AnalysisPlan.model_validate(state.plan)
        pending_step = next(
            (step for step in plan.steps if step.id not in state.completed_step_ids),
            None,
        )
        if pending_step is None:
            return tracker.complete({})
        if state.tool_call_count >= settings.max_tool_calls:
            raise ToolError(
                f"工具调用已达到上限 {settings.max_tool_calls}，仍有计划步骤未完成：{pending_step.purpose}"
            )
        selected_ids = list(dict.fromkeys(pending_step.dataset_ids))
        unknown_ids = [item for item in selected_ids if item not in {dataset.id for dataset in datasets}]
        if unknown_ids:
            raise ToolError(f"计划引用了不存在的数据表：{', '.join(unknown_ids)}")
        dataset_by_id = {item.id: item for item in datasets}
        selected_datasets = [dataset_by_id[item] for item in selected_ids]
        arguments: dict[str, Any] = {
            "dataset_ids": selected_ids,
            "sql": pending_step.sql,
        }
        tracker.record_diagnostics({
            "selected_dataset_ids": selected_ids,
            "selected_table_names": [item.table_name for item in selected_datasets],
            "planned_tool": "execute_sql",
        })
        try:
            result = execute_sql(
                state.task_id,
                selected_datasets,
                pending_step.sql,
                pending_step.purpose,
                state.run_id,
            )
        except Exception as exc:
            repository.add_artifact(
                state.task_id, state.run_id, "error", f"{pending_step.purpose}失败",
                {"tool_name": "execute_sql", "error": str(exc), "arguments": arguments},
                status="failed",
            )
            if (isinstance(exc, ToolError) and len(state.query_failures) < 2
                    and state.tool_call_count + 1 < settings.max_tool_calls):
                return tracker.complete({
                    "query_failures": [*state.query_failures, {
                        "step_id": pending_step.id,
                        "purpose": pending_step.purpose,
                        "arguments": arguments,
                        "error": str(exc),
                    }],
                    "tool_call_count": state.tool_call_count + 1,
                })
            if isinstance(exc, ToolError):
                return tracker.complete({
                    'tool_results': [*state.tool_results, {
                        'result_id': f'failed_{pending_step.id}', 'tool_name': 'execute_sql',
                        'arguments': arguments, 'status': 'error', 'rows': [], 'evidence_ids': [],
                        'summary': f'未完成：{pending_step.purpose}', 'error': str(exc),
                        'warnings': [f'以下分析未完成，不得据此宣称核对通过：{pending_step.purpose}'],
                    }],
                    'completed_step_ids': list(dict.fromkeys([*state.completed_step_ids, pending_step.id])),
                    'tool_call_count': state.tool_call_count + 1, 'query_failures': [],
                })
            raise ToolError(f"计划步骤“{pending_step.purpose}”执行失败：{exc}") from exc
        repository.add_artifact(
            state.task_id, state.run_id, "query", pending_step.purpose,
            {
                "tool_name": result.tool_name,
                "summary": result.summary,
                "row_count": len(result.rows),
                "arguments": result.arguments,
                "columns": list(result.rows[0]) if result.rows else [],
                "warnings": result.warnings,
                "execution_metadata": result.execution_metadata,
            },
            result.evidence_ids,
        )
        completed = list(dict.fromkeys([*state.completed_step_ids, pending_step.id]))
        return tracker.complete({
            "tool_results": [*state.tool_results, result.model_dump(mode="json")],
            "tool_call_count": state.tool_call_count + 1,
            "completed_step_ids": completed,
            "query_failures": [],
        })
    except Exception as exc:
        tracker.fail(exc)
        raise


def _with_evidence_scope(function):
    @wraps(function)
    def scoped(state: AnalysisState):
        with repository.evidence_scope(state.task_id):
            repository.list_evidence_by_ids(state.task_id, _current_evidence_ids(state))
            return function(state)
    return scoped


@_with_evidence_scope
def draft_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status=TaskStatus.EXECUTING, progress=70, status_message="正在整理分析结论")
    tracker = start_node(state, "draft")
    try:
        draft_context = _draft_context(state)
        generated_draft = generate_report(llm,
            "draft_writer",
            draft_context,
            ChapterAnalysisDraft,
            thinking=False,
            prompt_override=tracker.prompt.content,
            ignored_response_fields={"calculation_details"},
            diagnostics=tracker.record_diagnostics,
        )
        draft = AnalysisDraft.model_validate(resolve_citations(generated_draft.model_dump(mode="json"), draft_context['evidence_catalog']))
        draft = _finalize_draft(state, draft)
        _publish_chart_artifacts(state, draft)
        return tracker.complete({"draft": draft.model_dump(mode="json")})
    except Exception as exc:
        tracker.fail(exc)
        raise


@_with_evidence_scope
def validate_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status=TaskStatus.VALIDATING, progress=80, status_message="正在核对证据引用")
    draft = AnalysisDraft.model_validate(state.draft)
    available = _current_evidence_ids(state)
    referenced = set(draft.summary_evidence_refs)
    issues: list[ValidationIssue] = []
    issues.extend(_validate_claim(
        state,
        "summary",
        draft.summary_evidence_refs,
        draft.summary_evidence_pointers,
    ))
    for index, metric in enumerate(draft.metrics):
        referenced.update(metric.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"metrics[{index}]",
            metric.evidence_refs,
            metric.evidence_pointers,
        ))
    for index, finding in enumerate(draft.findings):
        referenced.update(finding.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"findings[{index}]",
            finding.evidence_refs,
            finding.evidence_pointers,
        ))
    for index, insight in enumerate(draft.insights):
        referenced.update(insight.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"insights[{index}]",
            insight.evidence_refs,
            insight.evidence_pointers,
        ))
    for section_index, section in enumerate(draft.sections):
        for block_index, block in enumerate(section.blocks):
            target = f"sections[{section_index}].blocks[{block_index}]"
            referenced.update(block.evidence_refs)
            issues.extend(_validate_claim(state, target, block.evidence_refs, block.evidence_pointers))
            for item_index, item in enumerate(block.items):
                referenced.update(item.evidence_refs)
                issues.extend(_validate_claim(state, f"{target}.items[{item_index}]", item.evidence_refs, item.evidence_pointers))
            for metric_index, metric in enumerate(block.metrics):
                referenced.update(metric.evidence_refs)
                issues.extend(_validate_claim(state, f"{target}.metrics[{metric_index}]", metric.evidence_refs, metric.evidence_pointers))
    missing = referenced - available
    if missing:
        issues.append(ValidationIssue(code="unknown_evidence", message=f"引用了不存在的证据：{', '.join(sorted(missing))}", severity=Severity.ERROR))
    for chart in report_charts(draft):
        evidence = repository.get_evidence(state.task_id, chart.dataset_ref)
        if chart.dataset_ref not in available or evidence is None:
            issues.append(ValidationIssue(code="unknown_chart_data", message=f"图表 {chart.title} 的数据证据不存在", severity=Severity.ERROR, target=chart.id))
            continue
        columns = set(evidence.columns) | {field for row in evidence.rows for field in row}
        referenced_fields = {
            chart.category_field,
            *(series.field for series in chart.series),
            *([chart.x_field] if chart.x_field else []),
            *([chart.y_field] if chart.y_field else []),
            *([chart.label_field] if chart.label_field else []),
        }
        unknown_fields = sorted(referenced_fields - columns)
        if unknown_fields:
            issues.append(ValidationIssue(
                code="unknown_chart_field",
                message=f"图表 {chart.title} 引用了不存在的字段：{', '.join(unknown_fields)}",
                severity=Severity.ERROR,
                target=chart.id,
            ))
    report = ValidationReport(passed=not any(item.severity == Severity.ERROR for item in issues), issues=issues, checked_evidence_ids=sorted(referenced & available))
    repository.add_artifact(
        state.task_id, state.run_id, "validation",
        "证据校验通过" if report.passed else "证据校验未通过",
        report.model_dump(mode="json"),
        report.checked_evidence_ids,
        status="ready" if report.passed else "failed",
    )
    return {"validation": report.model_dump(mode="json")}


@_with_evidence_scope
def reflect_node(state: AnalysisState) -> dict[str, Any]:
    revision_round = state.revision_round + 1
    repository.update_task(state.task_id, status=TaskStatus.REFLECTING, progress=88, status_message=f"正在进行第 {revision_round} 轮质量复核")
    tracker = start_node(state, "reflect")
    try:
        reflection_context = _reflection_context(state, revision_round)
        decision = generate_review(
            llm,
            reflection_context,
            thinking=False,
            prompt_override=tracker.prompt.content,
            diagnostics=tracker.record_diagnostics,
        )
        reflection = decision.model_dump(mode="json")
        repository.add_artifact(state.task_id, state.run_id, 'validation', '模型终审决定', reflection)
        return tracker.complete({"reflection": reflection, "revision_round": revision_round,
                                 "decision_history": [*state.decision_history, reflection]})
    except Exception as exc:
        tracker.fail(exc)
        raise


def route_reflection(state: AnalysisState) -> str:
    return route_reflection_impl(state)


def repair_citations_node(state: AnalysisState) -> dict[str, Any]:
    updated = state.model_copy(update={'reflection': {
        'verdict': 'revise', 'route': 'rewrite', 'reason': '修复结构性引用错误', 'issues': [],
    }})
    result = draft_node(updated)
    return {**result, 'citation_repair_round': state.citation_repair_round + 1,
            'reflection': updated.reflection}


def route_validation(state: AnalysisState) -> str:
    return route_validation_impl(state)


def route_execute(state: AnalysisState) -> str:
    return route_execute_impl(state)


def _delivery_gate(state: AnalysisState, draft: AnalysisDraft, validation: ValidationReport) -> DeliveryGate:
    """Publish the model's decision without a backend correctness gate."""
    checks: list[DeliveryCheck] = []
    unresolved_errors = 0

    review_accepted = bool(state.reflection and state.reflection.get("verdict") == "pass")
    checks.append(DeliveryCheck(
        code="model_review_accepted",
        label="模型审核通过",
        passed=review_accepted,
        severity=Severity.ERROR if not review_accepted else Severity.INFO,
        message="模型已认可本报告。" if review_accepted else "模型尚未认可报告，修订资源已用尽。",
    ))

    if not review_accepted:
        status = TaskStatus.NEEDS_REVIEW.value
    elif draft.warnings:
        status = TaskStatus.COMPLETED_WITH_WARNINGS.value
    else:
        status = TaskStatus.COMPLETED.value
    return DeliveryGate(status=status, checks=checks,
                        repair_count=state.citation_repair_round + min(state.revision_round, settings.max_revision_rounds),
                        issues=[],
                        new_information_count=len(draft.sections) if draft.report_schema_version == 2 else len(draft.insights), unresolved_error_count=unresolved_errors)


def finish_node(state: AnalysisState) -> dict[str, Any]:
    draft = AnalysisDraft.model_validate(state.draft)
    validation = ValidationReport(passed=True)
    gate = _delivery_gate(state, draft, validation)
    draft.delivery = gate
    if gate.status == TaskStatus.NEEDS_REVIEW.value:
        message = "本次分析未达到交付条件：" + "；".join(check.message for check in gate.checks if not check.passed)[:800]
        if not state.is_replay:
            repository.add_message(state.task_id, "assistant", message)
        repository.update_task(
            state.task_id, status=TaskStatus.NEEDS_REVIEW, progress=95,
            status_message="模型尚未认可报告", error=message,
        )
        return {"draft": draft.model_dump(mode="json"), "final_status": gate.status, "error": message}
    final_status = TaskStatus(gate.status)
    if not state.is_replay:
        repository.add_message(state.task_id, "assistant", draft.summary)
    repository.update_task(
        state.task_id, status=final_status, progress=100,
        status_message="分析完成（有警告）" if draft.warnings else "分析完成",
        result=draft, clear_error=True,
    )
    if not state.is_replay:
        try:
            context_manager.record_completed_analysis(state.task_id, state.user_question, draft)
        except Exception:
            # Memory is an optimization. A successful, validated analysis must still be delivered.
            pass
    return {"draft": draft.model_dump(mode="json"), "final_status": final_status.value}


def _evidence_catalog(state: AnalysisState) -> list[dict[str, Any]]:
    allowed = _current_evidence_ids(state)
    return [
        compact_evidence(item)
        for item in repository.list_evidence_by_ids(state.task_id, allowed)
    ]


def _draft_context(
    state: AnalysisState,
) -> dict[str, Any]:
    return {
        "user_question": state.analysis_question or state.user_question,
        "user_original_request": state.user_question,
        "clarification_answer": state.clarification_answer,
        "analysis_plan": state.plan,
        "delivery_decision": state.intent,
        # During a rewrite the current draft supersedes the snapshot result;
        # sending both duplicates the same report in the model request.
        "previous_result": (state.previous_result or {}) if not state.draft else {},
        "confirmed_policies": {},
        "verified_results": _verified_result_catalog(state),
        "evidence_catalog": _evidence_catalog(state),
        "allowed_chart_types": ["line", "bar", "stacked_bar", "pie", "waterfall", "scatter", "table"],
        "previous_draft": state.draft,
        "revision_feedback": _revision_feedback(state),
    }


def _reflection_context(
    state: AnalysisState,
    revision_round: int,
) -> dict[str, Any]:
    return {
        "user_question": state.analysis_question or state.user_question,
        "clarification_answer": state.clarification_answer,
        "revision_round": revision_round,
        "maximum_revision_rounds": settings.max_revision_rounds,
        "budget_exhausted": revision_round > settings.max_revision_rounds,
        "prior_review_decisions": state.decision_history,
        "analysis_plan": state.plan,
        "analysis_draft": state.draft,
        "user_original_request": state.user_question,
        "report_outline": [{"id": section["id"], "title": section["title"]}
                           for section in (state.draft or {}).get("sections", [])],
        "incomplete_steps": [item for item in _verified_result_catalog(state) if item.get('status') == 'error'],
        # Evidence catalog is the canonical, citation-aware representation.
        # Raw tool rows would duplicate every query result in final review.
        "query_results": _verified_result_catalog(state),
        "data_revision": state.data_revision,
        "evidence_catalog": _evidence_catalog(state),
        "remaining_tool_calls": max(0, settings.max_tool_calls - state.tool_call_count),
    }


def _verified_result_catalog(state: AnalysisState) -> list[dict[str, Any]]:
    return [
        {
            "result_id": item.get("result_id"),
            "tool_name": item.get("tool_name"),
            "status": item.get("status"),
            "summary": item.get("summary"),
            "evidence_ids": item.get("evidence_ids", []),
            "warnings": item.get("warnings", []),
            "error": item.get("error"),
            "execution_metadata": item.get("execution_metadata", {}),
            "query": (item.get("arguments") or {}).get("sql"),
        }
        for item in state.tool_results
    ]


def _revision_feedback(state: AnalysisState) -> dict[str, Any] | None:
    if state.reflection and state.reflection.get("verdict") == "revise":
        return {"review": state.reflection}
    return None


def _finalize_draft(state: AnalysisState, draft: AnalysisDraft) -> AnalysisDraft:
    """Attach tool execution details to the model-generated draft."""
    normalized = draft.model_copy(deep=True)
    normalized.calculation_details = [
        CalculationDetail(
            id=str(item.get("result_id") or f"calculation_{index + 1}"),
            tool_name=str(item.get("tool_name") or "unknown"),
            title=str(item.get("summary") or "计算结果"),
            status="success" if item.get("status") == "success" else "error",
            row_count=len(item.get("rows") or []),
            evidence_refs=list(item.get("evidence_ids") or []),
            query=(item.get("arguments") or {}).get("sql"),
            warnings=list(item.get("warnings") or []),
        )
        for index, item in enumerate(state.tool_results)
    ]
    return normalized


def _publish_chart_artifacts(state: AnalysisState, draft: AnalysisDraft) -> None:
    existing = {
        str(item.payload.get("chart", {}).get("id"))
        for item in repository.list_artifacts(state.task_id, state.run_id)
        if item.artifact_type == "chart"
    }
    for chart in report_charts(draft):
        if chart.id in existing:
            continue
        repository.add_artifact(
            state.task_id,
            state.run_id,
            "chart",
            chart.title,
            {"chart": chart.model_dump(mode="json")},
            [chart.dataset_ref],
        )


def prepare_context_node(state: AnalysisState) -> dict[str, Any]:
    if state.context_prepared:
        return {}
    started_at = time.perf_counter()
    run = repository.get_run_by_id(state.run_id)
    messages = [MessageRecord.model_validate(item) for item in state.conversation_messages]
    if not messages:
        snapshot = repository.get_task(state.task_id)
        messages = [item for item in snapshot.messages if item.sequence <= run.message_sequence]
    conversation, _budget = context_manager.prepare(
        state.task_id, messages, current_question=state.user_question,
        dynamic_context={'datasets': [
            {key: item.get(key) for key in ('id', 'display_name', 'row_count')} for item in state.datasets
        ]},
    )
    log_event(logger, 'workflow.context.prepared', task_id=state.task_id, run_id=state.run_id,
              duration_ms=duration_ms(started_at))
    return {
        'conversation_summary': conversation.model_dump(mode='json'),
        'context_prepared': True,
    }


def referenced_evidence_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(referenced_evidence_ids(item) for item in value))
    if not isinstance(value, dict):
        return set()
    refs = set(value.get('evidence_refs', [])) | set(value.get('summary_evidence_refs', []))
    refs.update(value.get('evidence_ids', []))
    for key in ('evidence_id', 'dataset_ref'):
        if value.get(key):
            refs.add(value[key])
    return refs | set().union(*(referenced_evidence_ids(item) for item in value.values()))


def assess_results_node(state: AnalysisState) -> dict[str, Any]:
    tracker = start_node(state, 'assess_results')
    try:
        plan = AnalysisPlan.model_validate(state.plan)
        previous_action = (state.result_decision or {}).get('action')
        no_progress_after_replan = previous_action == 'replan' and not plan.steps
        decision_context = {
            'user_question': state.analysis_question or state.user_question,
            'conversation_context': state.conversation_summary or {},
            'delivery_decision': state.intent,
            'analysis_plan': state.plan, 'completed_step_ids': state.completed_step_ids,
            'pending_steps': [
                step.model_dump(mode='json') for step in plan.steps
                if step.id not in state.completed_step_ids
            ],
            'query_results': _verified_result_catalog(state), 'query_failures': state.query_failures,
            'previous_result': state.previous_result,
            'evidence_catalog': _evidence_catalog(state),
            'dataset_catalog': planner_catalog(
                [DatasetInfo.model_validate(item) for item in state.datasets], state.task_id
            ),
            'decision_history': state.decision_history,
            'remaining_tool_calls': max(0, settings.max_tool_calls - state.tool_call_count),
            'technical_progress': {
                'plan_step_count': len(plan.steps),
                'completed_step_count': len(state.completed_step_ids),
                'tool_result_count': len(state.tool_results),
                'replan_produced_no_steps': no_progress_after_replan,
            },
        }
        response_model = ConvergenceDecision if no_progress_after_replan else ResultDecision
        convergence_instruction = ''
        if no_progress_after_replan:
            convergence_instruction = (
                '\n当前属于无进展收敛场景：你上次要求 replan，但规划模型仍决定没有可执行步骤。'
                '你必须依据已有证据自行选择 answer（对话分析）、draft（用户要求报告）或 ask_user（确实必须补充输入）；'
                '后端不替你判断报告正确性。'
            )
        decision = llm.structured('result_reviewer', decision_context, response_model,
            thinking=False, prompt_override=tracker.prompt.content + convergence_instruction,
            diagnostics=tracker.record_diagnostics)
        value = decision.model_dump(mode='json')
        repository.add_artifact(state.task_id, state.run_id, 'validation', '模型查询结果决定', value)
        return tracker.complete({'result_decision': value,
                                 'decision_history': [*state.decision_history, value]})
    except Exception as exc:
        tracker.fail(exc)
        raise


def route_results(state: AnalysisState) -> str:
    action = ResultDecision.model_validate(state.result_decision).action
    if action == 'continue':
        return route_execute_impl(state)
    return {'replan': 'plan', 'draft': 'draft', 'ask_user': 'ask_user', 'answer': 'answer'}[action]


def answer_node(state: AnalysisState) -> dict[str, Any]:
    decision = ResultDecision.model_validate(state.result_decision)
    repository.add_message(state.task_id, 'assistant', decision.answer.strip())
    repository.update_task(state.task_id, status=TaskStatus.OFF_TOPIC, progress=100,
                           status_message='分析回答完成', clear_error=True)
    return {'final_status': TaskStatus.OFF_TOPIC.value}


def route_after_execute(state: AnalysisState) -> str:
    """A rejected SQL query returns to the model so it can repair its own SQL."""
    return "plan" if state.query_failures else "assess_results"


def ask_user_node(state: AnalysisState) -> dict[str, Any]:
    decision = state.reflection or state.result_decision or {}
    question = decision.get('question') or decision.get('reason')
    if not question:
        raise ValueError('模型未提供待确认的问题')
    repository.add_message(state.task_id, 'assistant', question)
    repository.update_task(state.task_id, status=TaskStatus.NEEDS_CLARIFICATION,
                           status_message='模型需要补充信息', clarification_question=question)
    return {'final_status': 'needs_clarification'}


def build_graph():
    def cancellable(node_name: str, node):
        def wrapped(state: AnalysisState):
            if repository.is_execution_cancelled(state.run_id):
                raise AnalysisCancelled(f"分析已由用户中止（{node_name}）")
            result = node(state)
            if repository.is_execution_cancelled(state.run_id):
                raise AnalysisCancelled(f"分析已由用户中止（{node_name}）")
            return result
        return wrapped

    builder = StateGraph(AnalysisState)
    builder.add_node("classify", cancellable("classify", classify_node_impl))
    builder.add_node("prepare_context", cancellable("prepare_context", prepare_context_node))
    builder.add_node("respond", cancellable("respond", respond_node_impl))
    builder.add_node("plan", cancellable("plan", plan_node))
    builder.add_node("clarify", cancellable("clarify", clarify_node))
    builder.add_node("execute", cancellable("execute", execute_node))
    builder.add_node("assess_results", cancellable("assess_results", assess_results_node))
    builder.add_node("ask_user", cancellable("ask_user", ask_user_node))
    builder.add_node("answer", cancellable("answer", answer_node))
    builder.add_edge("answer", END)
    builder.add_edge("ask_user", END)
    builder.add_node("draft", cancellable("draft", draft_node))
    builder.add_node("repair_citations", cancellable("repair_citations", repair_citations_node))
    builder.add_edge("repair_citations", "validate")
    builder.add_node("validate", cancellable("validate", validate_node))
    # Kept as a resume-compatible entry for runs created by older versions;
    # new runs never route through it.
    builder.add_node("reflect", cancellable("reflect", reflect_node))
    builder.add_node("finish", cancellable("finish", finish_node))
    builder.add_conditional_edges(
        START,
        lambda state: state.entry_node,
        {"classify": "classify", "plan": "plan", "execute": "execute", "draft": "draft", "reflect": "reflect"},
    )
    builder.add_conditional_edges("classify", route_intent_impl, {"plan": "prepare_context", "off_topic": "respond"})
    builder.add_edge("prepare_context", "plan")
    builder.add_edge("respond", END)
    builder.add_conditional_edges("plan", route_plan_impl, {"execute": "execute", "clarify": "clarify"})
    builder.add_edge("clarify", END)
    builder.add_conditional_edges(
        "execute", route_after_execute, {"plan": "plan", "assess_results": "assess_results"}
    )
    builder.add_conditional_edges("assess_results", route_results,
                                  {name: name for name in ('execute', 'plan', 'draft', 'ask_user', 'answer')})
    # Structural evidence checks run before the model reviewer.
    builder.add_edge("draft", "reflect")
    builder.add_conditional_edges("validate", route_validation_impl, {"finish": "finish", "reflect": "reflect", "repair_citations": "repair_citations"})
    builder.add_conditional_edges(
        "reflect",
        route_reflection_impl,
        {"finish": "finish", "replan": "plan", "rewrite": "draft", "ask_user": "ask_user"},
    )
    builder.add_edge("finish", END)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
    atexit.register(connection.close)
    return builder.compile(checkpointer=SqliteSaver(connection))


graph = build_graph()


def run_analysis(run_id: str) -> None:
    started_at = time.perf_counter()
    run = repository.get_run_by_id(run_id)
    task_id = run.task_id
    question = run.question
    snapshot = repository.get_task(task_id)
    try:
        repository.assert_run_context_current(task_id, run_id)
    except ValueError as exc:
        repository.finish_execution(run_id, "failed", str(exc))
        log_event(logger, "analysis.rejected_stale", task_id=task_id, run_id=run_id)
        return
    if not repository.mark_execution_running(run_id):
        return
    messages = [item for item in snapshot.messages if item.sequence <= run.message_sequence]
    try:
        conversation = context_manager.classification_context(task_id, messages, current_question=question)
        initial = AnalysisState(
            task_id=task_id,
            run_id=run_id,
            user_question=question,
            analysis_question=question,
            entry_node=run.entry_node,
            conversation_summary=conversation.model_dump(mode="json"),
            conversation_messages=[item.model_dump(mode="json") for item in messages],
            previous_result=snapshot.result.model_dump(mode="json") if snapshot.result else None,
            inherited_evidence_ids=[item.id for item in repository.list_evidence(task_id)
                                    if item.data_revision == snapshot.data_revision],
            data_revision=snapshot.data_revision,
            datasets=[item.model_dump(mode="json") for item in snapshot.datasets],
            confirmed_relationships=[item.model_dump(mode="json") for item in snapshot.relationships if item.status == "confirmed"],
        )
        result = graph.invoke(initial, config={"configurable": {"thread_id": run_id},
                                               "recursion_limit": 24 + settings.max_tool_calls * 4 + settings.max_revision_rounds * 6})
        final_status = str(result.get("final_status") or repository.get_task(task_id).status.value)
        has_draft = final_status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.COMPLETED_WITH_WARNINGS.value,
            TaskStatus.NEEDS_REVIEW.value,
        }
        draft = AnalysisDraft.model_validate(result["draft"]) if has_draft else None
        activate = final_status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.COMPLETED_WITH_WARNINGS.value,
        }
        repository.finish_execution(run_id, final_status, result.get("error"), draft, activate=activate)
        log_event(
            logger,
            "analysis.completed",
            task_id=task_id,
            run_id=run_id,
            status=final_status,
            activated=activate,
            duration_ms=duration_ms(started_at),
        )
    except AnalysisCancelled:
        log_event(logger, "analysis.cancelled", task_id=task_id, run_id=run_id,
                  duration_ms=duration_ms(started_at))
    except LLMError as exc:
        if isinstance(exc, LLMUnavailableError):
            message = '本地模型暂时不可用，请检查模型服务后重试。原报告未受影响。'
        elif isinstance(exc, LLMContextOverflowError):
            message = f'本次请求超出模型上下文容量：{exc}'
        else:
            message = str(exc)
        repository.finish_execution(run_id, "failed", message)
        repository.restore_active_run_after_failure(task_id, run_id, message)
        log_event(logger, "analysis.failed", task_id=task_id, run_id=run_id, error_type=type(exc).__name__, duration_ms=duration_ms(started_at))
    except Exception as exc:
        repository.finish_execution(run_id, "failed", str(exc))
        repository.restore_active_run_after_failure(task_id, run_id, str(exc))
        logger.exception(
            "analysis.failed",
            extra={"event_fields": {"event": "analysis.failed", "task_id": task_id, "run_id": run_id, "error_type": type(exc).__name__, "duration_ms": duration_ms(started_at)}},
        )
