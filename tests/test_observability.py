import json
import logging
from pathlib import Path

from app.observability import JsonFormatter, bind_log_context, log_event


def test_json_logs_include_trace_fields_without_sensitive_payload(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("analyse-agent-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with bind_log_context(request_id="req-1", task_id="task-1"):
        log_event(
            logger,
            "analysis.completed",
            run_id="run-1",
            duration_ms=123,
            content="SENSITIVE-CELL-VALUE",
            rows=[{"salary": "SENSITIVE-CELL-VALUE"}],
        )
    handler.close()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event"] == "analysis.completed"
    assert payload["request_id"] == "req-1"
    assert payload["task_id"] == "task-1"
    assert payload["run_id"] == "run-1"
    assert "SENSITIVE-CELL-VALUE" not in path.read_text(encoding="utf-8")


def test_json_logs_do_not_serialize_exception_messages(tmp_path: Path) -> None:
    path = tmp_path / "exception.jsonl"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("analyse-agent-exception-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise ValueError("SENSITIVE-CELL-VALUE")
    except ValueError:
        logger.exception("operation.failed")
    handler.close()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["exception_type"] == "ValueError"
    assert payload["trace"]
    assert "SENSITIVE-CELL-VALUE" not in path.read_text(encoding="utf-8")
