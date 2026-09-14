from unittest.mock import MagicMock, Mock

import pytest

from app import workflow as w
from app.models import AnalysisDraft, AnalysisPlan, AnalysisState, DatasetInfo, PlanStep, ToolExecutionResult


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


def test_missing_measure_preserves_clarification(isolated, monkeypatch):
    state, _ = isolated
    monkeypatch.setattr(w.llm, 'structured', lambda *a, **k: w.PlanDecision(
        action='clarify', goal='确认分析字段', clarification='请指定可分析的字段',
        clarification_options=[], steps=[]))
    output = w.plan_node(state)
    assert not output['plan']['can_execute']
    assert output['plan']['clarification_question']
    assert output['plan']['steps'] == []
    assert w.route_plan(state.model_copy(update=output)) == 'clarify'


def test_planner_receives_tool_contracts(isolated, monkeypatch):
    state, _ = isolated
    planner = Mock(return_value=w.PlanDecision(
        action='clarify', goal='scope', clarification='scope?', clarification_options=[], steps=[]))
    monkeypatch.setattr(w.llm, 'structured', planner)
    w.plan_node(state)
    context = planner.call_args.args[1]
    contracts = {tool['name']: tool for tool in context['tool_descriptions']}
    assert set(contracts) == set(context['available_tools'])
    assert contracts['query_overdue']['required_fields'] == ['客户名称', '逾期余额', '报表行类型']
    assert 'query_data' in contracts['query_overdue']['alternative']


def test_independent_sort_steps_are_executed(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal='rankings', can_execute=True, steps=[
        PlanStep(id='profile_a', purpose='A画像', tool='profile_table', dataset_id='a'),
        PlanStep(id='profile_b', purpose='B画像', tool='profile_table', dataset_id='b'),
    ]).model_dump()
    profile = Mock(return_value=ToolExecutionResult(
        result_id='r', tool_name='profile_table', status='success', summary='profiled'))
    monkeypatch.setattr(w, 'profile_table', profile)
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ['profile_a']
    assert w.route_execute(state) == 'execute'
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == ['profile_a', 'profile_b']
    assert profile.call_count == 2


def test_query_limit_is_rejected_instead_of_rewritten(isolated):
    with pytest.raises(w.ToolError, match='超过执行上限'):
        w._query_spec_from_arguments({
            'query': {
                'dataset_id': 'a',
                'measures': [{'field': '金额', 'aggregation': 'sum', 'alias': '金额合计'}],
                'limit': w.settings.max_query_rows + 1,
            }
        })


@pytest.mark.parametrize('primary,related,expected', [
    (None, ['b', 'a'], ['b', 'a']),
    ('a', [], ['a']),
    ('a', ['b', 'a'], ['a', 'b']),
])
def test_executor_accepts_either_dataset_input(isolated, monkeypatch, primary, related, expected):
    state, _ = isolated
    state.plan = AnalysisPlan(goal='reports', can_execute=True, steps=[
        PlanStep(id='reports', purpose='reports', tool='query_financial_report',
                 dataset_id=primary, dataset_ids=related),
    ]).model_dump()
    tool = Mock(return_value=ToolExecutionResult(
        result_id='r', tool_name='query_data', status='success', summary='ok'))
    monkeypatch.setattr(w, 'query_financial_report', tool)
    output = w.execute_node(state)
    assert [dataset.id for dataset in tool.call_args.args[1]] == expected
    assert output['completed_step_ids'] == ['reports']


@pytest.mark.parametrize('related,error', [([], '未指定数据表'), (['missing'], '不存在的数据表')])
def test_executor_rejects_empty_or_unknown_dataset_list(isolated, related, error):
    state, _ = isolated
    state.plan = AnalysisPlan(goal='reports', can_execute=True, steps=[
        PlanStep(id='reports', purpose='reports', tool='query_financial_report', dataset_ids=related),
    ]).model_dump()
    with pytest.raises(w.ToolError, match=error):
        w.execute_node(state)


def test_query_failure_returns_to_model_and_preserves_completed_steps(isolated, monkeypatch):
    state, repo = isolated
    state.plan = AnalysisPlan(goal='rank', can_execute=True, steps=[
        PlanStep(id='query', purpose='rank', tool='query_data', dataset_id='a'),
    ]).model_dump()
    bad = w.QueryDecision(dimensions=[], measures=[], filters=[], order_by='total DESC',
                          descending=True, limit=10, joins=[])
    good = bad.model_copy(update={'order_by': 'total'})
    model = Mock(side_effect=[bad, good])
    monkeypatch.setattr(w.llm, 'structured', model)
    query = Mock(side_effect=[w.ToolError('unknown sort column'), ToolExecutionResult(
        result_id='r', tool_name='query_data', status='success', summary='ok')])
    monkeypatch.setattr(w, 'query_from_spec', query)
    state = state.model_copy(update=w.execute_node(state))
    assert state.completed_step_ids == []
    assert state.tool_call_count == 1
    assert state.query_failures[0]['arguments']['query']['order_by'] == 'total DESC'
    assert w.route_execute(state) == 'execute'
    state = state.model_copy(update=w.execute_node(state))
    assert model.call_args.args[1]['previous_query_failures'][0]['error'] == 'unknown sort column'
    assert query.call_args.args[2].order_by == 'total'
    assert state.completed_step_ids == ['query']
    assert state.query_failures == []
    assert state.tool_call_count == 2


