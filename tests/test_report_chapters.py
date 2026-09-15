from copy import deepcopy
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.llm import LLMOutputTruncatedError, _ollama_schema, _report_output_schema
from app.models import AnalysisDraft, ChapterAnalysisDraft, ReportOutline, ReportSection, Insight
from app.report_document import report_sections, evidence_ids
from app.report_generation import generate_report, ReportIntroduction, resolve_citations


def chapter(index=0, title='核心结论'):
    return {'id': f's{index}', 'title': title, 'blocks': [
        {'id': f'b{index}', 'kind': 'paragraph', 'text': f'{title}正文',
         'evidence_pointers': [{'citation_id': 'cite_amount'}]}]}


def report():
    return ChapterAnalysisDraft(summary='概述', sections=[chapter(i, title) for i, title in enumerate(
        ['核心结论', '关键数据', '问题与原因', '行动建议'])])


def test_v2_schema_has_chapters_without_legacy_body():
    schema = _report_output_schema(_ollama_schema(ChapterAnalysisDraft.model_json_schema()))
    assert 'sections' in schema['required']
    assert not {'metrics', 'findings', 'insights', 'charts'} & schema['properties'].keys()
    assert 'evidence_pointers' in schema['$defs']['ReportBlock']['properties']


def test_v2_rejects_empty_body_duplicate_ids_and_hidden_content():
    with pytest.raises(ValidationError):
        ChapterAnalysisDraft(summary='not a report', sections=[])
    with pytest.raises(ValidationError):
        ChapterAnalysisDraft(summary='x', sections=[chapter(), chapter()])
    with pytest.raises(ValidationError):
        ReportSection.model_validate({'id': 's', 'title': 'x', 'blocks': [
            {'id': 'b', 'kind': 'paragraph', 'text': 'x', 'items': [{'text': 'hidden'}]}]})


def test_generation_preserves_outline_and_never_falls_back_to_v1():
    gateway = Mock()
    expected = report()
    outline = ReportOutline(sections=[{'id': s.id, 'title': s.title, 'purpose': '分析'} for s in expected.sections])
    gateway.structured.side_effect = [outline, expected]
    result = generate_report(gateway, 'draft_writer', {'user_question': '指定结构'}, ChapterAnalysisDraft, prompt_override='write')
    assert result == expected
    assert gateway.structured.call_args.args[1]['report_outline'] == outline.model_dump(mode='json')
    gateway.structured.side_effect = [AnalysisDraft(summary='old')] * 3
    with pytest.raises(ValidationError):
        generate_report(gateway, 'draft_writer', {'user_question': '报告'}, ChapterAnalysisDraft, prompt_override='write')


def test_truncation_generates_each_chapter_with_full_context():
    gateway = Mock()
    expected = report()
    outline = ReportOutline(sections=[{'id': s.id, 'title': s.title, 'purpose': '分析'} for s in expected.sections])
    contexts = []
    def generate(name, context, model, **options):
        contexts.append(deepcopy(context))
        if model is ReportOutline:
            return outline
        if model is ChapterAnalysisDraft:
            raise LLMOutputTruncatedError('length')
        if model is ReportIntroduction:
            return ReportIntroduction(title='报告', summary='概述')
        return expected.sections[len(context['completed_sections'])]
    gateway.structured.side_effect = generate
    result = generate_report(gateway, 'draft_writer', {'user_question': '报告', 'evidence_catalog': []},
                             ChapterAnalysisDraft, prompt_override='write')
    assert [s.title for s in result.sections] == [s.title for s in expected.sections]
    assert len(contexts[-1]['completed_sections']) == 3
    assert all('evidence_catalog' in context for context in contexts)


def test_outline_mismatch_is_repaired_with_finite_attempts():
    gateway = Mock()
    outline = ReportOutline(sections=[{'id': 's0', 'title': '核心结论', 'purpose': '分析'}])
    wrong = ChapterAnalysisDraft(summary='x', sections=[chapter(1, 'wrong')])
    gateway.structured.side_effect = [outline, wrong, wrong, wrong]
    with pytest.raises(ValueError, match='report_outline'):
        generate_report(gateway, 'draft_writer', {'user_question': '报告'}, ChapterAnalysisDraft, prompt_override='write')
    assert gateway.structured.call_count == 4


def test_nested_citations_and_legacy_actions_are_preserved():
    pointer = {'citation_id': 'cite_amount', 'evidence_id': 'ev', 'row_index': 0, 'field': 'amount', 'raw_value': '10'}
    resolved = resolve_citations(report().model_dump(mode='json'), [{'rows': [{'value_pointers': {'amount': pointer}}]}])
    assert resolved['sections'][0]['blocks'][0]['evidence_refs'] == ['ev']
    assert evidence_ids(resolved) == {'ev'}
    legacy = AnalysisDraft(summary='old', insights=[Insight(id='i', type='risk', title='风险', conclusion='事实',
                           significance='影响', action='立即核对账款', evidence_refs=['ev'])])
    original = legacy.model_dump_json()
    assert '立即核对账款' in report_sections(legacy)[0].blocks[0].text
    assert legacy.model_dump_json() == original


def test_html_and_excel_keep_section_order_and_actions(tmp_path, monkeypatch):
    from app.config import settings
    from app.repository import repository
    from app.exports import export_html, export_excel
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'app.sqlite')
    repository.initialize()
    snapshot = repository.get_task(repository.create_task()).model_copy(update={'result': report()})
    html = export_html(snapshot).read_text(encoding='utf-8')
    assert [html.index(f'<h2>{s.title}</h2>') for s in snapshot.result.sections] == sorted(
        html.index(f'<h2>{s.title}</h2>') for s in snapshot.result.sections)
    assert '行动建议正文' in html
    workbook = load_workbook(export_excel(snapshot))
    values = [str(cell.value) for row in workbook['分析摘要'] for cell in row if cell.value]
    assert '行动建议正文' in values
    assert [values.index(s.title) for s in snapshot.result.sections] == sorted(values.index(s.title) for s in snapshot.result.sections)
    workbook.close()
