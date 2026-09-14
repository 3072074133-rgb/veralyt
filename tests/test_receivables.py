from types import SimpleNamespace

from app.models import AnalysisPlan
from app.repository import repository

pytest_plugins = ['test_monthly_financial_report']


def test_snake_case_plan_wrapper(monkeypatch):
    from app.llm import llm
    monkeypatch.setattr(llm.client, 'chat', lambda **kwargs: SimpleNamespace(
        message=SimpleNamespace(content='{"analysis_plan":{"goal":"查询","can_execute":true,"steps":[]}}', tool_calls=None),
        prompt_eval_count=0, eval_count=0, done_reason='stop'))
    result = llm.structured('analysis_planner', {}, AnalysisPlan, thinking=False)
    assert result.goal == '查询'


def test_overdue_query_uses_explicit_selected_dataset(imported_report):
    from app.receivables import query_overdue
    task_id, datasets = imported_report
    dataset = next(item for item in datasets if item.display_name == '应收账款')
    run = repository.start_execution(task_id, '查询逾期客户')
    result = query_overdue(task_id, dataset, run)
    assert result.rows[0]['客户名称'] == '远航商贸（模拟）'
    assert float(result.rows[0]['逾期金额']) == 50000
