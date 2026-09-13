from unittest.mock import MagicMock, Mock

import pytest

from app import workflow as w
from app.models import AnalysisDraft, AnalysisPlan, AnalysisState, DatasetInfo, PlanStep, QuerySpec, ToolExecutionResult


@pytest.fixture
def isolated(monkeypatch):
    tracker = Mock()
    tracker.complete.side_effect = lambda output: output
    monkeypatch.setattr(w, 'start_node', lambda *args: tracker)
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


def test_query_limit_is_clamped_by_backend(isolated):
    state, _ = isolated
    spec = w._query_spec_from_arguments({
        'query': {'dataset_id': 'a', 'measures': ['金额'], 'limit': 10**15}
    }, [DatasetInfo(id='a', file_id='f', table_name='a', display_name='A', row_count=1,
                    columns=[{'name': '金额', 'display_name': '金额', 'data_type': 'DOUBLE',
                              'null_count': 0, 'sample_values': [], 'semantic_type': 'amount',
                              'role': 'measure', 'default_aggregation': 'sum', 'semantic_confidence': 1.0}])])
    assert isinstance(spec, QuerySpec)
    assert spec.limit == w.settings.max_query_rows


def test_delivery_gate_rejects_raw_only_result(isolated):
    state, _ = isolated
    state = state.model_copy(update={
        'plan': AnalysisPlan(goal='汇总', can_execute=True, steps=[]).model_dump(),
        'tool_results': [{'status': 'success', 'rows': [{'金额': '10'}], 'evidence_ids': ['e']}],
        'validation': {'passed': True, 'issues': [], 'checked_evidence_ids': ['e']},
        'draft': AnalysisDraft(summary='原始金额', metrics=[], findings=[], insights=[]).model_dump(),
    })
    gate = w._delivery_gate(state, AnalysisDraft.model_validate(state.draft),
                            w.ValidationReport.model_validate(state.validation))
    assert gate.status == 'needs_review'
    assert not next(item for item in gate.checks if item.code == 'new_information_present').passed


def test_derived_metric_produces_auditable_insight():
    from app.insights import derive_insights
    result = {'evidence_ids': ['e1'], 'rows': [{'指标': '净利润率', '百分比': '27.00', '公式': '净利润/营业收入*100'}],
              'arguments': {'strategy': 'derived_metric', 'metric': '净利润率'}}
    insights = derive_insights(result, 'derived_metric')
    assert len(insights) == 1
    assert insights[0].formula == '净利润/营业收入*100'
    assert insights[0].evidence_pointers[0].source_type == 'cell'


def test_hybrid_followup_keeps_execution_when_planner_requests_clarification(isolated, monkeypatch):
    state, _ = isolated
    state = state.model_copy(update={
        'user_question': '继续分析：你有什么建议吗',
        'previous_result': {'title': '上轮结果', 'summary': '已有分析'},
        'intent': {'route': 'analysis'},
    })
    monkeypatch.setattr(w, 'llm', type('FakeLLM', (), {
        'structured': staticmethod(lambda *args, **kwargs: w.PlanDecision(action='clarify', clarification='请选择分析维度')),
    })())
    output = w.plan_node(state)
    assert output['plan']['can_execute'] is True
    assert output['plan']['steps'] == []


@pytest.mark.parametrize('mode,route', [('deterministic', 'finish'), ('model', 'rewrite')])
def test_validation_failure_only_rewrites_model_draft(isolated, mode, route):
    state, _ = isolated
    state.draft_execution_mode = mode
    state.validation = {'passed': False, 'issues': [{'code': 'bad', 'message': 'unsupported', 'severity': 'error'}]}
    assert w.reflect_node(state)['reflection']['route'] == route
