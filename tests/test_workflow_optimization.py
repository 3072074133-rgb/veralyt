from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app import workflow as w
from app.models import AnalysisDraft, AnalysisPlan, AnalysisState, DatasetInfo, PlanStep, ToolExecutionResult


@pytest.fixture
def isolated(monkeypatch):
    tracker = Mock()
    tracker.complete.side_effect = lambda output: output
    monkeypatch.setattr(w, 'begin_node', lambda *args: tracker)
    repo = MagicMock()
    repo.assert_run_context_current = Mock()
    monkeypatch.setattr(w, 'repository', repo)
    a = DatasetInfo(id='a', file_id='f', table_name='a', display_name='A', row_count=1, columns=[])
    b = a.model_copy(update={'id': 'b', 'table_name': 'b', 'display_name': 'B'})
    return AnalysisState(task_id='task', run_id='run', user_question='分析数据',
                         datasets=[a.model_dump(), b.model_dump()]), repo


def test_missing_measure_preserves_clarification(isolated):
    state, _ = isolated
    output = w.plan_node(state)
    assert not output['plan']['can_execute']
    assert output['plan']['clarification_question']
    assert output['plan']['steps'] == []
    assert w.route_plan(state.model_copy(update=output)) == 'clarify'


def test_independent_sort_steps_are_executed(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal='rankings', can_execute=True, steps=[
        PlanStep(id='sort_a', purpose='A排序', tool='query_data', dataset_id='a'),
        PlanStep(id='sort_b', purpose='B排序', tool='query_data', dataset_id='b'),
    ]).model_dump()
    monkeypatch.setattr(w, '_is_sectioned_department_profit_request', lambda *args: True)
    query = Mock(return_value=ToolExecutionResult(result_id='r', tool_name='query_data', status='success',
                 summary='ranked', arguments={'sql': 'SELECT * FROM a ORDER BY amount'}))
    monkeypatch.setattr(w, 'query_department_profit', query)
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ['sort_a']
    assert w.route_execute(state) == 'execute'
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ['sort_a', 'sort_b']
    assert query.call_count == 2


def test_overdue_draft_uses_latest_success(isolated, monkeypatch):
    state, _ = isolated
    state.tool_results = [dict(status=status, result_id=identifier,
                              arguments={'strategy': 'overdue_ranking'})
                          for identifier, status in [('old', 'success'), ('new', 'success'), ('failed', 'error')]]
    draft = AnalysisDraft(summary='answer')
    render = Mock(return_value=draft)
    monkeypatch.setattr(w, 'overdue_draft', render)
    monkeypatch.setattr(w, '_finalize_draft', lambda *args: draft)
    output = w.draft_node(state)
    assert render.call_args.args[0]['result_id'] == 'new'
    assert output['draft_execution_mode'] == 'deterministic'


@pytest.mark.parametrize('mode,route', [('deterministic', 'finish'), ('model', 'rewrite')])
def test_validation_failure_only_rewrites_model_draft(isolated, mode, route):
    state, _ = isolated
    state.draft_execution_mode = mode
    state.validation = {'passed': False, 'issues': [{'code': 'bad', 'message': 'unsupported', 'severity': 'error'}]}
    assert w.reflect_node(state)['reflection']['route'] == route


@pytest.mark.parametrize('route', ['derived_metric', 'explanation'])
def test_successful_followup_replay_preserves_main_report(isolated, monkeypatch, route):
    state, repo = isolated
    state.intent = {'route': route, 'metric': '净利润率'}
    repo.get_run_by_id.return_value = SimpleNamespace(id='replay', task_id='task', question='metric',
        forked_from_node_execution_id='source', prompt_version_id='prompt')
    repo.get_node_execution.return_value = SimpleNamespace(input_state=state.model_dump(),
        state_schema_version=3, run_id='old', node_name='draft', id='source')
    repo.prompt_versions_for_run.return_value = {}
    graph = Mock()
    graph.invoke.return_value = {'final_status': 'completed', 'draft': AnalysisDraft(summary='answer').model_dump(),
                                 'intent': state.intent}
    monkeypatch.setattr(w, 'graph', graph)
    w.run_replay('replay')
    assert repo.finish_execution.call_args.kwargs['activate'] is False
    repo.restore_active_run_after_failure.assert_not_called()
