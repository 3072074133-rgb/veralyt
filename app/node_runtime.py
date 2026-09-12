from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from .config import settings
from .llm import llm, model_budget
from .models import AnalysisState, PromptVersion
from .observability import duration_ms, log_event
from .repository import repository


NODE_PROMPTS = {
    "classify": "intent_classifier",
    "plan": "analysis_planner",
    "execute": "tool_orchestrator",
    "draft": "draft_writer",
    "reflect": "reflection_reviewer",
}
logger = logging.getLogger(__name__)


@dataclass
class NodeExecutionTracker:
    execution_id: str
    prompt: PromptVersion
    prompt_versions: dict[str, str]
    run_id: str
    task_id: str
    node_name: str
    started_at: float
    execution_mode: str = 'deterministic'

    def record_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        if diagnostics.get('execution_mode') == 'model':
            self.execution_mode = 'model'
        repository.record_node_diagnostics(self.execution_id, diagnostics)

    def complete(self, output: dict[str, Any]) -> dict[str, Any]:
        self.record_diagnostics({'execution_mode': self.execution_mode})
        tracked = {**output, "prompt_versions": self.prompt_versions}
        repository.finish_node_execution(self.execution_id, tracked)
        repository.touch_execution(self.run_id)
        log_event(
            logger,
            "workflow.node.completed",
            task_id=self.task_id,
            run_id=self.run_id,
            node_name=self.node_name,
            node_execution_id=self.execution_id,
            status="completed",
            duration_ms=duration_ms(self.started_at),
        )
        return tracked

    def fail(self, error: Exception) -> None:
        self.record_diagnostics({'execution_mode': self.execution_mode})
        repository.finish_node_execution(self.execution_id, None, error=str(error))
        repository.touch_execution(self.run_id)
        logger.exception(
            "workflow.node.failed",
            exc_info=(type(error), error, error.__traceback__),
            extra={
                "event_fields": {
                    "event": "workflow.node.failed",
                    "task_id": self.task_id,
                    "run_id": self.run_id,
                    "node_name": self.node_name,
                    "node_execution_id": self.execution_id,
                    "status": "failed",
                    "duration_ms": duration_ms(self.started_at),
                }
            },
        )


def begin_node(state: AnalysisState, node_name: str) -> NodeExecutionTracker:
    started_at = time.perf_counter()
    repository.touch_execution(state.run_id)
    prompt_name = NODE_PROMPTS[node_name]
    requested = state.prompt_versions.get(node_name)
    if requested:
        prompt = repository.get_prompt_version(requested)
        if prompt.node_name != node_name:
            raise ValueError("提示词版本与节点不匹配")
    else:
        prompt = repository.ensure_prompt_version(
            node_name,
            llm.prompt_version(prompt_name),
            llm.load_prompt(prompt_name),
        )
    versions = {**state.prompt_versions, node_name: prompt.id}
    budget = model_budget(prompt_name)
    execution_id = repository.start_node_execution(
        state.run_id,
        node_name,
        state.model_dump(mode="json"),
        prompt.id,
        {
            "model": settings.ollama_model,
            "temperature": 0,
            "num_ctx": budget.context_tokens,
            "num_predict": budget.output_tokens,
            "input_safety_tokens": budget.safety_tokens,
            "max_num_ctx": model_budget(
                prompt_name, context_tokens=settings.model_max_context_tokens
            ).context_tokens,
        },
        state.schema_version,
    )
    repository.record_node_diagnostics(execution_id, {
        'input_state_chars': len(state.model_dump_json()),
        'execution_mode': 'deterministic',
    })
    log_event(
        logger,
        "workflow.node.started",
        task_id=state.task_id,
        run_id=state.run_id,
        node_name=node_name,
        node_execution_id=execution_id,
        prompt_version_id=prompt.id,
    )
    return NodeExecutionTracker(
        execution_id, prompt, versions, state.run_id, state.task_id, node_name, started_at
    )
