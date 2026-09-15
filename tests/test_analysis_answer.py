import pytest
from pydantic import ValidationError

from app.models import AnalysisState, ResultDecision, ConvergenceDecision
from app.repository import repository
from app.workflow import answer_node, route_results, assess_results_node


def test_answer_preserves_conversation_requirement_and_ends_without_report(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'answer.sqlite')
    repository.initialize()
    task = repository.create_task()
    run = repository.queue_analysis(task, 'all tables')
    context = {'memory': {'confirmed_requirements': ['Do not generate a report']}}
    state = AnalysisState(task_id=task, run_id=run, user_question='all tables',
                          conversation_summary=context,
                          plan={'goal': 'analyze', 'can_execute': True, 'steps': []})
    def model(name, payload, schema, **kwargs):
        assert payload['conversation_context'] == context
        return schema(action='answer', reason='user requested conversation', answer='Revenue is 100.')
    monkeypatch.setattr('app.workflow.llm.structured', model)
    update = assess_results_node(state)
    state = state.model_copy(update=update)
    assert route_results(state) == 'answer'
    output = answer_node(state)
    assert output['final_status'] == 'off_topic'
    assert repository.get_task(task).result is None
    assert repository.get_task(task).messages[-1].content == 'Revenue is 100.'


@pytest.mark.parametrize('schema', [ResultDecision, ConvergenceDecision])
def test_answer_requires_content(schema):
    with pytest.raises(ValidationError):
        schema(action='answer', reason='done', answer=' ')
    assert schema(action='answer', reason='done', answer='Analysis').answer == 'Analysis'
