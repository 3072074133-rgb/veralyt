from pathlib import Path

import pytest

from app.config import settings
from app.context_manager import ContextManager
from app.models import AnalysisDraft, ConversationMemory, EvidenceRecord, Finding
from app.repository import repository


@pytest.fixture()
def memory_repo(tmp_path: Path):
    old_path = repository.db_path
    repository.db_path = tmp_path / "memory.sqlite"
    repository.initialize()
    task_id = repository.create_task()
    yield task_id
    repository.db_path = old_path


def _add_dialogue(task_id: str, count: int, width: int = 80) -> None:
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        repository.add_message(task_id, role, f"第{index}条消息" + "财务分析内容" * width)


def _small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_context_tokens", 1800)
    monkeypatch.setattr(settings, "context_output_reserve_tokens", 300)
    monkeypatch.setattr(settings, "context_base_overhead_tokens", 200)
    monkeypatch.setattr(settings, "context_safety_ratio", 0.1)
    monkeypatch.setattr(settings, "context_recent_messages", 4)
    monkeypatch.setattr(settings, "summary_batch_tokens", 700)


def test_short_conversation_does_not_summarize(memory_repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_dialogue(memory_repo, 2, width=2)
    calls = 0

    def unexpected_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("短会话不应调用总结模型")

    monkeypatch.setattr("app.context_manager.llm.structured", unexpected_call)
    snapshot = repository.get_task(memory_repo)
    context, budget = ContextManager().prepare(
        memory_repo,
        snapshot.messages,
        current_question="新的问题",
    )

    assert calls == 0
    assert budget.compacted is False
    assert len(context.recent_messages) == 2
    assert context.memory is None


def test_long_conversation_compacts_and_preserves_recent_messages(
    memory_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 30, width=10)
    calls: list[list[dict]] = []

    def summarize(_prompt, payload, _model, *, thinking, max_attempts):
        assert max_attempts == 1
        calls.append(payload["messages_to_merge"])
        previous = payload["previous_memory"] or {}
        return ConversationMemory(
            task_goal=previous.get("task_goal") or "分析收入趋势",
            confirmed_requirements=["按月分析"],
        )

    monkeypatch.setattr("app.context_manager.llm.structured", summarize)
    snapshot = repository.get_task(memory_repo)
    context, budget = ContextManager().prepare(
        memory_repo,
        snapshot.messages,
        current_question="继续分析",
    )

    saved = repository.get_conversation_memory(memory_repo)
    assert calls
    assert budget.compacted is True
    assert saved is not None and saved.covered_until_sequence > 0
    assert context.memory is not None
    assert [item.sequence for item in context.recent_messages[-4:]] == [
        item.sequence for item in snapshot.messages[-4:]
    ]


def test_invalid_evidence_keeps_previous_memory_and_analysis_can_continue(
    memory_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 10, width=20)

    def summarize(*args, **kwargs):
        return ConversationMemory(task_goal="分析", referenced_evidence_ids=["ev_missing"])

    monkeypatch.setattr("app.context_manager.llm.structured", summarize)
    snapshot = repository.get_task(memory_repo)
    context, budget = ContextManager().prepare(
        memory_repo,
        snapshot.messages,
        current_question="继续分析",
    )

    assert budget.compacted is False
    assert context.memory is None
    assert context.recent_messages
    assert repository.get_conversation_memory(memory_repo) is None
    events = repository.events_after(memory_repo, 0)
    assert any(item.event_type == "conversation.compaction_failed" for item in events)


def test_existing_memory_merges_uncovered_messages_with_bounded_requests(
    memory_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 20, width=20)
    repository.save_conversation_memory(
        memory_repo,
        ConversationMemory(task_goal="分析报表，统计各部门盈亏"),
        covered_until_sequence=0,
        expected_version=None,
    )

    calls = []
    def summarize(_prompt, payload, _model, **kwargs):
        calls.append(payload)
        return ConversationMemory.model_validate(payload['previous_memory'])

    monkeypatch.setattr("app.context_manager.llm.structured", summarize)
    snapshot = repository.get_task(memory_repo)
    context, budget = ContextManager().prepare(
        memory_repo,
        snapshot.messages,
        current_question="分析报表",
        dynamic_context={"datasets": "x" * 5000},
    )

    assert context.memory is not None
    assert context.memory.task_goal == "分析报表，统计各部门盈亏"
    assert budget.compacted is True
    assert 1 <= len(calls) <= 2
    assert repository.get_conversation_memory(memory_repo).covered_until_sequence > 0
    assert len(context.recent_messages) < len(snapshot.messages)


def test_completed_analysis_is_saved_with_verified_evidence(memory_repo: str) -> None:
    repository.add_evidence(
        EvidenceRecord(
            id="ev_income",
            task_id=memory_repo,
            title="收入证据",
            source="query_data",
            columns=["收入"],
            rows=[{"收入": "1286400.00"}],
        )
    )
    draft = AnalysisDraft(
        summary="收入存在增长趋势。",
        findings=[Finding(detail="本期收入增长。", evidence_refs=["ev_income"])],
    )

    ContextManager().record_completed_analysis(memory_repo, "分析收入趋势", draft)

    saved = repository.get_conversation_memory(memory_repo)
    assert saved is not None
    assert saved.memory.completed_analyses[0].evidence_ids == ["ev_income"]
    assert saved.memory.referenced_evidence_ids == ["ev_income"]


def test_persisted_cursor_prevents_reprocessing_after_restart(
    memory_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 12, width=10)
    calls = 0

    batches = []
    def summarize(_prompt, payload, _model, *, thinking, max_attempts):
        nonlocal calls
        calls += 1
        batches.append([item['sequence'] for item in payload['messages_to_merge']])
        return ConversationMemory(task_goal="分析收入")

    monkeypatch.setattr("app.context_manager.llm.structured", summarize)
    snapshot = repository.get_task(memory_repo)
    ContextManager().prepare(memory_repo, snapshot.messages, current_question="继续")
    first_calls = calls
    first_record = repository.get_conversation_memory(memory_repo)

    ContextManager().prepare(memory_repo, repository.get_task(memory_repo).messages, current_question="继续")
    second_record = repository.get_conversation_memory(memory_repo)

    assert first_calls > 0
    assert second_record is not None and first_record is not None
    assert calls - first_calls <= 2
    assert all(sequence > first_record.covered_until_sequence for batch in batches[first_calls:] for sequence in batch)
    assert second_record.covered_until_sequence >= first_record.covered_until_sequence


def test_summary_failure_does_not_advance_existing_cursor(memory_repo, monkeypatch):
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 20, width=20)
    previous = repository.save_conversation_memory(memory_repo, ConversationMemory(task_goal='retained'), 0, expected_version=None)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise ValueError('invalid summary')
    monkeypatch.setattr('app.context_manager.llm.structured', fail)
    ContextManager().prepare(memory_repo, repository.get_task(memory_repo).messages, current_question='next')
    current = repository.get_conversation_memory(memory_repo)
    assert len(calls) == 2
    assert current == previous
