from __future__ import annotations

import json
import re
from typing import Any

from .models import AnalysisState, EvidencePointer, Severity, ValidationIssue
from .repository import repository
from .report_generation import citation_id


def validate_claim(
    state: AnalysisState,
    target: str,
    evidence_refs: list[str],
    pointers: list[EvidencePointer],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not evidence_refs or not pointers:
        issues.append(ValidationIssue(code="missing_evidence", message="结论缺少证据引用；请提供支持该结论的单元格或计算结果引用，无法支持时由模型修改结论。", severity=Severity.ERROR, target=target))
    available = current_evidence_ids(state)
    for pointer_index, pointer in enumerate(pointers):
        evidence = repository.get_evidence(state.task_id, pointer.evidence_id)
        cell_valid = (
            pointer.evidence_id in evidence_refs
            and pointer.evidence_id in available
            and evidence is not None
            and 0 <= pointer.row_index < len(evidence.rows)
            and pointer.field in evidence.rows[pointer.row_index]
            and str(evidence.rows[pointer.row_index][pointer.field]) == pointer.raw_value
            and (pointer.citation_id is None or pointer.citation_id == citation_id(pointer.evidence_id, pointer.row_index, pointer.field, pointer.raw_value))
        )
        # Derived claims point to a persisted calculation row.  Their
        # reliability comes from the input cell pointers and formula, rather
        # than pretending that the calculated value existed in the source
        # spreadsheet.
        if pointer.source_type == "derived":
            input_valid = bool(pointer.formula and pointer.input_pointers)
            if input_valid:
                input_valid = not validate_claim(
                    state,
                    target,
                    evidence_refs,
                    pointer.input_pointers,
                )
            is_valid = (
                cell_valid
                and input_valid
            )
        else:
            is_valid = cell_valid
        if not is_valid:
            reasons = []
            if pointer.evidence_id not in evidence_refs:
                reasons.append('未在本结论 evidence_refs 中声明该证据 ID')
            if pointer.evidence_id not in available or evidence is None:
                reasons.append('证据 ID 不在本轮可用证据中')
            elif not 0 <= pointer.row_index < len(evidence.rows):
                reasons.append(f'row_index 从 0 开始，有效范围 0..{len(evidence.rows) - 1}')
            else:
                row = evidence.rows[pointer.row_index]
                if pointer.field not in row:
                    reasons.append(f'该行可用字段：{list(row)}')
                elif str(row[pointer.field]) != pointer.raw_value:
                    reasons.append(f'提交 raw_value={pointer.raw_value!r}，该单元格原值={str(row[pointer.field])!r}，须逐字一致')
            if pointer.source_type == 'derived':
                reasons.append('派生值须提供 formula 和有效 input_pointers；检查输入引用错误')
            candidates = _candidate_value_pointers(pointer, evidence)
            if candidates:
                reasons.append(
                    '根据项目名称、原值或当前行找到的可选数值引用（由模型按结论选择，不代表系统替换）：'
                    + json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))
                )
            issues.append(ValidationIssue(
                code="invalid_evidence_pointer",
                message=f"证据单元格定位无效：{pointer.evidence_id}/{pointer.row_index}/{pointer.field}。" + '；'.join(reasons) + '。依据证据目录自行修正引用，勿猜测数值。',
                severity=Severity.ERROR,
                target=f"{target}.evidence_pointers[{pointer_index}]",
            ))
    return issues


def _candidate_value_pointers(pointer: EvidencePointer, evidence: Any) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    matching_rows: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(evidence.rows):
        label_matches = any(str(value) == pointer.field for value in row.values())
        value_matches = any(str(value) == pointer.raw_value for value in row.values())
        if label_matches or value_matches or index == pointer.row_index:
            matching_rows.append((index, row))
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row_index, row in matching_rows:
        for field, value in row.items():
            key = (row_index, field)
            if key in seen or field in {'来源行号', 'source_row', 'source_row_number', 'excel_row'}:
                continue
            if not _is_numeric_value(value):
                continue
            seen.add(key)
            candidates.append({
                'citation_id': citation_id(pointer.evidence_id, row_index, field, value),
                'context': row,
                'evidence_id': pointer.evidence_id,
                'row_index': row_index,
                'field': field,
                'raw_value': str(value),
            })
            if len(candidates) >= 6:
                return candidates
    return candidates


def _is_numeric_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(re.fullmatch(
        r'\s*[+-]?(?:\d[\d,]*)(?:\.\d+)?(?:%|元|万元|亿元|千元)?\s*', value,
    ))


def current_evidence_ids(state: AnalysisState) -> set[str]:
    return {
        evidence_id
        for result in state.tool_results
        if result.get("status") == "success"
        for evidence_id in result.get("evidence_ids", [])
    }
