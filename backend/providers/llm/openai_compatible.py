import json
from collections.abc import AsyncIterator

import httpx

from backend.providers.llm.base import Completion, LLMError, LLMUnavailable, Message


def _wire(message: Message) -> dict:
    """Translate a message to the OpenAI chat format."""
    wire: dict = {"role": message["role"], "content": message["content"]}
    if message.get("tool_calls"):
        wire["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for c in message["tool_calls"]]
    if message["role"] == "tool":
        wire["tool_call_id"] = message.get("tool_call_id", "")
    return wire

TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


class OpenAICompatibleProvider:
    """Any endpoint that speaks the OpenAI chat-completions API (NVIDIA, vLLM, ...).

    `base_url` includes the version prefix, e.g. https://integrate.api.nvidia.com/v1.
    """

    name = "openai-compatible"

    def __init__(self, base_url: str, *, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", headers=headers, timeout=TIMEOUT, transport=transport)

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("models")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"LLM endpoint is not reachable: {e}") from e
        return [m["id"] for m in response.json()["data"]]

    async def loaded_memory_bytes(self) -> int:
        return 0  # the model runs elsewhere

    async def stream_chat(self, messages: list[Message], *, model: str) -> AsyncIterator[str]:
        body = {"model": model, "messages": [_wire(m) for m in messages], "stream": True}
        try:
            async with self._client.stream("POST", "chat/completions", json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")
                    raise LLMError(f"LLM endpoint returned {response.status_code}: {detail[:300]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    choices = json.loads(data).get("choices") or []
                    text = choices[0].get("delta", {}).get("content") if choices else None
                    if text:
                        yield text
        except httpx.ConnectError as e:
            raise LLMUnavailable(f"LLM endpoint is not reachable: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {e!r}") from e

    async def complete(self, messages: list[Message], *, model: str, tools: list[dict] | None = None,
                       json_schema: dict | None = None, max_tokens: int | None = None) -> Completion:
        body: dict = {"model": model, "messages": [_wire(m) for m in messages], "temperature": 0}
        if tools:
            body["tools"] = tools
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema", "json_schema": {"name": "reply", "schema": json_schema, "strict": True}}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            response = await self._client.post("chat/completions", json=body)
        except httpx.ConnectError as e:
            raise LLMUnavailable(f"LLM endpoint is not reachable: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {e!r}") from e
        if response.status_code >= 400:
            raise LLMError(f"LLM endpoint returned {response.status_code}: {response.text[:300]}")
        message = response.json()["choices"][0]["message"]
        calls = []
        for i, call in enumerate(message.get("tool_calls") or []):
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append({"id": call.get("id") or f"call_{i}", "name": call["function"]["name"],
                          "arguments": arguments})
        return Completion(content=message.get("content") or "", tool_calls=calls)

    async def aclose(self) -> None:
        await self._client.aclose()
