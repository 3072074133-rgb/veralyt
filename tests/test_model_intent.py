from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.llm import LLMContextOverflowError, LLMStructuredOutputError, llm
from app.models import AnalysisState, IntentDecision
from app.repository import repository
from app.workflow import build_graph, classify_node

pytest_plugins = ['test_monthly_financial_report']


def test_route_protocol():
    assert IntentDecision.model_json_schema()['required'] == ['route']
    assert IntentDecision(route='conversation', reply='你好').is_analysis is False
    assert IntentDecision.model_validate({'is_analysis': False}).route == 'conversation'
    for value in ({'route': 'invalid'}, {'route': 'derived_metric'},
                  {'route': 'conversation', 'is_analysis': True}, {'route': 'analysis', 'extra': 1}):
        with pytest.raises(ValidationError):
            IntentDecision.model_validate(value)


@pytest.mark.parametrize('error', [LLMContextOverflowError, LLMStructuredOutputError])
def test_classifier_downgrades_to_clarification(imported_report, monkeypatch, error):
    task, datasets = imported_report
    run = repository.start_execution(task, '你好')
    def fail(*args, **kwargs):
        assert len(str(args[1])) < 3000
        assert args[1]['user_question'] == '你好'
        raise error('test')
    monkeypatch.setattr(llm, 'structured', fail)
    output = classify_node(AnalysisState(task_id=task, run_id=run, user_question='你好',
        datasets=[d.model_dump() for d in datasets], conversation_summary={
            'recent_messages': [{'role': 'assistant', 'content': '历史' * 10000}] * 20}))
    assert output['intent']['route'] == 'clarification'


def test_conversation_does_not_query(imported_report, monkeypatch):
    task, datasets = imported_report
    run = repository.start_execution(task, '你好')
    monkeypatch.setattr(llm, 'structured', lambda *a, **k: IntentDecision(route='conversation', reply='你好！'))
    def no_query(*a, **k):
        pytest.fail('conversation must not query')
    monkeypatch.setattr('app.workflow.query_financial_report', no_query)
    output = build_graph().invoke(AnalysisState(task_id=task, run_id=run, user_question='你好',
        datasets=[d.model_dump() for d in datasets]), config={'configurable': {'thread_id': run}})
    assert output['final_status'] == 'off_topic'
    assert not output.get('tool_results')
    assert repository.get_task(task).status_message == '本轮已回复'


def test_contextual_followup_is_not_forced_into_clarification(imported_report, monkeypatch):
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
    assert output['intent']['route'] == 'explanation'


def test_repair_does_not_echo_large_invalid_output(monkeypatch):
    calls = []
    def chat(**kwargs):
        calls.append(kwargs)
        content = '{"route":"' + 'x' * 10000 + '"}' if len(calls) == 1 else '{"route":"conversation","reply":"你好"}'
        return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
    monkeypatch.setattr(llm.client, 'chat', chat)
    assert llm.structured('intent_classifier', {'user_question': '你好'}, IntentDecision, thinking=False).route == 'conversation'
    assert len(calls) == 2
    assert len(calls[1]['messages'][0]['content']) < 3000


@pytest.mark.ollama
@pytest.mark.parametrize('question,route,metric', [
    ('你好', 'conversation', None), ('早上好呀', 'conversation', None),
    ('谢谢你', 'conversation', None), ('净利润率是什么意思', 'conversation', None),
    ('利润率怎么样', 'clarification', None), ('净利润率是多少', 'derived_metric', '净利润率'),
    ('根据应收账款明细，找出逾期金额最高的客户并汇总账龄', 'analysis', None),
])
def test_real_model_intent(question, route, metric):
    decision = llm.structured('intent_classifier', {'user_question': question,
        'has_uploaded_data': True, 'available_metrics': ['净利润率', '毛利率', '营业利润率', '资产负债率'],
        'recent_messages': [{'role': 'assistant', 'content': '已完成月度财务报表分析'}]},
        IntentDecision, thinking=False)
    assert decision.route == route
    if metric:
        assert decision.metric == metric
    if route == 'conversation':
        assert decision.reply


@pytest.mark.ollama
def test_real_clarification_answer():
    decision = llm.structured('intent_classifier', {'user_question': '净利润率',
        'has_uploaded_data': True, 'available_metrics': ['净利润率', '毛利率'],
        'recent_messages': [{'role': 'user', 'content': '利润率怎么样'},
                            {'role': 'assistant', 'content': '你想了解毛利率还是净利润率？'}]},
        IntentDecision, thinking=False)
    assert decision.route == 'derived_metric'
    assert decision.metric == '净利润率'
