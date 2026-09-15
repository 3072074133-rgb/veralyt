from app.evidence_validation import current_evidence_ids
from app.models import (
    AnalysisDraft, AnalysisState, ConvergenceDecision, ReflectionDecision,
    ValidationIssue, ValidationReport,
)
from app.workflow import _delivery_gate, _reflection_context, referenced_evidence_ids
from app.workflow_nodes.routing import route_reflection


def test_previous_report_evidence_is_available_in_followup() -> None:
    state = AnalysisState(
        task_id="task", run_id="run", user_question="修改报告",
        inherited_evidence_ids=["old_evidence"],
        tool_results=[{"status": "success", "evidence_ids": ["new_evidence"]}],
    )
    assert current_evidence_ids(state) == {"old_evidence", "new_evidence"}


def test_referenced_evidence_ids_collects_complete_previous_report() -> None:
    report = {
        "summary_evidence_refs": ["summary"],
        "metrics": [{"evidence_refs": ["metric"]}],
        "insights": [{"evidence_pointers": [{"evidence_id": "insight"}]}],
        "charts": [{"dataset_ref": "chart"}],
    }
    assert referenced_evidence_ids(report) == {"summary", "metric", "insight", "chart"}


def test_model_approval_is_the_only_report_delivery_gate() -> None:
    state = AnalysisState(
        task_id="task", run_id="run", user_question="生成报告",
        reflection=ReflectionDecision(verdict="pass", route="finish", reason="模型认可").model_dump(),
    )
    validation = ValidationReport(passed=False, issues=[ValidationIssue(
        code="missing_evidence", message="后端无法定位引用", severity="error",
    )])
    gate = _delivery_gate(state, AnalysisDraft(summary="模型认可的报告"), validation)
    assert gate.status == "completed"
    assert gate.unresolved_error_count == 0
    assert [check.code for check in gate.checks] == ["model_review_accepted"]


def test_model_can_approve_or_ask_user_after_revision_budget() -> None:
    approved = AnalysisState(
        task_id="task", run_id="run", user_question="q", revision_round=99,
        reflection={"verdict": "pass", "route": "finish", "reason": "正确"},
    )
    assert route_reflection(approved) == "finish"
    question = approved.model_copy(update={
        "reflection": {"verdict": "revise", "route": "ask_user", "reason": "需要确认口径"},
    })
    assert route_reflection(question) == "ask_user"


def test_no_progress_decision_remains_a_model_choice() -> None:
    assert set(ConvergenceDecision.model_json_schema()["properties"]["action"]["enum"]) == {
        "draft", "ask_user", "answer",
    }


def test_final_review_reassesses_current_draft_without_stale_review() -> None:
    state = AnalysisState(
        task_id="task", run_id="run", user_question="修改报告",
        draft={"summary": "已经修订"},
        reflection={"verdict": "revise", "route": "rewrite", "reason": "旧意见"},
        decision_history=[{"action": "draft", "reason": "数据足够"}],
    )
    context = _reflection_context(state, 2)
    assert context["analysis_draft"] == {"summary": "已经修订"}
    assert "previous_review" not in context
    assert "decision_history" not in context


def test_final_review_uses_summary_query_results_without_raw_rows() -> None:
    state = AnalysisState(
        task_id="task", run_id="run", user_question="修改报告",
        tool_results=[{
            "result_id": "r1", "tool_name": "execute_sql", "status": "success",
            "rows": [{"amount": "100"}], "evidence_ids": ["ev1"],
        }],
    )
    context = _reflection_context(state, 1)
    assert context["query_results"][0]["result_id"] == "r1"
    assert "rows" not in context["query_results"][0]
