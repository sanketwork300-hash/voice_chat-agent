"""The support agent: one graph run per customer turn.

The model is used in four places: to label the customer's message, to choose
which read tools to call, to pick between root causes when more than one is
supported, and to word replies. Everything else is decided here in code: which
details are still needed, which root causes the tool results support, what
change to propose, whether the customer confirmed it, and when to escalate.

Events streamed to the caller during a turn:
    token            text of the reply, in order
    activity         a check being run, in customer language
    state            status, service, and intent after they change
    pending_action   a change waiting for the customer's confirmation
    action_resolved  that change was executed, declined, cancelled, or expired
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Literal

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from backend.agent import extract, prompts
from backend.agent.confirmation import classify_reply
from backend.agent.state import (
    ChatMessage,
    PendingAction,
    Stage,
    SupportState,
    ToolCallRecord,
)
from backend.agent.state import ConversationStatus as Status
from backend.agent.taxonomy import (
    INTENTS,
    RESOLUTIONS,
    RESOURCE_ENTITIES,
    ROOT_CAUSES,
    SERVICE_NAMES,
    SLOT_ENTITY,
    SLOT_QUESTIONS,
    Intent,
    Resolution,
    RootCause,
    Service,
    investigable,
    refine_intent,
    slot_filled,
    supported_causes,
)
from backend.memory.store import Store
from backend.metrics import ESCALATIONS, RAG_SEARCHES, RAG_SECONDS
from backend.providers.llm import LLMError, LLMProvider, Message
from backend.rag.knowledge import KnowledgeBase, Retrieved
from backend.security.redaction import StreamRedactor, redact
from backend.tools.catalog import CATALOG, read_tools
from backend.tools.gateway import CallContext, PolicyError, ToolGateway

log = logging.getLogger(__name__)

HISTORY_LIMIT = 12          # messages kept for the model's context
MAX_TOOL_ROUNDS = 3         # model replies that may request tools, per investigation
MAX_TOOL_CALLS = 6          # tool calls per investigation
MAX_CLARIFICATIONS = 2      # unidentified problems tolerated before escalating
MAX_FAILED_TOOL_CALLS = 3
STALLED_TURNS = 4           # consecutive turns without progress before escalating
MAX_CONFIRMATION_REASKS = 1  # voice: unclear replies to a proposed change re-asked before it is withdrawn
VERIFY_ATTEMPTS = 5         # re-reads after a write, one second apart

# Tool errors that mean the check itself failed, as opposed to finding that something is missing.
_INFRA_ERRORS = {"timeout", "unavailable", "access_denied", "invalid"}
_ENDED = (Status.RESOLVED, Status.ESCALATED)

NO_DOCUMENTATION_TEXT = (
    "I don't have documentation that covers that, so I can't answer it reliably. "
    f"I can help with problems and questions about {prompts.SUPPORTED}.")
NOT_INSPECTABLE_TEXT = (
    "I can't inspect {service} resources yet, and I don't have documentation that covers this problem.")
NOT_CHECKED_TEXT = "I couldn't check your {service} resources myself; this guidance comes from the documentation."
_CITATION = re.compile(r"\[(\d+)\]")

ESCALATION_TEXT = (
    "I'm passing this to a human support engineer, along with everything I've checked so far. "
    "They'll pick it up from here.")


def checkpoint_serde() -> JsonPlusSerializer:
    """Serializer that is allowed to restore the state's own types from a checkpoint."""
    types = [Status, ToolCallRecord, PendingAction, ChatMessage, Service, Intent, RootCause, Resolution]
    return JsonPlusSerializer(allowed_msgpack_modules=[(t.__module__, t.__name__) for t in types])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(s: SupportState) -> str:
    """What must change for a turn to count as progress."""
    return json.dumps([
        s.status, s.intent, sorted(s.entities.items()), len(s.tool_calls), s.root_cause_code,
        s.pending_action.action_id if s.pending_action else None,
    ])


