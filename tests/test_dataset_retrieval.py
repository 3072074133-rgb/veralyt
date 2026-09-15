from types import SimpleNamespace

import pytest

from app.config import settings
from app.dataset_retrieval import planner_catalog
from app.llm import LLMContextOverflowError, LLMStructuredOutputError, OllamaGateway, _select_context
from app.models import DatasetColumn, DatasetInfo, IntentDecision, PlanDecision


def _dataset(dataset_id: str, name: str, columns: list[DatasetColumn]) -> DatasetInfo:
    return DatasetInfo(
        id=dataset_id,
        file_id="file",
        table_name=f"table_{dataset_id}",
        display_name=name,
        row_count=10,
        columns=columns,
    )


def test_planner_catalog_preserves_model_choice_across_all_tables() -> None:
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

    compact = planner_catalog([unrelated, total])
    assert [item["dataset_id"] for item in compact] == ["team", "total"]
    assert compact[1]["table_name"] == "table_total"
    assert compact[1]["fields"][0] == {"name": "Department", "type": "String"}
    assert "eligible_fixed_tools" not in compact[1]


def test_planner_catalog_never_binds_tools_to_column_names() -> None:
    receivables = _dataset(
        "receivables",
        "应收账款",
        [
            DatasetColumn(name="客户名称", display_name="客户名称", data_type="String", null_count=0),
            DatasetColumn(name="逾期余额", display_name="逾期余额", data_type="Int64", null_count=0),
            DatasetColumn(name="报表行类型", display_name="报表行类型", data_type="String", null_count=0),
        ],
    )
    expenses = _dataset(
        "expenses",
        "费用明细",
        [DatasetColumn(name="费用项目", display_name="费用项目", data_type="String", null_count=0)],
    )

    catalog = planner_catalog([expenses, receivables])

    assert all("eligible_fixed_tools" not in item for item in catalog)
    assert catalog[1]["fields"][0]["name"] == "客户名称"


def test_structured_query_rejects_plain_text_instead_of_silent_fallback(
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

    with pytest.raises(LLMStructuredOutputError):
        gateway.structured('analysis_planner', {}, PlanDecision, thinking=False)


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
        gateway.structured('analysis_planner', {"catalog": "x" * 5000}, PlanDecision, thinking=False)
    assert called is False


def test_wide_catalog_preserves_all_fields_in_source_order() -> None:
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

    compact = planner_catalog([dataset])[0]

    assert [item["name"] for item in compact["fields"]] == [column.name for column in columns]
    assert compact["field_count"] == 32


def test_catalog_does_not_rank_or_remove_tables() -> None:
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

    catalog = planner_catalog([noisy, summary])
    assert [item["dataset_id"] for item in catalog] == ["noisy", "summary"]


def test_context_selection_does_not_replace_model_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_context_tokens", 1024)
    monkeypatch.setattr(settings, "model_max_context_tokens", 2048)
    monkeypatch.setattr(settings, "model_default_output_tokens", 256)
    monkeypatch.setattr(settings, "model_input_safety_tokens", 128)

    with pytest.raises(LLMContextOverflowError):
        _select_context("analysis_planner", "short prompt", {"catalog": "x" * 10000})


def test_context_selection_uses_maximum_window_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_context_tokens", 1024)
    monkeypatch.setattr(settings, "model_max_context_tokens", 2048)
    monkeypatch.setattr(settings, "model_default_output_tokens", 256)
    monkeypatch.setattr(settings, "model_input_safety_tokens", 128)

    selection = _select_context("analysis_planner", "short prompt", {"catalog": "x" * 3000})

    assert selection.escalated is True
    assert selection.budget.context_tokens == 2048


def test_structured_call_sets_context_and_output_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = OllamaGateway()
    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            message=SimpleNamespace(
                content='{"route":"analysis","reason":"ok","reply":null}',
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

    assert result.route == "analysis"
    assert captured["options"]["num_ctx"] == settings.model_context_tokens
    assert captured["options"]["num_predict"] == settings.model_classifier_output_tokens


def test_default_runtime_can_escalate_beyond_base_context(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.model_settings import model_settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    monkeypatch.setattr(settings, "model_max_context_tokens", 8192)
    model_settings.reset_cache()
    gateway = OllamaGateway()
    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=(
            '{"action":"analyze","goal":"g","clarification":null,'
            '"clarification_options":[],"steps":[]}'
        ), tool_calls=[]), prompt_eval_count=3000, eval_count=10, done_reason="stop")

    monkeypatch.setattr(gateway.client, "chat", fake_chat)
    gateway.structured("analysis_planner", {"catalog": "x" * 12000}, PlanDecision,
                       thinking=False, prompt_override="Return JSON")
    assert captured["options"]["num_ctx"] == 8192
