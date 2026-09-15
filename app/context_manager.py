from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .config import settings
from .llm import LLMContextOverflowError, LLMStructuredOutputError, llm
from .model_context import cloud_context_policy
from .model_settings import model_settings
from .models import (
    AnalysisDraft,
    AnalysisMemory,
    ConversationContext,
    ConversationMemory,
    MessageRecord,
    utc_now,
)
from .repository import repository


MAX_MEMORY_EXACT_FACTS = 256
MAX_MEMORY_COMPLETED_ANALYSES = 10
MAX_MEMORY_EVIDENCE_IDS = 512


@dataclass(frozen=True)
class ContextBudget:
    input_tokens: int
    message_tokens: int
    available_message_tokens: int
    compacted: bool
    context_limit_tokens: int
    compression_trigger_tokens: int
    context_limit_source: str


class ContextManager:
    def classification_context(self, task_id: str, messages: list[MessageRecord], *, current_question: str) -> ConversationContext:
        record = repository.get_conversation_memory(task_id)
        history = self._without_current_question(messages, current_question)
        covered = record.covered_until_sequence if record else 0
        return ConversationContext(
            memory=record.memory if record else None,
            recent_messages=[item for item in history if item.sequence > covered],
        )

    def prepare(
        self,
        task_id: str,
        messages: list[MessageRecord],
        *,
        current_question: str,
        dynamic_context: Any = None,
        fits_context: Callable[[ConversationContext], bool] | None = None,
    ) -> tuple[ConversationContext, ContextBudget]:
        history = self._without_current_question(messages, current_question)
        record = repository.get_conversation_memory(task_id)
        memory = record.memory if record else None
        covered = record.covered_until_sequence if record else 0
        version = record.version if record else None
        unsummarized = [item for item in history if item.sequence > covered]
        context_limit, compression_trigger, context_source = self._context_policy()
        available = self._available_message_tokens(
            dynamic_context,
            compression_trigger_tokens=compression_trigger,
        )
        compacted = False

        def fits() -> bool:
            if fits_context is not None:
                return fits_context(ConversationContext(memory=memory, recent_messages=unsummarized))
            return self._context_tokens(memory, unsummarized) <= available

        while not fits():
            candidates = unsummarized
            if not candidates:
                raise LLMContextOverflowError("会话摘要仍超过模型上下文上限")
            batch = self._summary_batch(candidates, memory)
            if not batch:
                raise LLMContextOverflowError("没有可用于会话摘要的历史消息")
            self._emit(task_id, "conversation.compacting", "正在整理较长会话")
            try:
                while True:
                    memory_size = estimate_tokens(memory.model_dump_json() if memory else '')
                    output_budget = max(settings.model_summary_output_tokens, memory_size + 512)
                    if output_budget > settings.model_summary_max_output_tokens:
                        raise LLMContextOverflowError('历史会话摘要的已有记忆超出输出预算，原始消息及已验证记忆已保留；请提高摘要输出上限或在新任务中分析。')
                    try:
                        updated = llm.structured(
                            "conversation_summarizer",
                            {
                                "previous_memory": memory.model_dump(mode="json") if memory else None,
                                "messages_to_merge": [self._message_payload(item) for item in batch],
                                "valid_evidence_ids": sorted(self._evidence_ids(task_id)),
                            },
                            ConversationMemory,
                            thinking=False,
                            max_attempts=3,
                            output_tokens=output_budget,
                            max_output_tokens=settings.model_summary_max_output_tokens,
                        )
                        break
                    except (LLMContextOverflowError, LLMStructuredOutputError) as exc:
                        if isinstance(exc, LLMStructuredOutputError) and 'done_reason=length' not in str(exc):
                            raise LLMStructuredOutputError(f'历史会话摘要格式校验失败：{exc}') from exc
                        if len(batch) == 1:
                            raise type(exc)(f'历史会话摘要失败，最小消息批次仍无法处理；原始消息及已验证记忆已保留。{exc}') from exc
                        batch = batch[:max(1, len(batch) // 2)]
                previous_facts = memory.exact_facts if memory else []
                preserved = {fact.model_dump_json(): fact for fact in previous_facts}
                for fact in updated.exact_facts:
                    key = fact.model_dump_json()
                    if key in preserved:
                        continue
                    preserved[key] = fact
                updated = updated.model_copy(update={"exact_facts": list(preserved.values())})
                updated = self._bound_memory(updated)
                self._validate_evidence(task_id, updated)
            except Exception as exc:
                self._emit(
                    task_id,
                    "conversation.compaction_failed",
                    "会话整理失败",
                    {"error": str(exc)},
                )
                raise

            updated = updated.model_copy(update={"last_updated_at": utc_now()})
            record = repository.save_conversation_memory(
                task_id,
                updated,
                batch[-1].sequence,
                expected_version=version,
            )
            memory, covered, version = record.memory, record.covered_until_sequence, record.version
            unsummarized = [item for item in history if item.sequence > covered]
            compacted = True

        context = ConversationContext(memory=memory, recent_messages=unsummarized)
        if compacted:
            self._emit(task_id, "conversation.compacted", "正在思考")
        tokens = self._context_tokens(memory, unsummarized)
        return context, ContextBudget(
            input_tokens=settings.context_base_overhead_tokens + tokens,
            message_tokens=tokens,
            available_message_tokens=available,
            compacted=compacted,
            context_limit_tokens=context_limit,
            compression_trigger_tokens=compression_trigger,
            context_limit_source=context_source,
        )

    def record_completed_analysis(
        self,
        task_id: str,
        question: str,
        draft: AnalysisDraft,
    ) -> None:
        record = repository.get_conversation_memory(task_id)
        memory = record.memory if record else ConversationMemory(task_goal=question)
        from .report_document import evidence_ids as collect_evidence_ids, report_text
        evidence_ids = sorted(collect_evidence_ids(draft))
        self._validate_evidence_ids(task_id, evidence_ids)
        completed = AnalysisMemory(
            question=question,
            conclusion=report_text(draft),
            evidence_ids=evidence_ids,
            limitations=list(draft.warnings),
        )
        analyses = [*memory.completed_analyses, completed]
        references = sorted(set(memory.referenced_evidence_ids) | set(evidence_ids))
        updated = memory.model_copy(
            update={
                "task_goal": memory.task_goal or question,
                "completed_analyses": analyses,
                "referenced_evidence_ids": references,
                "last_updated_at": utc_now(),
            }
        )
        updated = self._bound_memory(updated)
        repository.save_conversation_memory(
            task_id,
            updated,
            record.covered_until_sequence if record else 0,
            expected_version=record.version if record else None,
        )

    @staticmethod
    def _context_policy() -> tuple[int, int, str]:
        runtime = model_settings.get()
        if runtime.mode == "openai_compatible":
            policy = cloud_context_policy(runtime.model)
            return (
                policy.max_context_tokens,
                policy.compression_trigger_tokens,
                policy.source,
            )

        context_limit = (
            runtime.context_window
            if model_settings.path.is_file()
            else settings.model_context_tokens
        )
        safety = int(context_limit * settings.context_safety_ratio)
        return context_limit, context_limit - safety, "local"

    def _available_message_tokens(
        self,
        dynamic_context: Any,
        *,
        compression_trigger_tokens: int,
    ) -> int:
        dynamic = estimate_tokens(json.dumps(dynamic_context, ensure_ascii=False, default=str))
        return max(
            256,
            compression_trigger_tokens
            - settings.context_output_reserve_tokens
            - settings.context_base_overhead_tokens
            - dynamic,
        )

    def _context_tokens(self, memory: ConversationMemory | None, messages: list[MessageRecord]) -> int:
        payload = {
            "memory": memory.model_dump(mode="json") if memory else None,
            "recent_messages": [self._message_payload(item) for item in messages],
        }
        return estimate_tokens(json.dumps(payload, ensure_ascii=False, default=str))

    def _summary_batch(
        self,
        candidates: list[MessageRecord],
        memory: ConversationMemory | None,
    ) -> list[MessageRecord]:
        base = estimate_tokens(memory.model_dump_json() if memory else "")
        selected: list[MessageRecord] = []
        used = base
        for item in candidates:
            cost = estimate_tokens(json.dumps(self._message_payload(item), ensure_ascii=False))
            if selected and used + cost > settings.summary_batch_tokens:
                break
            selected.append(item)
            used += cost
        return selected

    @staticmethod
    def _without_current_question(messages: list[MessageRecord], question: str) -> list[MessageRecord]:
        if messages and messages[-1].role == "user" and messages[-1].content == question:
            return messages[:-1]
        return messages

    @staticmethod
    def _message_payload(message: MessageRecord) -> dict[str, Any]:
        return {
            "sequence": message.sequence,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at,
        }

    @staticmethod
    def _evidence_ids(task_id: str) -> set[str]:
        return repository.evidence_ids(task_id)

    def _validate_evidence(self, task_id: str, memory: ConversationMemory) -> None:
        referenced = set(memory.referenced_evidence_ids)
        for item in memory.completed_analyses:
            referenced.update(item.evidence_ids)
        self._validate_evidence_ids(task_id, sorted(referenced))

    def _validate_evidence_ids(self, task_id: str, evidence_ids: list[str]) -> None:
        unknown = set(evidence_ids) - self._evidence_ids(task_id)
        if unknown:
            raise ValueError(f"会话摘要引用了不存在的证据：{', '.join(sorted(unknown))}")

    @staticmethod
    def _bound_memory(memory: ConversationMemory) -> ConversationMemory:
        """Keep durable memory bounded while retaining the newest information."""
        facts = sorted(memory.exact_facts, key=lambda item: item.source_sequence)
        analyses = memory.completed_analyses[-MAX_MEMORY_COMPLETED_ANALYSES:]
        evidence_ids = list(dict.fromkeys(memory.referenced_evidence_ids))[-MAX_MEMORY_EVIDENCE_IDS:]
        return memory.model_copy(update={
            "exact_facts": facts[-MAX_MEMORY_EXACT_FACTS:],
            "completed_analyses": analyses,
            "referenced_evidence_ids": evidence_ids,
        })

    @staticmethod
    def _emit(task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        repository.update_task(
            task_id,
            status_message=message,
            event_type=event_type,
            payload=payload,
        )


def estimate_tokens(text: str) -> int:
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    ascii_count = sum(ord(char) < 128 for char in text)
    other = len(text) - chinese - ascii_count
    estimate = chinese * 1.5 + ascii_count / 4 + other
    return max(1, int(estimate * 1.15) + 1)


context_manager = ContextManager()
