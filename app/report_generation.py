"""Bounded report generation with lossless context and section fallback."""
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from pydantic import create_model

from .llm import LLMContextOverflowError, LLMStructuredOutputError, LLMOutputTruncatedError, _estimate_tokens
from .config import settings
from .models import ChapterAnalysisDraft, EvidencePointer, ReportOutline, ReportSection, StrictModel
from pydantic import Field, ValidationError


class ReportIntroduction(StrictModel):
    title: str
    summary: str
    summary_evidence_pointers: list[EvidencePointer] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)


def generate_chapter_report(gateway, context, **options):
    context = deepcopy(context)
    catalog = context.get('evidence_catalog', [])
    for key in ('previous_draft', 'previous_result'):
        if context.get(key):
            context[key] = model_citation_context(context[key], catalog)
    context['evidence_catalog'] = model_evidence_catalog(catalog)
    prompt = (options.get('prompt_override') or gateway.load_prompt('draft_writer')) + CITATION_MODEL_RULE
    options = {key: value for key, value in options.items()
               if key not in {'prompt_override', 'ignored_response_fields', 'output_tokens', 'stop_on_length'}}
    notify = options.get('diagnostics') or (lambda value: None)

    def call(model, payload, instruction, tokens=6144, check=None):
        for attempt in range(3):
            try:
                result = gateway.structured('draft_writer', payload, model, **options,
                    prompt_override=prompt + '\n' + instruction, output_tokens=tokens, stop_on_length=True)
                result = model.model_validate(result.model_dump(mode='json'))
                if check:
                    check(result)
                return result
            except (ValidationError, ValueError, LLMStructuredOutputError) as exc:
                if attempt == 2:
                    raise
                payload = {**payload, 'structure_repair': str(exc)[:2000]}
        raise ValueError('Report structure repair exhausted')

    notify({'progress_message': '正在组织报告章节'})
    outline = call(ReportOutline, context,
        '当前只生成章节大纲。用户指定标题和顺序时逐项遵循；未指定时自主组织。'
        '大纲不是摘要，禁止使用固定通用模板替代用户要求。修改旧报告时保留无关章节ID。'
        '每章给出唯一id、title、purpose；正文内容稍后生成。', 1536)
    context['report_outline'] = outline.model_dump(mode='json')

    def check_order(draft):
        if [(s.id, s.title) for s in draft.sections] != [(s.id, s.title) for s in outline.sections]:
            raise ValueError('Sections must exactly follow report_outline IDs, titles and order')

    instruction = ('生成完整v2报告。sections严格遵循report_outline；每章用blocks组织正文。'
                   'summary只写简短概述，不复制章节正文。不要输出旧metrics/findings/insights/charts顶层字段。')
    notify({'progress_message': '正在撰写报告正文'})
    try:
        return call(ChapterAnalysisDraft, context, instruction, check=check_order)
    except (LLMContextOverflowError, LLMOutputTruncatedError):
        pass
    intro = call(ReportIntroduction, context, '仅生成标题、简短摘要、摘要引用、假设、风险和后续问题，不生成正文。', 3072)
    sections = []
    for index, target in enumerate(outline.sections):
        notify({'progress_message': f'正在撰写章节（{index + 1}/{len(outline.sections)}）：{target.title}'})
        def check_section(section):
            if (section.id, section.title) != (target.id, target.title):
                raise ValueError('Section ID and title must match current_section')
        payload = {**context, 'current_section': target.model_dump(mode='json'),
                   'report_introduction': intro.model_dump(mode='json'),
                   'completed_sections': [section.model_dump(mode='json') for section in sections]}
        sections.append(call(ReportSection, payload,
            '仅生成current_section指定章节。完整大纲和前文章节用于保持一致，禁止重复其他章节。'
            '保留证据引用，blocks的ID在整篇报告内唯一。', check=check_section))
    result = ChapterAnalysisDraft(**intro.model_dump(mode='json'), sections=sections)
    check_order(result)
    return result


REPORT_BLOCKS = (
    ('摘要与引用', ('title', 'summary', 'summary_evidence_refs', 'summary_evidence_pointers', 'verification_level')),
    ('指标与引用', ('metrics',)),
    ('发现与引用', ('findings',)),
    ('洞察与引用', ('insights',)),
    ('图表与说明', ('charts', 'assumptions', 'warnings', 'suggested_questions', 'delivery')),
)


