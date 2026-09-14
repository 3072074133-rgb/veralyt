"""Lightweight workflow node timing logs.

Failed structured outputs and their request context are stored as run artifacts.
Other diagnostics remain transient; lifecycle events go to the application logger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any

from .llm import llm
from .observability import duration_ms, log_event

logger = logging.getLogger(__name__)
NODE_PROMPTS = {
    "classify": "intent_classifier",
    "plan": "analysis_planner",
    "execute": "tool_orchestrator",
    "draft": "draft_writer",
    "reflect": "reflection_reviewer",
}


class _LazyPrompt:
    def __init__(self, name: str) -> None:
        self.name = name
        self._content: str | None = None

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = llm.load_prompt(self.name)
        return self._content


@dataclass
class NodeTracker:
    task_id: str
    run_id: str
    node_name: str
    prompt: Any | None
    started_at: float
    execution_mode: str = "model"
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def record_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        if diagnostics.get('progress_message'):
            from .repository import repository
            message = diagnostics['progress_message']
            repository.update_task(self.task_id, status_message=message, event_type='report.progress')
        if "output_attempt" in diagnostics:
            from .repository import repository
            repository.add_artifact(
                self.task_id, self.run_id, "validation", "模型原始输出记录",
                diagnostics["output_attempt"], status="ready",
            )
        if "output_failure" in diagnostics:
            from .repository import repository
            repository.add_artifact(
                self.task_id, self.run_id, "error", "模型输出格式校验记录",
                diagnostics["output_failure"], status="failed",
            )
        self.diagnostics.update(diagnostics)
        if diagnostics.get("execution_mode") == "model":
            self.execution_mode = "model"

    def complete(self, output: dict[str, Any]) -> dict[str, Any]:
        self.record_diagnostics({"execution_mode": self.execution_mode})
        log_event(
            logger,
            "workflow.node.completed",
            task_id=self.task_id,
            run_id=self.run_id,
            node_name=self.node_name,
            status="completed",
            duration_ms=duration_ms(self.started_at),
        )
        return output

    def fail(self, error: Exception) -> None:
        logger.exception(
            "workflow.node.failed",
            exc_info=(type(error), error, error.__traceback__),
            extra={
                "event_fields": {
                    "event": "workflow.node.failed",
                    "task_id": self.task_id,
                    "run_id": self.run_id,
                    "node_name": self.node_name,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "duration_ms": duration_ms(self.started_at),
                }
            },
        )


def start_node(state: Any, node_name: str) -> NodeTracker:
    # Prompt files are loaded lazily by the LLM gateway.  Deterministic nodes
    # should not perform prompt I/O merely to record timing.
    prompt = _LazyPrompt(NODE_PROMPTS[node_name])
    started_at = time.perf_counter()
    log_event(
        logger,
        "workflow.node.started",
        task_id=state.task_id,
        run_id=state.run_id,
        node_name=node_name,
    )
    return NodeTracker(state.task_id, state.run_id, node_name, prompt, started_at)
