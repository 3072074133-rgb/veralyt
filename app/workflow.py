from __future__ import annotations

import atexit
import sqlite3
import logging
import time
from functools import wraps
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .analysis_tools import ToolError, profile_table, query_department_profit, query_from_spec
from .config import settings
from .context_manager import context_manager
from .dataset_retrieval import (
    planner_catalog,
    query_catalog,
)
from .evidence_validation import (
    current_evidence_ids as _current_evidence_ids,
    validate_claim as _validate_claim,
)
from .llm import LLMContextOverflowError, LLMError, LLMUnavailableError, llm
from .report_generation import compact_evidence, generate_report, resolve_citations
from .financial_reports import query_financial_report
from .receivables import query_overdue
from .tool_catalog import TOOL_DESCRIPTIONS
from .models import (
    AnalysisDraft,
    AnalysisPlan, PlanDecision,
    AnalysisState,
    DatasetInfo,
    GeneratedAnalysisDraft,
    CalculationDetail,
    DeliveryCheck,
    DeliveryGate,
    QuerySpec,
    QueryDecision,
    ReflectionDecision,
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
__all__ = [
    "_validate_claim",
]


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
        compact_catalog = planner_catalog(catalog_datasets)
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
                "conversation_context": state.conversation_summary or {},
                "intent_decision": state.intent or {},
                "previous_result": state.previous_result or {},
                "dataset_catalog": compact_catalog,
                "confirmed_policies": {},
                "confirmed_relationships": state.confirmed_relationships,
                "available_tools": [tool["name"] for tool in TOOL_DESCRIPTIONS],
                "tool_descriptions": TOOL_DESCRIPTIONS,
                "current_plan": state.plan,
                "completed_step_ids": state.completed_step_ids,
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
        if state.reflection and state.reflection.get("route") == "replan":
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
        analysis_question = state.analysis_question or state.user_question
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
        selected_ids = list(dict.fromkeys([
            *([pending_step.dataset_id] if pending_step.dataset_id else []),
            *pending_step.dataset_ids,
        ]))
        if not selected_ids:
            raise ToolError(f"计划步骤“{pending_step.purpose}”未指定数据表")
        name = pending_step.tool
        unknown_ids = [item for item in selected_ids if item not in {dataset.id for dataset in datasets}]
        if unknown_ids:
            raise ToolError(f"计划引用了不存在的数据表：{', '.join(unknown_ids)}")
        dataset_by_id = {item.id: item for item in datasets}
        selected_datasets = [dataset_by_id[item] for item in selected_ids]
        selected = selected_datasets[0]
        arguments: dict[str, Any] = {"dataset_id": selected.id}
        tracker.record_diagnostics({
            "selected_dataset_id": selected.id,
            "selected_dataset_name": selected.display_name,
            "planned_tool": name,
        })
        if name == "query_data":
            query_context = {
                "user_question": analysis_question,
                "clarification_answer": state.clarification_answer,
                "analysis_plan": state.plan,
                "current_step": pending_step.model_dump(mode="json"),
                "selected_dataset": query_catalog(selected),
                "related_datasets": [query_catalog(item) for item in selected_datasets if item.id != selected.id],
                "confirmed_relationships": state.confirmed_relationships,
                "available_results": state.tool_results,
                "latest_validation_failure": state.validation,
                "previous_query_failures": state.query_failures,
                "query_repair_instruction": (
                    "根据 previous_query_failures 修正无法执行的参数，保留分析目的。"
                    "若历史中出现相同参数和相同报错，说明之前的修正没有解决问题，不要再次原样提交。"
                    "关联策略由你决定，无须关系预先确认；系统不会替换字段或连接方式。"
                ),
            }
            decision = llm.structured(
                "tool_orchestrator",
                query_context,
                QueryDecision,
                thinking=False,
                prompt_override=tracker.prompt.content,
                diagnostics=tracker.record_diagnostics,
            )
            arguments = {
                "query": {**decision.model_dump(mode="json"), "dataset_id": selected.id},
                "title": pending_step.purpose,
            }
        try:
            if name == "query_overdue":
                result = query_overdue(state.task_id, selected, state.run_id)
            elif name == "query_financial_report":
                result = query_financial_report(state.task_id, selected_datasets, state.run_id)
            elif name == "profile_table":
                result = profile_table(state.task_id, selected, state.run_id)
            elif name == "query_department_profit":
                result = query_department_profit(state.task_id, selected, state.run_id)
            elif name == "query_data":
                result = query_from_spec(
                    state.task_id,
                    datasets,
                    _query_spec_from_arguments(arguments),
                    arguments.get("title", pending_step.purpose),
                    state.run_id,
                )
            else:
                raise ToolError(f"不支持的分析工具：{name}")
        except Exception as exc:
            repository.add_artifact(
                state.task_id, state.run_id, "error", f"{pending_step.purpose}失败",
                {"tool_name": name, "error": str(exc), "arguments": arguments},
                status="failed",
            )
            if (name == "query_data" and isinstance(exc, ToolError)
                    and len(state.query_failures) < 2
                    and state.tool_call_count + 1 < settings.max_tool_calls):
                return tracker.complete({
                    "query_failures": [*state.query_failures, {
                        "arguments": arguments, "error": str(exc),
                    }],
                    "tool_call_count": state.tool_call_count + 1,
                })
            raise ToolError(f"计划步骤“{pending_step.purpose}”执行失败：{exc}") from exc
        artifact_type = "query" if name == "query_data" else "table"
        repository.add_artifact(
            state.task_id, state.run_id, artifact_type, pending_step.purpose,
            {
                "tool_name": result.tool_name,
                "summary": result.summary,
                "row_count": len(result.rows),
                "arguments": result.arguments,
                "columns": list(result.rows[0]) if result.rows else [],
                "warnings": result.warnings,
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
            GeneratedAnalysisDraft,
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
    missing = referenced - available
    if missing:
        issues.append(ValidationIssue(code="unknown_evidence", message=f"引用了不存在的证据：{', '.join(sorted(missing))}", severity=Severity.ERROR))
    for chart in draft.charts:
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
        ValidationReport.model_validate(state.validation)
        reflection_context = _reflection_context(state, revision_round)
        decision = llm.structured(
            "reflection_reviewer",
            reflection_context,
            ReflectionDecision,
            thinking=False,
            prompt_override=tracker.prompt.content,
            diagnostics=tracker.record_diagnostics,
        )
        reflection = decision.model_dump(mode="json")
        return tracker.complete({"reflection": reflection, "revision_round": revision_round})
    except Exception as exc:
        tracker.fail(exc)
        raise


def route_reflection(state: AnalysisState) -> str:
    return route_reflection_impl(state)


def route_validation(state: AnalysisState) -> str:
    return route_validation_impl(state)


def route_execute(state: AnalysisState) -> str:
    return route_execute_impl(state)


def _delivery_gate(state: AnalysisState, draft: AnalysisDraft, validation: ValidationReport) -> DeliveryGate:
    """Expose structural validation and the model review verdict to the UI."""
    checks: list[DeliveryCheck] = []
    evidence_traceable = validation.passed
    checks.append(DeliveryCheck(code="evidence_traceable", label="证据可追溯", passed=evidence_traceable,
                                severity=Severity.ERROR if not evidence_traceable else Severity.INFO,
                                message="结论引用可追溯到证据或计算输入。" if evidence_traceable else "存在未通过证据校验的结论。"))

    unresolved_errors = sum(1 for issue in validation.issues if issue.severity == Severity.ERROR)
    no_errors = unresolved_errors == 0
    checks.append(DeliveryCheck(code="no_unresolved_errors", label="无未解决错误", passed=no_errors,
                                severity=Severity.ERROR if not no_errors else Severity.INFO,
                                message="没有未解决的证据错误。" if no_errors else f"仍有 {unresolved_errors} 项证据错误。"))

    review_accepted = not state.reflection or state.reflection.get("verdict") == "pass"
    checks.append(DeliveryCheck(
        code="model_review_accepted",
        label="模型复核通过",
        passed=review_accepted,
        severity=Severity.ERROR if not review_accepted else Severity.INFO,
        message="模型未要求继续修改。" if review_accepted else "模型复核仍要求修改结果。",
    ))

    if not no_errors or not review_accepted:
        status = TaskStatus.NEEDS_REVIEW.value
    elif draft.warnings:
        status = TaskStatus.COMPLETED_WITH_WARNINGS.value
    else:
        status = TaskStatus.COMPLETED.value
    return DeliveryGate(status=status, checks=checks,
                        repair_count=min(state.revision_round, settings.max_revision_rounds),
                        issues=[item.model_dump(mode="json") for item in validation.issues],
                        new_information_count=len(draft.insights), unresolved_error_count=unresolved_errors)


def finish_node(state: AnalysisState) -> dict[str, Any]:
    draft = AnalysisDraft.model_validate(state.draft)
    validation = ValidationReport.model_validate(state.validation)
    gate = _delivery_gate(state, draft, validation)
    draft.delivery = gate
    if gate.status == TaskStatus.NEEDS_REVIEW.value:
        message = "本次分析未达到交付条件：" + "；".join(check.message for check in gate.checks if not check.passed)[:800]
        if not state.is_replay:
            repository.add_message(state.task_id, "assistant", message)
        repository.update_task(
            state.task_id, status=TaskStatus.NEEDS_REVIEW, progress=95,
            status_message="结果需要人工复核", error=message,
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
        "clarification_answer": state.clarification_answer,
        "analysis_plan": state.plan,
        "previous_result": state.previous_result or {},
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
        "analysis_plan": state.plan,
        "analysis_draft": state.draft,
        "structural_validation": state.validation,
        "evidence_catalog": _evidence_catalog(state),
        "revision_round": revision_round,
        "maximum_revision_rounds": settings.max_revision_rounds,
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
        }
        for item in state.tool_results
    ]


def _revision_feedback(state: AnalysisState) -> dict[str, Any] | None:
    if state.reflection and state.reflection.get("verdict") == "revise":
        return {"review": state.reflection, "structural_validation": state.validation}
    return None


def _query_spec_from_arguments(arguments: dict[str, Any]) -> QuerySpec:
    raw = arguments.get("query")
    if not isinstance(raw, dict):
        raise ToolError("结构化查询缺少 query 对象")
    try:
        spec = QuerySpec.model_validate(raw)
    except Exception as exc:
        raise ToolError(f"结构化查询参数无效：{exc}") from exc
    if spec.limit > settings.max_query_rows:
        raise ToolError(f"查询行数 {spec.limit} 超过执行上限 {settings.max_query_rows}")
    return spec


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
    for chart in draft.charts:
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
    builder.add_node("draft", cancellable("draft", draft_node))
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
    builder.add_conditional_edges("execute", route_execute_impl, {"execute": "execute", "draft": "draft"})
    # Structural evidence checks run before the model reviewer.
    builder.add_edge("draft", "validate")
    builder.add_conditional_edges("validate", route_validation_impl, {"finish": "finish", "reflect": "reflect"})
    builder.add_conditional_edges(
        "reflect",
        route_reflection_impl,
        {"finish": "finish", "replan": "plan", "rewrite": "draft"},
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
            datasets=[item.model_dump(mode="json") for item in snapshot.datasets],
            confirmed_relationships=[item.model_dump(mode="json") for item in snapshot.relationships if item.status == "confirmed"],
        )
        result = graph.invoke(initial, config={"configurable": {"thread_id": run_id},
                                               "recursion_limit": 16 + settings.max_tool_calls + settings.max_revision_rounds * 6})
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
