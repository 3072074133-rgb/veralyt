import json
from types import SimpleNamespace

import httpx
import pytest

from app.llm import LLMError, OllamaGateway, _read_cloud_stream
from app.models import ModelSettings


def event(value):
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


def test_cloud_stream_delivers_deltas_before_completion():
    received = []
    body = ': keepalive\n\n'
    for piece in ['{"summary":"', '收入', '正常"}']:
        body += event({'choices': [{'index': 0, 'delta': {'content': piece}}]})
    body += event({'choices': [{'delta': {}, 'finish_reason': 'length'}]})
    body += event({'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})
    body += 'data: [DONE]\n\n'
    result = _read_cloud_stream(httpx.Response(200, text=body), received.append)
    assert received == ['{"summary":"', '收入', '正常"}']
    assert result.message.content == ''.join(received)
    assert result.done_reason == 'length'
    assert result.eval_count == 10


def test_cloud_stream_rejects_disconnect_even_if_partial_json_is_valid():
    body = event({'choices': [{'delta': {'content': '{"ok":true}'}}]})
    with pytest.raises(LLMError, match='完成标记'):
        _read_cloud_stream(httpx.Response(200, text=body), None)


def test_cloud_stream_rejects_error_event():
    with pytest.raises(LLMError, match='返回错误'):
        _read_cloud_stream(httpx.Response(200, text=event({'error': {'message': 'failed'}})), None)


def test_ollama_stream_accumulates_content_and_final_usage(monkeypatch):
    gateway = OllamaGateway()
    calls = []
    def chat(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(message=SimpleNamespace(content='a'), done=False)
        yield SimpleNamespace(message=SimpleNamespace(content='b'), done=True,
                              done_reason='stop', prompt_eval_count=8, eval_count=2)
    monkeypatch.setattr(gateway.client, 'chat', chat)
    received = []
    result = gateway._chat(ModelSettings(mode='ollama', provider='ollama', model='test',
        base_url=gateway.client._client.base_url.__str__()), [], thinking=False,
        temperature=0, context_tokens=4096, output_tokens=128, on_delta=received.append)
    assert calls[0]['stream'] is True
    assert received == ['a', 'b']
    assert result.message.content == 'ab'
    assert result.prompt_eval_count == 8
