from types import SimpleNamespace

from app.models import AnalysisState, EvidencePointer, GeneratedAnalysisDraft
from app.report_generation import compact_evidence, resolve_citations, generate_report
from app.evidence_validation import validate_claim
from app.workflow_nodes.routing import route_reflection, route_validation


def catalog(identity, rows):
    return compact_evidence(SimpleNamespace(id=identity, title=identity, source='query', columns=['amount', 'period'], rows=rows))


def test_cross_table_and_period_citations_are_distinct_and_resolve_exactly():
    first = catalog('single', [{'amount': 10, 'period': 'start'}, {'amount': 10, 'period': 'end'}])
    second = catalog('combined', [{'amount': 10, 'period': 'end'}])
    pointers = [row['value_pointers']['amount'] for item in [first, second] for row in item['rows']]
    assert len({p['citation_id'] for p in pointers}) == 3
    selected = pointers[1]
    draft = {'findings': [{'evidence_refs': [], 'evidence_pointers': [{'citation_id': selected['citation_id']}]}]}
    resolved = resolve_citations(draft, [first, second])
    assert resolved['findings'][0]['evidence_pointers'][0] == selected
    assert resolved['findings'][0]['evidence_refs'] == ['single']
    assert draft['findings'][0]['evidence_refs'] == []


def test_missing_and_foreign_citations_fail(monkeypatch):
    state = AnalysisState(task_id='task', run_id='run', user_question='q')
    monkeypatch.setattr('app.evidence_validation.repository.get_evidence', lambda *args: None)
    assert validate_claim(state, 'insights[0]', [], [])[0].code == 'missing_evidence'
    pointer = EvidencePointer(citation_id='foreign')
    assert validate_claim(state, 'findings[0]', ['old'], [pointer])[0].code == 'invalid_evidence_pointer'


def test_derived_value_cannot_bypass_cell_validation(monkeypatch):
    state = AnalysisState(task_id='task', run_id='run', user_question='q', tool_results=[{'status': 'success', 'evidence_ids': ['ev']}])
    monkeypatch.setattr('app.evidence_validation.repository.get_evidence', lambda *args: SimpleNamespace(rows=[{'amount': 10}]))
    source = EvidencePointer(evidence_id='ev', row_index=0, field='amount', raw_value='10')
    fake = source.model_copy(update={'raw_value': '999', 'source_type': 'derived', 'formula': 'x', 'input_pointers': [source]})
    assert validate_claim(state, 'metrics[0]', ['ev'], [fake])


def test_last_repair_runs_and_is_validated():
    state = AnalysisState(task_id='task', run_id='run', user_question='q', revision_round=3,
                          reflection={'verdict': 'revise', 'route': 'rewrite', 'reason': 'fix'})
    assert route_reflection(state) == 'rewrite'
    state.validation = {'passed': True}
    assert route_validation(state) == 'reflect'
    state.validation = {'passed': False}
    assert route_validation(state) == 'finish'


def test_local_repair_preserves_other_claims():
    draft = GeneratedAnalysisDraft(summary='summary', findings=[{'title': 'A', 'detail': 'unchanged'}, {'title': 'B', 'detail': 'repair'}])
    calls = []
    def structured(prompt, context, model, **kwargs):
        calls.append(context['repair_target'])
        assert context['previous_draft']['detail'] == 'repair'
        return model(title='B', detail='supported', evidence_refs=['ev'], evidence_pointers=[{'citation_id': 'chosen'}])
    gateway = SimpleNamespace(structured=structured, load_prompt=lambda _: 'repair')
    result = generate_report(gateway, 'draft_writer', {
        'previous_draft': draft.model_dump(mode='json'),
        'revision_feedback': {'structural_validation': {'issues': [{'code': 'invalid_evidence_pointer', 'target': 'findings[1].evidence_pointers[0]'}]}},
    }, GeneratedAnalysisDraft)
    assert calls == ['findings[1]']
    assert result.findings[0] == draft.findings[0]
    assert result.findings[1].detail == 'supported'
