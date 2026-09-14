from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ChartSpec


def ordered_chart_rows(chart: ChartSpec, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(rows)
    primary = chart.series[0].field if chart.series else None
    if chart.sort_order == "category_asc":
        ordered.sort(key=lambda row: _natural_key(row.get(chart.category_field)))
    elif chart.sort_order == "value_desc" and primary:
        ordered.sort(key=lambda row: _number(row.get(primary)) or Decimal(0), reverse=True)
    if chart.chart_type == "scatter" and len(ordered) > chart.max_items:
        step = (len(ordered) - 1) / (chart.max_items - 1)
        return [ordered[round(index * step)] for index in range(chart.max_items)]
    return ordered[: chart.max_items]


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip().replace(",", "").rstrip("%")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _natural_key(value: Any) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value))
    )
