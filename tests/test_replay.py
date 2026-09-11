from pathlib import Path

import pytest

from app.llm import LLMStructuredOutputError
from app.models import (
    AnalysisDraft,
    AnalysisPlan,
    AnalysisState,
    DatasetColumn,
    DatasetInfo,
    Finding,
    IntentDecision,
    PlanStep,
    TaskStatus,
)
from app.repository import repository
from app.workflow import classify_node, plan_node, run_replay


@pytest.fixture()
def replay_repo(tmp_path: Path):
    old_path = repository.db_path
    repository.db_path = tmp_path / "replay.sqlite"
    repository.initialize()
    task_id = repository.create_task()
    yield task_id
    repository.db_path = old_path


def _draft(summary: str) -> AnalysisDraft:
    return AnalysisDraft(summary=summary, findings=[Finding(detail=summary, evidence_refs=[])])


def _source_execution(task_id: str) -> tuple[str, str]:
    run_id = repository.start_execution(task_id, "分析收入")
    prompt = repository.ensure_prompt_version("draft", "1.0.0", "你是草稿节点。请严格返回结构化分析结果。")
    state = AnalysisState(
        task_id=task_id,
        run_id=run_id,
        user_question="分析收入",
        entry_node="classify",
        datasets=[],
        intent={"is_analysis": True, "reason": "分析请求", "confidence": 1},
        plan={"goal": "分析收入", "can_execute": True},
        tool_results=[],
    )
    execution_id = repository.start_node_execution(
        run_id,
        "draft",
        state.model_dump(mode="json"),
        prompt.id,
        {"model": "qwen3.5:4b"},
        state.schema_version,
    )
    repository.finish_node_execution(execution_id, {"draft": _draft("原结果").model_dump(mode="json")})
    repository.finish_execution(run_id, "completed", result=_draft("原结果"), activate=True)
    return run_id, execution_id


