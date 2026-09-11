from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .models import DatasetInfo


SEMANTIC_ALIASES: tuple[tuple[str, ...], ...] = (
    ("部门", "department", "dept", "团队", "team", "group", "组织"),
    ("盈亏", "盈利", "亏损", "利润", "profit", "loss", "net profit", "gross profit"),
    ("收入", "营收", "销售额", "revenue", "income", "sales"),
    ("费用", "成本", "支出", "expense", "cost", "amount"),
    ("月份", "月度", "month", "jan", "feb", "mar", "apr"),
    ("季度", "quarter", "q1", "q2", "q3", "q4"),
    ("区域", "地区", "region", "area"),
    ("数量", "销量", "qty", "quantity", "count"),
)


@dataclass(frozen=True)
class DatasetMatch:
    dataset: DatasetInfo
    score: float
    reasons: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset.id,
            "display_name": self.dataset.display_name,
            "score": round(self.score, 2),
            "reasons": list(self.reasons),
        }


def retrieve_datasets(
    question: str,
    datasets: list[DatasetInfo],
    *,
    limit: int = 3,
) -> list[DatasetMatch]:
    """Rank tables locally so the LLM never has to scan the complete catalog."""
    if not datasets:
        return []
    terms = _expanded_terms(question)
    alias_groups = _active_alias_groups(question)
    ranked = [_score_dataset(dataset, terms, alias_groups) for dataset in datasets]
    normalized_question = _normalize(question)
    ranked = [DatasetMatch(item.dataset, item.score + 100,
                          (*item.reasons, '明确指定工作表'))
              if any(len(name) >= 2 and _normalize(name) in normalized_question for name in
                     [item.dataset.display_name, item.dataset.source_region.sheet_name if item.dataset.source_region else ''])
              else item for item in ranked]
    ranked.sort(key=lambda item: (-item.score, -_semantic_quality(item.dataset), item.dataset.display_name))
    positive = [item for item in ranked if item.score > 0]
    return (positive or ranked)[: max(1, limit)]


def planner_catalog(
    matches: list[DatasetMatch],
    *,
    question: str = "",
    field_limit: int | None = None,
    sample_limit: int = 3,
) -> list[dict[str, Any]]:
    """Compact table-level catalog for planning; samples are limited to matched values."""
    catalog = []
    relationships = detect_dataset_relationships([match.dataset for match in matches])
    for match in matches:
        columns = _relevant_columns(question, match.dataset, field_limit)
        catalog.append({
            "dataset_id": match.dataset.id,
            "display_name": match.dataset.display_name,
            "row_count": match.dataset.row_count,
            "fields": [
                {
                    "name": column.name,
                    "type": column.data_type,
                    "semantic_type": column.semantic_type,
                    "role": column.role,
                }
                for column in columns
            ],
            "matched_samples": _matched_samples(match.dataset, match.reasons, sample_limit),
            "field_count": len(match.dataset.columns),
            "fields_omitted": max(0, len(match.dataset.columns) - len(columns)),
            "retrieval_score": round(match.score, 2),
            "retrieval_reasons": list(match.reasons),
            "relationships": [
                item for item in relationships
                if item["left_dataset_id"] == match.dataset.id
                or item["right_dataset_id"] == match.dataset.id
            ],
        })
    return catalog


def query_catalog(
    dataset: DatasetInfo,
    *,
    question: str = "",
    field_limit: int | None = None,
    sample_limit: int = 3,
) -> dict[str, Any]:
    """Detailed schema for one selected table, with bounded sample values."""
    columns = _relevant_columns(question, dataset, field_limit)
    return {
        "dataset_id": dataset.id,
        "display_name": dataset.display_name,
        "row_count": dataset.row_count,
        "columns": [
            {
                "name": column.name,
                "display_name": column.display_name,
                "type": column.data_type,
                "semantic_type": column.semantic_type,
                "role": column.role,
                "unit": column.unit or column.currency,
                "default_aggregation": column.default_aggregation,
                "sample_values": [_short(value) for value in column.sample_values[:sample_limit]],
            }
            for column in columns
        ],
        "field_count": len(dataset.columns),
        "fields_omitted": max(0, len(dataset.columns) - len(columns)),
    }


