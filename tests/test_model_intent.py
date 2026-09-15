from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.llm import LLMContextOverflowError, LLMStructuredOutputError, llm
from app.models import AnalysisState, IntentDecision, PlanDecision, PlanStep
from app.repository import repository
from app.workflow import build_graph, classify_node
from app import workflow

pytest_plugins = ['test_monthly_financial_report']


def test_route_protocol():
    assert IntentDecision.model_json_schema()['required'] == ['route', 'reply']
    for value in ({'route': 'invalid'}, {'route': 'legacy_route'}, {'is_analysis': False},
                  {'route': 'conversation', 'is_analysis': True}, {'route': 'analysis', 'extra': 1}):
        with pytest.raises(ValidationError):
            IntentDecision.model_validate(value)


@pytest.mark.parametrize('error', [LLMContextOverflowError, LLMStructuredOutputError])
def test_classifier_propagates_model_failure(imported_report, monkeypatch, error):
    task, datasets = imported_report
    run = repository.start_execution(task, '你好')
    def fail(*args, **kwargs):
        assert args[1]['user_question'] == '你好'
        assert len(args[1]['conversation_context']['recent_messages']) == 20
        assert args[1]['conversation_context']['recent_messages'][0]['content'] == '历史' * 10000
        raise error('test')
    monkeypatch.setattr(llm, 'structured', fail)
    with pytest.raises(error, match="test"):
        classify_node(AnalysisState(task_id=task, run_id=run, user_question='你好',
            datasets=[d.model_dump() for d in datasets], conversation_summary={
                'recent_messages': [{'role': 'assistant', 'content': '历史' * 10000}] * 20}))



def test_conversation_does_not_query(imported_report, monkeypatch):
    task, datasets = imported_report
    run = repository.start_execution(task, '你好')
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: IntentDecision(route='conversation', reply='你好！'))
    def no_query(*a, **k):
        pytest.fail('conversation must not query')
    monkeypatch.setattr('app.workflow.execute_sql', no_query)
    output = build_graph().invoke(AnalysisState(task_id=task, run_id=run, user_question='你好',
        datasets=[d.model_dump() for d in datasets]), config={'configurable': {'thread_id': run}})
    assert output['final_status'] == 'off_topic'
    assert not output.get('tool_results')
    assert repository.get_task(task).status_message == '本轮已回复'


def test_classifier_preserves_model_clarification_for_contextual_followup(imported_report, monkeypatch):
    task, datasets = imported_report
    run = repository.start_execution(task, '现金流健康度')
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: IntentDecision(
        route='clarification', reply='请选择一个财务维度'))
    output = classify_node(AnalysisState(
        task_id=task, run_id=run, user_question='现金流健康度',
        datasets=[d.model_dump() for d in datasets],
        previous_result={'title': '月度财务报表分析', 'summary': '已完成财务分析',
                         'insights': [{'title': '现金余额变化', 'conclusion': '期末现金增加'}]},
    ))
    assert output['intent']['route'] == 'clarification'


def test_classifier_preserves_model_route_for_scope_like_text(imported_report, monkeypatch):
    task, datasets = imported_report
    run = repository.start_execution(task, '分别分析各张报表，不做表间核对')
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: IntentDecision(
        route='conversation', reply='这是模型决定直接回复的内容。'))
    output = classify_node(AnalysisState(
        task_id=task, run_id=run,
        user_question='分别分析各张报表，不做表间核对',
        datasets=[d.model_dump() for d in datasets],
        conversation_summary={'recent_messages': [
            {'role': 'assistant', 'content': '我已识别到多张报表。在开始计算前，请确认本次分析范围：'}
        ]},
    ))
    assert output['intent']['route'] == 'conversation'