def test_llm_node_records_prompt_and_state(replay_repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = repository.start_execution(replay_repo, "分析收入")
    state = AnalysisState(task_id=replay_repo, run_id=run_id, user_question="分析收入")

    def structured(*args, **kwargs):
        assert kwargs["prompt_override"]
        return IntentDecision(is_analysis=True, reason="分析请求", confidence=1)

    monkeypatch.setattr("app.workflow.llm.structured", structured)
    output = classify_node(state)

    nodes = repository.list_node_executions(replay_repo, run_id)
    detail = repository.get_node_execution(replay_repo, nodes[0].id)
    assert output["intent"]["route"] == 'analysis'
    assert nodes[0].node_name == "classify"
    assert nodes[0].prompt_version.content_hash
    assert detail.input_state["user_question"] == "分析收入"
    assert detail.output_state["intent"]["route"] == 'analysis'


def test_retry_intent_skips_llm_and_restores_previous_question(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.workflow.settings.model_intent_enabled', False)
    run_id = repository.start_execution(replay_repo, "重新分析")
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="重新分析",
        datasets=[{"id": "dataset-1"}],
        conversation_summary={
            "recent_messages": [
                {"role": "user", "content": "分析报表，统计各部门盈亏"},
                {"role": "assistant", "content": "已完成分析"},
            ]
        },
    )

    def unexpected_call(*args, **kwargs):
        raise AssertionError("retry intent should not call the model")

    monkeypatch.setattr("app.workflow.llm.structured", unexpected_call)
    output = classify_node(state)

    assert output["intent"]["route"] == 'analysis'
    assert output["user_question"] == "分析报表，统计各部门盈亏"
    detail = repository.get_node_execution(replay_repo, repository.list_node_executions(replay_repo, run_id)[0].id)
    assert detail.diagnostics["classification_source"] == "deterministic_retry"


def test_clear_analysis_request_skips_model_classification(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.workflow.settings.model_intent_enabled', False)
    run_id = repository.start_execution(replay_repo, "分析报表，统计各部门盈亏")
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="分析报表，统计各部门盈亏",
        datasets=[{"id": "dataset-1"}],
    )

    def unexpected_call(*args, **kwargs):
        raise AssertionError("clear analysis intent should not call the model")

    monkeypatch.setattr("app.workflow.llm.structured", unexpected_call)
    output = classify_node(state)

    assert output["intent"]["route"] == 'analysis'
    detail = repository.get_node_execution(replay_repo, repository.list_node_executions(replay_repo, run_id)[0].id)
    assert detail.diagnostics["classification_source"] == "deterministic_analysis"


def test_generic_request_restores_specific_goal_from_memory(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('app.workflow.settings.model_intent_enabled', False)
    run_id = repository.start_execution(replay_repo, "分析报表")
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="分析报表",
        datasets=[{"id": "dataset-1"}],
        conversation_summary={
            "memory": {
                "task_goal": "分析报表，统计各部门盈亏",
                "completed_analyses": [
                    {"question": "分析报表，统计各部门盈亏"},
                ],
            },
            "recent_messages": [
                {"role": "user", "content": "分析报表"},
            ],
        },
    )

    monkeypatch.setattr(
        "app.workflow.llm.structured",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not be called")),
    )
    output = classify_node(state)

    assert output["user_question"] == "分析报表，统计各部门盈亏"


def test_generic_report_request_uses_local_plan_without_model(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = repository.start_execution(replay_repo, "分析报表")
    dataset = DatasetInfo(
        id="dataset-1",
        file_id="file-1",
        table_name="data_1",
        display_name="经营报表",
        row_count=2,
        columns=[DatasetColumn(name="Amount", display_name="金额", data_type="Float64", null_count=0)],
    )
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="分析报表",
        datasets=[dataset.model_dump(mode="json")],
    )

    def unexpected_call(*args, **kwargs):
        raise AssertionError("generic overview should not call the planner model")

    monkeypatch.setattr("app.workflow.llm.structured", unexpected_call)
    output = plan_node(state)

    plan = AnalysisPlan.model_validate(output["plan"])
    assert plan.steps == [
        PlanStep(
            id="overview",
            purpose="汇总主要金额指标并提取可复核证据",
            tool="auto_analyze",
            dataset_id="dataset-1",
        )
    ]
    detail = repository.get_node_execution(replay_repo, repository.list_node_executions(replay_repo, run_id)[0].id)
    assert detail.diagnostics["planning_source"] == "deterministic_overview"


def test_sectioned_department_profit_uses_local_plan_without_model(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = repository.start_execution(replay_repo, "分析报表，统计各部门盈亏")
    dataset = DatasetInfo(
        id="dataset-1",
        file_id="file-1",
        table_name="data_1",
        display_name="KZE Total",
        row_count=5,
        columns=[
            DatasetColumn(
                name="Department",
                display_name="Department",
                data_type="String",
                null_count=0,
                sample_values=["Ebiz Revenue (Kimmy&Carl)", "Gross Profit", "Net Profit"],
            ),
            DatasetColumn(name="Sum", display_name="Sum", data_type="Float64", null_count=0),
        ],
    )
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="分析报表，统计各部门盈亏",
        datasets=[dataset.model_dump(mode="json")],
    )

    monkeypatch.setattr(
        "app.workflow.llm.structured",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner model should not be called")),
    )
    output = plan_node(state)

    plan = AnalysisPlan.model_validate(output["plan"])
    assert plan.steps[0].tool == "query_data"
    assert plan.steps[0].dataset_id == "dataset-1"
    detail = repository.get_node_execution(replay_repo, repository.list_node_executions(replay_repo, run_id)[0].id)
    assert detail.diagnostics["planning_source"] == "deterministic_department_profit"


def test_specific_planner_disables_hidden_thinking(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = repository.start_execution(replay_repo, "按部门统计收入")
    dataset = DatasetInfo(
        id="dataset-1",
        file_id="file-1",
        table_name="data_1",
        display_name="经营报表",
        row_count=2,
        columns=[
            DatasetColumn(name="Department", display_name="部门", data_type="String", null_count=0),
            DatasetColumn(name="Revenue", display_name="收入", data_type="Float64", null_count=0),
        ],
    )
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="按部门统计收入",
        datasets=[dataset.model_dump(mode="json")],
    )

    def structured(*args, **kwargs):
        assert kwargs["thinking"] is False
        return AnalysisPlan(
            goal="按部门统计收入",
            can_execute=True,
            steps=[PlanStep(id="income", purpose="统计部门收入", tool="query_data")],
        )

    monkeypatch.setattr("app.workflow.llm.structured", structured)
    output = plan_node(state)

    assert output["plan"]["steps"][0]["dataset_id"] == "dataset-1"


def test_ambiguous_request_still_rejects_invalid_model_structure(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = repository.start_execution(replay_repo, "你好")
    state = AnalysisState(
        task_id=replay_repo,
        run_id=run_id,
        user_question="你好",
        datasets=[{"id": "dataset-1"}],
    )

    monkeypatch.setattr(
        "app.workflow.llm.structured",
        lambda *args, **kwargs: (_ for _ in ()).throw(LLMStructuredOutputError("invalid IntentDecision")),
    )

    result = classify_node(state)
    assert result['intent']['route'] == 'clarification'


def test_replay_starts_from_snapshot_and_activates_new_branch(
    replay_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_run_id, execution_id = _source_execution(replay_repo)
    new_prompt = repository.ensure_prompt_version(
        "draft",
        "custom-1",
        "你是修改后的草稿节点。输出应更简洁并严格引用证据。",
    )
    replay_run_id = repository.start_execution(
        replay_repo,
        "分析收入",
        status="queued",
        parent_run_id=source_run_id,
        forked_from_node_execution_id=execution_id,
        entry_node="draft",
        prompt_version_id=new_prompt.id,
    )
    captured: dict = {}

    def invoke(state, config):
        captured["state"] = state
        captured["config"] = config
        return {"final_status": "completed", "draft": _draft("新结果").model_dump(mode="json")}

    monkeypatch.setattr("app.workflow.graph.invoke", invoke)
    run_replay(replay_run_id)

    replay = repository.get_run(replay_repo, replay_run_id)
    source = repository.get_run(replay_repo, source_run_id)
    assert captured["state"].entry_node == "draft"
    assert captured["state"].is_replay is True
    assert captured["state"].prompt_versions["draft"] == new_prompt.id
    assert captured["config"]["configurable"]["thread_id"] == replay_run_id
    assert replay.status == "completed" and replay.is_active is True
    assert source.is_active is False
    assert repository.get_task(replay_repo).result.summary == "新结果"


def test_failed_replay_preserves_active_result(replay_repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    source_run_id, execution_id = _source_execution(replay_repo)
    new_prompt = repository.ensure_prompt_version("draft", "custom-1", "这是一个会导致失败但长度足够的测试提示词内容。")
    replay_run_id = repository.start_execution(
        replay_repo,
        "分析收入",
        status="queued",
        parent_run_id=source_run_id,
        forked_from_node_execution_id=execution_id,
        entry_node="draft",
        prompt_version_id=new_prompt.id,
    )

    def fail(*args, **kwargs):
        raise RuntimeError("模型失败")

    monkeypatch.setattr("app.workflow.graph.invoke", fail)
    run_replay(replay_run_id)

    assert repository.get_run(replay_repo, replay_run_id).status == "failed"
    assert repository.get_run(replay_repo, source_run_id).is_active is True
    snapshot = repository.get_task(replay_repo)
    assert snapshot.status == TaskStatus.COMPLETED
    assert snapshot.result.summary == "原结果"
