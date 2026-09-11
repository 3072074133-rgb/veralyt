from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .models import AnalysisDraft, ChartSeries, ChartSpec, EvidencePointer, EvidenceRecord, Metric


TEMPORAL_NAMES = ("日期", "时间", "期间", "月份", "年度", "year", "month", "date", "period")
PERCENTAGE_NAMES = ("占比", "百分比", "比例", "比率", "率", "percentage", "percent", "ratio", "rate", "%")
MEASURE_NAMES = (
    "净利润", "利润", "金额", "收入", "营收", "销售额", "成本", "费用", "预算", "实际",
    "余额", "应收", "应付", "回款", "amount", "revenue", "sales", "profit", "cost", "expense",
)


def normalize_draft_charts(
    draft: AnalysisDraft,
    evidence: Iterable[EvidenceRecord],
    question: str,
) -> AnalysisDraft:
    """Rebuild chart specifications from trusted evidence and remove duplicates."""
    normalized = draft.model_copy(deep=True)
    evidence_by_id = {item.id: item for item in evidence}
    rebuilt: list[ChartSpec] = []

    for index, requested in enumerate(normalized.charts):
        source = evidence_by_id.get(requested.dataset_ref)
        if source is None or not source.rows:
            continue
        spec = build_chart_spec(source, question, requested=requested, index=index)
        if spec is not None:
            rebuilt.append(spec)

    if not rebuilt:
        for index, source in enumerate(evidence_by_id.values()):
            spec = build_chart_spec(source, question, index=index)
            if spec is not None:
                rebuilt.append(spec)
                break

    normalized.charts = deduplicate_charts(rebuilt, evidence_by_id)
    if not normalized.metrics:
        normalized.metrics = build_kpis(evidence_by_id.values())
    return normalized


def build_kpis(evidence: Iterable[EvidenceRecord], limit: int = 4) -> list[Metric]:
    """Create KPI tiles only for scalar evidence; comparison changes are never inferred."""
    metrics: list[Metric] = []
    for source in evidence:
        if len(source.rows) != 1:
            continue
        row = source.rows[0]
        for field in source.columns:
            value = row.get(field)
            if _number(value) is None or _is_auxiliary_metric(field) or _is_temporal_name(field):
                continue
            unit = _unit_for(field)
            rendered = f"{value}{unit}" if unit == "%" and not str(value).endswith("%") else str(value)
            metrics.append(Metric(
                label=field,
                value=rendered,
                change=None,
                direction="neutral",
                evidence_refs=[source.id],
                evidence_pointers=[EvidencePointer(
                    evidence_id=source.id, row_index=0, field=field,
                    raw_value=str(value), unit=unit,
                )],
            ))
            if len(metrics) >= limit:
                return metrics
    return metrics


def build_chart_spec(
    evidence: EvidenceRecord,
    question: str,
    *,
    requested: ChartSpec | None = None,
    index: int = 0,
) -> ChartSpec | None:
    rows = evidence.rows
    if not rows:
        return None
    columns = [name for name in evidence.columns if any(name in row for row in rows)]
    if not columns:
        columns = list(rows[0])

    numeric = [name for name in columns if _is_numeric_column(rows, name)]
    if not numeric:
        return None
    dimensions = [name for name in columns if name not in numeric]
    if _wants_scatter(question) and len(numeric) >= 2 and len(rows) >= 2:
        requested_fields = [item.field for item in requested.series] if requested else []
        axes = [field for field in requested_fields if field in numeric][:2]
        if len(axes) < 2:
            axes = [name for name in numeric if not _is_auxiliary_metric(name)][:2]
        if len(axes) == 2:
            label = next((name for name in dimensions if not _is_temporal_name(name)), None)
            return ChartSpec(
                id=requested.id if requested else f"chart_{evidence.id}_{index + 1}",
                title=(requested.title if requested and requested.title.strip() else f"{axes[0]}与{axes[1]}关系"),
                chart_type="scatter",
                dataset_ref=evidence.id,
                category_field=label or axes[0],
                series=[ChartSeries(name=axes[1], field=axes[1])],
                orientation="vertical",
                sort_order="source",
                max_items=200,
                x_field=axes[0],
                y_field=axes[1],
                label_field=label,
            )
    if len(rows) == 1:
        return None
    category = _choose_category(rows, dimensions, numeric, requested)
    measures = _choose_measures(numeric, requested)
    if category is None or not measures:
        return None

    chart_type, orientation, sort_order = _chart_policy(question, category, measures, rows)
    title = requested.title if requested and requested.title.strip() else _default_title(category, measures[0], chart_type)
    unit = requested.unit if requested else _unit_for(measures[0])
    return ChartSpec(
        id=requested.id if requested else f"chart_{evidence.id}_{index + 1}",
        title=title,
        chart_type=chart_type,
        dataset_ref=evidence.id,
        category_field=category,
        series=[ChartSeries(name=name, field=name) for name in measures[:4]],
        unit=unit,
        orientation=orientation,
        sort_order=sort_order,
        max_items=20,
    )


