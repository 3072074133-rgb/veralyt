from types import SimpleNamespace

import pytest

from app.models import AnalysisPlan, AnalysisState, IntentDecision
from app.repository import repository

pytest_plugins = ['test_monthly_financial_report']


def test_snake_case_plan_wrapper(monkeypatch):
    from app.llm import llm
    monkeypatch.setattr(llm.client, 'chat', lambda **kwargs: SimpleNamespace(
        message=SimpleNamespace(content='{"analysis_plan":{"goal":"查询","can_execute":true,"steps":[]}}', tool_calls=None),
        prompt_eval_count=0, eval_count=0, done_reason='stop'))
    result = llm.structured('analysis_planner', {}, AnalysisPlan, thinking=False)
    assert result.goal == '查询'


def test_overdue_example_end_to_end(imported_report, monkeypatch):
    from app.workflow import build_graph
    from app.dataset_retrieval import retrieve_datasets
    task_id, datasets = imported_report
    question = '根据应收账款明细，找出逾期金额最高的客户并汇总账龄'
    assert retrieve_datasets(question, datasets)[0].dataset.display_name == '应收账款'
    monkeypatch.setattr('app.workflow.llm.structured', lambda name, *args, **kwargs:
        IntentDecision(route='analysis') if name == 'intent_classifier' else pytest.fail('Unneeded model call'))
    run = repository.start_execution(task_id, question)
    result = build_graph().invoke(AnalysisState(task_id=task_id, run_id=run, user_question=question,
        datasets=[d.model_dump(mode='json') for d in datasets]), config={'configurable': {'thread_id': run}})
    assert result['final_status'] == 'completed_with_warnings'
    assert result['validation']['passed'], result['validation']
    assert result['tool_results'][0]['rows'][0]['客户名称'] == '远航商贸（模拟）'
    assert float(result['tool_results'][0]['rows'][0]['逾期金额']) == 50000
    assert '截止日' in result['draft']['warnings'][0]
