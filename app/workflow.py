from __future__ import annotations

import atexit
import hashlib
import re
import sqlite3
import json
import logging
import time
from functools import wraps
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .analysis_tools import ToolError, auto_analyze, profile_table, query_department_profit, query_from_spec
from .chart_builder import normalize_draft_charts
from .config import settings
from .context_manager import context_manager
from .dataset_retrieval import (
    DatasetMatch,
    is_placeholder_column_name,
    planner_catalog,
    query_catalog,
    retrieve_datasets,
)
from .evidence_validation import (
    as_decimal as _as_decimal,
    current_evidence_ids as _current_evidence_ids,
    evidence_row_count_supports as _evidence_row_count_supports,
    numeric_tokens as _numeric_tokens,
    numeric_values_match as _numeric_values_match,
    unsupported_numbers as _unsupported_numbers,
    validate_claim as _validate_claim,
)
from .llm import LLMContextOverflowError, LLMError, LLMStructuredOutputError, LLMUnavailableError, llm
from .financial_reports import financial_kind, financial_draft, query_financial_report
from .followups import calculate_metric, metric_draft, FORMULAS
from .receivables import is_overdue_ranking, query_overdue, overdue_draft
from .insights import derive_insights
from .knowledge_service import retrieve_for_run
from .models import (
    AnalysisDraft,
    AnalysisPlan, PlanDecision,
    AnalysisState,
    DatasetInfo,
    EvidencePointer,
    GeneratedAnalysisDraft,
    Metric,
    Finding,
    ChartSeries,
    ChartSpec,
    CalculationDetail,
    DeliveryCheck,
    DeliveryGate,
    Insight,
    PlanStep,
    QuerySpec,
    QueryDecision,
    ReflectionDecision,
    ReviewIssue,
    Severity,
    TaskStatus,
    ToolExecutionResult,
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
from .workflow_nodes.rules import is_contextual_followup


logger = logging.getLogger(__name__)


class AnalysisCancelled(Exception):
    """Raised at workflow node boundaries after a user cancellation."""
__all__ = [
    "_as_decimal",
    "_evidence_row_count_supports",
    "_numeric_tokens",
    "_numeric_values_match",
    "_unsupported_numbers",
    "_validate_claim",
]


def _normalize_intent_text(value: str) -> str:
    normalized = re.sub(r"[\s，。！？,.!?、；;：:]", "", value).casefold()
    return re.sub(r"^(请|麻烦|帮我)", "", normalized)


def _is_retry_analysis_request(question: str) -> bool:
    normalized = _normalize_intent_text(question)
    return bool(
        re.fullmatch(
            r"(?:(?:重新|再|继续)(?:分析|计算|统计|汇总)(?:一下|一次|一遍|数据|这份数据|当前数据)?|"
            r"重做分析|(?:重新|再)跑(?:一下|一次|一遍)?)",
            normalized,
        )
    )


def _previous_analysis_question(state: AnalysisState) -> str | None:
    context = state.conversation_summary or {}
    recent_messages = context.get("recent_messages")
    if isinstance(recent_messages, list):
        for message in reversed(recent_messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = str(message.get("content") or "").strip()
            if (
                content
                and not _is_retry_analysis_request(content)
                and not _is_generic_analysis_request(content)
            ):
                return content

    memory = context.get("memory")
    if isinstance(memory, dict):
        completed = memory.get("completed_analyses")
        if isinstance(completed, list):
            for analysis in reversed(completed):
                if not isinstance(analysis, dict):
                    continue
                question = str(analysis.get("question") or "").strip()
                if (
                    question
                    and not _is_retry_analysis_request(question)
                    and not _is_generic_analysis_request(question)
                ):
                    return question
        task_goal = str(memory.get("task_goal") or "").strip()
        if (
            task_goal
            and not _is_retry_analysis_request(task_goal)
            and not _is_generic_analysis_request(task_goal)
        ):
            return task_goal
    return None


def _has_clear_analysis_intent(question: str, has_uploaded_data: bool) -> bool:
    if not has_uploaded_data:
        return False
    normalized = _normalize_intent_text(question)
    if re.search(r"(?:不要|无需|不用|停止|取消)(?:再)?分析", normalized):
        return False
    action = re.search(r"分析|计算|统计|汇总|筛选|排序|比较|对比|同比|环比|趋势|异常|对账|预测", normalized)
    data_subject = re.search(
        r"数据|报表|表格|部门|收入|利润|盈亏|销售|成本|费用|金额|预算|回款|账龄|字段|记录",
        normalized,
    )
    return bool(action and data_subject)


def classify_node(state: AnalysisState) -> dict[str, Any]:
    return classify_node_impl(state)




def route_intent(state: AnalysisState) -> str:
    return route_intent_impl(state)


def respond_node(state: AnalysisState) -> dict[str, Any]:
    return respond_node_impl(state)


# Compatibility alias for older imports.
off_topic_node = respond_node


def plan_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status=TaskStatus.PLANNING, progress=35, status_message="正在理解指标和分析口径")
    tracker = start_node(state, "plan")
    try:
        datasets = [DatasetInfo.model_validate(item) for item in state.datasets]
        if (state.intent or {}).get('route') != 'clarification' and is_overdue_ranking(state.user_question):
            candidates = [d for d in datasets if financial_kind(d) == '应收账款']
            if len(candidates) == 1:
                return tracker.complete({'plan': AnalysisPlan(goal='查询逾期客户排名并说明账龄缺口',
                    can_execute=True, steps=[PlanStep(id='overdue_ranking', purpose='按客户汇总原表逾期余额',
                    tool='auto_analyze', dataset_id=candidates[0].id)]).model_dump(mode='json')})
            return tracker.complete({'plan': AnalysisPlan(goal='确认应收明细', can_execute=False,
                clarification_question='请明确要分析的应收账款表，需包含客户名称和逾期余额。').model_dump(mode='json')})
        intent = state.intent or {}
        if intent.get('route') == 'clarification':
            question = ('你想了解毛利率、营业利润率，还是净利润率？请写出具体指标名称。'
                        if intent.get('metric') == 'ambiguous' else intent.get('reply') or intent.get('suggested_response') or
                        '请明确要查询的指标和期间。')
            return tracker.complete({'plan': AnalysisPlan(goal='确认本轮需求', can_execute=False,
                clarification_question=question).model_dump(mode='json')})
        if intent.get('route') == 'derived_metric':
            if intent.get('metric') not in FORMULAS:
                return tracker.complete({'plan': AnalysisPlan(goal='确认指标口径', can_execute=False,
                    clarification_question='请明确指标名称，目前支持净利润率、毛利率、营业利润率和资产负债率。').model_dump(mode='json')})
            result, reason = calculate_metric(state, intent['metric'])
            if result:
                return tracker.complete({'plan': AnalysisPlan(goal='复用证据计算指标', can_execute=True,
                    steps=[]).model_dump(mode='json'), 'tool_results': [result.model_dump(mode='json')]})
            needed = {key[0] for key in FORMULAS[intent['metric']]}
            financial = [d for d in datasets if financial_kind(d) in needed]
            if not financial or '分母为零' in reason:
                return tracker.complete({'plan': AnalysisPlan(goal='确认计算输入', can_execute=False,
                    clarification_question=reason).model_dump(mode='json')})
            return tracker.complete({'plan': AnalysisPlan(goal='补充指标输入', can_execute=True,
                steps=[PlanStep(id='financial_report', purpose='查询当前版本的财务输入', tool='auto_analyze',
                    dataset_id=financial[0].id, dataset_ids=[d.id for d in financial[1:]])]).model_dump(mode='json')})
        matches = retrieve_datasets(
            state.user_question,
            datasets,
            limit=settings.dataset_candidate_limit,
        )
        compact_catalog = planner_catalog(
            matches, question=state.user_question, field_limit=60, sample_limit=3
        )
        tracker.record_diagnostics({
            "dataset_count": len(datasets),
            "candidate_count": len(matches),
            "dataset_candidates": [match.summary() for match in matches],
            "knowledge_match_count": len(state.knowledge_context),
            "knowledge_matches": [
                {
                    "chunk_id": item.get("chunk_id"),
                    "knowledge_base_name": item.get("knowledge_base_name"),
                    "document_title": item.get("document_title"),
                    "score": item.get("score"),
                }
                for item in state.knowledge_context
            ],
        })
        department_profit = next(
            (
                match
                for match in matches
                if _is_sectioned_department_profit_request(state.user_question, match.dataset)
            ),
            None,
        )
        if department_profit:
            plan = AnalysisPlan(
                goal="统计各部门净利润并识别亏损部门",
                can_execute=True,
                metrics=["净利润"],
                dimensions=["部门"],
                assumptions=["按报表中各部门段落对应的 Net Profit 行识别部门净利润。"],
                steps=[PlanStep(
                    id="department_profit",
                    purpose="提取并汇总各部门净利润",
                    tool="query_data",
                    dataset_id=department_profit.dataset.id,
                )],
            )
            tracker.record_diagnostics({"planning_source": "deterministic_department_profit"})
        elif _is_generic_analysis_request(state.user_question) and any(financial_kind(d) for d in datasets):
            matches = retrieve_datasets(state.user_question, datasets, limit=len(datasets))
            financial = [d for d in datasets if financial_kind(d)]
            plan = AnalysisPlan(goal="按财务报表项目提取金额并核对明细及主表", can_execute=True,
                steps=[PlanStep(id="financial_report", purpose="分析整套财务报表并进行勾稽核对",
                                tool="auto_analyze", dataset_id=financial[0].id,
                                dataset_ids=[d.id for d in financial[1:]])])
            tracker.record_diagnostics({"planning_source": "deterministic_financial_report"})
        elif _is_generic_analysis_request(state.user_question):
            viable_match = next((match for match in matches if _has_named_measure(match.dataset)), None)
            if viable_match is None:
                plan = AnalysisPlan(
                    goal="确认要统计的业务指标",
                    can_execute=False,
                    clarification_question=(
                        "当前数据表只识别到未命名列等占位字段，无法可靠确定分析指标。"
                        "请指定要统计的字段名称，或先在数据集页面修正表头。"
                    ),
                )
            else:
                overview_matches = [match for match in matches if _has_named_measure(match.dataset)][:3]
                plan = AnalysisPlan(
                        goal="生成数据概览",
                        can_execute=True,
                        steps=[PlanStep(
                            id=f"overview_{index + 1}",
                            purpose="汇总主要金额指标并提取可复核证据",
                            tool="auto_analyze",
                            dataset_id=match.dataset.id,
                        ) for index, match in enumerate(overview_matches)],
                )
            tracker.record_diagnostics({"planning_source": "deterministic_overview"})
        else:
            plan_context = {
                "user_question": state.user_question,
                "conversation_context": state.conversation_summary or {},
                "previous_result": state.previous_result or {},
                "dataset_catalog": compact_catalog,
                "knowledge_context": state.knowledge_context,
                "confirmed_policies": {},
                "confirmed_relationships": state.confirmed_relationships,
                "available_tools": ["auto_analyze", "profile_table", "query_data"],
                "current_plan": state.plan,
                "completed_step_ids": state.completed_step_ids,
                "revision_feedback": _revision_feedback(state),
            }
            try:
                plan = llm.structured(
                    "analysis_planner", plan_context, PlanDecision,
                thinking=False,
                prompt_override=tracker.prompt.content,
                fallback_contexts=[
                    {
                        **plan_context,
                        "dataset_catalog": planner_catalog(
                            matches[:2], question=state.user_question, field_limit=40, sample_limit=1
                        ),
                    },
                    {
                        **plan_context,
                        "dataset_catalog": planner_catalog(
                            matches[:1], question=state.user_question, field_limit=24, sample_limit=0
                        ),
                    },
                ],
                    diagnostics=tracker.record_diagnostics,
                )
                if isinstance(plan, PlanDecision):
                    plan = AnalysisPlan(goal=plan.goal or state.user_question, can_execute=plan.action == "analyze", steps=plan.steps, clarification_question=plan.clarification)
            except LLMStructuredOutputError as exc:
                tracker.record_diagnostics({"planner_fallback": "minimal_deterministic", "planner_error": str(exc)})
                viable = next((item for item in matches if _has_named_measure(item.dataset)), None)
                if viable is None:
                    plan = AnalysisPlan(goal="确认分析指标", can_execute=False,
                        clarification_question="模型计划格式无法解析，请明确要统计的指标或字段名称。")
                else:
                    plan = AnalysisPlan(goal="生成基础数据概览", can_execute=True, steps=[PlanStep(
                        id="fallback_overview", purpose="汇总主要金额指标并提取可复核证据",
                        tool="auto_analyze", dataset_id=viable.dataset.id)])
        # In a hybrid flow a contextual follow-up may be answerable from the
        # current evidence even when the model cannot name a new field.  Keep
        # the question, reuse the evidence, and let the draft model explain
        # the limitation instead of forcing a second generic clarification.
        if is_contextual_followup(state.user_question, bool(state.previous_result)) and not plan.can_execute:
            plan = AnalysisPlan(
                goal=state.user_question,
                can_execute=True,
                assumptions=["本轮基于上一轮已验证结果和证据回答；如需新期间或新维度，再补充具体范围。"],
                steps=[],
            )
            tracker.record_diagnostics({"planning_source": "hybrid_context_reuse"})
        plan = _normalize_plan_for_request(plan, state.user_question)
        plan = _bind_plan_datasets(plan, matches, state.confirmed_relationships)
        if plan.can_execute and not plan.steps and not is_contextual_followup(state.user_question, bool(state.previous_result)):
            plan.steps = [PlanStep(
                id="analysis",
                purpose=plan.goal,
                tool="auto_analyze",
                dataset_id=matches[0].dataset.id if matches else None,
            )]
        update: dict[str, Any] = {
            "plan": plan.model_dump(mode="json"),
            "dataset_candidates": [match.summary() for match in matches],
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
    question = plan.clarification_question or "请补充会影响分析结果的统计口径。"
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
        if pending_step is None and state.reflection and state.reflection.get("route") == "execute":
            matches = retrieve_datasets(
                state.user_question,
                datasets,
                limit=settings.dataset_candidate_limit,
            )
            pending_step = PlanStep(
                id=f"revision_{state.revision_round}",
                purpose=str(state.reflection.get("reason") or "补充复核所需计算"),
                tool="query_data",
                dataset_id=matches[0].dataset.id if matches else None,
            )
        if pending_step is None:
            return tracker.complete({})
        if state.tool_call_count >= settings.max_tool_calls:
            raise ToolError(
                f"工具调用已达到上限 {settings.max_tool_calls}，仍有计划步骤未完成：{pending_step.purpose}"
            )
        candidate_ids = [item.get("dataset_id") for item in state.dataset_candidates]
        selected = next(
            (item for item in datasets if item.id == pending_step.dataset_id),
            next((item for item in datasets if item.id in candidate_ids), datasets[0]),
        )
        name = pending_step.tool
        selected_ids = list(dict.fromkeys([selected.id, *pending_step.dataset_ids]))
        selected_datasets = [item for item in datasets if item.id in selected_ids]
        arguments: dict[str, Any] = {"dataset_id": selected.id}
        tracker.record_diagnostics({
            "selected_dataset_id": selected.id,
            "selected_dataset_name": selected.display_name,
            "planned_tool": name,
        })
        deterministic_strategy = (
            "sectioned_department_profit"
            if name == "query_data" and _is_sectioned_department_profit_request(
                state.user_question, selected
            )
            else None
        )
        if deterministic_strategy:
            tracker.record_diagnostics({"deterministic_query_strategy": deterministic_strategy})
        elif name == "query_data":
            query_context = {
                "user_question": state.user_question,
                "analysis_plan": state.plan,
                "current_step": pending_step.model_dump(mode="json"),
                "selected_dataset": query_catalog(
                    selected, question=state.user_question, field_limit=60, sample_limit=3
                ),
                "related_datasets": [query_catalog(item, question=state.user_question, field_limit=30, sample_limit=2) for item in selected_datasets if item.id != selected.id],
                "confirmed_relationships": state.confirmed_relationships,
                "available_results": state.tool_results[-3:],
                "knowledge_context": state.knowledge_context,
                "latest_validation_failure": state.validation,
            }
            decision = llm.structured(
                "tool_orchestrator",
                query_context,
                QueryDecision,
                thinking=False,
                prompt_override=tracker.prompt.content,
                fallback_contexts=[
                    {
                        **query_context,
                        "selected_dataset": query_catalog(
                            selected, question=state.user_question, field_limit=40, sample_limit=1
                        ),
                    },
                    {
                        **query_context,
                        "selected_dataset": query_catalog(
                            selected, question=state.user_question, field_limit=24, sample_limit=0
                        ),
                        "available_results": _verified_result_catalog(state)[-2:],
                    },
                ],
                diagnostics=tracker.record_diagnostics,
            )
            measures = [m if isinstance(m, dict) else {"field": m, "aggregation": "sum"} for m in decision.measures]
            if not measures:
                candidate = next((c for c in selected.columns if c.role == "measure" or c.semantic_type in {"amount", "metric"}), None)
                if candidate:
                    measures = [{"field": candidate.name, "aggregation": candidate.default_aggregation if candidate.default_aggregation != "none" else "sum"}]
            arguments = {"query": {**decision.model_dump(mode="json"), "measures": measures, "dataset_id": selected.id}, "title": "查询结果"}
        analysis_request = "；".join(
            part
            for part in [_context_text(state.conversation_summary), state.user_question, str(state.plan or "")]
            if part
        )
        try:
            if pending_step.id == 'overdue_ranking':
                result = query_overdue(state.task_id, selected, state.run_id)
            elif pending_step.id == "financial_report":
                result = query_financial_report(state.task_id, selected_datasets, state.run_id)
            elif name == "profile_table":
                result = profile_table(state.task_id, selected, state.run_id)
            elif deterministic_strategy == "sectioned_department_profit":
                result = query_department_profit(state.task_id, selected, state.run_id)
            elif name == "query_data":
                result = query_from_spec(
                    state.task_id,
                    datasets,
                    _query_spec_from_arguments(arguments, datasets),
                    arguments.get("title", pending_step.purpose),
                    state.run_id,
                )
            else:
                result = auto_analyze(state.task_id, selected_datasets, analysis_request, state.run_id)
        except Exception as exc:
            previous = next((r for r in reversed(state.tool_results) if r.get("status") == "success" and r.get("arguments", {}).get("query")), None)
            if isinstance(exc, LLMStructuredOutputError) and previous and name == "query_data":
                try:
                    result = query_from_spec(state.task_id, datasets, _query_spec_from_arguments(previous["arguments"], datasets), previous["arguments"].get("title", pending_step.purpose), state.run_id)
                except Exception:
                    result = None
                if result is not None:
                    return {"tool_results": [*state.tool_results, result], "completed_step_ids": [*state.completed_step_ids, pending_step.id]}
            result = ToolExecutionResult(
                result_id="failed", tool_name=name, arguments=arguments, status="error",
                summary="工具执行失败", error=str(exc), warnings=[],
            )
            repository.add_artifact(
                state.task_id, state.run_id, "error", f"{pending_step.purpose}失败",
                {"tool_name": name, "error": str(exc), "arguments": arguments},
                status="failed",
            )
            attempts = dict(state.step_attempts)
            attempts[pending_step.id] = attempts.get(pending_step.id, 0) + 1
            if attempts[pending_step.id] >= 2:
                raise ToolError(f"计划步骤“{pending_step.purpose}”连续执行失败：{exc}") from exc
            return tracker.complete({
                "tool_results": [*state.tool_results, result.model_dump(mode="json")],
                "tool_call_count": state.tool_call_count + 1,
                "step_attempts": attempts,
            })
        artifact_type = "query" if name == "query_data" else "table"
        repository.add_artifact(
            state.task_id, state.run_id, artifact_type, pending_step.purpose,
            {
                "tool_name": result.tool_name,
                "summary": result.summary,
                "row_count": len(result.rows),
                "arguments": result.arguments,
                "columns": list(result.rows[0]) if result.rows else [],
                "rows": result.rows[:200],
                "warnings": result.warnings,
            },
            result.evidence_ids,
        )
        completed = list(dict.fromkeys([*state.completed_step_ids, pending_step.id]))
        return tracker.complete({
            "tool_results": [*state.tool_results, result.model_dump(mode="json")],
            "tool_call_count": state.tool_call_count + 1,
            "completed_step_ids": completed,
        })
    except Exception as exc:
        tracker.fail(exc)
        raise


def _latest_strategy_result(state: AnalysisState, strategy: str, *, metric: str | None = None) -> dict[str, Any] | None:
    return next((result for result in reversed(state.tool_results)
                 if result.get('status') == 'success'
                 and result.get('arguments', {}).get('strategy') == strategy
                 and (metric is None or result.get('arguments', {}).get('metric') == metric)), None)


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
        overdue_result = _latest_strategy_result(state, 'overdue_ranking')
        if overdue_result:
            draft = _derive_insights_for_draft(state, _finalize_draft(state, overdue_draft(overdue_result)))
            return tracker.complete({'draft': draft.model_dump(mode='json'), 'draft_execution_mode': 'deterministic'})
        if (state.intent or {}).get('route') == 'derived_metric':
            result = _latest_strategy_result(state, 'derived_metric', metric=state.intent['metric'])
            if result is None:
                calculated, reason = calculate_metric(state, state.intent['metric'])
                if calculated is None:
                    raise ToolError(reason)
                result = calculated.model_dump(mode='json')
                state.tool_results = [*state.tool_results, result]
            draft = _finalize_draft(state, metric_draft(result))
            draft = _derive_insights_for_draft(state, draft)
            return tracker.complete({'draft': draft.model_dump(mode='json'), 'tool_results': state.tool_results, 'draft_execution_mode': 'deterministic'})
        financial_result = next((r for r in reversed(state.tool_results)
                                 if r.get('status') == 'success' and
                                 r.get('arguments', {}).get('strategy') == 'financial_report'), None)
        if financial_result:
            draft = _finalize_draft(state, financial_draft(financial_result))
            draft = _derive_insights_for_draft(state, draft)
            return tracker.complete({'draft': draft.model_dump(mode='json'), 'draft_execution_mode': 'deterministic'})
        if _uses_sectioned_department_profit_result(state):
            department_profit = _department_profit_draft(state)
            if department_profit is not None:
                department_profit = _finalize_draft(state, department_profit)
                department_profit = _derive_insights_for_draft(state, department_profit)
                _publish_chart_artifacts(state, department_profit)
                tracker.record_diagnostics({"draft_source": "deterministic_department_profit"})
                return tracker.complete({"draft": department_profit.model_dump(mode="json"), "draft_execution_mode": "deterministic"})
        if _is_generic_analysis_request(state.user_question):
            overview = _generic_overview_draft(state)
            if overview is not None:
                if state.reflection and not (state.validation or {}).get('passed', True):
                    overview.summary_evidence_pointers = []
                    for item in [*overview.metrics, *overview.findings]:
                        item.evidence_pointers = []
                overview = _finalize_draft(state, overview)
                overview = _derive_insights_for_draft(state, overview)
                _publish_chart_artifacts(state, overview)
                return tracker.complete({"draft": overview.model_dump(mode="json"), "draft_execution_mode": "deterministic"})
        draft_context = _draft_context(state, row_limit=12)
        generated_draft = llm.structured(
            "draft_writer",
            draft_context,
            GeneratedAnalysisDraft,
            thinking=False,
            prompt_override=tracker.prompt.content,
            ignored_response_fields={"calculation_details"},
            fallback_contexts=[
                _draft_context(state, row_limit=8),
                _draft_context(state, row_limit=4, include_previous=False),
            ],
            diagnostics=tracker.record_diagnostics,
        )
        draft = AnalysisDraft.model_validate(generated_draft.model_dump(mode="json"))
        draft = _finalize_draft(state, draft)
        draft = _derive_insights_for_draft(state, draft)
        _publish_chart_artifacts(state, draft)
        return tracker.complete({"draft": draft.model_dump(mode="json"), "draft_execution_mode": "model"})
    except Exception as exc:
        tracker.fail(exc)
        raise


def _derive_insights_for_draft(state: AnalysisState, draft: AnalysisDraft) -> AnalysisDraft:
    """Add deterministic, evidence-backed conclusions during draft creation."""
    strategy = _deterministic_strategy(state)
    results = [item for item in reversed(state.tool_results)
               if item.get("status") == "success" and item.get("rows") and item.get("evidence_ids")
               and (strategy not in {"derived_metric", "overdue_ranking", "financial_report"}
                    or item.get("arguments", {}).get("strategy") == strategy)]
    if not results:
        return draft
    generated: list[Insight] = []
    for result in results:
        generated.extend(derive_insights(result, strategy))
    # A rewrite must not duplicate deterministic conclusions.
    existing = {item.id for item in draft.insights}
    draft.insights.extend(item for item in generated if item.id not in existing)
    draft.insights = draft.insights[:12]
    if generated:
        draft.summary = _insight_summary(draft.summary, draft.insights)
        draft.summary_evidence_refs = list(dict.fromkeys([
            *draft.summary_evidence_refs,
            *(ref for item in draft.insights for ref in item.evidence_refs),
        ]))
        draft.summary_evidence_pointers = list({
            (pointer.evidence_id, pointer.row_index, pointer.field, pointer.raw_value, pointer.source_type): pointer
            for item in draft.insights for pointer in item.evidence_pointers
        }.values())
    return draft


@_with_evidence_scope
def derive_insights_node(state: AnalysisState) -> dict[str, Any]:
    """Compatibility wrapper for integrations using the previous graph."""
    return {"draft": _derive_insights_for_draft(state, AnalysisDraft.model_validate(state.draft)).model_dump(mode="json")}


def _insight_summary(summary: str, insights: list[Insight]) -> str:
    """Keep the original evidence summary while adding a concise answer."""
    conclusions = [item.conclusion for item in insights[:3] if item.conclusion]
    if not conclusions:
        return summary
    prefix = "；".join(conclusions)
    return f"{prefix}。{summary}" if prefix not in summary else summary


@_with_evidence_scope
def validate_node(state: AnalysisState) -> dict[str, Any]:
    repository.update_task(state.task_id, status=TaskStatus.VALIDATING, progress=80, status_message="正在核对数字和证据引用")
    draft = AnalysisDraft.model_validate(state.draft)
    available = _current_evidence_ids(state)
    evidence_field_names = {
        column
        for evidence_id in available
        if (evidence := repository.get_evidence(state.task_id, evidence_id)) is not None
        for column in evidence.columns
    }
    referenced = set(draft.summary_evidence_refs)
    issues: list[ValidationIssue] = []
    for target, text in [
        ("title", draft.title),
        *[(f"charts[{index}].title", item.title) for index, item in enumerate(draft.charts)],
    ]:
        numbers = _numeric_tokens(text, ignored_terms=evidence_field_names)
        if numbers:
            issues.append(ValidationIssue(
                code="unreferenced_title_number",
                message=f"标题包含无法定位到证据单元格的数字：{', '.join(numbers)}",
                severity=Severity.ERROR,
                target=target,
            ))
    issues.extend(_validate_claim(
        state,
        "summary",
        draft.summary,
        draft.summary_evidence_refs,
        draft.summary_evidence_pointers,
        require_evidence=True,
    ))
    for index, metric in enumerate(draft.metrics):
        referenced.update(metric.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"metrics[{index}]",
            f"{metric.label} {metric.value} {metric.change or ''}",
            metric.evidence_refs,
            metric.evidence_pointers,
            require_evidence=True,
        ))
    for index, finding in enumerate(draft.findings):
        referenced.update(finding.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"findings[{index}]",
            f"{finding.title} {finding.detail}",
            finding.evidence_refs,
            finding.evidence_pointers,
            require_evidence=True,
        ))
    for index, insight in enumerate(draft.insights):
        referenced.update(insight.evidence_refs)
        issues.extend(_validate_claim(
            state,
            f"insights[{index}]",
            f"{insight.title} {insight.conclusion} {insight.value or ''}",
            insight.evidence_refs,
            insight.evidence_pointers,
            require_evidence=bool(insight.value or insight.evidence_refs),
        ))
        if not insight.significance:
            issues.append(ValidationIssue(
                code="insight_missing_significance", message=f"分析结论缺少影响说明：{insight.title}",
                severity=Severity.ERROR, target=f"insights[{index}]",
            ))
        if insight.severity != Severity.INFO and not insight.action:
            issues.append(ValidationIssue(
                code="insight_missing_action", message=f"重要分析结论缺少可执行建议：{insight.title}",
                severity=Severity.ERROR, target=f"insights[{index}]",
            ))
    missing = referenced - available
    if missing:
        issues.append(ValidationIssue(code="unknown_evidence", message=f"引用了不存在的证据：{', '.join(sorted(missing))}", severity=Severity.ERROR))
    for chart in draft.charts:
        if chart.dataset_ref not in available:
            issues.append(ValidationIssue(code="unknown_chart_data", message=f"图表 {chart.title} 的数据证据不存在", severity=Severity.ERROR, target=chart.id))
    # Assumptions, warnings and follow-up questions are explanatory metadata.
    # Numeric claims in the answer body remain strictly evidence-backed above,
    # while dates/step counts in these narrative fields must not block delivery.
    if not draft.findings and not draft.metrics:
        issues.append(ValidationIssue(code="empty_result", message="分析结果没有指标或发现", severity=Severity.ERROR))
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
        validation = ValidationReport.model_validate(state.validation)
        if not validation.passed:
            previous_issues = (state.reflection or {}).get('issues', [])
            unchanged = {i.get('problem') for i in previous_issues} == {i.message for i in validation.issues}
            route = 'finish' if state.draft_execution_mode == 'deterministic' or (unchanged and previous_issues) else 'rewrite'
            return tracker.complete({
                "reflection": ReflectionDecision(
                    verdict="revise",
                    route=route,
                    reason="复核问题未发生变化，已停止无效重试。" if route == 'finish' else "程序校验未通过，必须修复证据引用。",
                    issues=[
                        ReviewIssue(
                            target=issue.target or "analysis_draft",
                            problem=issue.message,
                            required_action="删除未获证据支持的数字，或逐字使用引用证据中已有的值。",
                            severity=issue.severity,
                        )
                        for issue in validation.issues
                    ],
                ).model_dump(mode="json"),
                "revision_round": revision_round,
            })
        if _deterministic_strategy(state) and validation.passed:
            strategy = _deterministic_strategy(state)
            log_event(logger, "workflow.deterministic_quality_passed", task_id=state.task_id, run_id=state.run_id, strategy=strategy)
            return tracker.complete({
                "reflection": ReflectionDecision(
                    verdict="pass",
                    route="finish",
                    reason="确定性结果已通过数字和证据校验。",
                ).model_dump(mode="json"),
                "revision_round": revision_round,
            })
        current_draft_hash = _stable_hash(state.draft or {})
        previous = state.reflection or {}
        previous_issues = previous.get("issues") or []
        current_issue_hash = _stable_hash(sorted(_stable_hash(issue) for issue in previous_issues))
        if previous.get("verdict") == "revise" and state.last_review_draft_hash == current_draft_hash and state.last_review_issue_hash == current_issue_hash:
            return tracker.complete({
                "reflection": ReflectionDecision(
                    verdict="revise", route="finish",
                    reason="草稿和复核问题均未变化，已停止无效重写。",
                    issues=[ReviewIssue.model_validate(issue) for issue in previous_issues],
                ).model_dump(mode="json"),
                "revision_round": settings.max_revision_rounds,
                "last_review_draft_hash": current_draft_hash,
                "last_review_issue_hash": current_issue_hash,
                "draft": {**(state.draft or {}), "warnings": [*((state.draft or {}).get("warnings") or []), "复核意见无法通过当前证据和策略自动修复，已停止重复重写。"]},
            })
        reflection_context = _reflection_context(state, revision_round, row_limit=12)
        try:
            decision = llm.structured(
                "reflection_reviewer",
                reflection_context,
                ReflectionDecision,
                thinking=False,
                prompt_override=tracker.prompt.content,
                fallback_contexts=[
                    _reflection_context(state, revision_round, row_limit=8),
                    _reflection_context(state, revision_round, row_limit=4),
                ],
                diagnostics=tracker.record_diagnostics,
            )
        except LLMStructuredOutputError as exc:
            tracker.record_diagnostics({
                "structured_output_fallback": True,
                "structured_output_error": str(exc),
            })
            log_event(
                logger,
                "workflow.reflection.structured_output_fallback",
                task_id=state.task_id,
                run_id=state.run_id,
                revision_round=revision_round,
            )
            decision = ReflectionDecision(
                verdict="pass",
                route="finish",
                reason="数字和证据的确定性校验已通过；模型质量复核输出格式异常，已采用校验结果完成。",
            )
        reflection = decision.model_dump(mode="json")
        issue_hash = _stable_hash(sorted(_stable_hash(issue) for issue in reflection.get("issues", [])))
        if reflection["verdict"] == "revise" and not any(issue.get("severity") == Severity.ERROR.value for issue in reflection.get("issues", [])):
            reflection["verdict"] = "pass"
            reflection["route"] = "finish"
            reflection["reason"] = f"复核仅提出建议性改进，当前结果可交付：{reflection['reason']}"
            current_draft = {**(state.draft or {}), "warnings": [*((state.draft or {}).get("warnings") or []), "质量复核提出了不影响当前结论的改进建议。"]}
            return tracker.complete({"reflection": reflection, "revision_round": revision_round, "draft": current_draft, "last_review_draft_hash": current_draft_hash, "last_review_issue_hash": issue_hash})
        return tracker.complete({"reflection": reflection, "revision_round": revision_round, "last_review_draft_hash": current_draft_hash, "last_review_issue_hash": issue_hash})
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
    """Apply the product's hard delivery contract in one place."""
    checks: list[DeliveryCheck] = []
    successful = [item for item in state.tool_results if item.get("status") == "success"]
    data_ready = bool(state.datasets) and bool(successful)
    checks.append(DeliveryCheck(code="data_ready", label="数据可用", passed=data_ready,
                                severity=Severity.ERROR if not data_ready else Severity.INFO,
                                message="已读取可分析数据和计算结果。" if data_ready else "没有可用的数据或计算结果。"))

    plan = state.plan or {}
    requirements_covered = bool(plan) and plan.get("can_execute", True) and not (state.intent or {}).get("route") == "clarification"
    checks.append(DeliveryCheck(code="requirements_covered", label="需求已覆盖", passed=requirements_covered,
                                severity=Severity.ERROR if not requirements_covered else Severity.INFO,
                                message="用户要求已映射到分析步骤。" if requirements_covered else "分析口径或必要字段仍需确认。"))

    planned_steps = {str(step.get("id")) for step in plan.get("steps", []) if isinstance(step, dict)}
    completed_steps = set(state.completed_step_ids)
    calculations_complete = bool(successful) and (not planned_steps or planned_steps <= completed_steps)
    checks.append(DeliveryCheck(code="calculations_complete", label="计算已完成", passed=calculations_complete,
                                severity=Severity.ERROR if not calculations_complete else Severity.INFO,
                                message="计划中的计算步骤已完成。" if calculations_complete else "仍有计算步骤未完成。"))

    evidence_traceable = validation.passed and all(
        item.evidence_refs and item.evidence_pointers for item in draft.insights if item.value is not None
    )
    checks.append(DeliveryCheck(code="evidence_traceable", label="证据可追溯", passed=evidence_traceable,
                                severity=Severity.ERROR if not evidence_traceable else Severity.INFO,
                                message="数字和结论均可追溯到证据或计算输入。" if evidence_traceable else "存在未通过证据校验的结论。"))

    requested_single = (
        (state.intent or {}).get("route") in {"derived_metric", "overdue_ranking"}
        or any(item.get("arguments", {}).get("strategy") == "overdue_ranking" for item in state.tool_results)
        or bool(re.search(r"只看|仅看|计算.*率|查询.*率", state.user_question))
    )
    required_insights = 1 if requested_single else 2
    enough_insights = len(draft.insights) >= required_insights
    checks.append(DeliveryCheck(code="new_information_present", label="产生新信息", passed=enough_insights,
                                severity=Severity.ERROR if not enough_insights else Severity.INFO,
                                message=f"已产生 {len(draft.insights)} 条分析结论，要求至少 {required_insights} 条。"))

    actions_executable = enough_insights and all(item.significance and item.action for item in draft.insights[:required_insights])
    checks.append(DeliveryCheck(code="actions_executable", label="结论可执行", passed=actions_executable,
                                severity=Severity.ERROR if not actions_executable else Severity.INFO,
                                message="重要结论均包含影响说明和建议。" if actions_executable else "重要结论缺少影响说明或执行建议。"))

    unresolved_errors = sum(1 for issue in validation.issues if issue.severity == Severity.ERROR)
    no_errors = unresolved_errors == 0
    checks.append(DeliveryCheck(code="no_unresolved_errors", label="无未解决错误", passed=no_errors,
                                severity=Severity.ERROR if not no_errors else Severity.INFO,
                                message="没有未解决的证据错误。" if no_errors else f"仍有 {unresolved_errors} 项证据或数字错误。"))

    if not data_ready or not requirements_covered:
        status = TaskStatus.NEEDS_CLARIFICATION.value
    elif not no_errors:
        status = TaskStatus.NEEDS_REVIEW.value
    elif not enough_insights or not actions_executable or not calculations_complete:
        status = TaskStatus.NEEDS_REVIEW.value
    elif draft.warnings:
        status = TaskStatus.COMPLETED_WITH_WARNINGS.value
    else:
        status = TaskStatus.COMPLETED.value
    return DeliveryGate(status=status, checks=checks,
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
    if state.revision_round >= settings.max_revision_rounds and state.reflection and state.reflection.get("verdict") != "pass" and validation.passed:
        warning = "自动复核已达到上限；数字和证据校验通过，结果按警告状态交付。"
        if warning not in draft.warnings:
            draft.warnings.append(warning)
        gate.status = TaskStatus.COMPLETED_WITH_WARNINGS.value
    final_status = TaskStatus(gate.status)
    if not state.is_replay:
        repository.add_message(state.task_id, "assistant", draft.summary)
    repository.update_task(
        state.task_id, status=final_status, progress=100,
        status_message="分析完成（有警告）" if draft.warnings else "分析完成",
        result=None if (state.intent or {}).get('route') in {'derived_metric', 'explanation'} else draft, clear_error=True,
    )
    if not state.is_replay:
        try:
            context_manager.record_completed_analysis(state.task_id, state.user_question, draft)
        except Exception:
            # Memory is an optimization. A successful, validated analysis must still be delivered.
            pass
    return {"draft": draft.model_dump(mode="json"), "final_status": final_status.value}


def _evidence_catalog(state: AnalysisState, *, row_limit: int = 100) -> list[dict[str, Any]]:
    allowed = _current_evidence_ids(state)
    return [
        {
            "id": item.id,
            "title": item.title,
            "source": item.source,
            "columns": item.columns,
            "row_count": len(item.rows),
            "rows": _sample_rows(item.rows, row_limit),
        }
        for item in repository.list_evidence_by_ids(state.task_id, allowed)
    ]


def _draft_context(
    state: AnalysisState,
    *,
    row_limit: int,
    include_previous: bool = True,
) -> dict[str, Any]:
    return {
        "user_question": state.user_question,
        "analysis_plan": state.plan,
        "previous_result": state.previous_result or {},
        "confirmed_policies": {},
        "knowledge_context": state.knowledge_context,
        "verified_results": _verified_result_catalog(state),
        "evidence_catalog": _evidence_catalog(state, row_limit=row_limit),
        "allowed_chart_types": ["line", "bar", "stacked_bar", "pie", "waterfall", "scatter", "table"],
        "previous_draft": _compact_draft(state.draft) if include_previous else None,
        "revision_feedback": _revision_feedback(state),
    }


def _reflection_context(
    state: AnalysisState,
    revision_round: int,
    *,
    row_limit: int,
) -> dict[str, Any]:
    return {
        "user_question": state.user_question,
        "analysis_plan": state.plan,
        "analysis_draft": _compact_draft(state.draft),
        "deterministic_validation": state.validation,
        "knowledge_context": state.knowledge_context,
        "evidence_catalog": _evidence_catalog(state, row_limit=row_limit),
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


def _sample_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    head = max(1, limit // 2)
    return [*rows[:head], *rows[-(limit - head):]]


def _revision_feedback(state: AnalysisState) -> dict[str, Any] | None:
    if state.reflection and state.reflection.get("verdict") == "revise":
        return {"review": state.reflection, "deterministic_validation": state.validation}
    return None


def _context_text(context: dict[str, Any] | None) -> str:
    return json.dumps(context, ensure_ascii=False, default=str) if context else ""


def _query_spec_from_arguments(
    arguments: dict[str, Any], datasets: list[DatasetInfo]
) -> QuerySpec:
    raw = arguments.get("query")
    if not isinstance(raw, dict):
        raise ToolError("结构化查询缺少 query 对象")
    normalized = dict(raw)
    order_by = normalized.get("order_by")
    if isinstance(order_by, str):
        match = re.match(r"^(.*?)(?:\s+(ASC|DESC))?$", order_by.strip(), flags=re.IGNORECASE)
        if match:
            normalized["order_by"] = match.group(1).strip()
            if match.group(2):
                normalized["descending"] = match.group(2).upper() == "DESC"
    dimensions = normalized.get("dimensions") or []
    measures = normalized.get("measures") or normalized.pop("metrics", []) or []
    aggregations = normalized.pop("aggregations", {})
    if not isinstance(dimensions, list) or not isinstance(measures, list):
        raise ToolError("查询维度和指标必须是列表")
    if isinstance(aggregations, dict):
        normalized["measures"] = [
            {
                "field": item,
                "aggregation": aggregations.get(item, "sum"),
            }
            if isinstance(item, str) else item
            for item in measures
        ]
    measure_order_aliases = {
        str(item.get("field")): str(item.get("alias"))
        for item in normalized.get("measures", [])
        if isinstance(item, dict) and item.get("field") and item.get("alias")
    }
    if normalized.get("order_by") in measure_order_aliases:
        normalized["order_by"] = measure_order_aliases[normalized["order_by"]]
    referenced_fields = {
        *[str(item) for item in dimensions],
        *[
            str(item.get("field")) if isinstance(item, dict) else str(item)
            for item in normalized.get("measures", [])
        ],
    }
    for item in normalized.get("filters") or []:
        if isinstance(item, dict) and item.get("field"):
            referenced_fields.add(str(item["field"]))
    if not normalized.get("dataset_id"):
        candidates = [
            dataset for dataset in datasets
            if referenced_fields <= {column.name for column in dataset.columns}
        ]
        if len(candidates) != 1:
            raise ToolError("查询未指定数据集，且字段不能唯一映射到一个数据集")
        normalized["dataset_id"] = candidates[0].id
    raw_limit = normalized.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else 100
    except (TypeError, ValueError):
        limit = 100
    normalized["limit"] = min(max(limit, 1), settings.max_query_rows)
    try:
        return QuerySpec.model_validate(normalized)
    except Exception as exc:
        raise ToolError(f"结构化查询参数无效：{exc}") from exc


def _is_sectioned_department_profit_request(question: str, dataset: DatasetInfo) -> bool:
    normalized = question.casefold()
    if not re.search(r"部门|department|team", normalized):
        return False
    if not re.search(r"盈亏|净利润|利润|profit", normalized):
        return False
    department = next((column for column in dataset.columns if column.name == "Department"), None)
    if department is None or not any(column.name == "Sum" for column in dataset.columns):
        return False
    samples = [str(value).strip().casefold() for value in department.sample_values]
    has_section_header = any(
        (" revenue" in value or " income" in value) and "(" in value
        for value in samples
    )
    has_net_profit = any(value == "net profit" for value in samples)
    return has_section_header and has_net_profit


def _is_sort_only_step(step: PlanStep) -> bool:
    purpose = re.sub(r"[\s，。；,;]", "", step.purpose)
    return bool(re.search(r"排序|升序|降序|从高到低|从低到高", purpose)) and not bool(
        re.search(r"同比|环比|趋势|异常|占比|贡献|预测|画像|质量", purpose)
    )


def _is_generic_analysis_request(question: str) -> bool:
    if _is_retry_analysis_request(question):
        return True
    normalized = re.sub(r"[\s，。！？,.!?、]", "", question).casefold()
    return normalized in {
        "分析", "分析数据", "数据分析", "帮我分析", "帮我分析数据", "分析一下",
        "分析一下数据", "看看数据", "帮我看看数据", "看一下数据", "分析报表",
        "分析表格", "分析报告", "帮我分析报表", "帮我分析表格",
    }


def _deterministic_strategy(state: AnalysisState) -> str | None:
    if _latest_strategy_result(state, "overdue_ranking"):
        return "overdue_ranking"
    route = (state.intent or {}).get("route")
    if route == "derived_metric":
        return "derived_metric"
    if any(item.get("arguments", {}).get("strategy") == "financial_report" for item in state.tool_results):
        return "financial_report"
    if _uses_sectioned_department_profit_result(state):
        return "sectioned_department_profit"
    if _is_generic_analysis_request(state.user_question):
        return "generic_overview"
    return None


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _normalize_plan_for_request(plan: AnalysisPlan, question: str) -> AnalysisPlan:
    if not plan.can_execute:
        return plan
    if any(step.id == 'financial_report' for step in plan.steps):
        return plan
    if not _is_generic_analysis_request(question):
        return plan
    source_steps = plan.steps[:3] or [PlanStep(
        id="overview", purpose="汇总主要金额指标并提取可复核证据", tool="auto_analyze"
    )]
    overview_steps = [step.model_copy(update={
        "id": "overview" if len(source_steps) == 1 else f"overview_{index + 1}",
        "purpose": "汇总主要金额指标并提取可复核证据",
        "tool": "auto_analyze",
    }) for index, step in enumerate(source_steps)]
    return AnalysisPlan(
        goal="概览主要金额指标及其业务分布",
        can_execute=True,
        metrics=["可识别的主要金额指标"],
        dimensions=["可用的首个业务分类维度"],
        filters=[],
        assumptions=["用户未指定具体指标和范围，默认对全部记录生成基础概览。"],
        steps=overview_steps,
        clarification_question=None,
    )


def _bind_plan_datasets(
    plan: AnalysisPlan,
    matches: list[DatasetMatch],
    confirmed_relationships: list[dict[str, Any]] | None = None,
) -> AnalysisPlan:
    if not matches:
        return plan
    by_id = {match.dataset.id: match.dataset.id for match in matches}
    by_name = {match.dataset.display_name.casefold(): match.dataset.id for match in matches}
    default_id = matches[0].dataset.id
    steps = []
    for step in plan.steps:
        requested = (step.dataset_id or "").strip()
        dataset_id = by_id.get(requested) or by_name.get(requested.casefold()) or default_id
        dataset_ids = [by_id.get(item) or by_name.get(str(item).casefold()) for item in step.dataset_ids]
        dataset_ids = [item for item in dataset_ids if item]
        joins = []
        confirmed = {
            (item.get("left_dataset_id"), item.get("left_field"), item.get("right_dataset_id"), item.get("right_field"))
            for item in (confirmed_relationships or [])
            if item.get("status") == "confirmed"
        }
        for join in step.joins:
            if (
                (dataset_id, join.left_field, join.right_dataset_id, join.right_field) in confirmed
                or any((join.right_dataset_id, join.right_field, dataset_id, join.left_field) == item for item in confirmed)
            ):
                joins.append(join)
        bound_ids = list(dict.fromkeys([*dataset_ids, *(item.right_dataset_id for item in joins)]))
        steps.append(step.model_copy(update={"dataset_id": dataset_id, "dataset_ids": bound_ids, "joins": joins}))
    return plan.model_copy(update={"steps": steps})


def _has_named_measure(dataset: DatasetInfo) -> bool:
    """Only consider real numeric fields for an automatic overview."""
    return any(
        column.role == "measure"
        and column.semantic_type not in {"date", "id"}
        and not is_placeholder_column_name(column.name)
        for column in dataset.columns
    )


def _normalize_optional_metric_changes(draft: AnalysisDraft, task_id: str) -> AnalysisDraft:
    """Drop optional comparison values that are not present in cited evidence."""
    normalized = draft.model_copy(deep=True)
    for metric in normalized.metrics:
        placeholder = metric.change and metric.change.strip().casefold() in {"null", "none", "n/a", "na", "-"}
        if metric.change and (placeholder or _unsupported_numbers(metric.change, metric.evidence_refs, task_id)):
            metric.change = None
            metric.direction = "neutral"
    for chart in normalized.charts:
        if chart.unit and chart.unit.strip().casefold() in {"null", "none", "n/a", "na", "-"}:
            chart.unit = None
    return normalized


def _finalize_draft(state: AnalysisState, draft: AnalysisDraft) -> AnalysisDraft:
    """Attach auditable calculations and apply deterministic chart policy."""
    normalized = _normalize_optional_metric_changes(draft, state.task_id)
    evidence_ids = _current_evidence_ids(state)
    evidence = repository.list_evidence_by_ids(state.task_id, evidence_ids)
    normalized = normalize_draft_charts(normalized, evidence, state.user_question)
    normalized.metrics = normalized.metrics[: settings.report_max_metrics]
    if not any(r.get('arguments', {}).get('strategy') == 'financial_report' for r in state.tool_results):
        normalized.findings = normalized.findings[: settings.report_max_findings]
    normalized.charts = normalized.charts[: settings.report_max_charts]
    normalized.suggested_questions = normalized.suggested_questions[: settings.report_max_suggested_questions]
    if _uses_sectioned_department_profit_result(state):
        assumption = (
            "部门盈亏按工作表层级结构取各 Revenue/Income 部门段内的 Net Profit 行，"
            "并使用 Sum 列作为汇总值。"
        )
        if assumption not in normalized.assumptions:
            normalized.assumptions.append(assumption)
    normalized.assumptions = normalized.assumptions[:10]
    normalized.warnings = normalized.warnings[:10]
    if not normalized.summary_evidence_refs:
        normalized.summary_evidence_refs = list(dict.fromkeys(
            evidence_id
            for item in [*normalized.metrics, *normalized.findings]
            for evidence_id in item.evidence_refs
        ))
    if not normalized.summary_evidence_pointers:
        normalized.summary_evidence_pointers = _resolve_evidence_pointers(
            normalized.summary, normalized.summary_evidence_refs, state.task_id
        )
    for metric in normalized.metrics:
        if not metric.evidence_pointers:
            metric.evidence_pointers = _resolve_evidence_pointers(
                f"{metric.label} {metric.value} {metric.change or ''}", metric.evidence_refs, state.task_id
            )
    for finding in normalized.findings:
        if not finding.evidence_pointers:
            finding.evidence_pointers = _resolve_evidence_pointers(
                f"{finding.title} {finding.detail}", finding.evidence_refs, state.task_id
            )
    normalized.verification_level = "cell"
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


def _uses_sectioned_department_profit_result(state: AnalysisState) -> bool:
    return any(
        item.get("status") == "success"
        and "regexp_matches(profit.label" in str((item.get("arguments") or {}).get("sql", ""))
        for item in state.tool_results
    )


def _compact_draft(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    if not draft:
        return None
    return {
        "title": draft.get("title"),
        "summary": draft.get("summary"),
        "summary_evidence_refs": draft.get("summary_evidence_refs", []),
        "metrics": [
            {key: item.get(key) for key in ("label", "value", "change", "direction", "evidence_refs")}
            for item in draft.get("metrics", [])
        ],
        "findings": [
            {key: item.get(key) for key in ("title", "detail", "severity", "evidence_refs")}
            for item in draft.get("findings", [])
        ],
        "charts": draft.get("charts", []),
        "assumptions": draft.get("assumptions", []),
        "warnings": draft.get("warnings", []),
        "suggested_questions": draft.get("suggested_questions", []),
    }


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


def _resolve_evidence_pointers(text: str, evidence_ids: list[str], task_id: str) -> list[EvidencePointer]:
    pointers: list[EvidencePointer] = []
    for token in _numeric_tokens(text):
        parsed = _as_decimal(token)
        if parsed is None:
            continue
        candidates: list[tuple[int, EvidencePointer]] = []
        for evidence_id in evidence_ids:
            evidence = repository.get_evidence(task_id, evidence_id)
            if evidence is None:
                continue
            for row_index, row in enumerate(evidence.rows):
                row_labels = [
                    str(value) for value in row.values()
                    if value not in (None, "") and _as_decimal(value) is None
                ]
                for field, value in row.items():
                    raw = _as_decimal(value)
                    textual_match = token in str(value) and str(value) in text
                    if raw is None and not textual_match:
                        continue
                    if raw is not None and not _numeric_values_match(token, parsed, raw, text):
                        continue
                    score = 0
                    if field.casefold() in text.casefold():
                        score += 3
                    if any(label.casefold() in text.casefold() for label in row_labels):
                        score += 2
                    candidates.append((score, EvidencePointer(
                        evidence_id=evidence_id,
                        row_index=row_index,
                        field=field,
                        raw_value=str(value),
                        unit="%" if token.endswith("%") else None,
                    )))
        if not candidates:
            continue
        best_score = max(score for score, _pointer in candidates)
        best = [pointer for score, pointer in candidates if score == best_score]
        if len(best) == 1 and best[0] not in pointers:
            pointers.append(best[0])
    return pointers


def _generic_overview_draft(state: AnalysisState) -> AnalysisDraft | None:
    results = [
        item for item in reversed(state.tool_results)
        if item.get("status") == "success" and item.get("rows") and item.get("evidence_ids")
    ]
    result = results[0] if results else None
    if result is None:
        return None
    rows = result["rows"]
    evidence_id = result["evidence_ids"][0]
    columns = list(rows[0])
    dimension = next(
        (name for name in columns if not _is_numeric_value(rows[0].get(name))),
        columns[0],
    )
    measure_candidates = [
        name
        for name in columns
        if name != dimension
        and _is_numeric_value(rows[0].get(name))
        and not any(token in name for token in ("记录数", "分组数", "占比", "贡献百分比", "排名", "阈值"))
    ]
    measure_priorities = ("净利润", "net profit", "利润", "profit", "金额", "amount", "收入", "revenue", "成本", "cost", "费用", "expense")
    measure = next(
        (
            name
            for token in measure_priorities
            for name in measure_candidates
            if token in name.casefold()
        ),
        None,
    )
    if measure is None:
        measure = measure_candidates[0] if measure_candidates else None
    if measure is None:
        return None

    numeric_values = [
        value for row in rows
        if (value := _numeric_float(row.get(measure))) is not None
    ]
    total_field = next((name for name in columns if name in {"范围合计", "总计", "合计"}), None)
    # A calculated sum is not an original evidence cell.  Do not present it as
    # a cell-backed metric; the report can still show the grouped values.
    total_value = _numeric_float(rows[0].get(total_field)) if total_field else None
    negative_count = sum(1 for value in numeric_values if value < 0)

    findings: list[Finding] = []
    for row_index, row in enumerate(rows[:5]):
        category = str(row.get(dimension, "未分类"))
        value = str(row.get(measure))
        percentage_name = next((name for name in columns if "占比" in name or "贡献百分比" in name), None)
        percentage = row.get(percentage_name) if percentage_name else None
        detail = f"{dimension}为{category}时，{measure}为{value}"
        if percentage is not None:
            detail += f"，绝对金额贡献为{percentage}%"
        pointers = [EvidencePointer(
            evidence_id=evidence_id, row_index=row_index,
            field=measure, raw_value=str(row.get(measure)),
        )]
        if percentage_name and percentage is not None:
            pointers.append(EvidencePointer(
                evidence_id=evidence_id, row_index=row_index,
                field=percentage_name, raw_value=str(percentage), unit="%",
            ))
        findings.append(Finding(
            title=f"{category}的{measure}", detail=detail + "。",
            evidence_refs=[evidence_id], evidence_pointers=pointers,
        ))

    first = rows[0]
    metric_pointers = [EvidencePointer(
        evidence_id=evidence_id, row_index=0,
        field=measure, raw_value=str(first.get(measure)),
    )]
    metrics = [Metric(
        label=f"首项{measure}",
        value=str(first.get(measure)),
        change=None,
        direction="neutral",
        evidence_refs=[evidence_id],
        evidence_pointers=metric_pointers,
    )]
    if total_value is not None:
        metrics.append(Metric(
            label=f"{measure}合计",
            value=_format_number(total_value),
            change=None,
            direction="neutral",
            evidence_refs=[evidence_id],
            evidence_pointers=[EvidencePointer(
                evidence_id=evidence_id, row_index=0,
                field=total_field or measure, raw_value=str(rows[0].get(total_field)) if total_field else _format_number(total_value),
            )],
        ))
    metrics.append(Metric(
        label=f"{dimension}分组数",
        value=str(rows[0]["结果分组数"]) if "结果分组数" in rows[0] else f"{len(rows)}个分组",
        change=None,
        direction="neutral",
        evidence_refs=[evidence_id],
        evidence_pointers=[EvidencePointer(
            evidence_id=evidence_id, row_index=0,
            field="结果分组数", raw_value=str(rows[0]["结果分组数"]),
        )] if "结果分组数" in rows[0] else [],
    ))
    draft = AnalysisDraft(
        title=f"{measure}数据概览",
        summary=(
            f"已按{dimension}汇总{measure}，共覆盖{len(rows)}个分组；"
            f"合计为{_format_number(total_value) if total_value is not None else '无法计算'}，"
            "结果按工具返回顺序展示，并保留可追溯证据。"
        ),
        metrics=metrics,
        findings=findings,
        charts=[
            ChartSpec(
                id="generic_overview",
                title=f"{measure}按{dimension}分布",
                chart_type="bar",
                dataset_ref=evidence_id,
                category_field=dimension,
                series=[ChartSeries(name=measure, field=measure)],
                unit=None,
            )
        ],
        assumptions=["用户未指定具体指标和范围，本次按全部记录生成基础概览。"],
        warnings=[
            "这是通用概览；指定指标、期间或部门后可获得更有针对性的结论。",
            *(["发现负值分组，建议进一步核查。"] if negative_count else []),
        ],
        suggested_questions=[f"按月份查看{measure}趋势", f"检查{measure}异常项目"],
    )
    # Keep one report while retaining a compact, auditable slice from each
    # independently analyzed table. This avoids silently dropping related sheets.
    seen_titles = {item.title for item in draft.findings}
    seen_charts = {item.id for item in draft.charts}
    for extra in results[1:3]:
        extra_rows = extra.get("rows") or []
        extra_evidence = (extra.get("evidence_ids") or [None])[0]
        if not extra_rows or not extra_evidence:
            continue
        extra_columns = list(extra_rows[0])
        extra_dimension = next((name for name in extra_columns if not _is_numeric_value(extra_rows[0].get(name))), extra_columns[0])
        extra_measure = next(
            (name for name in extra_columns if name != extra_dimension and _is_numeric_value(extra_rows[0].get(name)) and name not in {"记录数", "范围合计"}),
            None,
        )
        if not extra_measure:
            continue
        extra_total = _numeric_float(extra_rows[0].get("范围合计"))
        if extra_total is None:
            continue
        draft.metrics.append(Metric(
            label=f"{extra_measure}合计（{extra_dimension}）",
            value=_format_number(extra_total),
            evidence_refs=[extra_evidence],
            evidence_pointers=[EvidencePointer(evidence_id=extra_evidence, row_index=0, field="范围合计" if "范围合计" in extra_columns else extra_measure, raw_value=str(extra_rows[0].get("范围合计" if "范围合计" in extra_columns else extra_measure)))],
        ))
        title = f"{extra_rows[0].get(extra_dimension, '未分类')}的{extra_measure}（{extra_dimension}）"
        if title not in seen_titles:
            draft.findings.append(Finding(
                title=title,
                detail=f"{extra_dimension}为{extra_rows[0].get(extra_dimension, '未分类')}时，{extra_measure}为{extra_rows[0].get(extra_measure)}。",
                evidence_refs=[extra_evidence],
                evidence_pointers=[EvidencePointer(evidence_id=extra_evidence, row_index=0, field=extra_measure, raw_value=str(extra_rows[0].get(extra_measure)))],
            ))
            seen_titles.add(title)
        chart_id = f"generic_overview_{extra_evidence}"
        if chart_id not in seen_charts:
            draft.charts.append(ChartSpec(
                id=chart_id,
                title=f"{extra_measure}按{extra_dimension}分布",
                chart_type="bar",
                dataset_ref=extra_evidence,
                category_field=extra_dimension,
                series=[ChartSeries(name=extra_measure, field=extra_measure)],
            ))
    return draft


def _department_profit_draft(state: AnalysisState) -> AnalysisDraft | None:
    result = next(
        (
            item
            for item in reversed(state.tool_results)
            if item.get("status") == "success"
            and item.get("rows")
            and item.get("evidence_ids")
            and "regexp_matches(profit.label" in str((item.get("arguments") or {}).get("sql", ""))
        ),
        None,
    )
    if result is None:
        return None

    rows = result["rows"]
    evidence_id = result["evidence_ids"][0]
    values = [
        (index, row, _as_decimal(row.get("净利润")))
        for index, row in enumerate(rows)
    ]
    values = [(index, row, value) for index, row, value in values if value is not None]
    if not values:
        return None

    highest = max(values, key=lambda item: item[2])
    lowest = min(values, key=lambda item: item[2])
    losses = [item for item in values if item[2] < 0]

    def pointer(item: tuple[int, dict[str, Any], Any]) -> EvidencePointer:
        index, row, _value = item
        return EvidencePointer(
            evidence_id=evidence_id,
            row_index=index,
            field="净利润",
            raw_value=str(row.get("净利润")),
        )

    findings = [
        Finding(
            title="净利润最高部门",
            detail=f"{highest[1].get('部门')}净利润为{highest[1].get('净利润')}。",
            evidence_refs=[evidence_id],
            evidence_pointers=[pointer(highest)],
        )
    ]
    findings.extend(
        Finding(
            title=f"{item[1].get('部门')}出现亏损",
            detail=f"该部门净利润为{item[1].get('净利润')}。",
            severity=Severity.WARNING,
            evidence_refs=[evidence_id],
            evidence_pointers=[pointer(item)],
        )
        for item in losses
    )

    return AnalysisDraft(
        title="各部门净利润分析",
        summary="已按部门汇总净利润并识别盈利与亏损部门，结果按净利润从高到低展示。",
        summary_evidence_refs=[evidence_id],
        metrics=[
            Metric(
                label=f"最高净利润（{highest[1].get('部门')}）",
                value=str(highest[1].get("净利润")),
                direction="up",
                evidence_refs=[evidence_id],
                evidence_pointers=[pointer(highest)],
            ),
            Metric(
                label=f"最低净利润（{lowest[1].get('部门')}）",
                value=str(lowest[1].get("净利润")),
                direction="down",
                evidence_refs=[evidence_id],
                evidence_pointers=[pointer(lowest)],
            ),
        ],
        findings=findings,
        charts=[ChartSpec(
            id="department_profit",
            title="各部门净利润对比",
            chart_type="bar",
            dataset_ref=evidence_id,
            category_field="部门",
            series=[ChartSeries(name="净利润", field="净利润")],
            orientation="vertical",
            sort_order="value_desc",
        )],
        warnings=(
            ["净利润为负的部门需要进一步核查收入、成本和费用构成。"]
            if losses
            else []
        ),
        suggested_questions=["查看亏损部门的月度变化", "拆分亏损部门的收入与成本构成"],
    )


def _is_numeric_value(value: Any) -> bool:
    return _as_decimal(value) is not None


def _numeric_float(value: Any) -> float | None:
    parsed = _as_decimal(value)
    return float(parsed) if parsed is not None else None


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.12g}"


def prepare_context_node(state: AnalysisState) -> dict[str, Any]:
    if state.context_prepared or (state.intent or {}).get('route') in {'derived_metric', 'clarification'}:
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
    knowledge = [item.model_dump(mode='json') for item in retrieve_for_run(state.run_id, state.user_question)]
    log_event(logger, 'workflow.context.prepared', task_id=state.task_id, run_id=state.run_id,
              duration_ms=duration_ms(started_at), knowledge_match_count=len(knowledge))
    return {'conversation_summary': conversation.model_dump(mode='json'),
            'knowledge_context': knowledge, 'context_prepared': True}


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
    # Deterministic validation is the normal quality gate.  The historical
    # reflection node remains callable for compatibility, but is no longer on
    # the default graph and cannot create a retry loop.
    builder.add_edge("draft", "validate")
    builder.add_conditional_edges("validate", route_validation_impl, {"finish": "finish", "reflect": "reflect"})
    builder.add_edge("finish", END)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
    atexit.register(connection.close)
    return builder.compile(checkpointer=SqliteSaver(connection))


graph = build_graph()


def _should_auto_activate(result: dict[str, Any], final_status: str) -> bool:
    return final_status in {TaskStatus.COMPLETED.value, TaskStatus.COMPLETED_WITH_WARNINGS.value} and (
        (result.get('intent') or {}).get('route') not in {'derived_metric', 'explanation'}
    )


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
            conversation_summary=conversation.model_dump(mode="json"),
            conversation_messages=[item.model_dump(mode="json") for item in messages],
            previous_result=snapshot.result.model_dump(mode="json") if snapshot.result else None,
            datasets=[item.model_dump(mode="json") for item in snapshot.datasets],
            confirmed_relationships=[item.model_dump(mode="json") for item in snapshot.relationships if item.status == "confirmed"],
        )
        result = graph.invoke(initial, config={"configurable": {"thread_id": run_id}})
        final_status = str(result.get("final_status") or repository.get_task(task_id).status.value)
        has_draft = final_status in {
            TaskStatus.COMPLETED.value,
            TaskStatus.COMPLETED_WITH_WARNINGS.value,
            TaskStatus.NEEDS_REVIEW.value,
        }
        draft = AnalysisDraft.model_validate(result["draft"]) if has_draft else None
        activate = _should_auto_activate(result, final_status)
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
            message = '本次请求超出模型上下文容量，请缩短问题或缩小分析范围。原报告未受影响。'
        else:
            message = '本次请求的模型输出未能可靠解析，请明确问题、缩小分析范围或稍后重试。'
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
