from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import settings
from .llm import LLMStructuredOutputError, LLMUnavailableError, llm
from .models import (
    AnalysisDraft,
    AnalysisMemory,
    ConversationContext,
    ConversationMemory,
    MessageRecord,
    utc_now,
)
from .repository import repository


@dataclass(frozen=True)
class ContextBudget:
    input_tokens: int
    message_tokens: int
    available_message_tokens: int
    compacted: bool


class ContextManager:
    def classification_context(self, task_id: str, messages: list[MessageRecord], *, current_question: str) -> ConversationContext:
        record = repository.get_conversation_memory(task_id)
        history = self._without_current_question(messages, current_question)
        return ConversationContext(memory=record.memory if record else None, recent_messages=history[-4:])

    def prepare(
        self,
        task_id: str,
        messages: list[MessageRecord],
        *,
        current_question: str,
        dynamic_context: Any = None,
    ) -> tuple[ConversationContext, ContextBudget]:
        history = self._without_current_question(messages, current_question)
        record = repository.get_conversation_memory(task_id)
        memory = record.memory if record else None
        covered = record.covered_until_sequence if record else 0
        version = record.version if record else None
        unsummarized = [item for item in history if item.sequence > covered]
        available = self._available_message_tokens(dynamic_context)
        compacted = False

        requests = 0
        while requests < 2 and self._context_tokens(memory, unsummarized) > available:
            candidates = unsummarized[:-settings.context_recent_messages]
            if not candidates:
                break
            batch = self._summary_batch(candidates, memory)
            if not batch:
                break
            self._emit(task_id, "conversation.compacting", "正在整理较长会话")
            updated: ConversationMemory | None = None
            last_error: Exception | None = None
            while batch and requests < 2:
                try:
                    requests += 1
                    updated = llm.structured(
                        "conversation_summarizer",
                        {
                            "previous_memory": memory.model_dump(mode="json") if memory else None,
                            "messages_to_merge": [self._message_payload(item) for item in batch],
                            "valid_evidence_ids": sorted(self._evidence_ids(task_id)),
                        },
                        ConversationMemory,
                        thinking=False,
                        max_attempts=1,
                    )
                    self._validate_evidence(task_id, updated)
                    break
                except LLMUnavailableError as exc:
                    last_error = exc
                    batch = []
                except (LLMStructuredOutputError, ValueError, TypeError) as exc:
                    last_error = exc
                    batch = batch[: len(batch) // 2] if len(batch) > 1 else []
                except Exception as exc:
                    last_error = exc
                    batch = []

            if updated is None or not batch:
                self._emit(
                    task_id,
                    "conversation.compaction_failed",
                    "会话整理失败，已使用现有上下文继续分析",
                    {"error": str(last_error or "未知错误")},
                )
                break

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

        recent = self._fit_messages(unsummarized, memory, available)
        context = ConversationContext(memory=memory, recent_messages=recent)
        tokens = self._context_tokens(memory, recent)
        return context, ContextBudget(
            input_tokens=settings.context_base_overhead_tokens + tokens,
            message_tokens=tokens,
            available_message_tokens=available,
            compacted=compacted,
        )

    def record_completed_analysis(
        self,
        task_id: str,
        question: str,
        draft: AnalysisDraft,
    ) -> None:
        record = repository.get_conversation_memory(task_id)
        memory = record.memory if record else ConversationMemory(task_goal=question)
        evidence_ids = sorted(
            {
                evidence_id
                for item in [*draft.metrics, *draft.findings]
                for evidence_id in item.evidence_refs
            }
        )
        self._validate_evidence_ids(task_id, evidence_ids)
        completed = AnalysisMemory(
            question=question,
            conclusion=draft.summary,
            evidence_ids=evidence_ids,
            limitations=list(draft.warnings),
        )
        analyses = [*memory.completed_analyses, completed][-10:]
        references = sorted(set(memory.referenced_evidence_ids) | set(evidence_ids))
        updated = memory.model_copy(
            update={
                "task_goal": memory.task_goal or question,
                "completed_analyses": analyses,
                "referenced_evidence_ids": references,
                "last_updated_at": utc_now(),
            }
        )
        repository.save_conversation_memory(
            task_id,
            updated,
            record.covered_until_sequence if record else 0,
            expected_version=record.version if record else None,
        )

    def _available_message_tokens(self, dynamic_context: Any) -> int:
        safety = int(settings.model_context_tokens * settings.context_safety_ratio)
        dynamic = estimate_tokens(json.dumps(dynamic_context, ensure_ascii=False, default=str))
        return max(
            256,
            settings.model_context_tokens
            - settings.context_output_reserve_tokens
            - settings.context_base_overhead_tokens
            - safety
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

    def _fit_messages(
        self,
        messages: list[MessageRecord],
        memory: ConversationMemory | None,
        available: int,
    ) -> list[MessageRecord]:
        selected: list[MessageRecord] = []
        for item in reversed(messages):
            candidate = [item, *selected]
            if self._context_tokens(memory, candidate) <= available:
                selected = candidate
            elif selected:
                break
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
