import pytest
from app.config import settings
from app.llm import _select_context, model_budget, LLMContextOverflowError


def test_classifier_uses_default_and_escalates_without_truncation(monkeypatch):
    monkeypatch.setattr(settings, 'model_context_tokens', 32768)
    monkeypatch.setattr(settings, 'model_max_context_tokens', 49152)
    monkeypatch.setattr('app.llm._estimate_tokens', lambda _: 8404)
    payload = {'previous_result': 'complete report'}
    normal = _select_context('intent_classifier', 'prompt', payload)
    assert normal.budget.context_tokens == 32768
    assert not normal.escalated
    monkeypatch.setattr('app.llm._estimate_tokens', lambda _: 35000)
    larger = _select_context('intent_classifier', 'prompt', payload)
    assert larger.budget.context_tokens == 49152
    assert larger.escalated
    assert larger.serialized_context == normal.serialized_context
    monkeypatch.setattr('app.llm._estimate_tokens', lambda _: 50000)
    with pytest.raises(LLMContextOverflowError):
        _select_context('intent_classifier', 'prompt', payload)
    assert model_budget('intent_classifier').output_tokens == settings.model_classifier_output_tokens
