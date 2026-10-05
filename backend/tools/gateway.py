"""The only path from the agent to the cloud.

Every call is checked against the catalog, the tools exposed for the current
state, and the tool's input schema. Read tools run directly. A write tool runs
only as a stored pending action that the customer has confirmed, with exactly
the stored arguments.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from backend.agent.state import PendingAction, Stage, ToolCallRecord
from backend.cloud.base import CloudError, CloudProvider
from backend.memory.store import Store
from backend.metrics import TOOL_CALLS, TOOL_SECONDS
from backend.security.redaction import redact_data
from backend.tools.catalog import CATALOG, ToolSpec, read_tools

ACTION_TTL = timedelta(minutes=10)


class PolicyError(Exception):
    """An attempt to do something the gateway does not allow."""


@dataclass(frozen=True)
class CallContext:
    conversation_id: str
    turn: int
    service: str | None
    entities: dict[str, str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validation_detail(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())


class ToolGateway:
    def __init__(self, cloud: CloudProvider, store: Store, *, timeout_s: float = 10.0):
        self._cloud = cloud
        self._store = store
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ reads

    async def call_read(self, ctx: CallContext, call_id: str, name: str, model_args: dict,
                        stage: Stage) -> ToolCallRecord:
        """Run a read tool the model (or the agent itself) asked for.

        Never raises for a bad request: the refusal is returned as a failed
        record so the model can see it and the debug view can show it.
        """
        spec = CATALOG.get(name)
        if spec is None:
            return await self._refused(ctx, call_id, name, model_args, stage, "unknown_tool",
                                       "no tool with this name exists")
        if not spec.read_only:
            return await self._refused(ctx, call_id, name, model_args, stage, "write_not_allowed",
                                       "changes can only be made as a confirmed action")
        if spec not in read_tools(ctx.service, ctx.entities):
            return await self._refused(ctx, call_id, name, model_args, stage, "not_exposed",
                                       "this tool is not available in the current state")

        # Resource identifiers come from the conversation, never from the model.
        args = {k: v for k, v in (model_args or {}).items() if k not in spec.bound}
        args.update({f: ctx.entities[e] for f, e in spec.bound.items() if ctx.entities.get(e)})
        return await self._run(ctx, call_id, spec, args, stage)

    # ----------------------------------------------------------------- writes

    def propose(self, tool: str, args: dict, summary: str) -> PendingAction:
        """Validate a change and store it for confirmation. Nothing is executed."""
        spec = CATALOG.get(tool)
        if spec is None or spec.read_only:
            raise PolicyError(f"{tool} is not a write tool")
        try:
            validated = spec.input_model.model_validate(args)
        except ValidationError as e:
            raise PolicyError(f"invalid arguments for {tool}: {_validation_detail(e)}") from e
        now = _now()
        return PendingAction(
            action_id=uuid.uuid4().hex[:12], tool=tool, args=validated.model_dump(mode="json"),
            summary=summary, created_at=now, expires_at=now + ACTION_TTL,
        )

    async def execute_confirmed(self, ctx: CallContext, call_id: str, action: PendingAction) -> ToolCallRecord:
        """Run a pending action with exactly its stored arguments."""
        if not action.confirmed:
            raise PolicyError("the action has not been confirmed by the customer")
        if _now() >= action.expires_at:
            raise PolicyError("the action has expired")
        spec = CATALOG.get(action.tool)
        if spec is None or spec.read_only:
            raise PolicyError(f"{action.tool} is not a write tool")
        return await self._run(ctx, call_id, spec, action.args, "execute")

    # --------------------------------------------------------------- internal

    async def _run(self, ctx: CallContext, call_id: str, spec: ToolSpec, args: dict, stage: Stage) -> ToolCallRecord:
        started = time.perf_counter()
        result: dict | None = None
        error: dict | None = None
        try:
            validated = spec.input_model.model_validate(args)
            args = validated.model_dump(mode="json")
            output = await asyncio.wait_for(
                asyncio.to_thread(spec.run, self._cloud, validated), timeout=self._timeout_s)
            result = redact_data(output.model_dump(mode="json"))
        except ValidationError as e:
            error = {"error": "invalid_arguments", "detail": _validation_detail(e)}
        except CloudError as e:
            error = {"error": e.kind, "detail": e.detail}
        except TimeoutError:
            error = {"error": "timeout", "detail": f"no answer within {self._timeout_s:.0f} seconds"}

        elapsed = time.perf_counter() - started
        TOOL_SECONDS.labels(spec.name).observe(elapsed)
        TOOL_CALLS.labels(spec.name, "ok" if error is None else error["error"]).inc()
        record = ToolCallRecord(
            id=call_id, turn=ctx.turn, stage=stage, tool=spec.name, args=args, read_only=spec.read_only,
            ok=error is None, result=result, error=error, latency_ms=round(elapsed * 1000),
        )
        await self._store.record_audit(ctx.conversation_id, "tool_call", record.model_dump(mode="json"))
        return record

    async def _refused(self, ctx: CallContext, call_id: str, name: str, args: dict, stage: Stage,
                       kind: str, detail: str) -> ToolCallRecord:
        record = ToolCallRecord(
            id=call_id, turn=ctx.turn, stage=stage, tool=name, args=args or {}, read_only=True,
            ok=False, error={"error": kind, "detail": detail}, latency_ms=0,
        )
        await self._store.record_audit(ctx.conversation_id, "tool_call_refused", record.model_dump(mode="json"))
        return record
