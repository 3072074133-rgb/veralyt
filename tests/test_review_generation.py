from types import SimpleNamespace
import pytest
from app.report_generation import compact_evidence, model_evidence_catalog
from app.review_generation import generate_review
from app.llm import LLMContextOverflowError
from app.models import ReflectionDecision, AnalysisState
from app.workflow_nodes.routing import route_validation


def test_shared_catalog_preserves_all_cells_and_missingness():
    rows = [{'amount': None, 'source_row': 8}, {'amount': '10.00', 'label': 'A'}]
    original = compact_evidence(SimpleNamespace(id='e', title='t', source='s', columns=['amount', 'label'], rows=rows))
    packed = model_evidence_catalog([original])[0]
    restored = [{field: row[0][index] for index, field in enumerate(packed['columns']) if index not in row[2]} for row in packed['rows']]
    assert restored == [{'amount': None}, {'amount': '10.00', 'label': 'A'}]
    assert original['rows'][0]['source_location_not_row_index'] == {'source_row': 8}
    assert model_evidence_catalog([packed]) == [packed]
    assert original['rows'][0]['cells']['amount'] is None


def test_legacy_structural_results_always_go_to_model_review():
    state = AnalysisState(task_id='t', run_id='r', user_question='q', validation={'passed': False, 'issues': [{'code': 'missing_evidence'}]})
    assert route_validation(state) == 'reflect'
    state.citation_repair_round = 3
    assert route_validation(state) == 'reflect'
    state.validation = {'passed': True}
    state.reflection = {'verdict': 'revise', 'route': 'rewrite'}
    assert route_validation(state) == 'reflect'


def test_batched_review_checks_every_section_and_consistency():
    calls = []
    def structured(prompt, context, model, **options):
        calls.append(context)
        if len(calls) == 1:
            raise LLMContextOverflowError('full')
        return ReflectionDecision(verdict='pass', route='finish', reason='checked')
    evidence = [compact_evidence(SimpleNamespace(id='e', title='t', source='s', columns=['x'], rows=[{'x': 10}]))]
    result = generate_review(SimpleNamespace(structured=structured), {'analysis_draft': {'summary': 's', 'findings': [{'detail': 'a'}, {'detail': 'b'}]}, 'evidence_catalog': evidence}, prompt_override='review')
    assert result.verdict == 'pass'
    assert [item['review_target'] for item in calls[1:-1]] == ['summary', 'findings[0]', 'findings[1]']
    assert all(item['evidence_catalog'] == model_evidence_catalog(evidence) for item in calls[1:-1])
    assert calls[-1]['analysis_draft']['findings'][1]['detail'] == 'b'


def test_unsplittable_evidence_fails_without_truncation():
    def fail(*args, **kwargs):
        raise LLMContextOverflowError('too large')
    with pytest.raises(LLMContextOverflowError, match='未截断'):
        generate_review(SimpleNamespace(structured=fail), {'analysis_draft': {'summary': 's'}, 'evidence_catalog': []}, prompt_override='review')


def test_reviewer_sees_citation_not_excel_or_query_coordinates():
    from app.report_generation import resolve_citations
    evidence = compact_evidence(SimpleNamespace(id='profit', title='profit', source='query',
        columns=['item', 'amount', 'source_row'], rows=[{'item': 'net profit', 'amount': '81000', 'source_row': 16}]))
    pointer = evidence['rows'][0]['value_pointers']['amount']
    draft = {'summary': 'net profit 81000', 'summary_evidence_pointers': [pointer]}
    def structured(prompt, context, model, **options):
        selected = context['analysis_draft']['summary_evidence_pointers'][0]
        assert selected == {'citation_id': pointer['citation_id']}
        assert context['citation_audit']['missing_citation_ids'] == []
        assert context['citation_audit']['resolved_citations'][pointer['citation_id']]['raw_value'] == '81000'
        assert 'source_row' not in context['evidence_catalog'][0]['columns']
        assert context['evidence_catalog'][0]['rows'][0][0] == ['net profit', '81000']
        restored = resolve_citations(context['analysis_draft'], [evidence])
        assert restored['summary_evidence_pointers'][0]['row_index'] == 0
        assert restored['summary_evidence_pointers'][0]['raw_value'] == '81000'
        return ReflectionDecision(verdict='pass', route='finish', reason='checked')
    generate_review(SimpleNamespace(structured=structured), {'analysis_draft': draft, 'evidence_catalog': [evidence]}, prompt_override='review')
    assert draft['summary_evidence_pointers'][0]['row_index'] == 0
    assert evidence['rows'][0]['source_location_not_row_index']['source_row'] == 16


@pytest.mark.parametrize('final_verdict', ['pass', 'revise'])
def test_model_review_is_final_without_program_override(final_verdict):
    evidence = compact_evidence(SimpleNamespace(id='e', title='expenses', source='query',
        columns=['amount'], rows=[{'amount': '67000'}]))
    pointer = evidence['rows'][0]['value_pointers']['amount']
    calls = []
    def structured(prompt, context, model, **options):
        calls.append(context)
        return ReflectionDecision(verdict=final_verdict, route='finish' if final_verdict == 'pass' else 'rewrite', reason='independent judgment')
    result = generate_review(SimpleNamespace(structured=structured), {
        'analysis_draft': {'metrics': [{'evidence_pointers': [pointer]}]},
        'evidence_catalog': [evidence]}, prompt_override='review')
    assert result.verdict == final_verdict
    assert len(calls) == 1
