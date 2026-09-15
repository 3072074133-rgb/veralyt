from app.models import AnalysisState
from app.workflow_nodes.routing import route_intent


def test_report_delivery_enters_workflow_without_new_query():
    state = AnalysisState(task_id='t', run_id='r', user_question='report', intent={
        'route': 'explanation', 'reply': 'Existing analysis', 'delivery': 'report', 'data_action': 'reuse',
    })
    assert route_intent(state) == 'plan'


def test_answer_can_query_without_requesting_report():
    state = AnalysisState(task_id='t', run_id='r', user_question='analysis', intent={
        'route': 'analysis', 'reply': None, 'delivery': 'answer', 'data_action': 'query',
    })
    assert route_intent(state) == 'plan'