def citation_id(evidence_id, row_index, field, value):
    encoded = json.dumps([evidence_id, row_index, field, str(value)], ensure_ascii=False).encode()
    return 'cite_' + hashlib.sha256(encoded).hexdigest()[:24]


def model_evidence_catalog(catalog):
    """Lossless column-oriented row encoding shared by all model nodes."""
    result = deepcopy(catalog)
    for evidence in result:
        if 'row_format' in evidence or any(not isinstance(row, dict) or 'cells' not in row for row in evidence['rows']):
            continue
        source_fields = {key for row in evidence['rows'] for key in row['source_location_not_row_index']}
        evidence['columns'] = [key for key in evidence['columns'] if key not in source_fields]
        evidence['citation_rule'] = 'Select citation_id from the same column as the value. Only citation_id identifies evidence; never infer or output row coordinates.'
        evidence['row_format'] = ['values_in_column_order', 'citation_ids_in_column_order', 'missing_column_indexes']
        encoded = []
        for row in evidence['rows']:
            cells = row['cells']
            encoded.append([
                [cells.get(field) for field in evidence['columns']],
                [row['value_pointers'].get(field, {}).get('citation_id') for field in evidence['columns']],
                [index for index, field in enumerate(evidence['columns']) if field not in cells],
            ])
        evidence['rows'] = encoded
    return result


def model_citation_context(value, catalog):
    """Keep backend coordinates out of model-facing drafts; retain business values."""
    lookup = {(p['evidence_id'], p['row_index'], p['field'], p['raw_value']): p['citation_id']
              for evidence in catalog for row in evidence['rows'] if isinstance(row, dict)
              for p in row.get('value_pointers', {}).values()}
    def visit(item):
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        if 'citation_id' in item or {'evidence_id', 'row_index', 'field', 'raw_value'} <= item.keys():
            cid = item.get('citation_id') or lookup.get((item.get('evidence_id'), item.get('row_index'), item.get('field'), str(item.get('raw_value'))))
            return {**{key: visit(child) for key, child in item.items()
                       if key not in {'evidence_id', 'row_index', 'field', 'raw_value', 'citation_id'}},
                    'citation_id': cid or 'unresolved_citation'}
        return {key: visit(child) for key, child in item.items()}
    return visit(value)


CITATION_MODEL_RULE = (
    '\n若工具结果标记 error，该步骤未完成。使用成功证据给出可支持的部分结论，在 warnings 说明缺口；不得假装完整核对通过。复核允许明确披露缺口的部分报告，不能仅因已披露的查询失败要求循环补查；缺乏证据的结论仍须修正。'
    '\n引用协议：证据目录采用 values_in_column_order 和 citation_ids_in_column_order。'
    '仅使用同列 citation_id 标识引用；不要输出或评判 row_index、Excel 行号或数组位置。'
    '定位信息由程序解析和校验。依据同一行的业务名称、期间、单位和数值判断证据是否支持结论，'
    '不得将合计当作客户明细。修正意见指向报告字段并给出正确 citation_id。'
    '本协议替代旧提示中复制 value_pointers 或核对行号的要求。'
)


def resolve_citations(draft, catalog):
    lookup = {
        pointer['citation_id']: pointer
        for evidence in catalog for row in evidence['rows']
        for pointer in row['value_pointers'].values()
    }
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: visit(item) for key, item in value.items()}
        selected = lookup.get(value.get('citation_id'))
        if selected:
            value.update(selected)
        for prefix in ('', 'summary_'):
            pointers = value.get(prefix + 'evidence_pointers')
            if pointers and all(item.get('citation_id') in lookup for item in pointers):
                def ids(items):
                    return [eid for item in items for eid in [item['evidence_id'], *ids(item.get('input_pointers', []))]]
                value[prefix + 'evidence_refs'] = list(dict.fromkeys(ids(pointers)))
        return value
    return visit(deepcopy(draft))


