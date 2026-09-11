from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import AnalysisState, EvidencePointer, Severity, ValidationIssue
from .repository import repository


def validate_claim(
    state: AnalysisState,
    target: str,
    text: str,
    evidence_refs: list[str],
    pointers: list[EvidencePointer],
    *,
    require_evidence: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if require_evidence and not evidence_refs:
        issues.append(ValidationIssue(
            code="missing_evidence",
            message="结论缺少证据引用",
            severity=Severity.ERROR,
            target=target,
        ))
        return issues
    available = current_evidence_ids(state)
    valid_pointers: list[EvidencePointer] = []
    for pointer in pointers:
        evidence = repository.get_evidence(state.task_id, pointer.evidence_id)
        is_valid = (
            pointer.evidence_id in evidence_refs
            and pointer.evidence_id in available
            and evidence is not None
            and 0 <= pointer.row_index < len(evidence.rows)
            and pointer.field in evidence.rows[pointer.row_index]
            and str(evidence.rows[pointer.row_index][pointer.field]) == pointer.raw_value
        )
        if not is_valid:
            issues.append(ValidationIssue(
                code="invalid_evidence_pointer",
                message=f"证据单元格定位无效：{pointer.evidence_id}/{pointer.row_index}/{pointer.field}",
                severity=Severity.ERROR,
                target=target,
            ))
        else:
            valid_pointers.append(pointer)
    ignored_terms = {
        column
        for evidence_id in evidence_refs
        if (evidence := repository.get_evidence(state.task_id, evidence_id)) is not None
        for column in evidence.columns
    }
    unsupported: list[str] = []
    for token in numeric_tokens(text, ignored_terms=ignored_terms):
        parsed = as_decimal(token)
        supported_by_pointer = parsed is not None and any(
            (
                (raw := as_decimal(pointer.raw_value)) is not None
                and numeric_values_match(token, parsed, raw, text)
            )
            or (token in pointer.raw_value and pointer.raw_value in text)
            for pointer in valid_pointers
        )
        if parsed is None or not (
            supported_by_pointer
            or evidence_row_count_supports(token, text, evidence_refs, state.task_id)
        ):
            unsupported.append(token)
    if unsupported:
        issues.append(ValidationIssue(
            code="unsupported_cell_value",
            message=f"数字无法唯一定位到引用证据的单元格：{', '.join(unsupported)}",
            severity=Severity.ERROR,
            target=target,
        ))
    return issues


def current_evidence_ids(state: AnalysisState) -> set[str]:
    return {
        evidence_id
        for result in state.tool_results
        if result.get("status") == "success"
        for evidence_id in result.get("evidence_ids", [])
    }


def evidence_row_count_supports(
    token: str, text: str, evidence_refs: list[str], task_id: str
) -> bool:
    parsed = as_decimal(token)
    if parsed is None or parsed != parsed.to_integral_value() or parsed < 0:
        return False
    count_phrase = re.compile(
        rf"(?:共|合计|包含)?\s*{re.escape(token)}\s*(?:行|条|个(?:部门|类别|项目|结果|分组))"
    )
    if not count_phrase.search(text):
        return False
    expected = int(parsed)
    return any(
        (evidence := repository.get_evidence(task_id, evidence_id)) is not None
        and len(evidence.rows) == expected
        for evidence_id in evidence_refs
    )


def numeric_values_match(token: str, claim: Decimal, raw: Decimal, text: str) -> bool:
    if claim == raw:
        return True
    if abs(claim) != abs(raw):
        return False
    if token.startswith("-"):
        return raw < 0
    if raw < 0:
        return any(word in text for word in ("下降", "减少", "降低", "下滑", "负", "亏损"))
    return True


def numeric_tokens(text: str, *, ignored_terms: set[str] | None = None) -> list[str]:
    claim_text = text
    # Spreadsheet-generated field names commonly contain identifiers such as
    # "未命名列3". Their suffix is metadata, not a numeric business claim.
    for term in sorted(ignored_terms or (), key=len, reverse=True):
        if term and any(character.isdigit() for character in term) and not re.fullmatch(r'[\d,.%+-]+', term):
            claim_text = claim_text.replace(term, "")
    claim_text = re.sub(r"(?<!\d)\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?!\d)", "", claim_text)
    claim_text = re.sub(r"\d{4}年", "", claim_text)
    claim_text = re.sub(r"\d{1,2}\s*[-至到]\s*\d{1,2}月", "", claim_text)
    claim_text = re.sub(r"\d{1,2}月", "", claim_text)
    return re.findall(r"-?\d[\d,]*(?:\.\d+)?%?", claim_text)


def unsupported_numbers(text: str, evidence_ids: list[str], task_id: str) -> list[str]:
    raw_tokens = numeric_tokens(text)
    if not raw_tokens:
        return []
    values: set[Decimal] = set()
    for evidence_id in evidence_ids:
        evidence = repository.get_evidence(task_id, evidence_id)
        if evidence:
            for row in evidence.rows:
                for value in row.values():
                    parsed = as_decimal(value)
                    if parsed is not None:
                        values.add(parsed)
                        values.add(abs(parsed))
    unsupported: list[str] = []
    for token in raw_tokens:
        parsed = as_decimal(token)
        if parsed is not None and parsed not in values and abs(parsed) not in values:
            unsupported.append(token)
    return unsupported


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip().replace(",", "").rstrip("%")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None
