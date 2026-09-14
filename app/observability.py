from __future__ import annotations

import json
import logging
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from .config import settings


_CONTEXT: ContextVar[dict[str, str]] = ContextVar("log_context", default={})
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_CONTEXT.get(),
        }
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, dict):
            payload.update(_safe_fields(fields))
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["trace"] = [
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])[-12:]
            ]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    if any(getattr(handler, "analyse_agent_handler", False) for handler in root.handlers):
        return
    target = log_dir or settings.log_dir
    target.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    file_handler = RotatingFileHandler(
        target / "app.jsonl",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.analyse_agent_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    root.setLevel(logging.INFO)


@contextmanager
def bind_log_context(**values: Any) -> Iterator[None]:
    merged = {**_CONTEXT.get(), **{key: str(value) for key, value in values.items() if value is not None}}
    token: Token[dict[str, str]] = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra={"event_fields": {"event": event, **fields}})


def duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key in {"prompt", "content", "rows", "question"}:
            continue
        if isinstance(value, str):
            result[key] = value[:500]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)[:500]
    return result
