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
    monkeypatch.setattr(settings, "summary_batch_tokens", 700)


def test_summary_overflow_splits_batch_without_skipping_messages(memory_repo, monkeypatch):
    from app.llm import LLMContextOverflowError
    _add_dialogue(memory_repo, 4, width=1)
    messages = repository.get_task(memory_repo).messages
    covered = []
    def summarize(prompt, context, model, **options):
        assert options['max_attempts'] == 3
        assert options['output_tokens'] >= 2048
        batch = context['messages_to_merge']
        if len(batch) > 1:
            raise LLMContextOverflowError('too large')
        covered.append(batch[0]['sequence'])
        return ConversationMemory(task_goal='analysis')
    monkeypatch.setattr('app.context_manager.llm.structured', summarize)
    ContextManager().prepare(memory_repo, messages, current_question='continue', fits_context=lambda c: not c.recent_messages)
    assert covered == [message.sequence for message in messages]
    assert repository.get_conversation_memory(memory_repo).covered_until_sequence == messages[-1].sequence
    assert repository.get_task(memory_repo).messages == messages


def test_summary_single_message_failure_preserves_checkpoint(memory_repo, monkeypatch):
    from app.llm import LLMStructuredOutputError
    _add_dialogue(memory_repo, 1, width=1)
    messages = repository.get_task(memory_repo).messages
    def fail(*args, **kwargs):
        raise LLMStructuredOutputError('done_reason=length')
    monkeypatch.setattr('app.context_manager.llm.structured', fail)
    with pytest.raises(LLMStructuredOutputError, match='最小消息批次'):
        ContextManager().prepare(memory_repo, messages, current_question='continue', fits_context=lambda c: False)
    assert repository.get_conversation_memory(memory_repo) is None
    assert repository.get_task(memory_repo).messages == messages


@pytest.mark.parametrize('value,valid', [('6800', True), ('几千', False)])
def test_exact_fact_checks_source_and_survives_later_summary(memory_repo, monkeypatch, value, valid):
    from app.models import ExactMemoryFact
    repository.add_message(memory_repo, 'user', '客户A欠款6800元')
    messages = repository.get_task(memory_repo).messages
    fact = ExactMemoryFact(subject='客户A', metric='欠款', value=value, unit='元',
                           source_sequence=messages[0].sequence, source_quote='客户A欠款6800元')
    monkeypatch.setattr('app.context_manager.llm.structured',
                        lambda *args, **kwargs: ConversationMemory(exact_facts=[fact]))
    manager = ContextManager()
    if not valid:
        with pytest.raises(ValueError, match='精确事实来源核对失败'):
            manager.prepare(memory_repo, messages, current_question='继续', fits_context=lambda c: not c.recent_messages)
        assert repository.get_conversation_memory(memory_repo) is None
        return
    context, _ = manager.prepare(memory_repo, messages, current_question='继续', fits_context=lambda c: not c.recent_messages)
    assert context.memory.exact_facts[0].value == '6800'
    repository.add_message(memory_repo, 'user', '继续分析')
    monkeypatch.setattr('app.context_manager.llm.structured', lambda *args, **kwargs: ConversationMemory())
    context, _ = manager.prepare(memory_repo, repository.get_task(memory_repo).messages,
                                  current_question='新问题', fits_context=lambda c: not c.recent_messages)
    assert context.memory.exact_facts == [fact]
    assert repository.get_task(memory_repo).messages[0].content == fact.source_quote


def test_classifier_compacts_before_request_using_its_own_budget(memory_repo, monkeypatch):
    from unittest.mock import Mock
    from app.workflow_nodes import classify
    from app.models import AnalysisState, IntentDecision
    from app.llm import _select_context
    _add_dialogue(memory_repo, 30, width=25)
    messages = repository.get_task(memory_repo).messages
    tracker = Mock()
    tracker.prompt.content = 'Classify the request.'
    tracker.complete.side_effect = lambda output: output
    monkeypatch.setattr(classify, 'start_node', lambda *args: tracker)
    calls = []
    def model(name, payload, response_model, **kwargs):
        calls.append(name)
        if name == 'conversation_summarizer':
            return ConversationMemory(task_goal='分析报表')
        _select_context(name, tracker.prompt.content, payload)
        assert payload['conversation_context']['memory'] is not None
        return IntentDecision(route='analysis', reply=None)
    monkeypatch.setattr(classify.llm, 'structured', model)
    output = classify.classify_node(AnalysisState(
        task_id=memory_repo, run_id='test', user_question='继续分析',
        conversation_messages=[item.model_dump(mode='json') for item in messages],
    ))
    assert calls[0] == 'conversation_summarizer'
    assert calls[-1] == 'intent_classifier'
    assert output['conversation_summary']['memory'] is not None
    assert len(repository.get_task(memory_repo).messages) == len(messages)


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

    def summarize(_prompt, payload, _model, *, thinking, max_attempts, **kwargs):
        assert max_attempts == 3
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


def test_invalid_summary_evidence_stops_instead_of_dropping_context(
    memory_repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_budget(monkeypatch)
    _add_dialogue(memory_repo, 10, width=20)

    def summarize(*args, **kwargs):
        return ConversationMemory(task_goal="分析", referenced_evidence_ids=["ev_missing"])

    monkeypatch.setattr("app.context_manager.llm.structured", summarize)
    snapshot = repository.get_task(memory_repo)
    with pytest.raises(ValueError, match="不存在的证据"):
        ContextManager().prepare(
            memory_repo,
            snapshot.messages,
            current_question="继续分析",
        )

    assert repository.get_conversation_memory(memory_repo) is None
    events = repository.events_after(memory_repo, 0)
    assert any(item.event_type == "conversation.compaction_failed" for item in events)


def test_existing_memory_merges_all_uncovered_messages_needed_to_fit(
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
    assert calls
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
    def summarize(_prompt, payload, _model, *, thinking, max_attempts, **kwargs):
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
    with pytest.raises(ValueError, match='invalid summary'):
        ContextManager().prepare(memory_repo, repository.get_task(memory_repo).messages, current_question='next')
    current = repository.get_conversation_memory(memory_repo)
    assert len(calls) == 1
    assert current == previous
