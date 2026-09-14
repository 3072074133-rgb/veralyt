import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import BaseModel

from app.llm import LLMError, LLMStructuredOutputError, LLMUnavailableError, OllamaGateway


class Reply(BaseModel):
    summary: str


def test_incomplete_object_does_not_extract_internal_array():
    from app.llm import _extract_json_payload
    with pytest.raises(json.JSONDecodeError):
        _extract_json_payload('{"refs":["a","b","c","d"],"charts":[{')
    assert _extract_json_payload('[1,2]') == [1, 2]
    assert _extract_json_payload('```json\n{"summary":"ok"}\n```') == {'summary': 'ok'}


def test_length_stop_increases_budget_and_records_reason(monkeypatch):
    gateway = OllamaGateway()
    truncated = response('{"summary":')
    truncated.done_reason = 'length'
    truncated.eval_count = 3072
    chat = Mock(side_effect=[truncated, response('{"summary":"ok"}')])
    monkeypatch.setattr(gateway.client, 'chat', chat)
    records = []
    assert gateway.structured('draft_writer', {}, Reply, thinking=False, diagnostics=records.append).summary == 'ok'
    first, second = [call.kwargs['options'] for call in chat.call_args_list]
    assert second['num_predict'] == first['num_predict'] * 2
    failure = next(item['output_failure'] for item in records if 'output_failure' in item)
    assert failure['response_diagnostics']['done_reason'] == 'length'
    assert '输出长度上限' in failure['errors'][0]


def test_repair_tracks_repeated_and_new_errors(monkeypatch):
    class Insight(BaseModel):
        title: str
        conclusion: str
    class Report(BaseModel):
        insights: list[Insight]
    gateway = OllamaGateway()
    chat = Mock(side_effect=[response('{"insights":[{"conclusion":"ok"}]}'),
                            response('{"insights":[{}]}'),
                            response('{"insights":[{"title":"title","conclusion":"ok"}]}')])
    monkeypatch.setattr(gateway.client, 'chat', chat)
    records = []
    result = gateway.structured('draft_writer', {}, Report, thinking=False, diagnostics=records.append)
    repair = json.loads(chat.call_args.kwargs['messages'][1]['content'])['output_repair']
    assert '第 1 条分析结论缺少必填标题' in repair['repeated_errors'][0]
    assert 'conclusion' in repair['new_errors'][0]
    failures = [item['output_failure'] for item in records if 'output_failure' in item]
    assert len(failures) == 2
    assert failures[1]['raw_output'] == '{"insights":[{}]}'
    assert 'output_repair' in failures[1]['request_messages'][1]['content']
    assert result.insights[0].title == 'title'


def response(content):
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))


def test_list_is_returned_to_model_with_original_content(monkeypatch):
    gateway = OllamaGateway()
    original = '[{"summary":"first"},{"summary":"second"}]'
    chat = Mock(side_effect=[response(original), response('{"summary":"combined"}')])
    monkeypatch.setattr(gateway.client, 'chat', chat)
    result = gateway.structured('draft_writer', {}, Reply, thinking=False, ignored_response_fields={'extra'})
    repair = json.loads(chat.call_args.kwargs['messages'][1]['content'])['output_repair']
    assert repair['previous_output'] == original
    assert 'array/list' in repair['validation_error']
    assert '2' in repair['validation_error']
    assert result.summary == 'combined'
    assert chat.call_count == 2


def test_empty_list_exhausts_format_retries(monkeypatch):
    gateway = OllamaGateway()
    chat = Mock(return_value=response('[]'))
    monkeypatch.setattr(gateway.client, 'chat', chat)
    with pytest.raises(LLMStructuredOutputError, match='array/list'):
        gateway.structured('draft_writer', {}, Reply, thinking=False)
    assert chat.call_count == 3


@pytest.mark.parametrize('error,expected', [
    (TypeError('program bug'), TypeError),
    (httpx.ReadTimeout('timeout'), LLMError),
    (ConnectionError('offline'), LLMUnavailableError),
])
def test_transport_and_program_errors_are_distinct(monkeypatch, error, expected):
    gateway = OllamaGateway()
    monkeypatch.setattr(gateway.client, 'chat', Mock(side_effect=error))
    with pytest.raises(expected) as caught:
        gateway.structured('draft_writer', {}, Reply, thinking=False)
    assert type(caught.value) is expected
