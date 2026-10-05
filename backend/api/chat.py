"""Chat over WebSocket.

Client → server:
    {"type": "message", "text": "..."}
    {"type": "confirm_action", "action_id": "..."}   Confirm button on a proposed change
    {"type": "cancel_action", "action_id": "..."}    Cancel button
    {"type": "escalate"}                             "Talk to a human" button
    {"type": "clear"}                                start a new conversation

Server → client: ready, status (thinking | idle), done, cleared, error, plus the
agent's own events (token, activity, state, pending_action, action_resolved, sources, escalated).
"""

import logging
import time
import uuid
from collections import deque
from typing import Annotated, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, StringConstraints, TypeAdapter, ValidationError

from backend.agent.agent import SupportAgent
from backend.metrics import CHAT_TURNS, LLM_FIRST_TOKEN_SECONDS, LLM_RESPONSE_SECONDS
from backend.providers.llm import LLMError, LLMUnavailable

log = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGE_CHARS = 2000
RATE_LIMIT_TURNS = 20
RATE_LIMIT_WINDOW_S = 60

ActionId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]


class UserMessage(BaseModel):
    type: Literal["message"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]


class ConfirmAction(BaseModel):
    type: Literal["confirm_action"]
    action_id: ActionId


class CancelAction(BaseModel):
    type: Literal["cancel_action"]
    action_id: ActionId


class Escalate(BaseModel):
    type: Literal["escalate"]


class Clear(BaseModel):
    type: Literal["clear"]


ClientEvent = TypeAdapter(
    Annotated[UserMessage | ConfirmAction | CancelAction | Escalate | Clear, Field(discriminator="type")])


class RateLimiter:
    """At most `limit` turns per `window_s` seconds for one conversation."""

    def __init__(self, limit: int, window_s: float):
        self._limit, self._window_s = limit, window_s
        self._turns: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._turns and now - self._turns[0] > self._window_s:
            self._turns.popleft()
        if len(self._turns) >= self._limit:
            return False
        self._turns.append(now)
        return True


@router.websocket("/ws/chat")
async def chat(ws: WebSocket) -> None:
    await ws.accept()
    agent: SupportAgent = ws.app.state.agent
    model: str = ws.app.state.llm_model
    limiter = RateLimiter(RATE_LIMIT_TURNS, RATE_LIMIT_WINDOW_S)

    async def start_conversation() -> str:
        conversation_id = uuid.uuid4().hex
        await ws.send_json({"type": "ready", "conversation_id": conversation_id, "model": model})
        return conversation_id

    conversation_id = await start_conversation()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = ClientEvent.validate_json(raw)
            except ValidationError:
                await ws.send_json({
                    "type": "error",
                    "message": f"Messages must be between 1 and {MAX_MESSAGE_CHARS} characters."})
                continue
            if isinstance(event, Clear):
                await ws.send_json({"type": "cleared"})
                conversation_id = await start_conversation()
                continue
            if not limiter.allow():
                await ws.send_json({
                    "type": "error", "message": "You're sending messages too quickly. Please wait a moment."})
                continue

            if isinstance(event, UserMessage):
                turn = {"event": "message", "text": event.text}
            elif isinstance(event, ConfirmAction):
                turn = {"event": "confirm", "action_id": event.action_id}
            elif isinstance(event, CancelAction):
                turn = {"event": "cancel", "action_id": event.action_id}
            else:
                turn = {"event": "escalate"}
            await run_turn(ws, agent, model, conversation_id, turn)
    except WebSocketDisconnect:
        pass


async def run_turn(ws: WebSocket, agent: SupportAgent, model: str, conversation_id: str, turn: dict) -> None:
    await ws.send_json({"type": "status", "status": "thinking"})
    start = time.perf_counter()
    first_token: float | None = None
    try:
        async for event in agent.handle_turn(conversation_id, channel="chat", **turn):
            if event["type"] == "token" and first_token is None:
                first_token = time.perf_counter() - start
            await ws.send_json(event)
    except WebSocketDisconnect:
        raise
    except LLMError as e:
        log.warning("chat turn failed (conversation %s): %s", conversation_id, e)
        await fail_turn(ws, (
            "The assistant's language model is not reachable right now. Please try again shortly."
            if isinstance(e, LLMUnavailable) else "The assistant could not produce a reply. Please try again."))
        return
    except Exception:
        log.exception("chat turn crashed (conversation %s)", conversation_id)
        await fail_turn(ws, "Something went wrong on our side. Please try again.")
        return

    total = time.perf_counter() - start
    if first_token is not None:
        LLM_FIRST_TOKEN_SECONDS.labels(model).observe(first_token)
    LLM_RESPONSE_SECONDS.labels(model).observe(total)
    CHAT_TURNS.labels("ok").inc()
    await ws.send_json({
        "type": "done",
        "latency_ms": {"first_token": round((first_token or total) * 1000), "total": round(total * 1000)}})
    await ws.send_json({"type": "status", "status": "idle"})


async def fail_turn(ws: WebSocket, message: str) -> None:
    CHAT_TURNS.labels("error").inc()
    # The client discards any partial reply when it receives an error.
    await ws.send_json({"type": "error", "message": message})
    await ws.send_json({"type": "status", "status": "idle"})
