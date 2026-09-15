from unittest.mock import MagicMock, Mock

import pytest

from app import workflow as w
from app.models import AnalysisDraft, AnalysisPlan, AnalysisState, DatasetInfo, PlanStep, ToolExecutionResult


@pytest.fixture
def isolated(monkeypatch):
    tracker = Mock()
    tracker.prompt.content = "test prompt"
    tracker.complete.side_effect = lambda output: output
    monkeypatch.setattr(w, "start_node", lambda *args: tracker)
    repo = MagicMock()
    monkeypatch.setattr(w, "repository", repo)
    a = DatasetInfo(id="a", file_id="f", table_name="a", display_name="A", row_count=1, columns=[])
    b = a.model_copy(update={"id": "b", "table_name": "b", "display_name": "B"})
    return AnalysisState(task_id="task", run_id="run", user_question="分析数据",
                         datasets=[a.model_dump(), b.model_dump()]), repo


def test_missing_measure_preserves_clarification(isolated, monkeypatch):
    state, _ = isolated
    monkeypatch.setattr(w.llm, "structured", lambda *a, **k: w.PlanDecision(
        action="clarify", goal="确认分析字段", clarification="请指定可分析的字段",
        clarification_options=[], steps=[]))
    output = w.plan_node(state)
    assert not output["plan"]["can_execute"]
    assert output["plan"]["clarification_question"]


def test_planner_receives_complete_schema_without_tool_contracts(isolated, monkeypatch):
    state, _ = isolated
    planner = Mock(return_value=w.PlanDecision(
        action="clarify", goal="scope", clarification="scope?", clarification_options=[], steps=[]))
    monkeypatch.setattr(w.llm, "structured", planner)
    w.plan_node(state)
    context = planner.call_args.args[1]
    assert "tool_descriptions" not in context and "available_tools" not in context
    assert [item["table_name"] for item in context["dataset_catalog"]] == ["a", "b"]


def test_multiple_sql_steps_are_executed_in_order(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal="rankings", can_execute=True, steps=[
        PlanStep(id="query_a", purpose="A画像", dataset_ids=["a"], sql="SELECT * FROM a"),
        PlanStep(id="query_b", purpose="B画像", dataset_ids=["b"], sql="SELECT * FROM b"),
    ]).model_dump()
    execute = Mock(return_value=ToolExecutionResult(
        result_id="r", tool_name="execute_sql", status="success", summary="queried"))
    monkeypatch.setattr(w, "execute_sql", execute)
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ["query_a"]
    assert w.route_execute(state) == "execute"
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ["query_a", "query_b"]
    assert execute.call_count == 2


def test_executor_passes_all_selected_datasets_and_raw_sql(isolated, monkeypatch):
    state, _ = isolated
    sql = "SELECT * FROM b UNION ALL SELECT * FROM a"
    state.plan = AnalysisPlan(goal="reports", can_execute=True, steps=[
        PlanStep(id="reports", purpose="reports", dataset_ids=["b", "a"], sql=sql),
    ]).model_dump()
    tool = Mock(return_value=ToolExecutionResult(
        result_id="r", tool_name="execute_sql", status="success", summary="ok"))
    monkeypatch.setattr(w, "execute_sql", tool)
    output = w.execute_node(state)
    assert [dataset.id for dataset in tool.call_args.args[1]] == ["b", "a"]
    assert tool.call_args.args[2] == sql
    assert output["completed_step_ids"] == ["reports"]


def test_executor_rejects_unknown_dataset(isolated):
    state, _ = isolated
    state.plan = AnalysisPlan(goal="reports", can_execute=True, steps=[
        PlanStep(id="reports", purpose="reports", dataset_ids=["missing"], sql="SELECT * FROM missing"),
    ]).model_dump()
    with pytest.raises(w.ToolError, match="不存在的数据表"):
        w.execute_node(state)


def test_query_failure_returns_to_planner_and_repaired_sql_executes(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal="rank", can_execute=True, steps=[
        PlanStep(id="query", purpose="rank", dataset_ids=["a"], sql="SELECT missing FROM a"),
    ]).model_dump()
    query = Mock(side_effect=[w.ToolError("unknown column"), ToolExecutionResult(
        result_id="r", tool_name="execute_sql", status="success", summary="ok")])
    monkeypatch.setattr(w, "execute_sql", query)
    state = state.model_copy(update=w.execute_node(state))
    assert state.query_failures[0]["arguments"]["sql"] == "SELECT missing FROM a"
    assert w.route_after_execute(state) == "plan"
    state.plan = AnalysisPlan(goal="rank", can_execute=True, steps=[
        PlanStep(id="query_repair", purpose="rank", dataset_ids=["a"], sql="SELECT * FROM a"),
    ]).model_dump()
    state = state.model_copy(update=w.execute_node(state))
    assert query.call_args.args[2] == "SELECT * FROM a"
    assert state.completed_step_ids == ["query_repair"]
    assert state.query_failures == []


def test_query_repair_stops_after_two_retries(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal="rank", can_execute=True, steps=[
        PlanStep(id="query", purpose="rank", dataset_ids=["a"], sql="SELECT missing FROM a"),
    ]).model_dump()
    query = Mock(side_effect=w.ToolError("invalid query"))
    monkeypatch.setattr(w, "execute_sql", query)
    for _ in range(3):
        state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ["query"]
    assert state.tool_results[-1]["status"] == "error"
    assert query.call_count == 3


def test_delivery_gate_accepts_model_selected_report(isolated):
    state, _ = isolated
    state = state.model_copy(update={
        "plan": AnalysisPlan(goal="汇总", can_execute=True, steps=[]).model_dump(),
        "validation": {"passed": True, "issues": [], "checked_evidence_ids": []},
        "draft": AnalysisDraft(summary="模型回答").model_dump(),
        "reflection": {"verdict": "pass", "route": "finish", "reason": "模型认可"},
    })
    gate = w._delivery_gate(state, AnalysisDraft.model_validate(state.draft),
                            w.ValidationReport.model_validate(state.validation))
    assert gate.status == "completed"


@pytest.mark.parametrize("question", ["分析数据", "分析一下", "重新分析", "继续分析"])
def test_generic_requests_use_model_sql_plan(isolated, monkeypatch, question):
    state, _ = isolated
    state.user_question = question
    decision = w.PlanDecision(action="analyze", goal="模型选择的数据画像", clarification=None,
        clarification_options=[], steps=[
            PlanStep(id="model_step", purpose="查看B字段分布", dataset_ids=["b"], sql="SELECT * FROM b")])
    model = Mock(return_value=decision)
    monkeypatch.setattr(w.llm, "structured", model)
    output = w.plan_node(state)
    assert output["plan"]["steps"] == [step.model_dump(mode="json") for step in decision.steps]
