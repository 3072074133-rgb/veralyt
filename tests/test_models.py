import pytest
from pydantic import ValidationError

from app.models import AnalysisDraft, ConversationMemory, DatasetColumn, Finding, GeneratedAnalysisDraft, IntentDecision, Metric


def test_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        IntentDecision(is_analysis=True, reason="分析请求", confidence=0.9, unknown=True)


def test_metric_uses_string_value() -> None:
    metric = Metric(label="收入", value="1286400.00", evidence_refs=["ev_1"])
    assert metric.value == "1286400.00"


def test_conversation_memory_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ConversationMemory(task_goal="分析收入", private_reasoning="不应保存")


def test_new_visualization_fields_keep_old_snapshots_compatible() -> None:
    column = DatasetColumn(name="收入", display_name="收入", data_type="Float64", null_count=0)
    draft = AnalysisDraft(summary="旧结果")

    assert column.semantic_type == "unknown"
    assert column.default_aggregation == "none"
    assert draft.calculation_details == []


def test_generated_report_does_not_apply_backend_content_limits() -> None:
    findings = [Finding(detail=f"发现 {index}") for index in range(9)]

    historical = AnalysisDraft(summary="历史报告", findings=findings)
    generated = GeneratedAnalysisDraft(summary="新报告", findings=findings)

    assert len(historical.findings) == 9
    assert len(generated.findings) == 9