def test_query_repair_stops_after_two_retries(isolated, monkeypatch):
    state, _ = isolated
    state.plan = AnalysisPlan(goal='rank', can_execute=True, steps=[
        PlanStep(id='query', purpose='rank', tool='query_data', dataset_id='a'),
    ]).model_dump()
    monkeypatch.setattr(w.llm, 'structured', Mock(return_value=w.QueryDecision(
        dimensions=[], measures=[], filters=[], order_by=None, descending=False, limit=10, joins=[])))
    query = Mock(side_effect=w.ToolError('invalid query'))
    monkeypatch.setattr(w, 'query_from_spec', query)
    for _ in range(2):
        state = state.model_copy(update=w.execute_node(state))
    with pytest.raises(w.ToolError, match='invalid query'):
        w.execute_node(state)
    assert query.call_count == 3


def test_delivery_gate_accepts_model_selected_report_shape(isolated):
    state, _ = isolated
    state = state.model_copy(update={
        'plan': AnalysisPlan(goal='汇总', can_execute=True, steps=[]).model_dump(),
        'tool_results': [{'status': 'success', 'rows': [{'金额': '10'}], 'evidence_ids': ['e']}],
        'validation': {'passed': True, 'issues': [], 'checked_evidence_ids': ['e']},
        'draft': AnalysisDraft(summary='原始金额', metrics=[], findings=[], insights=[]).model_dump(),
    })
    gate = w._delivery_gate(state, AnalysisDraft.model_validate(state.draft),
                            w.ValidationReport.model_validate(state.validation))
    assert gate.status == 'completed'
    assert all(item.code != 'new_information_present' for item in gate.checks)


def test_delivery_gate_accepts_model_selected_zero_step_answer(isolated):
    state, _ = isolated
    state = state.model_copy(update={
        'plan': AnalysisPlan(goal='使用已有上下文回答', can_execute=True, steps=[]).model_dump(),
        'tool_results': [],
        'validation': {'passed': True, 'issues': [], 'checked_evidence_ids': []},
        'draft': AnalysisDraft(summary='模型基于已有上下文给出的回答').model_dump(),
    })
    gate = w._delivery_gate(
        state,
        AnalysisDraft.model_validate(state.draft),
        w.ValidationReport.model_validate(state.validation),
    )
    assert gate.status == 'completed'


def test_planner_clarification_is_preserved(isolated, monkeypatch):
    state, _ = isolated
    state = state.model_copy(update={
        'user_question': '继续分析：你有什么建议吗',
        'previous_result': {'title': '上轮结果', 'summary': '已有分析'},
        'intent': {'route': 'analysis', 'reply': None},
    })
    monkeypatch.setattr(w, 'llm', type('FakeLLM', (), {
        'structured': staticmethod(lambda *args, **kwargs: w.PlanDecision(
            action='clarify', goal='确认分析维度', clarification='请选择分析维度',
            clarification_options=[], steps=[])),
    })())
    output = w.plan_node(state)
    assert output['plan']['can_execute'] is False
    assert output['plan']['clarification_question'] == '请选择分析维度'


def test_validation_failure_enters_model_review(isolated, monkeypatch):
    state, _ = isolated
    state.validation = {'passed': False, 'issues': [{'code': 'bad', 'message': 'unsupported', 'severity': 'error'}]}
    monkeypatch.setattr(w.llm, 'structured', lambda *args, **kwargs: w.ReflectionDecision(
        verdict='revise', route='rewrite', reason='修复证据引用'))
    assert w.reflect_node(state)['reflection']['route'] == 'rewrite'


@pytest.mark.parametrize('question', ['分析数据', '分析一下', '重新分析', '继续分析'])
def test_generic_requests_use_model_plan(isolated, monkeypatch, question):
    state, _ = isolated
    state.user_question = question
    state.conversation_summary = {'recent_messages': [{'role': 'user', 'content': '比较两个数据集'}]}
    decision = w.PlanDecision(action='analyze', goal='模型选择的数据画像', clarification=None,
        clarification_options=[], steps=[
        PlanStep(id='model_step', purpose='查看B字段分布', tool='profile_table', dataset_id='b')])
    model = Mock(return_value=decision)
    monkeypatch.setattr(w.llm, 'structured', model)
    output = w.plan_node(state)
    model.assert_called_once()
    assert model.call_args.args[1]['user_question'] == question
    assert model.call_args.args[1]['conversation_context'] == state.conversation_summary
    assert output['plan']['steps'] == [step.model_dump(mode='json') for step in decision.steps]
    assert output['plan']['goal'] == decision.goal
