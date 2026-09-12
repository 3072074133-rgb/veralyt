from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ollama import Client
from pydantic import BaseModel, ValidationError

from .config import PROJECT_ROOT, settings
from .observability import duration_ms, log_event


ModelT = TypeVar("ModelT", bound=BaseModel)
logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMStructuredOutputError(LLMError):
    pass


class LLMContextOverflowError(LLMError):
    pass


@dataclass(frozen=True)
class ModelBudget:
    context_tokens: int
    output_tokens: int
    safety_tokens: int

    @property
    def input_limit(self) -> int:
        return max(256, self.context_tokens - self.output_tokens - self.safety_tokens)


@dataclass(frozen=True)
class ContextSelection:
    context: dict[str, Any]
    serialized_context: str
    input_tokens: int
    original_input_tokens: int
    variant_index: int
    budget: ModelBudget
    escalated: bool


class OllamaGateway:
    def __init__(self) -> None:
        # Local calls must not inherit corporate/system HTTP proxy variables.
        self.client = Client(host=settings.ollama_host, trust_env=False, timeout=180)
        self.prompt_dir = PROJECT_ROOT / "prompts"

    def load_prompt(self, name: str) -> str:
        path = self.prompt_dir / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        return re.sub(r"^---\s*.*?\s*---\s*", "", text, count=1, flags=re.DOTALL)

    def prompt_version(self, name: str) -> str:
        text = (self.prompt_dir / f"{name}.md").read_text(encoding="utf-8")
        match = re.search(r"^prompt_version:\s*([^\r\n]+)", text, flags=re.MULTILINE)
        return match.group(1).strip() if match else "file"

    def structured(
        self,
        prompt_name: str,
        context: dict[str, Any],
        response_model: type[ModelT],
        *,
        thinking: bool,
        prompt_override: str | None = None,
        ignored_response_fields: set[str] | None = None,
        fallback_contexts: list[dict[str, Any]] | None = None,
        diagnostics: Callable[[dict[str, Any]], None] | None = None,
        max_attempts: int = 2,
    ) -> ModelT:
        prompt = prompt_override or self.load_prompt(prompt_name)
        schema = _ollama_schema(response_model.model_json_schema())
        ignored_response_fields = ignored_response_fields or set()
        for field in ignored_response_fields:
            schema.get("properties", {}).pop(field, None)
        if isinstance(schema.get("required"), list):
            schema["required"] = [field for field in schema["required"] if field not in ignored_response_fields]
        selection = _select_context(prompt_name, prompt, [context, *(fallback_contexts or [])])
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": selection.serialized_context},
        ]
        _record_diagnostics(diagnostics, _selection_diagnostics(selection))
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            if attempt:
                if isinstance(last_error, ValidationError):
                    detail = '; '.join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in last_error.errors()[:4])
                else:
                    detail = 'invalid JSON'
                repair = f'\n上次输出校验失败：{detail[:240]}。只返回符合 Schema 的 JSON。'
                selection = _select_context(prompt_name, prompt + repair, [context, *(fallback_contexts or [])])
                messages = [{'role': 'system', 'content': prompt + repair},
                            {'role': 'user', 'content': selection.serialized_context}]
            input_tokens = _estimate_tokens(''.join(message['content'] for message in messages))
            _ensure_input_budget(input_tokens, selection.budget)
            started_at = time.perf_counter()
            _record_diagnostics(diagnostics, {'execution_mode': 'model', 'model_request_attempt': attempt + 1})
            try:
                response = self.client.chat(
                    model=settings.ollama_model,
                    messages=messages,
                    format=schema,
                    think=thinking,
                    options={
                        "temperature": 0,
                        "num_ctx": selection.budget.context_tokens,
                        "num_predict": selection.budget.output_tokens,
                    },
                )
                _record_diagnostics(
                    diagnostics,
                    _response_diagnostics(
                        response,
                        input_tokens=input_tokens,
                        attempt=attempt + 1,
                        selection=selection,
                    ),
                )
                content = response.message.content.strip()
                payload = _extract_json_payload(content)
                wrapper_names = {response_model.__name__, re.sub(r'(?<!^)(?=[A-Z])', '_', response_model.__name__).lower()}
                if isinstance(payload, dict) and len(payload) == 1 and next(iter(payload)) in wrapper_names:
                    wrapped = next(iter(payload.values()))
                    if isinstance(wrapped, dict):
                        payload = wrapped
                    elif response_model.__name__ == 'IntentDecision' and wrapped in ('分析', '非分析'):
                        payload = {'is_analysis': wrapped == '分析', 'confidence': 0,
                                   'reason': '模型仅返回粗粒度类别，需要确认当前需求',
                                   'route': 'clarification' if wrapped == '分析' else 'off_topic'}
                for field in ignored_response_fields:
                    payload.pop(field, None)
                parsed = response_model.model_validate(payload)
                log_event(
                    logger,
                    "llm.request.completed",
                    prompt_name=prompt_name,
                    model=settings.ollama_model,
                    attempt=attempt + 1,
                    input_tokens=input_tokens,
                    context_tokens=selection.budget.context_tokens,
                    output_tokens=selection.budget.output_tokens,
                    escalated=selection.escalated,
                    duration_ms=duration_ms(started_at),
                )
                return parsed
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                _record_diagnostics(diagnostics, {"validation_error": str(exc)})
                log_event(
                    logger,
                    "llm.response.invalid",
                    prompt_name=prompt_name,
                    model=settings.ollama_model,
                    attempt=attempt + 1,
                    duration_ms=duration_ms(started_at),
                )
            except Exception as exc:
                logger.exception(
                    "llm.request.failed",
                    extra={"event_fields": {"event": "llm.request.failed", "prompt_name": prompt_name, "model": settings.ollama_model, "attempt": attempt + 1, "duration_ms": duration_ms(started_at)}},
                )
                raise LLMUnavailableError(f"无法连接本地模型 {settings.ollama_model}：{exc}") from exc
        raise LLMStructuredOutputError(f"模型结构化输出连续 {max_attempts} 次校验失败：{last_error}")


