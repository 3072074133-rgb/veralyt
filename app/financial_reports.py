"""Financial statement extraction: balances are not additive transaction rows."""
from __future__ import annotations

from decimal import Decimal
from .config import settings
from .models import DatasetInfo


FIELDS = {
    '利润表': ['本月金额'], '资产负债表': ['月初余额', '月末余额'],
    '现金流量表': ['本月金额'], '费用明细': ['本月发生额', '本月现金支付', '非现金费用'],
    '应收账款': ['月初余额', '本月服务收入', '本月收款', '月末余额', '逾期余额'],
    '应付账款': ['月初余额', '本月服务成本', '本月付款', '月末余额', '逾期余额'],
}
DETAIL_SHEETS = {'费用明细', '应收账款', '应付账款'}


def financial_kind(dataset: DatasetInfo) -> str | None:
    name = dataset.source_region.sheet_name if dataset.source_region else dataset.display_name
    columns = {c.name for c in dataset.columns}
    if name in FIELDS and {'来源行号', '报表行类型', *FIELDS[name]} <= columns:
        return name
    return None


def financial_query(datasets: list[DatasetInfo]) -> str:
    def quote(s: str) -> str:
        return '"' + s.replace('"', '""') + '"'
    parts = []
    for dataset in datasets:
        kind = financial_kind(dataset)
        if not kind:
            continue
        label = quote(dataset.columns[0].name)
        for field in FIELDS[kind]:
            parts.append(f"SELECT '{kind}' AS 报表, regexp_replace({label}, "
                         "'^(?:[一二三四五六七八九十]+、|加[：:]|减[：:])\\s*', '') AS 项目, "
                         f"'{field}' AS 口径, TRY_CAST({quote(field)} AS DECIMAL(24,6)) AS 金额, "
                         f"来源行号, 报表行类型 AS 类型 FROM {quote(dataset.table_name)} "
                         f"WHERE 报表行类型 <> 'note' AND {quote(field)} IS NOT NULL")
    return ' UNION ALL '.join(parts)


def query_financial_report(task_id: str, datasets: list[DatasetInfo], run_id: str | None = None):
    from .analysis_tools import query_data, _persist_result, ToolError
    sql = financial_query(datasets)
    result = query_data(task_id, datasets, sql, '财务报表原始项目', run_id)
    if len(result.rows) >= settings.max_query_rows:
        raise ToolError('财务报表结果达到查询行数上限，不能使用截断明细核对')
    rows = list(result.rows)
    kinds = {financial_kind(d) for d in datasets}
    if len(kinds) != len(datasets):
        raise ToolError('存在同名财务报表，无法确定报告期间，请明确选择一套报表')
    checks = []

    def amount(sheet: str, item: str, field: str) -> Decimal:
        matches = [r for r in rows if (r['报表'], r['项目'], r['口径']) == (sheet, item, field)]
        if len(matches) != 1 or matches[0]['金额'] is None:
            raise ToolError(f'财务项目缺失、重复或不是有效金额：{sheet}/{item}/{field}')
        return Decimal(str(matches[0]['金额']))

    def check(label: str, difference: Decimal):
        checks.append({'报表': '勾稽核对', '项目': label, '口径': '差额', '金额': str(difference),
                       '来源行号': None, '类型': 'check'})

    for kind in sorted(kinds & DETAIL_SHEETS):
        for field in FIELDS[kind]:
            detail = [r for r in rows if r['报表'] == kind and r['口径'] == field and r['类型'] == 'detail']
            if not detail or any(r['金额'] is None for r in detail):
                raise ToolError(f'{kind}/{field}没有完整的可计算明细')
            total = sum((Decimal(str(r['金额'])) for r in detail), Decimal(0))
            totals = [r for r in rows if r['报表'] == kind and r['口径'] == field and r['项目'] == '合计']
            if totals:
                check(f'{kind}{field}明细与合计', total - amount(kind, '合计', field))
            rows.append({'报表': kind, '项目': '明细汇总', '口径': field, '金额': str(total),
                         '来源行号': None, '类型': 'derived'})
        if kind in {'应收账款', '应付账款'}:
            occurrence, payment = (('本月服务收入', '本月收款') if kind == '应收账款'
                                   else ('本月服务成本', '本月付款'))
            check(f'{kind}期初发生收付期末', amount(kind, '明细汇总', '月初余额')
                  + amount(kind, '明细汇总', occurrence) - amount(kind, '明细汇总', payment)
                  - amount(kind, '明细汇总', '月末余额'))
    if '资产负债表' in kinds:
        for field in FIELDS['资产负债表']:
            check(f'资产负债平衡{field}', amount('资产负债表', '资产总计', field)
                  - amount('资产负债表', '负债和所有者权益总计', field))
        for kind in sorted(kinds & {'应收账款', '应付账款'}):
            for field in FIELDS['资产负债表']:
                check(f'{kind}与主表{field}', amount(kind, '明细汇总', field)
                      - amount('资产负债表', kind, field))
    if {'资产负债表', '现金流量表'} <= kinds:
        check('月末现金与资产负债表', amount('现金流量表', '月末现金及现金等价物余额', '本月金额')
              - amount('资产负债表', '货币资金', '月末余额'))
        check('现金期初加净变动等于期末', amount('资产负债表', '货币资金', '月初余额')
              + amount('现金流量表', '现金及现金等价物净增加额', '本月金额')
              - amount('资产负债表', '货币资金', '月末余额'))
    if {'利润表', '应收账款'} <= kinds:
        check('收入与应收发生额', amount('利润表', '营业收入', '本月金额')
              - amount('应收账款', '明细汇总', '本月服务收入'))
    if {'利润表', '应付账款'} <= kinds:
        check('成本与应付发生额', amount('利润表', '营业成本', '本月金额')
              - amount('应付账款', '明细汇总', '本月服务成本'))
    if '利润表' in kinds:
        check('利润总额减所得税等于净利润', amount('利润表', '利润总额', '本月金额')
              - amount('利润表', '所得税费用', '本月金额') - amount('利润表', '净利润', '本月金额'))
    if {'利润表', '费用明细'} <= kinds:
        check('期间费用与明细', sum((amount('利润表', name, '本月金额')
              for name in ('销售费用', '管理费用', '财务费用')), Decimal(0))
              - amount('费用明细', '明细汇总', '本月发生额'))
    if {'利润表', '资产负债表', '现金流量表', '费用明细'} <= kinds:
        # Indirect cash reconciliation does not assume profit equals collections.
        adjusted = (amount('利润表', '净利润', '本月金额')
                    + amount('费用明细', '明细汇总', '非现金费用')
                    + amount('利润表', '财务费用', '本月金额')
                    + amount('资产负债表', '应收账款', '月初余额')
                    - amount('资产负债表', '应收账款', '月末余额')
                    + amount('资产负债表', '应付账款', '月末余额')
                    - amount('资产负债表', '应付账款', '月初余额')
                    + amount('资产负债表', '应交税费', '月末余额')
                    - amount('资产负债表', '应交税费', '月初余额'))
        check('简化服务业务净利润调整经营现金流', adjusted
              - amount('现金流量表', '经营活动现金流量净额', '本月金额'))
    derived = _persist_result(task_id, 'query_data', {'strategy': 'financial_report', 'sql': sql,
        'source_evidence_ids': result.evidence_ids, 'checks': checks}, '月度财务报表项目及核对',
        rows + checks, run_id, source_dataset_ids=[d.id for d in datasets])
    return derived