def revision_fields(context, fields):
    feedback = context.get('revision_feedback') or {}
    # Model review may require coordinated changes across summary, findings,
    # insights and recommendations. Let the writer return the complete report
    # instead of deriving a section restriction from issue targets.
    if (feedback.get('review') or {}).get('issues'):
        return None
    issues = [*(feedback.get('review') or {}).get('issues', []),
              *(feedback.get('structural_validation') or {}).get('issues', [])]
    if not context.get('previous_draft') or not issues:
        return None
    targets = set()
    for issue in issues:
        root = re.split(r'[.\[]', issue.get('target') or '')[0]
        if root not in fields:
            return None
        targets.add(root)
    return targets


def compact_evidence(item):
    columns = list(dict.fromkeys([*item.columns, *(key for row in item.rows for key in row)]))
    source_fields = {'来源行号', 'source_row', 'source_row_number', 'excel_row'}
    rows = []
    for row_index, row in enumerate(item.rows):
        cells = {key: row[key] for key in columns if key in row and key not in source_fields}
        source_location = {key: row[key] for key in columns if key in row and key in source_fields}
        value_pointers = {
            key: {
                'citation_id': citation_id(item.id, row_index, key, value),
                'evidence_id': item.id,
                'row_index': row_index,
                'field': key,
                'raw_value': str(value),
            }
            for key, value in cells.items()
            if _is_numeric_value(value)
        }
        rows.append({
            'row_index': row_index,
            'cells': cells,
            'source_location_not_row_index': source_location,
            'value_pointers': value_pointers,
        })
    return {
        'id': item.id, 'title': item.title, 'source': item.source,
        'data_revision': getattr(item, 'data_revision', None),
        'source_revision_ids': getattr(item, 'source_revision_ids', []),
        'run_id': getattr(item, 'run_id', None), 'query': getattr(item, 'query', None),
        'columns': columns, 'row_count': len(item.rows),
        'citation_rule': (
            'Copy a complete object from rows[].value_pointers for a quantitative evidence pointer. '
            'rows[].row_index is the only valid row_index; source_location_not_row_index is source metadata.'
        ),
        'rows': rows,
    }


def _is_numeric_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(re.fullmatch(
        r'\s*[+-]?(?:\d[\d,]*)(?:\.\d+)?(?:%|元|万元|亿元|千元)?\s*', value,
    ))


