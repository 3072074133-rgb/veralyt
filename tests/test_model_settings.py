from __future__ import annotations

from contextlib import contextmanager

import httpx

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.llm import LLMContextOverflowError, OllamaGateway, _select_runtime_context
from app.main import app
from app.model_settings import MASKED_KEY, model_settings
from app.models import ModelSettings, ModelSettingsUpdate


def test_runtime_settings_mask_preserve_and_clear(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    model_settings.reset_cache()
    saved = model_settings.update(ModelSettingsUpdate(
        mode="openai_compatible",
        provider="custom",
        model="finance-model",
        api_key="secret-value",
        base_url="https://models.example.test/v1",
    ))
    assert saved.api_key == MASKED_KEY
    assert saved.api_key_configured is True
    assert model_settings.get().api_key == "secret-value"

    model_settings.update(ModelSettingsUpdate(model="finance-model-v2", api_key=MASKED_KEY))
    assert model_settings.get().api_key == "secret-value"
    model_settings.update(ModelSettingsUpdate(clear_api_key=True))
    assert model_settings.get().api_key == ""


def test_cloud_settings_ignore_context_window_but_local_settings_accept_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    model_settings.reset_cache()

    cloud = model_settings.update(ModelSettingsUpdate(
        mode="openai_compatible",
        provider="custom",
        model="cloud-model",
        base_url="https://models.example.test/v1",
        context_window=131072,
    ))
    assert cloud.context_window == settings.model_context_tokens

    local = model_settings.update(ModelSettingsUpdate(
        mode="ollama",
        provider="ollama",
        model="local-model",
        base_url="http://127.0.0.1:11434",
        context_window=65536,
    ))
    assert local.context_window == 65536


def test_cloud_context_is_provider_managed_without_local_overflow() -> None:
    config = ModelSettings(
        mode="openai_compatible",
        provider="custom",
        model="cloud-model",
        base_url="https://models.example.test/v1",
    )
    selection = _select_runtime_context(
        config,
        "analysis_planner",
        "prompt",
        {"large_input": "x" * (settings.model_max_context_tokens * 8)},
    )

    assert selection.provider_managed is True
    assert selection.input_tokens <= selection.budget.input_limit


def test_cloud_context_uses_model_table_limit_before_request(monkeypatch) -> None:
    config = ModelSettings(
        mode="openai_compatible",
        provider="custom",
        model="private-model",
        base_url="https://models.example.test/v1",
    )
    selection = _select_runtime_context(
        config, "analysis_planner", "prompt", {"large_input": "x" * 900_000}
    )
    assert selection.budget.context_tokens == 200_000
    assert selection.provider_managed is True
    with pytest.raises(LLMContextOverflowError):
        from app.llm import _ensure_selection_input
        _ensure_selection_input(selection.input_tokens, selection)


def test_settings_api_and_connection_test_do_not_save_candidate(tmp_path, monkeypatch) -> None:
    from app.repository import repository
    monkeypatch.setattr(repository, "db_path", tmp_path / "metadata.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    model_settings.reset_cache()
    monkeypatch.setattr("app.api.llm.test_connection", lambda value: f"connected:{value.model}")
    with TestClient(app) as client:
        original = client.get("/api/v1/settings").json()["llm"]
        assert original["mode"] == "ollama"
        tested = client.post("/api/v1/settings/test", json={"llm": {
            "mode": "openai_compatible", "provider": "custom", "model": "cloud-model",
            "api_key": "temporary", "base_url": "https://example.test/v1",
        }})
        assert tested.status_code == 200
        assert tested.json()["model"] == "cloud-model"
        assert client.get("/api/v1/settings").json()["llm"]["mode"] == "ollama"

        saved = client.put("/api/v1/settings", json={"llm": {
            "mode": "openai_compatible", "provider": "custom", "model": "cloud-model",
            "api_key": "persistent", "base_url": "https://example.test/v1",
        }})
        assert saved.status_code == 200
        assert saved.json()["llm"]["api_key"] == MASKED_KEY


def test_cloud_chat_uses_configured_openai_compatible_endpoint(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @contextmanager
        def stream(self, method, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            yield httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":4}}\n\n'
                'data: [DONE]\n\n'
            ))

    monkeypatch.setattr("app.llm.httpx.Client", FakeClient)
    config = ModelSettings(
        mode="openai_compatible", provider="custom", model="finance-model",
        api_key="secret", base_url="https://gateway.example/v1",
    )
    response = OllamaGateway._cloud_chat(
        config, [{"role": "user", "content": "hello"}],
        temperature=0.2, output_tokens=256, structured=True,
    )
    assert response.message.content == '{"ok":true}'
    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["model"] == "finance-model"
    assert captured["payload"]["stream"] is True
    assert set(captured["payload"]) == {"model", "messages", "stream", "response_format"}
    assert response.prompt_eval_count == 12
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert "context_window" not in captured["payload"]
    assert "num_ctx" not in captured["payload"]


def test_cloud_context_error_does_not_retry_without_response_format(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @contextmanager
        def stream(self, method, url, *, headers, json):
            calls.append(json)
            yield httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "maximum context length exceeded"}})

    monkeypatch.setattr("app.llm.httpx.Client", FakeClient)
    config = ModelSettings(
        mode="openai_compatible", provider="custom", model="finance-model",
        api_key="secret", base_url="https://gateway.example/v1",
    )
    with pytest.raises(LLMContextOverflowError):
        OllamaGateway._cloud_chat(
            config, [{"role": "user", "content": "hello"}],
            temperature=0.2, output_tokens=256, structured=True,
        )
    assert len(calls) == 1
    assert "response_format" in calls[0]
