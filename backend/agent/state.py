from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from backend.agent.taxonomy import Intent, Resolution, RootCause, Service


class ConversationStatus(StrEnum):
    IDENTIFYING = "IDENTIFYING"
    COLLECTING_INFORMATION = "COLLECTING_INFORMATION"
    DIAGNOSING = "DIAGNOSING"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


Stage = Literal["resolve", "investigate", "execute", "verify"]


class ToolCallRecord(BaseModel):
    id: str
    turn: int
    stage: Stage
    requested_by: Literal["model", "agent"] = "agent"
    tool: str
    args: dict
    read_only: bool
    ok: bool
    result: dict | None = None
    error: dict | None = None
    latency_ms: int


class PendingAction(BaseModel):
    action_id: str
    tool: str
    args: dict
    summary: str
    created_at: datetime
    expires_at: datetime
    # Set only by the confirmation path; the gateway refuses to execute without it.
    confirmed: bool = False


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class SupportState(BaseModel):
    conversation_id: str = ""
    channel: Literal["chat", "voice"] = "chat"
    status: ConversationStatus = ConversationStatus.IDENTIFYING
    turn: int = 0

    # Input of the current turn.
    event: Literal["message", "confirm", "cancel", "escalate"] = "message"
    user_text: str = ""
    event_action_id: str | None = None

    # Working fields of the current turn; reset at the start of each one.
    route: str = ""
    reply: str = ""
    message_kind: str = ""  # the classifier's label for this message: problem, question, or other
    turn_fingerprint: str = ""  # state at the start of the turn, to detect turns without progress
    latency_ms: dict[str, int] = Field(default_factory=dict)

    service: Service | None = None
    intent: Intent | None = None
    problem_statement: str = ""  # the customer message that stated the current problem
    entities: dict[str, str] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)

    retrieved_documents: list[dict] = Field(default_factory=list)  # from this turn's knowledge-base search
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)

    root_cause_code: RootCause | None = None
    evidence: list[str] = Field(default_factory=list)
    proposed_resolution_code: Resolution | None = None
    pending_action: PendingAction | None = None
    outcome: Literal["fixed", "advice_given", "declined"] | None = None

    failed_tool_calls: int = 0
    turns_without_progress: int = 0
    clarification_attempts: int = 0
    confirmation_reasks: int = 0  # voice: unclear replies to the pending change since it was proposed
    escalation_reason: str | None = None

    messages: list[ChatMessage] = Field(default_factory=list)

    def latest_result(self, tool: str) -> dict | None:
        """The most recent successful result of a tool, if any."""
        for call in reversed(self.tool_calls):
            if call.tool == tool and call.ok:
                return call.result
        return None
