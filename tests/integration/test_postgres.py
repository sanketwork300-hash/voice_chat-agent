"""The Postgres-backed store and checkpointer. Skipped when Postgres is not reachable (`make up`)."""

import asyncio
import uuid
from contextlib import AsyncExitStack

import psycopg
import pytest

from backend.agent.agent import SupportAgent
from backend.agent.state import ConversationStatus as Status
from backend.config import Settings
from backend.main import open_storage
from backend.rag.knowledge import NoKnowledgeBase, PgKnowledgeBase
from backend.tools.gateway import ToolGateway
from tests.fakes import FakeCloud, FakeEmbedder, ScriptedLLM, calls, doc_chunk, label

SETTINGS = Settings(_env_file=None)
DSN = SETTINGS.database_url.replace("+psycopg", "")
WEB = "i-0aaaaaaaaaaaaaaa1"


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="Postgres is not running")


def test_state_and_audit_survive_a_restart_of_the_agent():
    conversation_id = f"test-{uuid.uuid4().hex}"
    cloud = FakeCloud()
    cloud.add_instance(WEB, "web-01")

    async def session(llm: ScriptedLLM, text: str):
        """One backend lifetime: open storage, run a turn, read everything back, close."""
        async with AsyncExitStack() as stack:
            store, checkpointer, _ = await open_storage(SETTINGS, stack)
            agent = SupportAgent(
                llm, "scripted-model", ToolGateway(cloud, store), store, checkpointer, NoKnowledgeBase())
            async for _ in agent.handle_turn(conversation_id, text=text):
                pass
            listed = [r for r in await store.list_conversations(200) if r["conversation_id"] == conversation_id]
            return await agent.get_state(conversation_id), await store.audit_trail(conversation_id), listed

    async def cleanup():
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
            for table, column in [("audit_log", "conversation_id"), ("conversations", "conversation_id"),
                                  ("checkpoints", "thread_id"), ("checkpoint_blobs", "thread_id"),
                                  ("checkpoint_writes", "thread_id")]:
                await conn.execute(f"DELETE FROM {table} WHERE {column} = %s", (conversation_id,))

    try:
        llm = ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")],
                          tool_rounds=[calls("ec2_get_instance", "ec2_get_security_group_rules")])
        state, audit, listed = asyncio.run(session(llm, "I can't ssh into web-01"))
        assert state.root_cause_code == "EC2_SG_SSH_BLOCKED" and state.missing_slots == ["source_cidr"]
        assert [a["event"] for a in audit] == ["tool_call"] * 5
        assert listed[0]["status"] == "COLLECTING_INFORMATION"

        # A new agent on a new connection pool picks the conversation up where it stopped.
        state, audit, listed = asyncio.run(session(ScriptedLLM(), "203.0.113.10"))
        assert state.status is Status.WAITING_FOR_CONFIRMATION and state.turn == 2
        assert state.tool_calls[1].result["name"] == "web-01"
        assert audit[-1]["event"] == "action_proposed" and audit[-1]["data"]["tool"] == "ec2_add_ingress_rule"
        assert listed[0]["status"] == "WAITING_FOR_CONFIRMATION"
    finally:
        asyncio.run(cleanup())


