"""Deterministic, evidence-backed analysis conclusions.

The workflow may use an LLM to phrase a report, but all numeric conclusions in
this module are calculated from tool rows.  This keeps the product focused on
what the data means rather than reproducing the source spreadsheet.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import EvidencePointer, Insight


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _fmt(value: Decimal, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def _pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}%"


def _pointer(evidence_id: str, index: int, field: str, value: Any, *, unit: str | None = None,
             source_type: str = "cell", formula: str | None = None,
             inputs: list[EvidencePointer] | None = None) -> EvidencePointer:
    return EvidencePointer(
        evidence_id=evidence_id,
        row_index=max(0, index),
        field=field,
        raw_value=str(value),
        unit=unit,
        source_type=source_type,
        formula=formula,
        input_pointers=inputs or [],
    )


def _financial_rows(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[tuple[str, str, str], tuple[int, dict[str, Any]]]]:
    evidence_id = (result.get("evidence_ids") or [""])[0]
    rows = list(result.get("rows") or [])
    located: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        key = (str(row.get("报表", "")), str(row.get("项目", "")), str(row.get("口径", "")))
        if key[0] and key not in located:
            located[key] = (index, row)
    return evidence_id, rows, located


def financial_insights(result: dict[str, Any]) -> list[Insight]:
    """Build conclusions from the normalized financial report evidence."""
    evidence_id, _rows, located = _financial_rows(result)
    if not evidence_id:
        return []
    insights: list[Insight] = []

    def get(*key: str) -> tuple[int, dict[str, Any], Decimal] | None:
        item = located.get(tuple(key))
        if item is None:
            return None
        index, row = item
        value = _decimal(row.get("金额"))
        return (index, row, value) if value is not None else None

    def ratio_insight(insight_id: str, title: str, numerator: tuple[int, dict[str, Any], Decimal],
                      denominator: tuple[int, dict[str, Any], Decimal], formula: str,
                      action: str, numerator_inputs: list[tuple[int, dict[str, Any], Decimal]] | None = None) -> None:
        ni, nr, nv = numerator
        di, dr, dv = denominator
        if dv == 0:
            return
        value = (nv / dv * 100).quantize(Decimal("0.01"))
        inputs = [
            *[_pointer(evidence_id, index, "金额", row.get("金额")) for index, row, _value in (numerator_inputs or [numerator])],
            _pointer(evidence_id, di, "金额", dr.get("金额")),
        ]
        insights.append(Insight(
            id=insight_id, type="ratio", title=title,
            conclusion=f"{title}为{value}%。", significance="该比率提供了可比较的经营效率口径。",
            action=action, value=str(value), unit="%", evidence_refs=[evidence_id],
            evidence_pointers=[_pointer(evidence_id, ni, "金额", value, unit="%", source_type="derived",
                                        formula=formula, inputs=inputs)],
            formula=formula, input_labels=["分子", "分母"],
        ))

    revenue = get("利润表", "营业收入", "本月金额")
    cost = get("利润表", "营业成本", "本月金额")
    gross_profit = None
    if revenue and cost and revenue[2] != 0:
        gross_profit = (revenue[0], revenue[1], revenue[2] - cost[2])
        ratio_insight("gross_margin", "毛利率", gross_profit, revenue,
                      "(营业收入-营业成本)/营业收入*100", "检查成本结构和主要低毛利业务。",
                      numerator_inputs=[revenue, cost])

    net_profit = get("利润表", "净利润", "本月金额")
    if revenue and net_profit:
        ratio_insight("net_margin", "净利润率", net_profit, revenue,
                      "净利润/营业收入*100", "结合费用明细检查利润被进一步压缩的项目。")

    op_profit = get("利润表", "营业利润", "本月金额")
    if revenue and op_profit:
        ratio_insight("operating_margin", "营业利润率", op_profit, revenue,
                      "营业利润/营业收入*100", "关注营业成本及期间费用对经营利润的影响。")

    expense = get("费用明细", "明细汇总", "本月发生额")
    if revenue and expense and revenue[2] != 0:
        ratio_insight("expense_ratio", "期间费用率", expense, revenue,
                      "费用明细合计/营业收入*100", "优先核查占比最高且非一次性的费用项目。")

    ar_open = get("应收账款", "明细汇总", "月初余额")
    ar_income = get("应收账款", "明细汇总", "本月服务收入")
    ar_collection = get("应收账款", "明细汇总", "本月收款")
    ar_end = get("应收账款", "明细汇总", "月末余额")
    ar_overdue = get("应收账款", "明细汇总", "逾期余额")
    if ar_open and ar_income and ar_collection:
        denominator = ar_open[2] + ar_income[2]
        if denominator != 0:
            ratio_insight("ar_collection_rate", "应收回款率",
                          (ar_collection[0], ar_collection[1], ar_collection[2]),
                          (ar_open[0], ar_open[1], denominator),
                          "本月收款/(月初应收+本月服务收入)*100", "对未回款客户安排分层催收并核对信用期限。")
    if ar_end and ar_overdue and ar_end[2] != 0:
        ratio_insight("ar_overdue_ratio", "应收逾期占比", ar_overdue, ar_end,
                      "逾期余额/应收月末余额*100", "优先处理逾期金额最高的客户并确认回款计划。")

    cash_begin = get("现金流量表", "月初现金及现金等价物余额", "本月金额")
    cash_end = get("现金流量表", "月末现金及现金等价物余额", "本月金额")
    operating_cash = get("现金流量表", "经营活动现金流量净额", "本月金额")
    if cash_begin and cash_end:
        change = cash_end[2] - cash_begin[2]
        refs = [
            _pointer(evidence_id, cash_begin[0], "金额", cash_begin[1].get("金额")),
            _pointer(evidence_id, cash_end[0], "金额", cash_end[1].get("金额")),
        ]
        direction = "增加" if change > 0 else "减少" if change < 0 else "未变"
        insights.append(Insight(
            id="cash_change", type="change", title="现金余额变化",
            conclusion=f"期末现金较期初{direction}{_fmt(abs(change))}。",
            significance="现金变化反映本期流动性结果。",
            action="结合经营、投资和筹资现金流拆分变化来源。",
            value=_fmt(change), unit="元", evidence_refs=[evidence_id],
            evidence_pointers=[_pointer(evidence_id, cash_end[0], "金额", change, unit="元",
                                        source_type="derived", formula="期末现金-期初现金", inputs=refs)],
            formula="期末现金-期初现金", input_labels=["期初现金", "期末现金"],
        ))
    if operating_cash and net_profit:
        gap = operating_cash[2] - net_profit[2]
        refs = [
            _pointer(evidence_id, operating_cash[0], "金额", operating_cash[1].get("金额")),
            _pointer(evidence_id, net_profit[0], "金额", net_profit[1].get("金额")),
        ]
        insights.append(Insight(
            id="cash_profit_gap", type="comparison", title="经营现金流与净利润差异",
            conclusion=f"经营现金流较净利润{'高' if gap > 0 else '低' if gap < 0 else '持平'}{_fmt(abs(gap))}。",
            significance="差异提示利润确认与现金收付之间存在结构性影响。",
            action="结合应收、应付、折旧和利息项目检查差异来源。",
            value=_fmt(gap), unit="元", evidence_refs=[evidence_id],
            evidence_pointers=[_pointer(evidence_id, operating_cash[0], "金额", gap, unit="元",
                                        source_type="derived", formula="经营现金流-净利润", inputs=refs)],
            formula="经营现金流-净利润", input_labels=["经营现金流", "净利润"],
        ))

    warnings = result.get("warnings") or []
    for index, warning in enumerate(warnings):
        insights.append(Insight(
            id=f"reconciliation_warning_{index}", type="reconciliation",
            title="勾稽核对存在差异", conclusion=str(warning),
            significance="来源之间存在未解释差异，可能影响相关结论的可靠性。",
            action="定位差异对应的明细、期间或口径后再用于决策。",
            severity="warning", evidence_refs=[evidence_id],
        ))
    return insights


def generic_insights(result: dict[str, Any]) -> list[Insight]:
    """Produce safe ranking/contribution conclusions for grouped query output."""
    evidence_id = (result.get("evidence_ids") or [""])[0]
    rows = list(result.get("rows") or [])
    if not evidence_id or len(rows) < 2:
        return []
    args = result.get("arguments") or {}
    dimensions = list(args.get("dimensions") or [])
    measures = list(args.get("measures") or [])
    dimension = dimensions[0] if dimensions else next((k for k in rows[0] if not isinstance(rows[0].get(k), (int, float))), None)
    measure = measures[0] if measures and isinstance(measures[0], str) else next((k for k, v in rows[0].items() if _decimal(v) is not None), None)
    if not dimension or not measure:
        return []
    numeric = [(_decimal(row.get(measure)), index, row) for index, row in enumerate(rows)]
    numeric = [(value, index, row) for value, index, row in numeric if value is not None]
    if len(numeric) < 2:
        return []
    numeric.sort(key=lambda item: item[0], reverse=True)
    top_value, top_index, top_row = numeric[0]
    total = sum((value for value, _, _ in numeric), Decimal(0))
    share = (top_value / total * 100).quantize(Decimal("0.01")) if total else None
    pointer = _pointer(evidence_id, top_index, measure, top_row.get(measure))
    insights = [Insight(
        id="top_group", type="ranking", title=f"{dimension}头部排名",
        conclusion=f"{top_row.get(dimension)}的{measure}最高，为{top_row.get(measure)}。",
        significance="头部对象是当前结果的主要贡献来源。",
        action=f"优先检查{top_row.get(dimension)}的规模、质量和可持续性。",
        value=str(top_row.get(measure)), evidence_refs=[evidence_id], evidence_pointers=[pointer],
    )]
    if share is not None:
        inputs = [_pointer(evidence_id, index, measure, row.get(measure)) for _, index, row in numeric]
        insights.append(Insight(
            id="top_group_share", type="contribution", title="头部贡献度",
            conclusion=f"{top_row.get(dimension)}占{measure}合计的{share}%。",
            significance="贡献度越高，整体结果越依赖该对象。",
            action="制定头部对象维护方案，同时降低单一对象集中风险。",
            value=str(share), unit="%", evidence_refs=[evidence_id],
            evidence_pointers=[_pointer(evidence_id, top_index, measure, share, unit="%",
                                        source_type="derived", formula="头部值/各组值合计*100", inputs=inputs)],
            formula="头部值/各组值合计*100", input_labels=["头部值", "各组值"],
        ))
    negatives = [(index, row) for value, index, row in numeric if value < 0]
    if negatives:
        index, row = negatives[0]
        insights.append(Insight(
            id="negative_value", type="anomaly", title="存在负值记录",
            conclusion=f"{row.get(dimension)}的{measure}为负值{row.get(measure)}。",
            significance="负值可能代表冲销、退货或数据录入异常。",
            action="核对该记录的业务类型和原始凭证。", severity="warning",
            evidence_refs=[evidence_id], evidence_pointers=[_pointer(evidence_id, index, measure, row.get(measure))],
        ))
    return insights


def overdue_insights(result: dict[str, Any]) -> list[Insight]:
    evidence_id = (result.get("evidence_ids") or [""])[0]
    rows = list(result.get("rows") or [])
    if not evidence_id or not rows:
        return []
    row = rows[0]
    customer = row.get("客户名称") or row.get("客户") or "逾期客户"
    amount = row.get("逾期金额")
    if amount is None:
        return []
    return [Insight(
        id="overdue_top_customer", type="ranking", title="逾期金额最高客户",
        conclusion=f"{customer}的逾期金额最高，为{amount}。",
        significance="该客户是当前应收逾期风险的首要来源。",
        action="优先确认回款计划、信用期限和逾期原因。",
        value=str(amount), evidence_refs=[evidence_id],
        evidence_pointers=[_pointer(evidence_id, 0, "逾期金额", amount)],
    )]


def derive_insights(result: dict[str, Any], strategy: str | None = None) -> list[Insight]:
    if strategy == "financial_report":
        return financial_insights(result)
    if strategy == "derived_metric":
        rows = list(result.get("rows") or [])
        evidence_id = (result.get("evidence_ids") or [""])[0]
        if not rows or not evidence_id:
            return []
        row = rows[0]
        metric = str(row.get("指标") or result.get("arguments", {}).get("metric") or "派生指标")
        value = str(row.get("百分比") or "")
        pointer = _pointer(evidence_id, 0, "百分比", value, unit="%")
        return [Insight(
            id="derived_metric", type="ratio", title=metric,
            conclusion=f"{metric}为{value}%。",
            significance="该指标由同一数据版本中的原始金额计算得到。",
            action="补充同期、预算或目标值后进行趋势和达标判断。",
            value=value, unit="%", evidence_refs=[evidence_id], evidence_pointers=[pointer],
            formula=str(row.get("公式") or result.get("arguments", {}).get("formula") or ""),
            input_labels=["计算输入"],
        )]
    if strategy == "overdue_ranking":
        return overdue_insights(result)
    return generic_insights(result)
