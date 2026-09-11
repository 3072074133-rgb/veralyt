from app.models import AnalysisState, IntentDecision
from app.followups import calculate_metric, requested_metric, clearly_off_topic
from app.repository import repository
from app.financial_reports import query_financial_report
pytest_plugins = ['test_monthly_financial_report']


def seed(task_id, datasets):
    run = repository.start_execution(task_id, '分析报表')
    query_financial_report(task_id, datasets, run)
    repository.finish_execution(run, 'completed')


def test_reuse_and_version_invalidation(imported_report):
    task_id, datasets = imported_report
    seed(task_id, datasets)
    run = repository.start_execution(task_id, '净利润率是多少')
    state = AnalysisState(task_id=task_id, run_id=run, user_question='净利润率是多少',
                          datasets=[d.model_dump(mode='json') for d in datasets])
    result, error = calculate_metric(state, '净利润率')
    assert not error
    assert result.rows[0]['百分比'] == '27.00'
    assert len(result.arguments['input_pointers']) == 2
    repository.finish_execution(run, 'completed')
    repository.publish_data_revision(task_id)
    newer = repository.start_execution(task_id, '净利润率是多少')
    state.run_id = newer
    assert calculate_metric(state, '净利润率')[0] is None


def test_followup_workflow_without_query(imported_report, monkeypatch):
    monkeypatch.setattr('app.workflow.llm.structured', lambda *args, **kwargs:
        IntentDecision(route='derived_metric', metric='净利润率'))
    from app.workflow import build_graph
    task_id, datasets = imported_report
    seed(task_id, datasets)
    monkeypatch.setattr('app.workflow.query_financial_report', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('No new financial query expected')))
    run = repository.start_execution(task_id, '净利润率怎么样')
    result = build_graph().invoke(AnalysisState(task_id=task_id, run_id=run, user_question='净利润率怎么样',
        datasets=[d.model_dump(mode='json') for d in datasets]), config={'configurable': {'thread_id': run}})
    assert result['validation']['passed'], result['validation']
    assert result['final_status'] == 'completed'
    assert '27.00%' in result['draft']['summary']
    assert result['tool_call_count'] == 0


def test_routes():
    assert requested_metric('你觉得利润率怎么样') == 'ambiguous'
    assert requested_metric('毛利率') == '毛利率'
    assert clearly_off_topic('今天天气怎么样')


def test_wrapped_intent_stays_strict(monkeypatch):
    from types import SimpleNamespace
    from app.llm import llm
    from app.models import IntentDecision
    monkeypatch.setattr(llm.client, 'chat', lambda **kwargs: SimpleNamespace(
        message=SimpleNamespace(content='{"IntentDecision":"分析"}', tool_calls=None),
        prompt_eval_count=0, eval_count=0, done_reason='stop'))
    result = llm.structured('intent_classifier', {'user_question': '你好'}, IntentDecision, thinking=False)
    assert result.route == 'clarification'
    assert result.metric is None


def test_missing_input_queries_current_data(imported_report, monkeypatch):
    monkeypatch.setattr('app.workflow.llm.structured', lambda *args, **kwargs:
        IntentDecision(route='derived_metric', metric='净利润率'))
    from app.workflow import build_graph
    task_id, datasets = imported_report
    run = repository.start_execution(task_id, '净利润率是多少')
    result = build_graph().invoke(AnalysisState(task_id=task_id, run_id=run, user_question='净利润率是多少',
        datasets=[d.model_dump(mode='json') for d in datasets]), config={'configurable': {'thread_id': run}})
    assert result['validation']['passed'], result['validation']
    assert result['tool_call_count'] == 1


def test_zero_denominator(imported_report):
    task_id, datasets = imported_report
    seed(task_id, datasets)
    evidence = repository.list_evidence(task_id)[-1]
    for row in evidence.rows:
        if row['报表'] == '利润表' and row['项目'] == '营业收入':
            row['金额'] = '0'
    repository.add_evidence(evidence)
    run = repository.start_execution(task_id, '净利润率')
    result, reason = calculate_metric(AnalysisState(task_id=task_id, run_id=run, user_question='净利润率',
        datasets=[d.model_dump(mode='json') for d in datasets]), '净利润率')
    assert result is None and '分母为零' in reason


def test_explanation_reuses_grounded_rows(imported_report):
    from app.followups import reuse_explanation
    task_id, datasets = imported_report
    seed(task_id, datasets)
    run = repository.start_execution(task_id, '解释利润和现金差异')
    state = AnalysisState(task_id=task_id, run_id=run, user_question='解释利润和现金差异',
                          datasets=[d.model_dump(mode='json') for d in datasets])
    result = reuse_explanation(state)
    assert result and result.arguments['source_evidence_ids']
    assert any(r['项目'] == '净利润' for r in result.rows)
