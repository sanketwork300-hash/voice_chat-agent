from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, NotRequired, Protocol, TypedDict


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: dict


class Message(TypedDict):
    """One chat message. Assistant messages may carry tool calls; tool messages answer one."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: NotRequired[list[ToolCall]]
    tool_call_id: NotRequired[str]
    tool_name: NotRequired[str]


@dataclass(frozen=True)
class Completion:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMError(Exception):
    """The provider answered with an error."""


class LLMUnavailable(LLMError):
    """The provider could not be reached."""


class LLMProvider(Protocol):
    name: str

    async def list_models(self) -> list[str]:
        """Names of the models the provider can serve right now."""
        ...

    def stream_chat(self, messages: list[Message], *, model: str) -> AsyncIterator[str]:
        """Yield the reply as text fragments, in order."""
        ...

    async def complete(self, messages: list[Message], *, model: str, tools: list[dict] | None = None,
                       json_schema: dict | None = None, max_tokens: int | None = None) -> Completion:
        """One deterministic (temperature 0) reply.

        `tools` are function definitions the model may call. `json_schema`
        constrains the reply to JSON matching the schema.
        """
        ...

    async def loaded_memory_bytes(self) -> int:
        """RAM this machine already spends on models the provider keeps loaded."""
        ...

    async def aclose(self) -> None: ...