def deduplicate_charts(
    charts: Iterable[ChartSpec],
    evidence_by_id: dict[str, EvidenceRecord],
) -> list[ChartSpec]:
    seen: set[tuple[Any, ...]] = set()
    result: list[ChartSpec] = []
    for chart in charts:
        evidence = evidence_by_id.get(chart.dataset_ref)
        if evidence is None:
            continue
        rows = ordered_chart_rows(chart, evidence.rows)
        signature = tuple(
            (
                str(row.get(chart.category_field, "")),
                str(row.get(chart.x_field, "")) if chart.x_field else "",
                str(row.get(chart.y_field, "")) if chart.y_field else "",
                *(str(row.get(series.field, "")) for series in chart.series),
            )
            for row in rows
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(chart)
    return result


def ordered_chart_rows(chart: ChartSpec, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(rows)
    primary = chart.series[0].field if chart.series else None
    if chart.sort_order == "category_asc":
        ordered.sort(key=lambda row: _natural_key(row.get(chart.category_field)))
    elif chart.sort_order == "value_desc" and primary:
        ordered.sort(key=lambda row: abs(_number(row.get(primary)) or Decimal(0)), reverse=True)
    if chart.chart_type == "scatter" and len(ordered) > chart.max_items:
        step = (len(ordered) - 1) / (chart.max_items - 1)
        return [ordered[round(index * step)] for index in range(chart.max_items)]
    return ordered[: chart.max_items]


def _choose_category(
    rows: list[dict[str, Any]],
    dimensions: list[str],
    numeric: list[str],
    requested: ChartSpec | None,
) -> str | None:
    if requested and requested.category_field in dimensions:
        return requested.category_field
    temporal = next((name for name in dimensions if _is_temporal_name(name)), None)
    if temporal:
        return temporal
    if dimensions:
        return dimensions[0]
    # Numeric-looking month/year columns are dimensions, not financial measures.
    return next((name for name in numeric if _is_temporal_name(name)), None)


def _choose_measures(numeric: list[str], requested: ChartSpec | None) -> list[str]:
    requested_fields = [item.field for item in requested.series] if requested else []
    valid_requested = [name for name in requested_fields if name in numeric and not _is_auxiliary_metric(name)]
    if valid_requested:
        return valid_requested
    candidates = [name for name in numeric if not _is_auxiliary_metric(name) and not _is_temporal_name(name)]
    candidates.sort(key=lambda name: (not any(token in name.casefold() for token in MEASURE_NAMES), numeric.index(name)))
    return candidates


def _chart_policy(
    question: str,
    category: str,
    measures: list[str],
    rows: list[dict[str, Any]],
) -> tuple[str, str, str]:
    text = question.casefold()
    if _is_temporal_name(category) or any(token in text for token in ("趋势", "走势", "环比", "同比", "逐月", "逐年")):
        return "line", "vertical", "category_asc"
    if any(token in text for token in ("贡献", "增减", "变动", "差异构成")) and _contains_positive_and_negative(rows, measures[0]):
        return "waterfall", "vertical", "source"
    if (
        any(token in text for token in ("占比", "构成", "比例", "份额"))
        and 2 <= len(rows) <= 8
        and _can_use_pie(rows, measures[0])
    ):
        return "pie", "vertical", "value_desc"
    if len(measures) > 1 and any(token in text for token in ("堆叠", "构成对比", "组成")):
        return "stacked_bar", "vertical", "value_desc"
    if len(rows) > 8 or any(token in text for token in ("排名", "排行", "最高", "最低", "top")):
        return "bar", "horizontal", "value_desc"
    return "bar", "vertical", "value_desc"


def _wants_scatter(question: str) -> bool:
    text = question.casefold()
    return any(token in text for token in ("散点", "相关", "关联", "关系", "correlation", "scatter"))


def _is_numeric_column(rows: list[dict[str, Any]], name: str) -> bool:
    values = [row.get(name) for row in rows[:100] if row.get(name) not in (None, "")]
    return bool(values) and sum(_number(value) is not None for value in values) / len(values) >= 0.8


def _contains_positive_and_negative(rows: list[dict[str, Any]], field: str) -> bool:
    values = [_number(row.get(field)) for row in rows]
    return any(value is not None and value > 0 for value in values) and any(value is not None and value < 0 for value in values)


def _can_use_pie(rows: list[dict[str, Any]], field: str) -> bool:
    values = [_number(row.get(field)) for row in rows]
    available = [value for value in values if value is not None]
    return bool(available) and all(value >= 0 for value in available) and any(value > 0 for value in available)


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip().replace(",", "").rstrip("%")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _is_temporal_name(name: str) -> bool:
    lowered = name.casefold()
    return any(token in lowered for token in TEMPORAL_NAMES)


def _is_auxiliary_metric(name: str) -> bool:
    lowered = name.casefold()
    return any(token in lowered for token in ("记录数", "排名", "阈值", "上期值", "同期值")) or any(
        token in lowered for token in PERCENTAGE_NAMES
    )


def _natural_key(value: Any) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value))
    )


def _unit_for(name: str) -> str | None:
    if any(token in name.casefold() for token in PERCENTAGE_NAMES):
        return "%"
    lowered = name.casefold()
    for unit in ("万元", "亿元", "元", "美元", "usd", "件", "人", "小时"):
        if unit in lowered:
            return unit.upper() if unit == "usd" else unit
    return None


def _default_title(category: str, measure: str, chart_type: str) -> str:
    suffix = "趋势" if chart_type == "line" else "构成" if chart_type == "pie" else "分布"
    return f"{measure}按{category}{suffix}"
