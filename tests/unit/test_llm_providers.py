import asyncio
import json

import httpx
import pytest

from backend.providers.llm import LLMError, LLMUnavailable, resolve_model
from backend.providers.llm.ollama import OllamaProvider
from backend.providers.llm.openai_compatible import OpenAICompatibleProvider

MESSAGES = [{"role": "user", "content": "hi"}]


def collect(provider, model="m") -> list[str]:
    async def run():
        return [fragment async for fragment in provider.stream_chat(MESSAGES, model=model)]
    return asyncio.run(run())


def ollama(handler) -> OllamaProvider:
    return OllamaProvider("http://ollama.test", num_ctx=2048, transport=httpx.MockTransport(handler))


def ndjson(*chunks: dict) -> bytes:
    return "\n".join(json.dumps(c) for c in chunks).encode()


def test_ollama_streams_fragments_and_disables_thinking():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=ndjson(
            {"message": {"content": "Hel"}},
            {"message": {"content": "lo"}},
            {"message": {"content": ""}, "done": True},
        ))

    assert collect(ollama(handler), model="qwen3:4b-instruct") == ["Hel", "lo"]
    assert seen["model"] == "qwen3:4b-instruct"
    assert seen["think"] is False
    assert seen["options"] == {"num_ctx": 2048}
    assert seen["keep_alive"] == "30m"


def test_ollama_error_line_raises():
    def handler(request):
        return httpx.Response(200, content=ndjson({"message": {"content": "a"}}, {"error": "out of memory"}))

    with pytest.raises(LLMError, match="out of memory"):
        collect(ollama(handler))


def test_ollama_http_error_raises_with_detail():
    def handler(request):
        return httpx.Response(404, json={"error": "model 'm' not found"})

    with pytest.raises(LLMError, match="not found"):
        collect(ollama(handler))


def test_ollama_connection_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMUnavailable):
        collect(ollama(handler))
    with pytest.raises(LLMUnavailable):
        asyncio.run(ollama(handler).list_models())


def test_ollama_complete_sends_tools_schema_and_history_and_parses_tool_calls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "", "tool_calls": [
            {"id": "abc", "function": {"name": "ec2_get_instance", "arguments": {}}},
            {"function": {"name": "ec2_get_instance_metrics", "arguments": {"minutes": 30}}},
        ]}})

    tools = [{"type": "function", "function": {"name": "ec2_get_instance", "parameters": {}}}]
    history = [
        {"role": "user", "content": "check it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "name": "ec2_get_instance", "arguments": {}}]},
        {"role": "tool", "content": "{}", "tool_call_id": "x1", "tool_name": "ec2_get_instance"},
    ]
    completion = asyncio.run(ollama(handler).complete(
        history, model="m", tools=tools, json_schema={"type": "object"}, max_tokens=50))

    assert completion.tool_calls == [
        {"id": "abc", "name": "ec2_get_instance", "arguments": {}},
        {"id": "call_1", "name": "ec2_get_instance_metrics", "arguments": {"minutes": 30}},
    ]
    assert seen["stream"] is False and seen["think"] is False
    assert seen["tools"] == tools and seen["format"] == {"type": "object"}
    assert seen["options"] == {"num_ctx": 2048, "temperature": 0, "num_predict": 50}
    assert seen["messages"][1]["tool_calls"] == [{"function": {"name": "ec2_get_instance", "arguments": {}}}]
    assert seen["messages"][2] == {"role": "tool", "content": "{}", "tool_name": "ec2_get_instance"}


def test_ollama_complete_raises_on_error_body():
    with pytest.raises(LLMError, match="bad schema"):
        asyncio.run(ollama(lambda request: httpx.Response(400, json={"error": "bad schema"})).complete([], model="m"))


def test_openai_compatible_complete_parses_tool_calls_and_requests_json_schema():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "s3_get_bucket", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "s3_check_bucket_access", "arguments": "{\"action\": \"read\"}"}},
        ]}}]})

    provider = OpenAICompatibleProvider("https://llm.test/v1", transport=httpx.MockTransport(handler))
    completion = asyncio.run(provider.complete(
        [{"role": "user", "content": "hi"}], model="m", json_schema={"type": "object"}))
    assert completion.content == ""
    assert completion.tool_calls[1] == {"id": "c2", "name": "s3_check_bucket_access", "arguments": {"action": "read"}}
    assert seen["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert seen["temperature"] == 0


def test_ollama_reports_memory_of_loaded_models():
    def handler(request):
        return httpx.Response(200, json={"models": [{"size": 3_000_000_000}, {"size": 1_500_000_000}]})

    assert asyncio.run(ollama(handler).loaded_memory_bytes()) == 4_500_000_000


def test_openai_compatible_parses_sse_and_sends_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        body = "\n\n".join([
            'data: {"choices": [{"delta": {"role": "assistant"}}]}',
            'data: {"choices": [{"delta": {"content": "Hi"}}]}',
            ": keep-alive",
            'data: {"choices": [{"delta": {"content": " there"}}]}',
            "data: [DONE]",
            'data: {"choices": [{"delta": {"content": "ignored"}}]}',
        ])
        return httpx.Response(200, content=body.encode())

    provider = OpenAICompatibleProvider(
        "https://llm.test/v1", api_key="k", transport=httpx.MockTransport(handler))
    assert collect(provider) == ["Hi", " there"]
    assert seen == {"auth": "Bearer k", "url": "https://llm.test/v1/chat/completions"}


class FakeProvider:
    name = "fake"

    def __init__(self, models=None):
        self.models = models

    async def list_models(self):
        if self.models is None:
            raise LLMUnavailable("down")
        return self.models


@pytest.mark.parametrize("available, expected", [
    (["big", "small"], "big"),
    (["small"], "small"),
    (["other"], "big"),
    (None, "big"),
])
def test_resolve_model(available, expected):
    assert asyncio.run(resolve_model(FakeProvider(available), "big", "small")) == expected