def catalog_size(catalog: Any) -> int:
    return len(json.dumps(catalog, ensure_ascii=False, default=str))


def detect_dataset_relationships(datasets: list[DatasetInfo]) -> list[dict[str, Any]]:
    """Find conservative cross-table key candidates from schema and samples."""
    relationships: list[dict[str, Any]] = []
    for left_index, left in enumerate(datasets):
        for right in datasets[left_index + 1:]:
            for left_column in left.columns:
                if (
                    is_placeholder_column_name(left_column.name)
                    or left_column.null_count == left.row_count
                    or not _relationship_key_column(left_column)
                ):
                    continue
                for right_column in right.columns:
                    if (
                        is_placeholder_column_name(right_column.name)
                        or right_column.null_count == right.row_count
                        or not _relationship_key_column(right_column)
                    ):
                        continue
                    if _normalize(left_column.name) != _normalize(right_column.name):
                        continue
                    if not _compatible_relationship_types(left_column.data_type, right_column.data_type):
                        continue
                    left_values = {_normalize(str(value)) for value in left_column.sample_values if value is not None}
                    right_values = {_normalize(str(value)) for value in right_column.sample_values if value is not None}
                    overlap = len(left_values & right_values) / max(1, min(len(left_values), len(right_values)))
                    if overlap < 0.5:
                        continue
                    relationships.append({
                        "left_dataset_id": left.id,
                        "left_dataset": left.display_name,
                        "left_field": left_column.name,
                        "right_dataset_id": right.id,
                        "right_dataset": right.display_name,
                        "right_field": right_column.name,
                        "confidence": round(min(0.99, 0.55 + overlap * 0.4), 2),
                        "reason": "字段名一致、类型兼容且样例值存在交集",
                    })
    return relationships


def _compatible_relationship_types(left: str, right: str) -> bool:
    left_numeric = any(token in left.casefold() for token in ("int", "float", "decimal"))
    right_numeric = any(token in right.casefold() for token in ("int", "float", "decimal"))
    left_text = any(token in left.casefold() for token in ("str", "string", "date", "time"))
    right_text = any(token in right.casefold() for token in ("str", "string", "date", "time"))
    return (left_numeric and right_numeric) or (left_text and right_text)


def _relationship_key_column(column: Any) -> bool:
    """Exclude measures and calendar columns that commonly share names by coincidence."""
    if column.role not in {"dimension", "identifier", "unknown"}:
        return False
    if column.semantic_type in {"date", "metric", "amount", "percentage"}:
        return False
    normalized = column.name.casefold().strip()
    if re.fullmatch(r"\d+(?:_\d+)?", normalized):
        return False
    return not re.fullmatch(
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|sum|total|grand total)",
        normalized,
    )


def _score_dataset(
    dataset: DatasetInfo,
    terms: set[str],
    alias_groups: list[set[str]],
) -> DatasetMatch:
    name = _normalize(dataset.display_name)
    column_names = [(column, _normalize(f"{column.name} {column.display_name}")) for column in dataset.columns]
    sample_text = [
        (column, _normalize(" ".join(str(value) for value in column.sample_values[:10])))
        for column in dataset.columns
    ]
    score = 0.0
    reasons: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        matched = False
        if term in name:
            score += 8
            reasons.append(f"表名匹配：{term}")
            matched = True
        matching_columns = [column.name for column, text in column_names if term in text]
        if matching_columns:
            score += 6 + min(2, len(matching_columns) - 1)
            reasons.append(f"字段匹配：{term} -> {', '.join(matching_columns[:3])}")
            matched = True
        matching_samples = [column.name for column, text in sample_text if term in text]
        if matching_samples:
            score += 3
            reasons.append(f"样例匹配：{term} -> {', '.join(matching_samples[:2])}")
            matched = True
        if matched and len(term) >= 4:
            score += 0.5
    searchable = " ".join(
        [
            name,
            *(text for _, text in column_names),
            *(text for _, text in sample_text),
        ]
    )
    covered_groups = sum(any(alias in searchable for alias in aliases) for aliases in alias_groups)
    if covered_groups > 1:
        score += 12 * (covered_groups - 1)
        reasons.append(f"同时覆盖 {covered_groups} 类问题语义")
    if any(column.role == "dimension" for column in dataset.columns):
        score += 0.25
    if any(column.role == "measure" for column in dataset.columns):
        score += 0.25
    return DatasetMatch(dataset=dataset, score=score, reasons=tuple(dict.fromkeys(reasons))[:12])


