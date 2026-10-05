import json
from collections.abc import AsyncIterator

import httpx

from backend.providers.llm.base import Completion, LLMError, LLMUnavailable, Message

# Generation on CPU can pause for a long time before the first token.
TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


def _wire(message: Message) -> dict:
    """Translate a message to Ollama's chat format."""
    wire: dict = {"role": message["role"], "content": message["content"]}
    if message.get("tool_calls"):
        wire["tool_calls"] = [
            {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in message["tool_calls"]]
    if message["role"] == "tool":
        wire["tool_name"] = message.get("tool_name", "")
    return wire


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, *, num_ctx: int, keep_alive: str = "30m",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self._num_ctx = num_ctx
        # Reloading the model costs several seconds, so keep it loaded between turns.
        self._keep_alive = keep_alive

    def _body(self, messages: list[Message], model: str, *, stream: bool, options: dict | None = None) -> dict:
        return {
            "model": model,
            "messages": [_wire(m) for m in messages],
            "stream": stream,
            # Qwen3 models reason before answering unless told not to, which
            # multiplies latency on CPU.
            "think": False,
            "keep_alive": self._keep_alive,
            "options": {"num_ctx": self._num_ctx, **(options or {})},
        }

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"Ollama is not reachable: {e}") from e
        return [m["name"] for m in response.json()["models"]]

    async def loaded_memory_bytes(self) -> int:
        try:
            response = await self._client.get("/api/ps")
            response.raise_for_status()
        except httpx.HTTPError:
            return 0
        return sum(m.get("size", 0) for m in response.json()["models"])

    async def stream_chat(self, messages: list[Message], *, model: str) -> AsyncIterator[str]:
        body = self._body(messages, model, stream=True)
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode(errors="replace")
                    raise LLMError(f"Ollama returned {response.status_code}: {detail[:300]}")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise LLMError(chunk["error"])
                    text = chunk.get("message", {}).get("content")
                    if text:
                        yield text
        except httpx.ConnectError as e:
            raise LLMUnavailable(f"Ollama is not reachable: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e!r}") from e

    async def complete(self, messages: list[Message], *, model: str, tools: list[dict] | None = None,
                       json_schema: dict | None = None, max_tokens: int | None = None) -> Completion:
        options: dict = {"temperature": 0}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        body = self._body(messages, model, stream=False, options=options)
        if tools:
            body["tools"] = tools
        if json_schema is not None:
            body["format"] = json_schema
        try:
            response = await self._client.post("/api/chat", json=body)
        except httpx.ConnectError as e:
            raise LLMUnavailable(f"Ollama is not reachable: {e}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e!r}") from e
        if response.status_code >= 400:
            raise LLMError(f"Ollama returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        if "error" in data:
            raise LLMError(data["error"])
        message = data.get("message", {})
        calls = [
            {"id": c.get("id") or f"call_{i}", "name": c["function"]["name"],
             "arguments": c["function"].get("arguments") or {}}
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        return Completion(content=message.get("content") or "", tool_calls=calls)

    async def aclose(self) -> None:
        await self._client.aclose()
