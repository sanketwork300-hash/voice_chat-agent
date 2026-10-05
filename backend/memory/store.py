"""Conversation index and audit log.

Conversation state itself lives in the LangGraph checkpointer. This store keeps
what the checkpointer does not: a list of conversations, and an append-only
record of every tool call and every step of a write action.
"""

import json
from datetime import datetime, timezone
from typing import Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

# One statement per entry: the connection pool prepares statements, and a
# prepared statement cannot hold several commands.
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        channel         TEXT NOT NULL,
        status          TEXT NOT NULL,
        intent          TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id              BIGSERIAL PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        event           TEXT NOT NULL,
        data            JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS audit_log_conversation ON audit_log (conversation_id, id)",
    """
    CREATE TABLE IF NOT EXISTS escalations (
        id              BIGSERIAL PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        trigger         TEXT NOT NULL,
        reason          TEXT NOT NULL,
        channel         TEXT NOT NULL,
        service         TEXT,
        intent          TEXT,
        handoff         JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluation_runs (
        run_id         TEXT PRIMARY KEY,
        started_at     TIMESTAMPTZ NOT NULL,
        finished_at    TIMESTAMPTZ NOT NULL,
        subset         TEXT NOT NULL,
        model          TEXT NOT NULL,
        scenario_count INT NOT NULL,
        wall_clock_s   REAL NOT NULL,
        metrics        JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluation_results (
        run_id      TEXT NOT NULL REFERENCES evaluation_runs ON DELETE CASCADE,
        position    INT NOT NULL,
        scenario_id TEXT NOT NULL,
        service     TEXT NOT NULL,
        variation   TEXT NOT NULL,
        passed      BOOLEAN NOT NULL,
        checks      JSONB NOT NULL,
        scenario    JSONB NOT NULL,
        outcome     JSONB NOT NULL,
        PRIMARY KEY (run_id, scenario_id)
    )
    """,
]


class Store(Protocol):
    async def setup(self) -> None: ...
    async def record_audit(self, conversation_id: str, event: str, data: dict) -> None: ...
    async def audit_trail(self, conversation_id: str) -> list[dict]: ...
    async def upsert_conversation(self, conversation_id: str, channel: str, status: str, intent: str | None) -> None: ...
    async def list_conversations(self, limit: int = 50) -> list[dict]: ...
    async def record_escalation(self, conversation_id: str, record: dict) -> None: ...
    async def escalations(self, conversation_id: str | None = None, limit: int = 50) -> list[dict]: ...
    async def save_evaluation(self, run: dict, results: list[dict]) -> None: ...
    async def list_evaluations(self, limit: int = 50) -> list[dict]: ...
    async def get_evaluation(self, run_id: str) -> dict | None: ...


