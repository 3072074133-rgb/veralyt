"""Bounded report generation with lossless context and section fallback."""
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from pydantic import create_model

from .llm import LLMContextOverflowError, LLMStructuredOutputError, LLMOutputTruncatedError, _estimate_tokens
from .config import settings


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
    context = deepcopy(context)
    # The registry resolves coordinates after generation; only IDs and original
    # cells are needed in model input, avoiding duplicated values and offsets.
    for evidence in context.get('evidence_catalog', []):
        if any(not isinstance(row, dict) or 'value_pointers' not in row for row in evidence['rows']):
            continue
        evidence['rows'] = [
            {'row_index': row['row_index'], 'cells': row['cells'], 'source_location_not_row_index': row['source_location_not_row_index'],
             'value_pointers': {field: {'citation_id': pointer['citation_id']}
                                for field, pointer in row['value_pointers'].items()}}
            for row in evidence['rows']
        ]
    notify = kwargs.get('diagnostics') or (lambda value: None)
    notify({'progress_message': '正在整理分析上下文'})
    ignored = kwargs.get('ignored_response_fields', set())
    fields = set(response_model.model_fields) - ignored
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
                repair_context['evidence_catalog'] = [
                    {**{key: value for key, value in evidence.items() if key != 'rows'},
                     'row_format': ['cells_in_column_order', 'citation_ids_in_column_order', 'source_location'],
                     'rows': [[
                         [row['cells'].get(field) for field in evidence['columns']],
                         [row['value_pointers'].get(field, {}).get('citation_id') for field in evidence['columns']],
                         row['source_location_not_row_index'],
                     ] for row in evidence['rows']]}
                    for evidence in context.get('evidence_catalog', [])
                ]
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