def test_clarification_answer_is_reclassified_by_model(imported_report, monkeypatch):
    task, datasets = imported_report
    original = '分析全部六张报表，重点关注财务勾稽关系与异常波动'
    answer = '1. 假设所有报表数据口径一致，直接基于现有字段进行交叉验证'
    parent = repository.start_execution(task, original)
    repository.finish_execution(parent, 'needs_clarification')
    repository.update_task(task, status='needs_clarification', progress=40,
                           clarification_question='请选择验证方式')
    run = repository.queue_analysis(task, answer)
    assert repository.get_run_by_id(run).parent_run_id == parent

    captured = {}
    def invoke(state, config):
        captured['state'] = state
        return {'final_status': 'needs_clarification'}
    monkeypatch.setattr(workflow.graph, 'invoke', invoke)
    workflow.run_analysis(run)

    state = captured['state']
    assert state.entry_node == 'classify'
    assert state.intent is None
    assert state.user_question == answer
    assert state.analysis_question == answer
    assert state.clarification_answer is None


@pytest.mark.parametrize('question', ['你好', '收入最高的部门是哪个？', '重新分析', '毛利率', '天气对销售的影响'])
def test_model_analysis_route_is_not_overridden_by_keyword_guard(imported_report, monkeypatch, question):
    task, datasets = imported_report
    run = repository.start_execution(task, question)
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: IntentDecision(route='analysis', reply=None))
    output = classify_node(AnalysisState(
        task_id=task, run_id=run, user_question=question,
        datasets=[d.model_dump() for d in datasets],
    ))
    decision = IntentDecision.model_validate(output['intent'])
    assert decision.route == 'analysis'


def test_confirmed_financial_clarification_preserves_model_plan(imported_report, monkeypatch):
    task, datasets = imported_report
    original = '分析全部六张报表，重点关注财务勾稽关系与异常波动'
    answer = '假设所有报表数据口径一致，直接基于现有字段进行交叉验证'
    run = repository.start_execution(task, answer)
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: PlanDecision(
        action='analyze', goal='按已确认口径执行六表交叉验证', clarification=None,
        clarification_options=[], steps=[
            PlanStep(id='model_query', purpose='跨表核对',
                     dataset_ids=[item.id for item in datasets[:3]],
                     sql=f'SELECT * FROM "{datasets[0].table_name}"')
        ]))
    output = workflow.plan_node(AnalysisState(
        task_id=task, run_id=run,
        user_question=original, analysis_question=original,
        clarification_answer=answer,
        intent={'route': 'analysis', 'reply': None},
        datasets=[item.model_dump() for item in datasets],
    ))
    plan = output['plan']
    assert plan['can_execute'] is True
    assert plan['clarification_question'] is None
    assert [step['id'] for step in plan['steps']] == ['model_query']
    assert plan['steps'][0]['dataset_ids'] == [item.id for item in datasets[:3]]


def test_complete_repair_context_reports_overflow(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'model_context_tokens', 4096)
    monkeypatch.setattr(settings, 'model_max_context_tokens', 4096)
    from app.llm import LLMContextOverflowError
    calls = []
    def chat(**kwargs):
        calls.append(kwargs)
        content = '{"route":"' + 'x' * 10000 + '"}' if len(calls) == 1 else '{"route":"conversation","reply":"你好"}'
        return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
    monkeypatch.setattr(llm.client, 'chat', chat)
    with pytest.raises(LLMContextOverflowError):
        llm.structured('intent_classifier', {'user_question': '你好'}, IntentDecision, thinking=False)
    assert len(calls) == 1


@pytest.mark.ollama
@pytest.mark.parametrize('question', [
    '你好', '早上好呀', '谢谢你', '净利润率是什么意思',
    '利润率怎么样', '净利润率是多少',
    '根据应收账款明细，找出逾期金额最高的客户并汇总账龄',
])
def test_real_model_intent(question):
    decision = llm.structured('intent_classifier', {'user_question': question,
        'has_uploaded_data': True,
        'recent_messages': [{'role': 'assistant', 'content': '已完成月度财务报表分析'}]},
        IntentDecision, thinking=False)
    assert decision.route in {'analysis', 'clarification', 'conversation', 'explanation'}
    if decision.route != 'analysis':
        assert decision.reply


@pytest.mark.ollama
def test_real_clarification_answer():
    decision = llm.structured('intent_classifier', {'user_question': '净利润率',
        'has_uploaded_data': True,
        'recent_messages': [{'role': 'user', 'content': '利润率怎么样'},
                            {'role': 'assistant', 'content': '你想了解毛利率还是净利润率？'}]},
        IntentDecision, thinking=False)
    assert decision.route == 'analysis'
