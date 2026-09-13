"""Reuse versioned financial evidence; all arithmetic runs outside the model."""
from decimal import Decimal
import re

from .models import AnalysisDraft, EvidencePointer, Metric
from .repository import repository

FORMULAS = {
    '净利润率': [('利润表', '净利润', '本月金额'), ('利润表', '营业收入', '本月金额')],
    '毛利率': [('利润表', '营业收入', '本月金额'), ('利润表', '营业成本', '本月金额')],
    '营业利润率': [('利润表', '营业利润', '本月金额'), ('利润表', '营业收入', '本月金额')],
    '资产负债率': [('资产负债表', '负债合计', '月末余额'), ('资产负债表', '资产总计', '月末余额')],
}


def requested_metric(question: str) -> str | None:
    for name in FORMULAS:
        if name in question or (name == '净利润率' and '净利率' in question):
            return name
    return 'ambiguous' if '利润率' in question else None


def clearly_off_topic(question: str) -> bool:
    return bool(re.search(r'天气|翻译|写邮件|写文章|讲笑话|写代码', question))


def eligible_evidence(state):
    revision = repository.get_run_by_id(state.run_id).data_revision
    dataset_ids = {d['id'] for d in state.datasets}
    provenance = {}
    for evidence in repository.iter_current_evidence(state.task_id, revision, state.run_id):
        if evidence.data_revision != revision or not evidence.source_dataset_ids:
            continue
        if not set(evidence.source_dataset_ids) <= dataset_ids:
            continue
        source_key = tuple(sorted(evidence.source_dataset_ids))
        if source_key not in provenance:
            provenance[source_key] = {revision_id for revision_id, _ in repository.source_revision_provenance(
                state.task_id, evidence.source_dataset_ids)}
        current_sources = provenance[source_key]
        if not current_sources or set(evidence.source_revision_ids) != current_sources:
            continue
        if {'报表', '项目', '口径', '金额'} <= set(evidence.columns):
            yield evidence


def reuse_explanation(state):
    from .analysis_tools import _persist_result
    evidence = next(eligible_evidence(state), None)
    if evidence is None and state.previous_result:
        # Generic analyses do not necessarily have the financial report
        # columns required by ``eligible_evidence``.  Reuse the exact evidence
        # IDs carried by the previous result so contextual follow-ups can be
        # answered without inventing a new query or asking the user to restate
        # the dataset.
        evidence_ids = list(state.previous_result.get("summary_evidence_refs") or [])
        for key in ("metrics", "findings", "insights"):
            for item in state.previous_result.get(key) or []:
                if isinstance(item, dict):
                    evidence_ids.extend(item.get("evidence_refs") or [])
        revision = repository.get_run_by_id(state.run_id).data_revision
        allowed = {item["id"] for item in state.datasets}
        for evidence_id in dict.fromkeys(evidence_ids):
            candidate = repository.get_evidence(state.task_id, evidence_id)
            if candidate and candidate.data_revision == revision and candidate.rows and set(candidate.source_dataset_ids) <= allowed:
                evidence = candidate
                break
    if evidence is None:
        return None
    return _persist_result(state.task_id, 'query_data', {'strategy': 'explanation',
        'source_evidence_ids': [evidence.id]}, '当前版本财务证据', evidence.rows, state.run_id,
        source_dataset_ids=evidence.source_dataset_ids)


def calculate_metric(state, metric):
    from .analysis_tools import _persist_result
    inputs = FORMULAS[metric]
    for evidence in eligible_evidence(state):
        located = [[(i, r) for i, r in enumerate(evidence.rows)
                    if (r['报表'], r['项目'], r['口径']) == key] for key in inputs]
        # Never combine different report sets or ambiguous periods.
        if not all(len(matches) == 1 for matches in located):
            continue
        try:
            values = [Decimal(str(matches[0][1]['金额'])) for matches in located]
        except Exception:
            continue
        if not all(v.is_finite() for v in values):
            continue
        numerator, denominator = ((values[0] - values[1], values[0]) if metric == '毛利率' else values)
        if denominator == 0:
            return None, '分母为零，无法计算该比率。'
        percent = (numerator / denominator * 100).quantize(Decimal('0.01'))
        pointers = [{'evidence_id': evidence.id, 'row_index': m[0][0], 'field': '金额',
                     'raw_value': str(m[0][1]['金额'])} for m in located]
        formula = '(营业收入-营业成本)/营业收入*100' if metric == '毛利率' else f'{inputs[0][1]}/{inputs[1][1]}*100'
        result = _persist_result(state.task_id, 'query_data',
            {'strategy': 'derived_metric', 'metric': metric, 'formula': formula, 'formula_version': 1,
             'input_pointers': pointers, 'data_revision': evidence.data_revision},
            f'{metric}派生计算', [{'指标': metric, '百分比': str(percent), '单位': '%',
                                 '公式': formula, '分子': str(numerator), '分母': str(denominator)}],
            state.run_id, source_dataset_ids=evidence.source_dataset_ids)
        return result, None
    return None, '当前有效证据缺少同期间、同口径的输入，请确认报表期间和相关金额。'


def metric_draft(result):
    row = result['rows'][0]
    eid = result['evidence_ids'][0]
    pointer = EvidencePointer(evidence_id=eid, row_index=0, field='百分比', raw_value=row['百分比'], unit='%')
    return AnalysisDraft(title=f"{row['指标']}回答", summary=f"{row['指标']}为{row['百分比']}%。"
        '该结果按当前报表口径计算；缺少同期、预算或可比基准，不能仅据此判断高低。',
        summary_evidence_refs=[eid], summary_evidence_pointers=[pointer],
        metrics=[Metric(label=row['指标'], value=f"{row['百分比']}%", evidence_refs=[eid], evidence_pointers=[pointer])],
        assumptions=['计算公式及输入值保存在本轮计算证据中；仅复用同一数据版本、同一证据表中的输入。'])
