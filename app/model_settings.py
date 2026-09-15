"""Runtime model configuration persisted by the local settings page."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import settings
from .models import ModelSettings, ModelSettingsPublic, ModelSettingsUpdate


MASKED_KEY = "••••••••••••••••"


class ModelSettingsService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cached_path: Path | None = None
        self._cached_mtime_ns: int | None = None
        self._cached: ModelSettings | None = None

    @property
    def path(self) -> Path:
        return settings.data_dir / "model-settings.json"

    def defaults(self) -> ModelSettings:
        return ModelSettings(
            mode="ollama",
            provider="ollama",
            model=settings.ollama_model,
            api_key="",
            base_url=settings.ollama_host,
            temperature=0,
            max_tokens=max(settings.model_draft_output_tokens, settings.model_default_output_tokens),
            context_window=settings.model_context_tokens,
        )

    def get(self) -> ModelSettings:
        with self._lock:
            path = self.path
            if not path.exists():
                self._cached_path = path
                self._cached_mtime_ns = None
                self._cached = None
                return self.defaults()
            mtime = path.stat().st_mtime_ns
            if self._cached is not None and path == self._cached_path and mtime == self._cached_mtime_ns:
                return self._cached.model_copy(deep=True)
            value = self.defaults()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                value = ModelSettings.model_validate({**value.model_dump(), **payload})
            except (OSError, ValueError, TypeError):
                value = self.defaults()
            self._cached_path = path
            self._cached_mtime_ns = mtime
            self._cached = value
            return value.model_copy(deep=True)

    def public(self) -> ModelSettingsPublic:
        value = self.get()
        return ModelSettingsPublic(
            **value.model_dump(exclude={"api_key"}),
            api_key=MASKED_KEY if value.api_key else "",
            api_key_configured=bool(value.api_key),
        )

    def resolve_update(self, patch: ModelSettingsUpdate) -> ModelSettings:
        current = self.get()
        values = current.model_dump()
        supplied = patch.model_dump(exclude_unset=True, exclude_none=True)
        incoming_key = supplied.pop("api_key", None)
        clear_key = supplied.pop("clear_api_key", False)
        target_mode = supplied.get("mode", current.mode)
        if target_mode == "openai_compatible":
            # Cloud providers own their model context limits. Keep the local
            # Ollama preference for a later mode switch, but ignore cloud input.
            supplied.pop("context_window", None)
        values.update(supplied)
        if clear_key:
            values["api_key"] = ""
        elif incoming_key is not None and incoming_key.strip() and not self.is_masked(incoming_key):
            values["api_key"] = incoming_key.strip()
        candidate = ModelSettings.model_validate(values)
        self._validate(candidate)
        return candidate

    def update(self, patch: ModelSettingsUpdate) -> ModelSettingsPublic:
        with self._lock:
            candidate = self.resolve_update(patch)
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(candidate.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(path)
            self._cached = candidate
            self._cached_path = path
            self._cached_mtime_ns = path.stat().st_mtime_ns
            return self.public()

    def resolve_for_test(self, patch: ModelSettingsUpdate) -> ModelSettings:
        return self.resolve_update(patch)

    @staticmethod
    def is_masked(value: str) -> bool:
        return value == MASKED_KEY or ("*" in value and len(value) >= 6)

    @staticmethod
    def _validate(value: ModelSettings) -> None:
        parsed = urlparse(value.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API 地址必须是完整的 http:// 或 https:// 地址")
        if value.mode == "openai_compatible" and not value.model.strip():
            raise ValueError("云端模型名称不能为空")

    def reset_cache(self) -> None:
        with self._lock:
            self._cached_path = None
            self._cached_mtime_ns = None
            self._cached = None


model_settings = ModelSettingsService()


def openai_compatible_endpoint(base_url: str, resource: str) -> str:
    root = base_url.rstrip("/")
    suffix = f"/{resource.lstrip('/')}"
    if root.endswith(suffix):
        return root
    return root + suffix


def extract_api_error(response: Any) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)[:500]
        return str(error)[:500]
    except Exception:
        return str(getattr(response, "text", ""))[:500]