def generate_report(gateway, prompt_name, context, response_model, **kwargs):
    if response_model is ChapterAnalysisDraft:
        return generate_chapter_report(gateway, context, **kwargs)
    context = deepcopy(context)
    catalog = context.get('evidence_catalog', [])
    for key in ('previous_draft', 'previous_result'):
        if context.get(key):
            context[key] = model_citation_context(context[key], catalog)
    kwargs['prompt_override'] = (kwargs.get('prompt_override') or gateway.load_prompt(prompt_name)) + CITATION_MODEL_RULE
    # The registry resolves coordinates after generation; only IDs and original
    # cells are needed in model input, avoiding duplicated values and offsets.
    context['evidence_catalog'] = model_evidence_catalog(context.get('evidence_catalog', []))
    notify = kwargs.get('diagnostics') or (lambda value: None)
    notify({'progress_message': '正在整理分析上下文'})
    ignored = kwargs.get('ignored_response_fields', set())
    fields = set(response_model.model_fields) - ignored - {'report_schema_version', 'sections'}
    targets = revision_fields(context, fields)
    feedback = (context.get('revision_feedback') or {}).get('structural_validation') or {}
    issues = feedback.get('issues', [])
    if context.get('previous_draft') and issues and all(
        issue.get('code') in {'invalid_evidence_pointer', 'missing_evidence'} for issue in issues
    ):
        result = deepcopy(context['previous_draft'])
        claim_targets = list(dict.fromkeys(
            re.match(r'^(summary|metrics\[\d+\]|findings\[\d+\]|insights\[\d+\])', issue.get('target') or '').group(0)
            for issue in issues
            if re.match(r'^(summary|metrics\[\d+\]|findings\[\d+\]|insights\[\d+\])', issue.get('target') or '')
        ))
        if len(claim_targets) and all(any((issue.get('target') or '').startswith(target) for target in claim_targets) for issue in issues):
            for target in claim_targets:
                if target == 'summary':
                    names = ['summary', 'summary_evidence_refs', 'summary_evidence_pointers']
                    model = create_model('SummaryRepair', **{name: (response_model.model_fields[name].annotation, deepcopy(response_model.model_fields[name])) for name in names})
                    old = {name: result[name] for name in names}
                else:
                    match = re.fullmatch(r'(\w+)\[(\d+)\]', target)
                    name, index = match[1], int(match[2])
                    model = response_model.model_fields[name].annotation.__args__[0]
                    old = result[name][index]
                repair_context = {**context, 'repair_target': target, 'previous_draft': old,
                                  'repair_instruction': '仅返回该结论对象。优先选择 citation_id 修复引用，不改动有效事实。无法支持原结论时修改该结论以忠实反映证据，禁止猜测坐标。'}
                repair_context['previous_invalid_citations'] = old.get('evidence_pointers', old.get('summary_evidence_pointers', []))
                options = {**kwargs, 'ignored_response_fields': set()}
                options['output_tokens'] = 1536
                options['prompt_override'] = (kwargs.get('prompt_override') or gateway.load_prompt(prompt_name)) + '\n当前是局部修复，仅输出 repair_target 对应对象，遵守本次 Schema。不得输出完整报告。'
                repaired = gateway.structured(prompt_name, repair_context, model, **options).model_dump(mode='json')
                if repaired == old:
                    notify({'progress_message': '引用修正未改变原结果，将重新校验并保留未解决错误'})
                if target == 'summary':
                    result.update(repaired)
                else:
                    result[name][index] = repaired
            return response_model.model_validate(result)
    try:
        notify({'progress_message': '正在修正报告引用' if context.get('previous_draft') else '正在生成报告'})
        if targets is None:
            return gateway.structured(prompt_name, context, response_model, **kwargs,
                                      output_tokens=max(6144, settings.model_draft_output_tokens), stop_on_length=True)
    except (LLMContextOverflowError, LLMOutputTruncatedError):
        pass
    except LLMStructuredOutputError as exc:
        if 'done_reason=length' not in str(exc):
            raise

    # Each section receives the same complete evidence; no evidence is sampled.
    result = deepcopy(context['previous_draft']) if targets is not None else {}
    blocks = [(label, [name for name in names if name in fields]) for label, names in REPORT_BLOCKS]
    covered = {name for _, names in blocks for name in names}
    blocks += [('其他内容', sorted(fields - covered))]
    blocks = [(label, names) for label, names in blocks if names and (targets is None or targets.intersection(names))]
    for index, (label, names) in enumerate(blocks, 1):
        notify({'progress_message': f'正在分段生成报告（{index}/{len(blocks)}：{label}）'})
        section_model = create_model('ReportBlock_' + str(index), **{
            name: (response_model.model_fields[name].annotation, deepcopy(response_model.model_fields[name]))
            for name in names})
        section_context = {**context, 'report_section': names,
                           'report_identity': {key: result[key] for key in ('title', 'summary') if key in result}}
        old = context.get('previous_draft')
        if old:
            section_context['previous_draft'] = {name: old[name] for name in names if name in old}
        options = {**kwargs, 'ignored_response_fields': set()}
        options['prompt_override'] = (kwargs.get('prompt_override') or gateway.load_prompt(prompt_name)) + (
            '\n当前为分段生成。仅输出 report_section 指定字段的 JSON 对象，遵守本次 Schema。'
            '依据同一任务要求、分析计划和完整证据生成本部分；不要输出其他字段，不要改变数值或证据引用。'
            '正文与其证据引用须一起核对。证据 ID 列表仅列实际引用的 ID，同一列表不要循环重复 ID。'
        )
        previous_size = _estimate_tokens(str(section_context.get('previous_draft') or ''))
        options['output_tokens'] = max(3072, previous_size + 1024)
        try:
            section = gateway.structured(prompt_name, section_context, section_model, **options)
        except LLMContextOverflowError as exc:
            raise LLMContextOverflowError(f'报告分段 {names} 的完整证据与输出预算仍超出容量；未删除证据或交付不完整报告。{exc}') from exc
        result.update(section.model_dump(mode='json'))
    return response_model.model_validate(result)