def test_pgvector_knowledge_base_stores_replaces_and_searches():
    table = f"test_chunks_{uuid.uuid4().hex[:12]}"
    chunks = [
        doc_chunk("ec2/ssh#0", "ec2", "Authorize inbound traffic", "Add an inbound rule to allow SSH on port 22."),
        doc_chunk("s3/naming#0", "s3", "Bucket naming rules", "Bucket names must be 3 to 63 characters long."),
    ]

    async def run():
        async with AsyncExitStack() as stack:
            _, _, pool = await open_storage(SETTINGS, stack)
            kb = PgKnowledgeBase(pool, FakeEmbedder(), table)
            try:
                assert await kb.count() == 0                                           # table not created yet
                assert await kb.search("ssh", service=None, k=3, min_score=0.0) == []
                await kb.setup()
                for chunk in chunks:
                    await kb.replace_document(chunk.document_id, [chunk])
                await kb.replace_document("ec2/ssh", [chunks[0]])                      # ingesting twice does not duplicate
                assert await kb.count() == 2

                found = await kb.search("allow SSH on port 22 inbound rule", service=None, k=3, min_score=0.3)
                assert [f.chunk_id for f in found] == ["ec2/ssh#0"] and 0.3 < found[0].score <= 1.0
                assert found[0].source_date.isoformat() == "2023-06-15"
                # The S3 filter finds nothing relevant, so the search widens to every service.
                assert (await kb.search("allow SSH on port 22 inbound rule", service="s3", k=3, min_score=0.3)
                        )[0].chunk_id == "ec2/ssh#0"
                assert await kb.search("weather forecast tomorrow", service=None, k=3, min_score=0.3) == []

                assert await kb.delete_except(["s3/naming"]) == 1
                assert await kb.count() == 1
            finally:
                async with pool.connection() as conn:
                    await conn.execute(f"DROP TABLE IF EXISTS {table}")

    asyncio.run(run())


def test_escalations_are_stored_and_listed_newest_first():
    conversation_id = f"test-{uuid.uuid4().hex}"
    record = {"trigger": "no_fault_found", "reason": "the checks that ran did not find the cause", "channel": "chat",
              "service": "ec2", "intent": "ec2_connectivity",
              "handoff": {"problem_statement": "no ssh", "checks": [{"tool": "ec2_get_instance", "ok": True}]}}

    async def run_test():
        async with AsyncExitStack() as stack:
            store, _, pool = await open_storage(SETTINGS, stack)
            try:
                await store.record_escalation(conversation_id, record)
                await store.record_escalation(conversation_id, {**record, "trigger": "customer_request"})
                rows = await store.escalations(conversation_id)
                assert [r["trigger"] for r in rows] == ["customer_request", "no_fault_found"]
                assert rows[1]["handoff"] == record["handoff"] and rows[1]["service"] == "ec2"
                assert any(r["conversation_id"] == conversation_id for r in await store.escalations(limit=500))
            finally:
                async with pool.connection() as conn:
                    await conn.execute("DELETE FROM escalations WHERE conversation_id = %s", (conversation_id,))

    asyncio.run(run_test())


def test_evaluation_runs_are_stored_and_read_back():
    from datetime import datetime, timezone
    run_id = f"test-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    run = {"run_id": run_id, "started_at": now, "finished_at": now, "subset": "smoke", "model": "scripted-model",
           "scenario_count": 2, "wall_clock_s": 12.5, "metrics": {"scenarios": 2, "write_safety_violations": 0}}
    rows = [
        {"scenario_id": f"T-{i}", "service": "ec2", "variation": "direct", "passed": i == 0,
         "checks": {"diagnosis": i == 0}, "scenario": {"intents": ["ec2_connectivity"]},
         "outcome": {"turns": [], "root_cause": None}}
        for i in range(2)]

    async def run_test():
        async with AsyncExitStack() as stack:
            store, _, pool = await open_storage(SETTINGS, stack)
            try:
                await store.save_evaluation(run, rows)
                listed = next(r for r in await store.list_evaluations(200) if r["run_id"] == run_id)
                assert listed["metrics"] == run["metrics"] and listed["wall_clock_s"] == 12.5
                stored = await store.get_evaluation(run_id)
                assert [r["scenario_id"] for r in stored["results"]] == ["T-0", "T-1"]   # kept in run order
                assert [r["passed"] for r in stored["results"]] == [True, False]
                assert await store.get_evaluation("no-such-run") is None
                # A run is stored whole or not at all: a duplicate id must not leave partial rows behind.
                with pytest.raises(psycopg.errors.UniqueViolation):
                    await store.save_evaluation(run, rows[:1])
                assert len((await store.get_evaluation(run_id))["results"]) == 2
            finally:
                async with pool.connection() as conn:
                    await conn.execute("DELETE FROM evaluation_runs WHERE run_id = %s", (run_id,))
                assert await store.get_evaluation(run_id) is None

    asyncio.run(run_test())