def _extract_json_payload(content: str) -> Any:
    """Accept fenced/wrapped JSON while still validating the resulting object."""
    candidate = re.sub(r"```(?:json)?\s*|\s*```", "", content, flags=re.IGNORECASE).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        starts = [index for index, char in enumerate(candidate) if char in "[{]"]
        for start in starts:
            opening, closing = candidate[start], '}' if candidate[start] == '{' else ']'
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        return json.loads(candidate[start:index + 1])
        raise


llm = OllamaGateway()


def model_budget(prompt_name: str, *, context_tokens: int | None = None) -> ModelBudget:
    configured_context = max(512, context_tokens or settings.model_context_tokens)
    if prompt_name == "intent_classifier":
        configured_context = min(configured_context, 4096)
        requested_output = settings.model_classifier_output_tokens
    elif prompt_name == "draft_writer":
        requested_output = settings.model_draft_output_tokens
    elif prompt_name == "conversation_summarizer":
        requested_output = settings.model_summary_output_tokens
    else:
        requested_output = settings.model_default_output_tokens
    safety = min(settings.model_input_safety_tokens, max(64, configured_context // 4))
    output = min(requested_output, max(128, configured_context - safety - 256))
    return ModelBudget(configured_context, output, safety)


def _select_context(
    prompt_name: str,
    prompt: str,
    contexts: list[dict[str, Any]],
) -> ContextSelection:
    serialized = [json.dumps(context, ensure_ascii=False, default=str) for context in contexts]
    token_counts = [_estimate_tokens(prompt + value) for value in serialized]
    base = model_budget(prompt_name)
    for index, input_tokens in enumerate(token_counts):
        if input_tokens <= base.input_limit:
            return ContextSelection(
                contexts[index], serialized[index], input_tokens, token_counts[0], index, base, False
            )

    max_context = max(base.context_tokens, settings.model_max_context_tokens)
    if prompt_name == "intent_classifier":
        max_context = base.context_tokens
    maximum = model_budget(prompt_name, context_tokens=max_context)
    for index in reversed(range(len(contexts))):
        if token_counts[index] <= maximum.input_limit:
            return ContextSelection(
                contexts[index], serialized[index], token_counts[index], token_counts[0], index, maximum, True
            )

    smallest = min(token_counts)
    raise LLMContextOverflowError(
        f"{prompt_name} 压缩后输入仍预计 {smallest} tokens，超过最大安全上限 "
        f"{maximum.input_limit}（上下文 {maximum.context_tokens}，预留输出 {maximum.output_tokens}）"
    )


def _ensure_input_budget(input_tokens: int, budget: ModelBudget) -> None:
    if input_tokens > budget.input_limit:
        raise LLMContextOverflowError(
            f"模型输入预计 {input_tokens} tokens，超过安全上限 {budget.input_limit}；"
            "请先缩小数据目录或分析范围"
        )


def _selection_diagnostics(selection: ContextSelection) -> dict[str, Any]:
    return {
        "estimated_original_input_tokens": selection.original_input_tokens,
        "estimated_input_tokens": selection.input_tokens,
        "configured_context_tokens": selection.budget.context_tokens,
        "reserved_output_tokens": selection.budget.output_tokens,
        "input_safety_tokens": selection.budget.safety_tokens,
        "input_token_limit": selection.budget.input_limit,
        "context_variant": selection.variant_index,
        "context_compacted": selection.variant_index > 0,
        "context_escalated": selection.escalated,
    }


def _response_diagnostics(
    response: Any,
    *,
    input_tokens: int,
    attempt: int,
    selection: ContextSelection,
) -> dict[str, Any]:
    message = response.message
    return {
        **_selection_diagnostics(selection),
        "estimated_input_tokens": input_tokens,
        "prompt_eval_count": getattr(response, "prompt_eval_count", None),
        "eval_count": getattr(response, "eval_count", None),
        "done_reason": getattr(response, "done_reason", None),
        "attempt": attempt,
        "has_tool_calls": bool(getattr(message, "tool_calls", None)),
        "response_content_chars": len(getattr(message, "content", "") or ""),
    }


def _record_diagnostics(
    callback: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if callback is not None:
        callback(payload)


def _estimate_tokens(text: str) -> int:
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    ascii_count = sum(ord(char) < 128 for char in text)
    other = len(text) - chinese - ascii_count
    return max(1, int((chinese * 1.5 + ascii_count / 4 + other) * 1.15) + 1)


def _ollama_schema(value: Any) -> Any:
    """Remove JSON Schema annotations unsupported by older local Ollama runners."""
    if isinstance(value, list):
        return [_ollama_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {
        key: _ollama_schema(item)
        for key, item in value.items()
        if key not in {"title", "default", "minimum", "maximum", "minLength", "maxLength"}
    }
    variants = cleaned.get("anyOf")
    if isinstance(variants, list):
        non_null = [item for item in variants if item.get("type") != "null"]
        if len(non_null) == 1:
            replacement = non_null[0]
            cleaned.pop("anyOf")
            cleaned.update(replacement)
    # Keep the model's required/optional distinction. Older code promoted every
    # property to required, which made otherwise valid partial plans impossible
    # for small local models to emit. Pydantic remains the final validator.
    return cleaned
