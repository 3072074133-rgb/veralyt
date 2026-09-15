"""Review complete evidence against bounded report sections."""
from copy import deepcopy
import re

from .llm import LLMContextOverflowError, LLMOutputTruncatedError
from .models import ReflectionDecision
from .report_generation import model_evidence_catalog, model_citation_context, CITATION_MODEL_RULE


def citation_audit(draft, catalog):
    """Expose exact citation resolution facts without making a review decision."""
    registry = {
        pointer['citation_id']: {
            'evidence_id': pointer['evidence_id'],
            'field': pointer['field'],
            'raw_value': pointer['raw_value'],
            'row_cells': row['cells'],
        }
        for evidence in catalog for row in evidence['rows']
        for pointer in row['value_pointers'].values()
    }

    def collect(value):
        if isinstance(value, list):
            return [citation for item in value for citation in collect(item)]
        if not isinstance(value, dict):
            return []
        own = [value['citation_id']] if value.get('citation_id') else []
        return [*own, *(citation for item in value.values() for citation in collect(item))]

    referenced = list(dict.fromkeys(collect(draft)))
    return {
        'referenced_count': len(referenced),
        'missing_citation_ids': [citation for citation in referenced if citation not in registry],
        'resolved_citations': {
            citation: registry[citation] for citation in referenced if citation in registry
        },
    }


def review_facts(decision, draft, catalog):
    """Resolve review claims without deciding whether the business verdict is right."""
    registry = {}
    for evidence in catalog:
        for row in evidence['rows']:
            for pointer in row['value_pointers'].values():
                registry[pointer['citation_id']] = {
                    'evidence_title': evidence['title'], 'cells': row['cells'],
                    'field': pointer['field'], 'raw_value': pointer['raw_value'],
                }
    def citations(value):
        if isinstance(value, list):
            return list(dict.fromkeys(cid for item in value for cid in citations(item)))
        if isinstance(value, dict):
            return list(dict.fromkeys(([value['citation_id']] if value.get('citation_id') else []) +
                                     [cid for item in value.values() for cid in citations(item)]))
        return []
    facts = []
    for issue in decision.issues:
        target = issue.target or ''
        match = re.match(r'^(metrics|findings|insights)\[(\d+)\]', target)
        claim = None
        if match:
            items = draft.get(match[1], [])
            index = int(match[2])
            if index < len(items):
                claim = items[index]
        elif target.startswith('summary'):
            claim = {key: value for key, value in draft.items() if key.startswith('summary')}
        elif target.startswith('sections'):
            claim = draft
            for token in re.findall(r'[A-Za-z_]+|\d+', target):
                if isinstance(claim, dict):
                    claim = claim.get(token)
                elif isinstance(claim, list) and token.isdigit() and int(token) < len(claim):
                    claim = claim[int(token)]
                else:
                    claim = None
                    break
        mentioned = list(dict.fromkeys(re.findall(r'cite_[A-Za-z0-9_]+', issue.model_dump_json())))
        if not mentioned:
            continue
        actual = citations(claim)
        facts.append({'target': target, 'target_claim': claim, 'actual_citation_ids': actual,
            'citations': [{'citation_id': cid, 'exists': cid in registry,
                           'used_by_target_claim': cid in actual if claim is not None else None,
                           'source': registry.get(cid)} for cid in mentioned]})
    return facts


def generate_review(gateway, context, **options):
    context = deepcopy(context)
    context['citation_audit'] = citation_audit(
        context['analysis_draft'], context.get('evidence_catalog', [])
    )
    context['analysis_draft'] = model_citation_context(context['analysis_draft'], context.get('evidence_catalog', []))
    context['evidence_catalog'] = model_evidence_catalog(context.get('evidence_catalog', []))
    options['prompt_override'] = (options.get('prompt_override') or gateway.load_prompt('final_reviewer')) + CITATION_MODEL_RULE
    def call(payload):
        decision = gateway.structured('final_reviewer', payload, ReflectionDecision,
                                  stop_on_length=True, **options)
        return decision
    try:
        return call(context)
    except (LLMContextOverflowError, LLMOutputTruncatedError):
        pass
    draft = context['analysis_draft']
    units = []
    for field, value in draft.items():
        if isinstance(value, list) and field in {'metrics', 'findings', 'insights', 'charts', 'sections'}:
            units.extend((f'{field}[{index}]', {field: [item]}) for index, item in enumerate(value))
        elif field not in {'summary', 'summary_evidence_refs', 'summary_evidence_pointers'}:
            units.append((field, {field: value}))
    units.insert(0, ('summary', {key: value for key, value in draft.items() if key.startswith('summary')}))
    decisions = []
    for target, section in units:
        payload = {**context, 'analysis_draft': section, 'review_target': target,
                   'review_scope': '仅复核本片段及原分析计划。完整证据未删减。issues.target 使用 review_target 的原报告坐标；不要将其他片段未提供当作报告缺失。'}
        try:
            decisions.append(call(payload))
        except LLMContextOverflowError as exc:
            raise LLMContextOverflowError(f'复核片段 {target} 加完整证据仍超出容量；未截断证据，也未判定报告通过。{exc}') from exc
    # All source checks completed above. This separate pass checks the full
    # report for contradictions, without pretending to recheck source cells.
    return call({key: value for key, value in {
        **context, 'evidence_catalog': [],
        'completed_section_reviews': [item.model_dump(mode='json') for item in decisions],
        'review_scope': '各片段已分别检查，判断详见 completed_section_reviews，可能包含修改意见。由你统一决定最终 verdict 和 route，程序不合并否决。检查跨片段一致性和用户要求。',
    }.items()})