def _expanded_terms(question: str) -> set[str]:
    normalized = _normalize(question)
    terms = {
        token
        for token in re.findall(r"[a-z][a-z0-9 _-]{1,}|[\u4e00-\u9fff]{2,}", normalized)
        if token.strip()
    }
    for aliases in SEMANTIC_ALIASES:
        if any(_normalize(alias) in normalized for alias in aliases):
            terms.update(_normalize(alias) for alias in aliases)
    return {term.strip() for term in terms if len(term.strip()) >= 2}


def _active_alias_groups(question: str) -> list[set[str]]:
    normalized = _normalize(question)
    return [
        {_normalize(alias) for alias in aliases}
        for aliases in SEMANTIC_ALIASES
        if any(_normalize(alias) in normalized for alias in aliases)
    ]


def _matched_samples(
    dataset: DatasetInfo,
    reasons: tuple[str, ...],
    sample_limit: int,
) -> dict[str, list[Any]]:
    fields = {
        reason.rsplit("->", 1)[-1].strip()
        for reason in reasons
        if reason.startswith("样例匹配：") and "->" in reason
    }
    result: dict[str, list[Any]] = {}
    for column in dataset.columns:
        if any(column.name in field for field in fields):
            result[column.name] = [_short(value) for value in column.sample_values[:sample_limit]]
    return result


def _relevant_columns(question: str, dataset: DatasetInfo, limit: int | None) -> list[Any]:
    if limit is None or len(dataset.columns) <= limit:
        return list(dataset.columns)
    terms = _expanded_terms(question)
    scored: list[tuple[float, int, Any]] = []
    for index, column in enumerate(dataset.columns):
        name = _normalize(f"{column.name} {column.display_name}")
        samples = _normalize(" ".join(str(value) for value in column.sample_values[:5]))
        score = 0.0
        for term in terms:
            if term in name:
                score += 20 + min(5, len(term) / 2)
            elif term in samples:
                score += 8
        if column.role in {"dimension", "measure"}:
            score += 4
        if column.semantic_type != "unknown":
            score += 2
        if column.default_aggregation != "none":
            score += 1
        scored.append((score, index, column))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [column for _, _, column in scored[: max(1, limit)]]


def _semantic_quality(dataset: DatasetInfo) -> float:
    """Score schema quality while treating generated placeholder headers as noise."""
    meaningful = [column for column in dataset.columns if not is_placeholder_column_name(column.name)]
    recognized = sum(
        column.role != "unknown" or column.semantic_type != "unknown"
        for column in meaningful
    )
    measures = sum(column.role == "measure" for column in meaningful)
    placeholder_count = len(dataset.columns) - len(meaningful)
    summary_bonus = 6 if re.search(r"(?:total|汇总|总表|合计|summary)", dataset.display_name.casefold()) else 0
    return recognized + measures * 0.5 + summary_bonus - placeholder_count * 3


_PLACEHOLDER_COLUMN_RE = re.compile(
    r"^(?:未命名列|unnamed(?::?\s*column)?|column)\s*:?[\s]*\d+(?:_\d+)?$",
    re.IGNORECASE,
)


def is_placeholder_column_name(name: str) -> bool:
    """Return whether an imported header is an automatically generated placeholder."""
    normalized = re.sub(r"\s+", "", str(name)).casefold()
    return bool(_PLACEHOLDER_COLUMN_RE.fullmatch(normalized))


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 80:
        return value[:77] + "..."
    return value