class MemoryStore:
    """In-process store for tests and for running without Postgres."""

    def __init__(self) -> None:
        self.audit: list[dict] = []
        self.conversations: dict[str, dict] = {}
        self.escalation_rows: list[dict] = []
        self.evaluations: dict[str, dict] = {}

    async def setup(self) -> None:
        pass

    async def record_audit(self, conversation_id: str, event: str, data: dict) -> None:
        self.audit.append({
            "conversation_id": conversation_id, "at": datetime.now(timezone.utc).isoformat(),
            "event": event, "data": json.loads(json.dumps(data, default=str)),
        })

    async def audit_trail(self, conversation_id: str) -> list[dict]:
        return [a for a in self.audit if a["conversation_id"] == conversation_id]

    async def upsert_conversation(self, conversation_id: str, channel: str, status: str, intent: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = self.conversations.setdefault(conversation_id, {"conversation_id": conversation_id, "created_at": now})
        row.update(channel=channel, status=status, intent=intent, updated_at=now)

    async def list_conversations(self, limit: int = 50) -> list[dict]:
        rows = sorted(self.conversations.values(), key=lambda r: r["updated_at"], reverse=True)
        return rows[:limit]

    async def record_escalation(self, conversation_id: str, record: dict) -> None:
        self.escalation_rows.append(json.loads(json.dumps({
            "id": len(self.escalation_rows) + 1, "conversation_id": conversation_id,
            "at": datetime.now(timezone.utc).isoformat(), **record}, default=str)))

    async def escalations(self, conversation_id: str | None = None, limit: int = 50) -> list[dict]:
        rows = [r for r in self.escalation_rows if conversation_id is None or r["conversation_id"] == conversation_id]
        return rows[::-1][:limit]

    async def save_evaluation(self, run: dict, results: list[dict]) -> None:
        self.evaluations[run["run_id"]] = json.loads(json.dumps({"run": run, "results": results}, default=str))

    async def list_evaluations(self, limit: int = 50) -> list[dict]:
        runs = [e["run"] for e in self.evaluations.values()]
        return sorted(runs, key=lambda r: r["started_at"], reverse=True)[:limit]

    async def get_evaluation(self, run_id: str) -> dict | None:
        return self.evaluations.get(run_id)


_RUN_COLUMNS = "run_id, started_at, finished_at, subset, model, scenario_count, wall_clock_s, metrics"


def _run_row(row: dict) -> dict:
    return {**row, "started_at": row["started_at"].isoformat(), "finished_at": row["finished_at"].isoformat()}


class PostgresStore:
    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def record_audit(self, conversation_id: str, event: str, data: dict) -> None:
        payload = Jsonb(json.loads(json.dumps(data, default=str)))
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit_log (conversation_id, event, data) VALUES (%s, %s, %s)",
                (conversation_id, event, payload))

    async def audit_trail(self, conversation_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT at, event, data FROM audit_log WHERE conversation_id = %s ORDER BY id", (conversation_id,))
            return [
                {"conversation_id": conversation_id, "at": r["at"].isoformat(), "event": r["event"], "data": r["data"]}
                for r in await cursor.fetchall()
            ]

    async def upsert_conversation(self, conversation_id: str, channel: str, status: str, intent: str | None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO conversations (conversation_id, channel, status, intent) VALUES (%s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                SET status = EXCLUDED.status, intent = EXCLUDED.intent, updated_at = now()
                """,
                (conversation_id, channel, status, intent))

    async def list_conversations(self, limit: int = 50) -> list[dict]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT conversation_id, channel, status, intent, created_at, updated_at "
                "FROM conversations ORDER BY updated_at DESC LIMIT %s", (limit,))
            return [
                {**r, "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}
                for r in await cursor.fetchall()
            ]

    async def record_escalation(self, conversation_id: str, record: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO escalations (conversation_id, trigger, reason, channel, service, intent, handoff) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (conversation_id, record["trigger"], record["reason"], record["channel"], record["service"],
                 record["intent"], Jsonb(json.loads(json.dumps(record["handoff"], default=str)))))

    async def escalations(self, conversation_id: str | None = None, limit: int = 50) -> list[dict]:
        where = "WHERE conversation_id = %s" if conversation_id else ""
        params = (conversation_id, limit) if conversation_id else (limit,)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT id, conversation_id, at, trigger, reason, channel, service, intent, handoff "
                f"FROM escalations {where} ORDER BY id DESC LIMIT %s", params)
            return [{**r, "at": r["at"].isoformat()} for r in await cursor.fetchall()]

    async def save_evaluation(self, run: dict, results: list[dict]) -> None:
        """Store a run and its per-scenario results together, or not at all."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                f"INSERT INTO evaluation_runs ({_RUN_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (run["run_id"], run["started_at"], run["finished_at"], run["subset"], run["model"],
                 run["scenario_count"], run["wall_clock_s"], Jsonb(run["metrics"])))
            for position, result in enumerate(results):
                await conn.execute(
                    "INSERT INTO evaluation_results (run_id, position, scenario_id, service, variation, passed, "
                    "checks, scenario, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (run["run_id"], position, result["scenario_id"], result["service"], result["variation"],
                     result["passed"], Jsonb(result["checks"]), Jsonb(result["scenario"]), Jsonb(result["outcome"])))

    async def list_evaluations(self, limit: int = 50) -> list[dict]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM evaluation_runs ORDER BY started_at DESC LIMIT %s", (limit,))
            return [_run_row(row) for row in await cursor.fetchall()]

    async def get_evaluation(self, run_id: str) -> dict | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(f"SELECT {_RUN_COLUMNS} FROM evaluation_runs WHERE run_id = %s", (run_id,))
            run = await cursor.fetchone()
            if run is None:
                return None
            cursor = await conn.execute(
                "SELECT scenario_id, service, variation, passed, checks, scenario, outcome FROM evaluation_results "
                "WHERE run_id = %s ORDER BY position", (run_id,))
            return {"run": _run_row(run), "results": await cursor.fetchall()}
