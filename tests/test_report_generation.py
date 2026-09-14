from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from app.llm import LLMContextOverflowError, _select_context
from app.report_generation import compact_evidence, generate_report, citation_id
from app.llm import OllamaGateway
from app.llm import LLMOutputTruncatedError
from app.models import GeneratedAnalysisDraft
from app.models import EvidencePointer
from app.evidence_validation import _candidate_value_pointers
import json


def test_evidence_compaction_roundtrips_exact_values_and_missing_cells():
    rows = [{'amount': '6800.00', 'unit': 'CNY', 'period': '2026-09'},
            {'amount': 6800, 'unit': None, 'extra': False}]
    item = SimpleNamespace(id='ev1', title='report', source='file', columns=['amount', 'unit'], rows=rows)
    packed = compact_evidence(item)
    restored = [{**row['cells'], **row['source_location_not_row_index']} for row in packed['rows']]
    assert restored == rows
    assert [row['row_index'] for row in packed['rows']] == [0, 1]
    assert packed['rows'][0]['value_pointers']['amount'] == {
        'citation_id': citation_id('ev1', 0, 'amount', '6800.00'),
        'evidence_id': 'ev1', 'row_index': 0, 'field': 'amount', 'raw_value': '6800.00',
    }
    assert 'unit' not in packed['rows'][0]['value_pointers']


def test_candidate_pointer_translates_project_and_source_row_to_evidence_row():
    evidence = SimpleNamespace(rows=[
        {'项目': '营业收入', '金额': '300000.000000', '来源行号': 5},
        {'项目': '营业利润', '金额': '108000.000000', '来源行号': 11},
    ])
    invalid = EvidencePointer(
        evidence_id='ev1', row_index=11, field='营业利润', raw_value='108000.000000',
    )
    assert _candidate_value_pointers(invalid, evidence) == [{
        'citation_id': citation_id('ev1', 1, '金额', '108000.000000'),
        'context': evidence.rows[1],
        'evidence_id': 'ev1', 'row_index': 1, 'field': '金额', 'raw_value': '108000.000000',
    }]


class Report(BaseModel):
    summary: str
    warnings: list[str]


def test_sections_keep_evidence_and_merge_only_after_completion():
    gateway = Mock()
    contexts = []

    def structured(name, context, model, **options):
        contexts.append(context)
        if model is Report:
            raise LLMContextOverflowError('large')
        field = context['report_section'][0]
        return model.model_validate({field: 'done' if field == 'summary' else ['warning']})

    gateway.structured.side_effect = structured
    context = {'evidence_catalog': [{'id': 'ev1', 'rows': [[6800]]}],
               'previous_draft': {'summary': 'old', 'warnings': ['old warning']}}
    result = generate_report(gateway, 'draft_writer', context, Report, prompt_override='report')
    assert result == Report(summary='done', warnings=['warning'])
    assert all(c['evidence_catalog'] == context['evidence_catalog'] for c in contexts)
    assert contexts[1]['previous_draft'] == {'summary': 'old'}
    assert contexts[2]['previous_draft'] == {'warnings': ['old warning']}
    assert context['previous_draft']['warnings'] == ['old warning']


def test_section_overflow_never_returns_partial_report():
    gateway = Mock()
    gateway.structured.side_effect = LLMContextOverflowError('too large')
    with pytest.raises(LLMContextOverflowError, match='summary'):
        generate_report(gateway, 'draft_writer', {}, Report, prompt_override='report')


def test_revision_reserves_output_before_selecting_context():
    first = _select_context('draft_writer', 'report', {})
    revision = _select_context('draft_writer', 'report', {'previous_draft': {'summary': 'old'}})
    assert revision.budget.output_tokens == first.budget.output_tokens * 2


@pytest.mark.parametrize('truncated', [False, True])
def test_repair_carries_only_one_report_version(monkeypatch, truncated):
    gateway = OllamaGateway()
    first = SimpleNamespace(message=SimpleNamespace(content='{"summary":"partial"}' if not truncated else '{"summary":', tool_calls=None),
                            done_reason='length' if truncated else 'stop')
    final = SimpleNamespace(message=SimpleNamespace(content='{"summary":"done","warnings":[]}', tool_calls=None), done_reason='stop')
    chat = Mock(side_effect=[first, final])
    monkeypatch.setattr(gateway.client, 'chat', chat)
    gateway.structured('draft_writer', {'previous_draft': {'summary': 'original', 'warnings': []}}, Report, thinking=False)
    repair = json.loads(chat.call_args.kwargs['messages'][1]['content'])
    if truncated:
        assert repair['previous_draft']['summary'] == 'original'
        assert repair['output_repair']['previous_output'] is None
    else:
        assert 'previous_draft' not in repair
        assert json.loads(repair['output_repair']['previous_output'])['summary'] == 'partial'


def test_local_revision_preserves_other_blocks_and_groups_summary_references():
    old = GeneratedAnalysisDraft(summary='original', warnings=['keep exactly']).model_dump(mode='json')
    context = {'previous_draft': old, 'revision_feedback': {'structural_validation': {
        'issues': [{'target': 'summary', 'message': 'bad reference'}]}}}
    gateway = Mock()
    def generate(name, current, model, **options):
        assert set(current['report_section']) == {'title', 'summary', 'summary_evidence_refs',
                                                  'summary_evidence_pointers', 'verification_level'}
        return model.model_validate({'summary': 'fixed'})
    gateway.structured.side_effect = generate
    result = generate_report(gateway, 'draft_writer', context, GeneratedAnalysisDraft, prompt_override='report')
    assert gateway.structured.call_count == 1
    assert result.summary == 'fixed'
    assert result.warnings == ['keep exactly']
    assert old['summary'] == 'original'


def test_truncated_full_report_switches_to_blocks_without_full_retry():
    gateway = Mock()
    def generate(name, context, model, **options):
        if model is GeneratedAnalysisDraft:
            assert options['stop_on_length']
            assert options['output_tokens'] >= 6144
            raise LLMOutputTruncatedError('length')
        return model.model_validate({'summary': 'done'} if 'summary' in model.model_fields else {})
    gateway.structured.side_effect = generate
    generate_report(gateway, 'draft_writer', {}, GeneratedAnalysisDraft,
                    ignored_response_fields={'calculation_details'}, prompt_override='report')
    assert gateway.structured.call_count == 6


def test_gateway_records_truncation_before_handing_back_to_report_generator(monkeypatch):
    gateway = OllamaGateway()
    chat = Mock(return_value=SimpleNamespace(message=SimpleNamespace(content='{"summary":', tool_calls=None), done_reason='length'))
    monkeypatch.setattr(gateway.client, 'chat', chat)
    records = []
    with pytest.raises(LLMOutputTruncatedError):
        gateway.structured('draft_writer', {}, Report, thinking=False, stop_on_length=True, diagnostics=records.append)
    assert chat.call_count == 1
    assert any('output_failure' in item for item in records)
