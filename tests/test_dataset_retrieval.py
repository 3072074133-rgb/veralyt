from types import SimpleNamespace

import pytest

from app.config import settings
from app.dataset_retrieval import (
    detect_dataset_relationships,
    is_placeholder_column_name,
    planner_catalog,
    query_catalog,
    retrieve_datasets,
)
from app.llm import LLMContextOverflowError, LLMStructuredOutputError, OllamaGateway, _select_context
from app.models import DatasetColumn, DatasetInfo, IntentDecision


def _dataset(dataset_id: str, name: str, columns: list[DatasetColumn]) -> DatasetInfo:
    return DatasetInfo(
        id=dataset_id,
        file_id="file",
        table_name=f"table_{dataset_id}",
        display_name=name,
        row_count=10,
        columns=columns,
    )


def test_retrieval_prefers_table_covering_department_and_profit() -> None:
    total = _dataset(
        "total",
        "KZE Total",
        [
            DatasetColumn(
                name="Department",
                display_name="Department",
                data_type="String",
                null_count=0,
                role="dimension",
                semantic_type="category",
                sample_values=["Ebiz Revenue", "Gross Profit", "Net Profit"],
            ),
            DatasetColumn(
                name="Sum",
                display_name="Sum",
                data_type="Float64",
                null_count=0,
                role="measure",
                semantic_type="metric",
            ),
        ],
    )
    unrelated = _dataset(
        "team",
        "Ebiz Team-1",
        [DatasetColumn(name="Sales", display_name="Sales", data_type="Float64", null_count=0)],
    )

    matches = retrieve_datasets("统计每个部门的盈亏", [unrelated, total])

    assert matches[0].dataset.id == "total"
    assert any("同时覆盖" in reason for reason in matches[0].reasons)
    compact = planner_catalog(matches)
    assert compact[0]["dataset_id"] == "total"
    assert compact[0]["matched_samples"]["Department"]


def test_choose_tool_rejects_plain_text_instead_of_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = OllamaGateway()
    response = SimpleNamespace(
        message=SimpleNamespace(content="普通说明文字", tool_calls=None),
        prompt_eval_count=10,
        eval_count=5,
        done_reason="stop",
    )
    monkeypatch.setattr(gateway.client, "chat", lambda **_kwargs: response)

    with pytest.raises(LLMStructuredOutputError, match="没有返回工具调用"):
        gateway.choose_tool({}, [])


def test_model_call_is_rejected_before_context_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = OllamaGateway()
    monkeypatch.setattr(settings, "model_context_tokens", 512)
    monkeypatch.setattr(settings, "model_max_context_tokens", 512)
    monkeypatch.setattr(settings, "model_input_safety_tokens", 128)
    called = False

    def unexpected_call(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gateway.client, "chat", unexpected_call)

    with pytest.raises(LLMContextOverflowError):
        gateway.choose_tool({"catalog": "x" * 5000}, [])
    assert called is False


def test_wide_catalog_keeps_question_relevant_fields() -> None:
    columns = [
        DatasetColumn(name=f"Other_{index}", display_name=f"Other {index}", data_type="String", null_count=0)
        for index in range(30)
    ] + [
        DatasetColumn(
            name="Department", display_name="部门", data_type="String", null_count=0,
            role="dimension", semantic_type="category",
        ),
        DatasetColumn(
            name="Profit", display_name="利润", data_type="Float64", null_count=0,
            role="measure", semantic_type="amount",
        ),
    ]
    dataset = _dataset("wide", "宽表", columns)

    compact = query_catalog(dataset, question="统计每个部门的利润", field_limit=2, sample_limit=0)

    assert [item["name"] for item in compact["columns"]] == ["Department", "Profit"]
    assert compact["field_count"] == 32
    assert compact["fields_omitted"] == 30


def test_retrieval_penalizes_placeholder_heavy_tables() -> None:
    noisy = _dataset(
        "noisy",
        "Warehouse · 区域 2",
        [
            DatasetColumn(
                name="未命名列3", display_name="未命名列3", data_type="Float64",
                null_count=0, role="measure", semantic_type="metric",
            ),
            DatasetColumn(
                name="未命名列4", display_name="未命名列4", data_type="Float64",
                null_count=0, role="measure", semantic_type="metric",
            ),
        ],
    )
    summary = _dataset(
        "summary",
        "KZE Total · 区域 1",
        [
            DatasetColumn(
                name="Department", display_name="Department", data_type="String",
                null_count=0, role="dimension", semantic_type="category",
            ),
            DatasetColumn(
                name="Net Profit", display_name="Net Profit", data_type="Float64",
                null_count=0, role="measure", semantic_type="amount",
            ),
        ],
    )

    assert is_placeholder_column_name("未命名列3")
    assert is_placeholder_column_name("Unnamed: 3")
    matches = retrieve_datasets("分析报表", [noisy, summary])
    assert matches[0].dataset.id == "summary"


def test_relationship_detection_requires_matching_samples() -> None:
    orders = _dataset(
        "orders", "订单明细", [DatasetColumn(
            name="Department", display_name="Department", data_type="String", null_count=0,
            role="dimension", semantic_type="category", sample_values=["销售", "运营"],
        )],
    )
    departments = _dataset(
        "departments", "部门信息", [DatasetColumn(
            name="Department", display_name="Department", data_type="String", null_count=0,
            role="dimension", semantic_type="category", sample_values=["销售", "运营", "财务"],
        )],
    )

    relations = detect_dataset_relationships([orders, departments])

    assert relations and relations[0]["left_field"] == "Department"
    assert relations[0]["confidence"] >= 0.8


def test_context_selection_compacts_before_escalating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_context_tokens", 1024)
    monkeypatch.setattr(settings, "model_max_context_tokens", 2048)
    monkeypatch.setattr(settings, "model_default_output_tokens", 256)
    monkeypatch.setattr(settings, "model_input_safety_tokens", 128)

    selection = _select_context(
        "analysis_planner", "short prompt", [{"catalog": "x" * 5000}, {"catalog": "x" * 200}]
    )

    assert selection.variant_index == 1
    assert selection.escalated is False
    assert selection.budget.context_tokens == 1024


def test_context_selection_uses_maximum_window_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_context_tokens", 1024)
    monkeypatch.setattr(settings, "model_max_context_tokens", 2048)
    monkeypatch.setattr(settings, "model_default_output_tokens", 256)
    monkeypatch.setattr(settings, "model_input_safety_tokens", 128)

    selection = _select_context("analysis_planner", "short prompt", [{"catalog": "x" * 3000}])

    assert selection.escalated is True
    assert selection.budget.context_tokens == 2048


def test_structured_call_sets_context_and_output_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = OllamaGateway()
    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            message=SimpleNamespace(
                content='{"is_analysis":true,"reason":"ok","confidence":0.9,"suggested_response":null}',
                tool_calls=[],
            ),
            prompt_eval_count=20,
            eval_count=30,
            done_reason="stop",
        )

    monkeypatch.setattr(gateway.client, "chat", fake_chat)
    result = gateway.structured(
        "intent_classifier", {"user_question": "分析收入"}, IntentDecision,
        thinking=False, prompt_override="Return JSON",
    )

    assert result.is_analysis is True
    assert captured["options"]["num_ctx"] == min(settings.model_context_tokens, 4096)
    assert captured["options"]["num_predict"] == settings.model_classifier_output_tokens