class SupportAgent:
    def __init__(self, llm: LLMProvider, model: str, gateway: ToolGateway, store: Store,
                 checkpointer: BaseCheckpointSaver, knowledge: KnowledgeBase, *,
                 rag_top_k: int = 3, rag_min_score: float = 0.74):
        self._llm = llm
        self._model = model
        self._gateway = gateway
        self._store = store
        self._knowledge = knowledge
        self._rag_top_k = rag_top_k
        self._rag_min_score = rag_min_score
        self._graph = self._build().compile(checkpointer=checkpointer)

    # ------------------------------------------------------------- public API

    async def handle_turn(self, conversation_id: str, *, channel: Literal["chat", "voice"] = "chat",
                          event: Literal["message", "confirm", "cancel", "escalate"] = "message", text: str = "",
                          action_id: str | None = None) -> AsyncIterator[dict]:
        """Run one turn and yield its events."""
        turn = {"conversation_id": conversation_id, "channel": channel, "event": event,
                "user_text": text, "event_action_id": action_id}
        config = {"configurable": {"thread_id": conversation_id}}
        async for item in self._graph.astream(turn, config, stream_mode="custom"):
            yield item

    async def get_state(self, conversation_id: str) -> SupportState | None:
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": conversation_id}})
        return SupportState.model_validate(snapshot.values) if snapshot.values else None

    # ------------------------------------------------------------------ graph

    def _build(self) -> StateGraph:
        graph = StateGraph(SupportState)
        for name in ("intake", "classify", "answer", "advance", "investigate", "diagnose", "explain",
                     "execute", "finish"):
            graph.add_node(name, getattr(self, f"_{name}"))

        def route(state: SupportState) -> str:
            return state.route

        graph.add_edge(START, "intake")
        graph.add_conditional_edges("intake", route, ["classify", "advance", "execute", "finish"])
        graph.add_conditional_edges("classify", route, ["answer", "advance", "finish"])
        graph.add_conditional_edges("advance", route, ["investigate", "answer", "finish"])
        graph.add_conditional_edges("investigate", route, ["diagnose", "finish"])
        graph.add_conditional_edges("diagnose", route, ["explain", "finish"])
        graph.add_edge("explain", "advance")
        graph.add_edge("answer", "finish")
        graph.add_edge("execute", "finish")
        graph.add_edge("finish", END)
        return graph

    # ------------------------------------------------------------------ nodes

    async def _intake(self, state: SupportState) -> dict:
        """Start the turn and handle everything that needs no model: confirmations and direct answers."""
        s = state.model_copy(deep=True)
        s.turn += 1
        s.route, s.reply, s.message_kind, s.latency_ms, s.retrieved_documents = "", "", "", {}, []
        s.turn_fingerprint = _fingerprint(s)
        text = s.user_text.strip()
        if s.event == "message":
            s.messages = [*s.messages, ChatMessage(role="user", text=text)][-HISTORY_LIMIT:]

        if s.pending_action and _now() >= s.pending_action.expires_at:
            await self._close_action(s, "expired")
            s.status = Status.DIAGNOSING

        pending = s.pending_action
        if s.event == "escalate":
            await self._escalate(s, "customer_request", "the customer pressed the escalation button")
        elif s.event != "message":
            if not pending or pending.action_id != s.event_action_id:
                self._say(s, "That change is no longer waiting for confirmation, so I haven't done anything.")
                s.route = "finish"
            elif s.event == "confirm":
                await self._confirm(s, "button")
            else:
                await self._decline(s, "button")
        elif extract.wants_human(text):
            await self._escalate(s, "customer_request", "the customer asked for a human")
        elif pending:
            reply = classify_reply(text)
            if reply == "affirm":
                await self._confirm(s, "message")
            elif reply == "decline":
                await self._decline(s, "message")
            else:
                s.route = "classify"
        elif self._fill_slot(s, text):
            s.route = "advance"
        else:
            s.route = "classify"
        return dict(s)

    async def _classify(self, state: SupportState) -> dict:
        """Label the message with the model, then merge what it found into the state."""
        s = state.model_copy(deep=True)
        started = time.perf_counter()
        completion = await self._llm.complete(
            [{"role": "system", "content": prompts.CLASSIFY_SYSTEM},
             {"role": "user", "content": prompts.classify_user(s, self._last_question(s))}],
            model=self._model, json_schema=prompts.CLASSIFY_SCHEMA, max_tokens=120)
        s.latency_ms["classify"] = round((time.perf_counter() - started) * 1000)

        try:
            label = json.loads(completion.content)
            kind, intent = label["kind"], Intent(label["intent"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            log.warning("unusable classification: %r", completion.content[:200])
            label, kind, intent = {}, "other", Intent.UNKNOWN

        found = self._extract_details(s, label)
        intent = refine_intent(intent, found)
        if kind == "question" and intent is not Intent.UNKNOWN and RESOURCE_ENTITIES & found.keys():
            kind = "problem"  # it names one of the customer's own resources, so it needs checking
        s.message_kind = kind
        is_problem = kind == "problem" and intent is not Intent.UNKNOWN
        if is_problem and (intent is not s.intent or s.status in _ENDED):
            await self._start_problem(s, intent)

        has_active_problem = s.intent not in (None, Intent.UNKNOWN) and s.status not in _ENDED
        if is_problem or (has_active_problem and found and kind != "question"):
            changed = {k for k in RESOURCE_ENTITIES if k in found and k in s.entities and found[k] != s.entities[k]}
            if "instance_ref" in changed and "instance_id" not in found:
                s.entities.pop("instance_id", None)  # a different instance was named; the old ID no longer applies
                s.entities.pop("instance_name", None)
            if changed & {"iam_user", "iam_role"} or (
                    {"iam_user", "iam_role"} & found.keys() and not s.entities.get("principal_checked")):
                s.entities.pop("principal_checked", None)
            if changed and s.root_cause_code:
                await self._drop_diagnosis(s)  # it was about a different resource
            s.entities.update(found)
            s.route = "advance"
        elif s.pending_action and s.channel == "voice" and kind != "question":
            # Spoken replies to a proposed change must be clear: re-ask once, then withdraw it.
            await self._reask_or_withdraw(s)
        elif kind == "problem" and not has_active_problem:
            s.clarification_attempts += 1
            if s.clarification_attempts > MAX_CLARIFICATIONS:
                await self._escalate(s, "unidentified", "the problem could not be identified after two clarification attempts")
            else:
                s.route = "answer"
        else:
            s.route = "answer"
        return dict(s)

    async def _answer(self, state: SupportState) -> dict:
        """Reply without touching the customer's resources: questions, small talk, unsupported requests.

        Questions about how AWS works are answered from the knowledge base or not at all.
        """
        s = state.model_copy(deep=True)
        uninspectable = s.intent not in (None, Intent.UNKNOWN) and not investigable(s.intent)
        is_question = s.message_kind == "question"
        # A question asked in the middle of troubleshooting may be about the conversation itself.
        about_conversation = bool(s.pending_action or s.root_cause_code or (s.intent and not uninspectable))

        if is_question or uninspectable:
            self._interim(s, "Let me look that up.")
        documents = await self._retrieve(s, s.user_text, s.service) if is_question or uninspectable else []
        note = ("The customer reports a problem. Give troubleshooting guidance for it from the excerpts."
                ) if uninspectable else None

        answered = bool(documents) and await self._answer_from_documentation(s, documents, note)
        if documents and not answered:
            self._emit({"type": "activity", "id": f"d{s.turn}", "label": "Searching the documentation",
                        "state": "warn", "detail": "The pages found do not answer this"})

        if answered:
            if uninspectable:
                self._say(s, NOT_CHECKED_TEXT.format(service=SERVICE_NAMES[s.service]))
        elif uninspectable:
            self._say(s, NOT_INSPECTABLE_TEXT.format(service=SERVICE_NAMES[s.service]))
        elif is_question and not about_conversation:
            self._say(s, NO_DOCUMENTATION_TEXT)
        else:
            await self._stream(s, "answer", self._answer_messages(s, prompts.ANSWER_TASK, None, None))

        if s.pending_action:
            self._say(s, f"Would you like me to go ahead? {s.pending_action.summary}")
        elif s.status is Status.COLLECTING_INFORMATION and s.missing_slots:
            self._say(s, SLOT_QUESTIONS[s.missing_slots[0]])
        s.route = "finish"
        return dict(s)

    async def _advance(self, state: SupportState) -> dict:
        """Decide the next step for the current problem: ask, investigate, or propose a fix."""
        s = state.model_copy(deep=True)
        spec = INTENTS[s.intent]
        if not investigable(s.intent):
            # No tools for this service yet: general guidance is all there is to give.
            s.status, s.outcome = Status.RESOLVED, "advice_given"
            s.route = "answer"
            return dict(s)

        if "instance" in spec.slots and s.entities.get("instance_ref") and not s.entities.get("instance_id"):
            if not await self._resolve_instance(s):
                return dict(s)
        if "principal" in spec.slots and not s.entities.get("principal_checked") and any(
                s.entities.get(k) for k in ("iam_user", "iam_role", "principal_ref")):
            if not await self._resolve_principal(s):
                return dict(s)

        missing = [slot for slot in spec.slots if not slot_filled(slot, s.entities)]
        if missing:
            self._ask(s, missing)
            return dict(s)

        if s.root_cause_code is None:
            s.missing_slots = []
            s.status = Status.DIAGNOSING
            self._emit_state(s)
            s.route = "investigate"
            return dict(s)

        resolution = RESOLUTIONS[s.proposed_resolution_code]
        if resolution.write is None:
            s.status, s.outcome, s.missing_slots = Status.RESOLVED, "advice_given", []
            s.route = "finish"
            return dict(s)

        missing = [slot for slot in resolution.write.slots if not slot_filled(slot, s.entities)]
        if missing:
            self._ask(s, missing)
            return dict(s)

        await self._propose(s)
        return dict(s)

    async def _investigate(self, state: SupportState) -> dict:
        """Let the model call read tools until the results support a root cause or it stops asking."""
        s = state.model_copy(deep=True)
        started = time.perf_counter()
        subject = self._subject(s)
        self._interim(s, f"Let me check {subject}." if subject else "Let me check that.")
        messages: list[Message] = [
            {"role": "system", "content": prompts.INVESTIGATE_SYSTEM},
            {"role": "user", "content": prompts.investigate_user(s)},
        ]
        # Results already obtained this turn are reused rather than fetched again.
        seen: dict[str, str] = {
            json.dumps([c.tool, {}], sort_keys=True): prompts.tool_result_text(c)
            for c in s.tool_calls if c.turn == s.turn and c.ok}
        calls_made = 0
        nudged = False

        for _ in range(MAX_TOOL_ROUNDS):
            tools = [t.llm_schema() for t in read_tools(s.service, s.entities)]
            completion = await self._llm.complete(messages, model=self._model, tools=tools, max_tokens=200)
            if not completion.tool_calls:
                if calls_made == 0 and not nudged:
                    nudged = True
                    messages.append({"role": "assistant", "content": completion.content})
                    messages.append({"role": "user", "content": prompts.INVESTIGATE_NUDGE})
                    continue
                break

            messages.append({"role": "assistant", "content": "", "tool_calls": completion.tool_calls})
            for call in completion.tool_calls:
                key = json.dumps([call["name"], call["arguments"]], sort_keys=True)
                if key in seen:
                    content = seen[key]
                elif calls_made >= MAX_TOOL_CALLS:
                    content = '{"note":"call limit reached"}'
                else:
                    calls_made += 1
                    record = await self._tool(s, call["name"], call["arguments"], "investigate")
                    record.requested_by = "model"
                    content = seen[key] = prompts.tool_result_text(record)
                messages.append({"role": "tool", "content": content, "tool_call_id": call["id"],
                                 "tool_name": call["name"]})
            # Stop as soon as a root cause is supported: asking the model whether it
            # is done costs a full model call and cannot change what the results show.
            if calls_made >= MAX_TOOL_CALLS or supported_causes(s):
                break

        await self._complete_checks(s)
        s.latency_ms["investigate"] = round((time.perf_counter() - started) * 1000)
        instance = s.latest_result("ec2_get_instance")
        if instance and instance.get("name"):
            s.entities["instance_name"] = instance["name"]

        if not await self._tools_failing(s):
            s.route = "diagnose"
        return dict(s)

    async def _diagnose(self, state: SupportState) -> dict:
        """Conclude a root cause, but only one that the tool results support."""
        s = state.model_copy(deep=True)
        supported = supported_causes(s)
        if not supported:
            code = RootCause.NO_FAULT_FOUND
        elif len(supported) == 1:
            code = next(iter(supported))
        else:
            code = await self._choose_cause(s, list(supported))

        s.root_cause_code = code
        s.evidence = supported.get(code, [])
        s.proposed_resolution_code = ROOT_CAUSES[code].resolution
        if code is RootCause.NO_FAULT_FOUND:
            await self._escalate(s, "no_fault_found", "the checks that ran did not find the cause")
        else:
            s.route = "explain"
        return dict(s)

    async def _explain(self, state: SupportState) -> dict:
        """Tell the customer what was found and what it means."""
        s = state.model_copy(deep=True)
        await self._stream(s, "explain", [
            {"role": "system", "content": prompts.respond_system(prompts.EXPLAIN_TASK, s.channel)},
            {"role": "user", "content": prompts.explain_user(s)},
        ])
        # Point the customer to the documentation for this cause. It is not fed to the model.
        cause = ROOT_CAUSES[s.root_cause_code]
        related = await self._retrieve(
            s, f"{cause.description}. {RESOLUTIONS[cause.resolution].advice}", s.service, announce=False)
        if related:
            self._emit_sources(s, "related")
        s.route = "advance"
        return dict(s)

    async def _execute(self, state: SupportState) -> dict:
        """Run the confirmed change, then re-read to verify it took effect."""
        s = state.model_copy(deep=True)
        action = s.pending_action
        write = RESOLUTIONS[s.proposed_resolution_code].write
        s.status = Status.EXECUTING
        self._emit_state(s)
        self._interim(s, "Applying that now.")

        try:
            record = await self._run_tool(
                s, CATALOG[action.tool].activity, "execute",
                lambda ctx, call_id: self._gateway.execute_confirmed(ctx, call_id, action))
        except PolicyError as e:
            log.error("refused to execute action %s: %s", action.action_id, e)
            await self._close_action(s, "refused")
            await self._escalate(s, "write_failed", f"a confirmed change was refused by policy: {e}")
            return dict(s)

        await self._store.record_audit(s.conversation_id, "action_executed", {
            "action_id": action.action_id, "tool": action.tool, "args": action.args, "ok": record.ok,
            "error": record.error})
        if not record.ok:
            await self._close_action(s, "failed")
            self._say(s, "I wasn't able to make that change.")
            await self._escalate(s, "write_failed", "a confirmed change failed to execute")
            return dict(s)

        s.status = Status.VERIFYING
        self._emit_state(s)
        verified = False
        for attempt in range(VERIFY_ATTEMPTS):
            check = await self._tool(s, write.verify_tool, {}, "verify")
            verified = check.ok and write.verify_ok(action.args, check.result)
            if verified or not check.ok:
                break
            if attempt < VERIFY_ATTEMPTS - 1:
                await asyncio.sleep(1)
        await self._store.record_audit(s.conversation_id, "action_verified", {
            "action_id": action.action_id, "verified": verified})

        await self._close_action(s, "executed")
        if verified:
            self._say(s, f"Done. {write.verified_text(s, action.args)}")
            s.status, s.outcome = Status.RESOLVED, "fixed"
            s.route = "finish"
        else:
            self._say(s, "I made the change, but I couldn't confirm that it took effect.")
            await self._escalate(s, "write_failed", "a confirmed change could not be verified")
        return dict(s)

    async def _finish(self, state: SupportState) -> dict:
        """Close the turn: detect a stalled conversation, record the reply, publish the final state."""
        s = state.model_copy(deep=True)
        if s.status not in _ENDED:
            stalled = _fingerprint(s) == s.turn_fingerprint
            s.turns_without_progress = s.turns_without_progress + 1 if stalled else 0
            if s.turns_without_progress >= STALLED_TURNS:
                await self._escalate(s, "no_progress", f"{STALLED_TURNS} turns passed without progress")

        if s.reply:
            s.messages = [*s.messages, ChatMessage(role="assistant", text=s.reply)][-HISTORY_LIMIT:]
        await self._store.upsert_conversation(
            s.conversation_id, s.channel, s.status.value, s.intent.value if s.intent else None)
        self._emit_state(s)
        return dict(s)

    # ---------------------------------------------------------------- helpers

    def _emit(self, event: dict) -> None:
        get_stream_writer()(event)

    def _emit_state(self, s: SupportState) -> None:
        self._emit({
            "type": "state", "status": s.status.value,
            "service": s.service.value if s.service else None,
            "intent": s.intent.value if s.intent else None,
        })

    def _interim(self, s: SupportState, text: str) -> None:
        """On a call, say something short while the slow part runs. Not part of the reply; chat shows nothing."""
        if s.channel == "voice":
            self._emit({"type": "interim", "text": text})

    @staticmethod
    def _subject(s: SupportState) -> str | None:
        """The resource this problem is about, by the name the customer would recognise."""
        for key in ("instance_name", "bucket", "function", "alarm", "iam_user", "iam_role"):
            if s.entities.get(key):
                return s.entities[key]
        return None

    async def _reask_or_withdraw(self, s: SupportState) -> None:
        s.confirmation_reasks += 1
        if s.confirmation_reasks > MAX_CONFIRMATION_REASKS:
            await self._decline(s, "unclear")
            return
        self._say(s, f"Sorry, I didn't catch that. {s.pending_action.summary} Shall I go ahead? Please say yes or no.")
        s.route = "finish"

    def _say(self, s: SupportState, text: str) -> None:
        """Send fixed text to the customer as its own paragraph."""
        text = redact(text)
        chunk = f"\n\n{text}" if s.reply else text
        s.reply += chunk
        self._emit({"type": "token", "text": chunk})

    async def _stream(self, s: SupportState, stage: str, messages: list[Message],
                      release_when: Callable[[str], bool] | None = None) -> str | None:
        """Stream a model-written reply to the customer and return it.

        With `release_when`, the reply is held back until the text so far passes
        the check, then streamed normally. If it never passes, nothing reaches
        the customer and None is returned.
        """
        started = time.perf_counter()
        redactor = StreamRedactor()
        prefix = "\n\n" if s.reply else ""

        def send(text: str) -> None:
            nonlocal prefix
            if text:
                s.reply += prefix + text
                self._emit({"type": "token", "text": prefix + text})
                prefix = ""

        full = ""
        holding = release_when is not None
        async for fragment in self._llm.stream_chat(messages, model=self._model):
            full += fragment
            if holding:
                if not release_when(full):
                    continue
                holding, fragment = False, full
            send(redactor.feed(fragment))
        s.latency_ms[stage] = round((time.perf_counter() - started) * 1000)

        if not full.strip():
            raise LLMError("the model returned an empty reply")
        if holding:
            return None
        send(redactor.flush())
        return full

    def _answer_messages(self, s: SupportState, task: str, note: str | None, excerpts: str | None) -> list[Message]:
        history: list[Message] = [{"role": m.role, "content": m.text} for m in s.messages[-7:-1]]
        final = f"Context for you (not said by the customer):\n{prompts.answer_context(s, note)}"
        if excerpts:
            final += f"\n\nDocumentation excerpts:\n{excerpts}"
        final += f"\n\nCustomer: {s.user_text}"
        return [{"role": "system", "content": prompts.respond_system(task, s.channel)}, *history,
                {"role": "user", "content": final}]

    async def _retrieve(self, s: SupportState, query: str, service: Service | None, *,
                        announce: bool = True) -> list[Retrieved]:
        """Search the knowledge base and record what was found in the state."""
        started = time.perf_counter()
        if announce:
            self._emit({"type": "activity", "id": f"d{s.turn}", "label": "Searching the documentation",
                        "state": "running"})
        try:
            found = await self._knowledge.search(
                query, service=service.value if service else None, k=self._rag_top_k, min_score=self._rag_min_score)
        except (OSError, psycopg.Error) as e:
            log.warning("knowledge-base search failed: %s", e)
            found = []
        elapsed = time.perf_counter() - started
        s.latency_ms["retrieve"] = round(elapsed * 1000)
        if announce:
            self._emit({
                "type": "activity", "id": f"d{s.turn}", "label": "Searching the documentation", "state": "ok",
                "detail": f"Found {len(found)} relevant section(s)" if found else "Nothing relevant found"})
        RAG_SECONDS.observe(elapsed)
        RAG_SEARCHES.labels("hit" if found else "empty").inc()
        s.retrieved_documents = [
            {"n": n, "chunk_id": d.chunk_id, "service": d.service, "topic": d.topic, "section": d.section,
             "source_url": d.source_url, "source_date": d.source_date.isoformat(), "score": round(d.score, 3),
             "cited": False}
            for n, d in enumerate(found, start=1)]
        return found

    async def _answer_from_documentation(self, s: SupportState, documents: list[Retrieved], note: str | None) -> bool:
        """Answer from retrieved excerpts.

        The reply reaches the customer only once it cites one of the excerpts.
        A reply without a citation (the model declined, or answered from memory)
        is discarded and False is returned.
        """
        def cited_numbers(text: str) -> set[int]:
            return {int(n) for n in _CITATION.findall(text) if 1 <= int(n) <= len(documents)}

        reply = await self._stream(
            s, "answer",
            self._answer_messages(s, prompts.DOCUMENTATION_TASK, note, prompts.documentation_block(documents)),
            release_when=lambda text: bool(cited_numbers(text)))
        if reply is None:
            return False
        cited = cited_numbers(reply)
        for document in s.retrieved_documents:
            document["cited"] = document["n"] in cited
        self._emit_sources(s, "cited")
        return True

    def _emit_sources(self, s: SupportState, kind: Literal["cited", "related"]) -> None:
        """Show the customer where an answer came from, or where to read more."""
        self._emit({"type": "sources", "kind": kind, "documents": [
            {"n": d["n"], "title": d["topic"], "section": d["section"], "url": d["source_url"],
             "source_date": d["source_date"], "cited": d["cited"]}
            for d in s.retrieved_documents]})

    def _ask(self, s: SupportState, missing: list[str]) -> None:
        s.missing_slots = missing
        s.status = Status.COLLECTING_INFORMATION
        self._say(s, SLOT_QUESTIONS[missing[0]])
        s.route = "finish"

    def _last_question(self, s: SupportState) -> str | None:
        if s.status is Status.COLLECTING_INFORMATION and s.missing_slots:
            return SLOT_QUESTIONS[s.missing_slots[0]]
        return None

    def _fill_slot(self, s: SupportState, text: str) -> bool:
        """Take the message as the answer to the question just asked, when it plainly is one."""
        if s.status is not Status.COLLECTING_INFORMATION or not s.missing_slots:
            return False
        slot = s.missing_slots[0]
        if slot == "instance":
            value = extract.instance_id(text)
            if value:
                s.entities.update(instance_id=value, instance_ref=value)
                return True
            value = extract.single_token(text)
            if value:
                s.entities["instance_ref"] = value
            return bool(value)
        if slot == "principal":
            answer = extract.principal_answer(text)
            if answer:
                self._set_principal(s, *answer)
            return bool(answer)
        if slot == "protocol":
            value = extract.protocol(text)
        elif slot == "source_cidr":
            value = extract.source_cidr(text)
        elif slot == "iam_action":
            value = extract.iam_action(text)
        elif slot == "timeout_seconds":
            seconds = extract.seconds(text)
            value = str(seconds) if seconds and 1 <= seconds <= 900 else None
        else:
            value = extract.single_token(text)
            if value and slot == "bucket":
                value = value.lower()
        if value:
            s.entities[SLOT_ENTITY.get(slot, slot)] = value
        return bool(value)

    def _extract_details(self, s: SupportState, label: dict) -> dict[str, str]:
        """Details in the message: by pattern where possible, from the model only if literally present."""
        text = s.user_text
        found: dict[str, str] = {}
        for field, entity in prompts.CLASSIFY_DETAILS.items():
            value = str(label.get(field) or "").strip()
            if value and extract.appears_in(value, text) and not extract.is_generic(value):
                found[entity] = value.lower() if entity == "bucket" else value
        arn = extract.principal_arn(text)
        if arn:
            found.pop("iam_user", None)
            found.pop("iam_role", None)
            found[f"iam_{arn[0]}"] = arn[1]
        action = extract.iam_action(text)
        if action:
            found["iam_action"] = action
        if "timeout_seconds" in s.missing_slots:
            seconds = extract.seconds(text)
            if seconds and 1 <= seconds <= 900:
                found["timeout_seconds"] = str(seconds)
        instance_id = extract.instance_id(text)
        if instance_id:
            found.update(instance_id=instance_id, instance_ref=instance_id)
        protocol = extract.protocol(text)
        if protocol:
            found["protocol"] = protocol
        if "source_cidr" in s.missing_slots:
            cidr = extract.source_cidr(text)
            if cidr:
                found["source_cidr"] = cidr
        return found

    async def _start_problem(self, s: SupportState, intent: Intent) -> None:
        """Begin working on a new problem, dropping the diagnosis of the previous one."""
        if s.pending_action:
            await self._close_action(s, "cancelled")
        service = INTENTS[intent].service
        if service is not s.service:
            s.entities = {}
        s.entities.pop("source_cidr", None)
        s.entities.pop("timeout_seconds", None)
        s.intent, s.service, s.problem_statement = intent, service, s.user_text.strip()
        s.tool_calls, s.evidence, s.missing_slots = [], [], []
        s.root_cause_code = s.proposed_resolution_code = s.outcome = s.escalation_reason = None
        s.clarification_attempts = s.failed_tool_calls = s.turns_without_progress = 0
        s.status = Status.COLLECTING_INFORMATION

    def _set_principal(self, s: SupportState, kind: str | None, name: str) -> None:
        """Record the user or role the customer named; `kind` None means they did not say which it is."""
        for key in ("iam_user", "iam_role", "principal_ref", "principal_checked"):
            s.entities.pop(key, None)
        s.entities[f"iam_{kind}" if kind else "principal_ref"] = name

    async def _resolve_principal(self, s: SupportState) -> bool:
        """Confirm that the named IAM user or role exists, trying both kinds. False when the turn ends here."""
        name = s.entities.get("iam_user") or s.entities.get("iam_role") or s.entities["principal_ref"]
        # Try the kind the customer stated first; a bare name is tried as a user, then as a role.
        role_stated = bool(s.entities.get("iam_role")) and not s.entities.get("iam_user")
        order = ["role", "user"] if role_stated else ["user", "role"]
        for key in ("iam_user", "iam_role", "principal_ref"):
            s.entities.pop(key, None)

        for kind in order:
            s.entities[f"iam_{kind}"] = name
            # Quiet: a miss on the first kind is part of finding out which kind it is, not a finding.
            record = await self._tool(s, f"iam_get_{kind}", {}, "resolve", quiet=True)
            if record.ok:
                s.entities["principal_checked"] = kind
                spec = CATALOG[record.tool]
                self._emit({"type": "activity", "id": record.id, "label": spec.activity, "state": "ok",
                            "detail": spec.summarize(spec.output_model(**record.result))})
                return True
            del s.entities[f"iam_{kind}"]
            if record.error["error"] != "not_found":
                if not await self._tools_failing(s):
                    self._say(s, "I couldn't look up that user or role just now. Please try again in a moment.")
                    s.route = "finish"
                return False

        self._say(s, f"I couldn't find an IAM user or role named {name}. Which user or role is it?")
        s.status = Status.COLLECTING_INFORMATION
        s.missing_slots = [slot for slot in INTENTS[s.intent].slots if not slot_filled(slot, s.entities)]
        s.route = "finish"
        return False

    async def _drop_diagnosis(self, s: SupportState) -> None:
        """Forget the current diagnosis and any fix proposed for it."""
        if s.pending_action:
            await self._close_action(s, "cancelled")
        s.tool_calls, s.evidence = [], []
        s.root_cause_code = s.proposed_resolution_code = s.outcome = None
        s.entities.pop("source_cidr", None)
        s.entities.pop("timeout_seconds", None)

    async def _resolve_instance(self, s: SupportState) -> bool:
        """Turn an instance name into its ID. Returns False when the turn ends here."""
        ref = s.entities["instance_ref"]
        record = await self._tool(s, "ec2_list_instances", {}, "resolve")
        if not record.ok:
            if not await self._tools_failing(s):
                self._say(s, "I couldn't look up your instances just now. Please try again in a moment.")
                s.route = "finish"
            return False

        instances = record.result["instances"]
        matches = [i for i in instances if extract.spoken_key(i["name"] or "") == extract.spoken_key(ref)]
        if len(matches) == 1:
            s.entities.update(instance_id=matches[0]["instance_id"], instance_name=matches[0]["name"])
            return True

        del s.entities["instance_ref"]
        if matches:
            ids = ", ".join(i["instance_id"] for i in matches)
            self._say(s, f"More than one instance is named {ref}: {ids}. Which instance ID is it?")
        elif instances:
            names = ", ".join(sorted(i["name"] or i["instance_id"] for i in instances))
            self._say(s, f"I couldn't find an instance named {ref}. The instances I can see are: {names}. Which one is it?")
        else:
            self._say(s, f"I couldn't find an instance named {ref}, and I don't see any instances in this account.")
        s.status = Status.COLLECTING_INFORMATION
        s.missing_slots = [slot for slot in INTENTS[s.intent].slots if not slot_filled(slot, s.entities)]
        s.route = "finish"
        return False

    async def _complete_checks(self, s: SupportState) -> None:
        """Run the intent's required checks that the model did not ask for."""
        exposed = {t.name for t in read_tools(s.service, s.entities)}
        for tool, args in INTENTS[s.intent].checks:
            if any(c.turn == s.turn and c.stage == "investigate" and c.error and c.error["error"] == "not_found"
                   for c in s.tool_calls):
                break  # the resource does not exist, so the remaining checks could only repeat that
            already_run = any(
                c.tool == tool and all(c.args.get(k) == v for k, v in args.items())
                and (c.ok or c.error["error"] == "not_found")
                for c in s.tool_calls)
            if tool in exposed and not already_run:
                await self._tool(s, tool, args, "investigate")

    async def _choose_cause(self, s: SupportState, candidates: list[RootCause]) -> RootCause:
        started = time.perf_counter()
        completion = await self._llm.complete(
            [{"role": "system", "content": prompts.DIAGNOSE_SYSTEM},
             {"role": "user", "content": prompts.diagnose_user(s, candidates)}],
            model=self._model, json_schema=prompts.diagnose_schema(candidates), max_tokens=40)
        s.latency_ms["diagnose"] = round((time.perf_counter() - started) * 1000)
        try:
            code = RootCause(json.loads(completion.content)["root_cause_code"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            code = None
        if code not in candidates:
            log.warning("unusable root-cause choice %r; taking the first supported cause", completion.content[:200])
            return candidates[0]
        return code

    async def _propose(self, s: SupportState) -> None:
        """Store the fix as a pending action and ask the customer to confirm it."""
        write = RESOLUTIONS[s.proposed_resolution_code].write
        args = write.build_args(s)
        try:
            action = self._gateway.propose(write.tool, args, write.summarize(s, args))
        except PolicyError as e:
            log.error("could not propose %s: %s", write.tool, e)
            await self._escalate(s, "write_failed", f"the fix could not be prepared: {e}")
            return
        s.pending_action = action
        s.missing_slots = []
        s.confirmation_reasks = 0
        s.status = Status.WAITING_FOR_CONFIRMATION
        await self._store.record_audit(s.conversation_id, "action_proposed", action.model_dump(mode="json"))
        self._emit({
            "type": "pending_action", "action_id": action.action_id, "summary": action.summary,
            "expires_at": action.expires_at.isoformat()})
        self._say(s, f"I can do this for you: {action.summary} Shall I go ahead?")
        s.route = "finish"

    async def _confirm(self, s: SupportState, via: str) -> None:
        s.pending_action.confirmed = True
        await self._store.record_audit(s.conversation_id, "action_confirmed", {
            "action_id": s.pending_action.action_id, "via": via})
        s.route = "execute"

    async def _decline(self, s: SupportState, via: str) -> None:
        summary = s.pending_action.summary
        await self._store.record_audit(s.conversation_id, "action_declined", {
            "action_id": s.pending_action.action_id, "via": via})
        await self._close_action(s, "declined", audit=False)
        s.status, s.outcome = Status.RESOLVED, "declined"
        opening = ("I couldn't tell whether that was a yes, so I've left it." if via == "unclear"
                   else "Okay, I haven't changed anything.")
        self._say(s, f"{opening} The fix, whenever you want to apply it: {summary}")
        s.route = "finish"

    async def _close_action(self, s: SupportState, outcome: str, *, audit: bool = True) -> None:
        """Drop the pending action and tell the client it is no longer waiting."""
        action = s.pending_action
        if audit and outcome != "executed":
            await self._store.record_audit(s.conversation_id, f"action_{outcome}", {"action_id": action.action_id})
        self._emit({"type": "action_resolved", "action_id": action.action_id, "outcome": outcome})
        s.pending_action = None

    async def _escalate(self, s: SupportState, trigger: str, reason: str) -> None:
        """Hand the conversation to a human: record what they need to pick it up, then tell the customer."""
        if s.pending_action:
            await self._close_action(s, "cancelled")
        s.status, s.escalation_reason, s.missing_slots = Status.ESCALATED, reason, []
        await self._store.record_audit(s.conversation_id, "escalated", {"trigger": trigger, "reason": reason})
        await self._store.record_escalation(s.conversation_id, {
            "trigger": trigger, "reason": reason, "channel": s.channel,
            "service": s.service.value if s.service else None, "intent": s.intent.value if s.intent else None,
            "handoff": self._handoff(s),
        })
        ESCALATIONS.labels(trigger).inc()
        self._emit({"type": "escalated", "trigger": trigger, "reason": reason})
        self._say(s, ESCALATION_TEXT)
        s.route = "finish"

    @staticmethod
    def _handoff(s: SupportState) -> dict:
        """What a support engineer needs: the problem, the resources, what was checked, and what it showed."""
        hidden = {"instance_ref", "principal_ref", "principal_checked"}
        return {
            "problem_statement": s.problem_statement,
            "turn": s.turn,
            "entities": {k: v for k, v in s.entities.items() if k not in hidden},
            "root_cause": s.root_cause_code.value if s.root_cause_code else None,
            "proposed_resolution": s.proposed_resolution_code.value if s.proposed_resolution_code else None,
            "checks": [
                {"tool": c.tool, "ok": c.ok,
                 "finding": CATALOG[c.tool].summarize(CATALOG[c.tool].output_model(**c.result)) if c.ok
                 else (c.error or {}).get("error")}
                for c in s.tool_calls
            ],
            "failed_tool_calls": s.failed_tool_calls,
            "turns_without_progress": s.turns_without_progress,
            "clarification_attempts": s.clarification_attempts,
            "last_customer_message": s.user_text or next(
                (m.text for m in reversed(s.messages) if m.role == "user"), ""),
        }

    async def _tools_failing(self, s: SupportState) -> bool:
        """Escalate when the cloud API is unreachable or too many checks have failed."""
        unreachable = any(
            c.turn == s.turn and c.error and c.error["error"] == "unavailable" for c in s.tool_calls)
        if unreachable:
            await self._escalate(s, "tool_failures", "the cloud API is unreachable")
        elif s.failed_tool_calls >= MAX_FAILED_TOOL_CALLS:
            await self._escalate(s, "tool_failures", f"{s.failed_tool_calls} tool calls failed")
        else:
            return False
        return True

    async def _tool(self, s: SupportState, name: str, args: dict, stage: Stage, *,
                    quiet: bool = False) -> ToolCallRecord:
        """Call a read tool through the gateway. `quiet` runs it without showing the customer an activity."""
        spec = CATALOG.get(name)
        # A call the gateway will refuse is the model's mistake; the customer is not shown it.
        allowed = spec is not None and spec in read_tools(s.service, s.entities)
        return await self._run_tool(
            s, spec.activity if allowed and not quiet else None, stage,
            lambda ctx, call_id: self._gateway.call_read(ctx, call_id, name, args, stage))

    async def _run_tool(self, s: SupportState, activity: str | None, stage: Stage, run) -> ToolCallRecord:
        """Run a gateway call, record it in the state, and show it to the customer as an activity."""
        call_id = f"t{len(s.tool_calls) + 1}"
        ctx = CallContext(s.conversation_id, s.turn, s.service.value if s.service else None, dict(s.entities))
        if activity:
            self._emit({"type": "activity", "id": call_id, "label": activity, "state": "running"})
        record = await run(ctx, call_id)
        s.tool_calls.append(record)

        if record.ok:
            spec = CATALOG[record.tool]
            outcome, detail = "ok", spec.summarize(spec.output_model(**record.result))
        elif record.error["error"] == "not_found":
            outcome, detail = "warn", "Not found"
        else:
            outcome, detail = "error", "This check could not be completed"
            if record.error["error"] in _INFRA_ERRORS:
                s.failed_tool_calls += 1
        if activity:
            self._emit({"type": "activity", "id": call_id, "label": activity, "state": outcome, "detail": detail})
        return record
