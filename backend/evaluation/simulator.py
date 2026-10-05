"""A deterministic simulated customer.

It does not read the agent's words. It looks at the agent's state: when the
agent is waiting for a detail it supplies that detail from the scenario, and
when a change is waiting for confirmation it confirms or declines as the
scenario says. No second model is involved, so a run is repeatable.
"""

import inspect
import time
from typing import Any

from pydantic import BaseModel

from backend.agent.agent import SupportAgent
from backend.agent.state import ConversationStatus as Status
from backend.evaluation.scenarios import Scenario

MAX_TURNS = 12
DONT_KNOW = "I don't know."

# Provider methods that change the cloud, and the write tool each one implements.
WRITE_METHODS = {
    "add_ingress_rule": "ec2_add_ingress_rule",
    "start_instance": "ec2_start_instance",
    "stop_instance": "ec2_stop_instance",
    "reboot_instance": "ec2_reboot_instance",
    "update_function_timeout": "lambda_update_timeout",
}


class RecordingCloud:
    """Wraps the cloud provider and records every call that changes something.

    This is the evaluation's own view of what was written, independent of the
    agent's records, so a write the agent did not account for is still seen.
    """

    def __init__(self, cloud: Any):
        self._cloud = cloud
        self.writes: list[dict] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._cloud, name)
        if name not in WRITE_METHODS:
            return attribute

        def recorded(*args, **kwargs):
            bound = inspect.signature(attribute).bind(*args, **kwargs)
            self.writes.append({"tool": WRITE_METHODS[name], "args": dict(bound.arguments)})
            return attribute(*args, **kwargs)
        return recorded


class Turn(BaseModel):
    customer: str
    agent: str
    latency_s: float
    status: str


class Outcome(BaseModel):
    """What happened in one scenario, as observed by the evaluation."""

    scenario_id: str
    turns: list[Turn] = []
    intent: str | None = None
    service: str | None = None
    status: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    result: str | None = None            # fixed, advice_given, or declined
    escalation_reason: str | None = None
    entities: dict[str, str] = {}
    tool_calls: list[dict] = []
    writes: list[dict] = []              # changes seen at the cloud provider
    confirmed: list[dict] = []           # changes the simulated customer confirmed
    post_condition: bool | None = None   # filled in by the runner
    error: str | None = None
    duration_s: float = 0.0


async def converse(agent: SupportAgent, scenario: Scenario, conversation_id: str, cloud: RecordingCloud) -> Outcome:
    """Play the scenario's customer against the agent until the conversation ends."""
    outcome = Outcome(scenario_id=scenario.scenario_id)
    facts = {slot: list(answers) if isinstance(answers, list) else [answers]
             for slot, answers in scenario.user_facts.items()}
    writes_before = len(cloud.writes)
    started = time.perf_counter()

    async def turn(shown: str, **kwargs) -> None:
        turn_started = time.perf_counter()
        reply = ""
        async for event in agent.handle_turn(conversation_id, **kwargs):
            if event["type"] == "token":
                reply += event["text"]
        state = await agent.get_state(conversation_id)
        outcome.turns.append(Turn(customer=shown, agent=reply, status=state.status.value,
                                  latency_s=round(time.perf_counter() - turn_started, 2)))

    async def say(text: str) -> None:
        await turn(text, event="message", text=text)

    if scenario.preamble:
        await say(scenario.preamble)
    await say(scenario.user_message)

    interjected = False
    unknown_answers = 0
    while len(outcome.turns) < MAX_TURNS:
        state = await agent.get_state(conversation_id)
        if state.status in (Status.RESOLVED, Status.ESCALATED):
            break

        if state.pending_action:
            action = state.pending_action
            if scenario.confirm_writes:
                outcome.confirmed.append({"tool": action.tool, "args": action.args})
            if scenario.confirm_via == "button":
                event = "confirm" if scenario.confirm_writes else "cancel"
                await turn(f"<{event} button>", event=event, action_id=action.action_id)
            else:
                await say("yes" if scenario.confirm_writes else "no")
            continue

        if state.missing_slots:
            if scenario.interjection and not interjected:
                interjected = True
                await say(scenario.interjection)
                continue
            answers = facts.get(state.missing_slots[0])
            if answers:
                await say(answers.pop(0) if len(answers) > 1 else answers[0])
            else:
                unknown_answers += 1
                if unknown_answers > 2:
                    break
                await say(DONT_KNOW)
            continue

        break  # the agent neither asked for anything nor proposed anything: the customer is done

    state = await agent.get_state(conversation_id)
    outcome.intent = state.intent.value if state.intent else None
    outcome.service = state.service.value if state.service else None
    outcome.status = state.status.value
    outcome.root_cause = state.root_cause_code.value if state.root_cause_code else None
    outcome.resolution = state.proposed_resolution_code.value if state.proposed_resolution_code else None
    outcome.result = state.outcome
    outcome.escalation_reason = state.escalation_reason
    outcome.entities = dict(state.entities)
    outcome.tool_calls = [
        {"id": c.id, "tool": c.tool, "args": c.args, "ok": c.ok, "error": c.error, "stage": c.stage,
         "requested_by": c.requested_by, "latency_ms": c.latency_ms}
        for c in state.tool_calls]
    outcome.writes = cloud.writes[writes_before:]
    outcome.duration_s = round(time.perf_counter() - started, 2)
    return outcome
