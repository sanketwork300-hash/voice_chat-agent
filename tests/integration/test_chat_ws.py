"""The WebSocket protocol and HTTP endpoints, with a scripted model and an in-memory cloud."""

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from backend.api import chat
from backend.api.chat import MAX_MESSAGE_CHARS
from backend.config import Settings
from backend.main import create_app
from backend.providers.llm import LLMError, LLMUnavailable
from backend.rag.knowledge import MemoryKnowledgeBase
from tests.fakes import FakeCloud, FakeEmbedder, ScriptedLLM, calls, doc_chunk, label

WEB = "i-0aaaaaaaaaaaaaaa1"


def client(llm: ScriptedLLM, cloud: FakeCloud | None = None, documents: list | None = None, **settings) -> TestClient:
    # Same model for both tiers, so the result does not depend on this machine's free RAM.
    config = Settings(_env_file=None, llm_model="scripted-model", llm_fallback_model="scripted-model",
                      database_url="memory://", rag_min_score=0.3, voice_enabled=False, **settings)
    knowledge = MemoryKnowledgeBase(FakeEmbedder())
    knowledge.add(documents or [])
    return TestClient(create_app(config, llm, cloud or FakeCloud(), knowledge))


def send(ws, payload: dict) -> list[dict]:
    """Send one client event and collect server events until the turn goes idle."""
    ws.send_json(payload)
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event == {"type": "status", "status": "idle"}:
            return events


def say(ws, text: str) -> list[dict]:
    return send(ws, {"type": "message", "text": text})


def text_of(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


def test_reply_is_streamed_and_turn_is_bracketed():
    llm = ScriptedLLM(labels=[label(kind="other")], replies=["Hello there friend"])
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["model"] == "scripted-model"
        events = say(ws, "  hi  ")

    assert events[0] == {"type": "status", "status": "thinking"}
    assert len([e for e in events if e["type"] == "token"]) > 1
    assert text_of(events) == "Hello there friend"
    done = events[-2]
    assert done["type"] == "done" and 0 <= done["latency_ms"]["first_token"] <= done["latency_ms"]["total"]
    assert llm.requests_of("stream")[0]["messages"][-1]["content"].endswith("Customer: hi")


def test_fix_is_confirmed_with_the_button_over_the_socket():
    cloud = FakeCloud()
    group_id = cloud.add_instance(WEB, "web-01")
    llm = ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")],
                      tool_rounds=[calls("ec2_get_instance", "ec2_get_security_group_rules")])
    with client(llm, cloud) as c, c.websocket_connect("/ws/chat") as ws:
        conversation_id = ws.receive_json()["conversation_id"]
        diagnosis = say(ws, "I can't ssh into web-01")
        activities = [e for e in diagnosis if e["type"] == "activity" and e["state"] != "running"]
        assert [(a["label"], a["state"]) for a in activities] == [
            ("Looking up your instances", "ok"), ("Checking the instance", "ok"), ("Checking the firewall rules", "ok"),
            ("Checking instance health checks", "ok"), ("Checking instance metrics", "ok")]
        assert activities[2]["detail"] == "SSH (port 22) is blocked, RDP (port 3389) is blocked"
        # Customers never see tool names or arguments.
        assert "ec2_get" not in str(diagnosis) and WEB not in str(diagnosis)

        proposal = say(ws, "203.0.113.10")
        pending = next(e for e in proposal if e["type"] == "pending_action")
        assert cloud.writes == []

        stale = send(ws, {"type": "confirm_action", "action_id": "0" * 12})
        assert cloud.writes == [] and "no longer waiting" in text_of(stale)

        result = send(ws, {"type": "confirm_action", "action_id": pending["action_id"]})
        assert cloud.writes == [("add_ingress_rule", group_id, "tcp", 22, "203.0.113.10/32")]
        assert text_of(result).startswith("Done.")
        assert {"type": "action_resolved", "action_id": pending["action_id"], "outcome": "executed"} in result
        assert [e["status"] for e in result if e["type"] == "state"] == ["EXECUTING", "VERIFYING", "RESOLVED"]

        debug = c.get(f"/api/debug/conversations/{conversation_id}").json()
        assert debug["state"]["root_cause_code"] == "EC2_SG_SSH_BLOCKED"
        assert [t["tool"] for t in debug["state"]["tool_calls"]][-2:] == ["ec2_add_ingress_rule", "ec2_get_security_group_rules"]
        assert [a["event"] for a in debug["audit"]][-3:] == ["action_executed", "tool_call", "action_verified"]
        listed = c.get("/api/debug/conversations").json()
        assert listed[0]["conversation_id"] == conversation_id and listed[0]["status"] == "RESOLVED"


def test_clear_starts_a_new_conversation():
    llm = ScriptedLLM(labels=[label(intent="ec2_connectivity"), label(kind="other")])
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        first = ws.receive_json()["conversation_id"]
        say(ws, "I can't ssh into my server")
        ws.send_json({"type": "clear"})
        assert ws.receive_json() == {"type": "cleared"}
        second = ws.receive_json()["conversation_id"]
        say(ws, "hello")
        assert first != second
        assert c.get(f"/api/debug/conversations/{second}").json()["state"]["intent"] is None
        assert c.get(f"/api/debug/conversations/{first}").json()["state"]["intent"] == "ec2_connectivity"


def test_talk_to_a_human_button_escalates_over_the_socket():
    llm = ScriptedLLM(labels=[label(intent="ec2_connectivity")])
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        conversation_id = ws.receive_json()["conversation_id"]
        say(ws, "I can't ssh into my server")
        ws.send_json({"type": "escalate"})
        events = []
        while not events or events[-1]["type"] not in ("done", "error"):
            events.append(ws.receive_json())
        assert {"type": "escalated", "trigger": "customer_request",
                "reason": "the customer pressed the escalation button"} in events
        detail = c.get(f"/api/debug/conversations/{conversation_id}").json()
        assert detail["state"]["status"] == "ESCALATED"
        assert detail["escalations"][0]["trigger"] == "customer_request"
        assert detail["escalations"][0]["handoff"]["problem_statement"] == "I can't ssh into my server"
        assert c.get("/api/debug/escalations").json()[0]["conversation_id"] == conversation_id


def test_model_failures_are_reported_without_internal_detail():
    llm = ScriptedLLM(labels=[label(kind="other")] * 3,
                      replies=[LLMUnavailable("down"), LLMError("boom at 10.0.0.5"), "ok"])
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        unreachable, failed, recovered = say(ws, "a"), say(ws, "b"), say(ws, "c")

    assert "not reachable" in unreachable[1]["message"]
    assert failed[1]["type"] == "error" and "boom" not in failed[1]["message"]
    assert text_of(recovered) == "ok"


def test_invalid_input_is_rejected_without_calling_the_model():
    llm = ScriptedLLM()
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        for bad in ('{"type": "message", "text": "   "}',
                    '{"type": "message", "text": "%s"}' % ("x" * (MAX_MESSAGE_CHARS + 1)),
                    '{"type": "confirm_action", "action_id": "../../etc/passwd"}',
                    '{"type": "shell", "cmd": "ls"}',
                    "not json"):
            ws.send_text(bad)
            assert ws.receive_json()["type"] == "error"
    assert llm.requests == []


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(chat, "RATE_LIMIT_TURNS", 2)
    llm = ScriptedLLM(labels=[label(kind="other")] * 2)
    with client(llm) as c, c.websocket_connect("/ws/chat") as ws:
        ws.receive_json()
        say(ws, "one")
        say(ws, "two")
        ws.send_json({"type": "message", "text": "three"})
        assert "too quickly" in ws.receive_json()["message"]
    assert len(llm.requests_of("classify")) == 2


def test_debug_endpoints_are_absent_when_debug_is_off():
    with client(ScriptedLLM(), debug_ui=False) as c:
        assert c.get("/api/debug/conversations").status_code == 404
    with client(ScriptedLLM()) as c:
        assert c.get("/api/debug/conversations/nope").status_code == 404


def test_metrics_health_and_system_endpoints():
    # Metrics are process-wide, so compare against the values before this test.
    def sample(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    turns = sample("chat_turns_total", status="ok")
    first_tokens = sample("llm_first_token_seconds_count", model="scripted-model")
    health_requests = sample("http_request_duration_seconds_count", method="GET", path="/health", status="200")

    llm = ScriptedLLM(labels=[label(kind="other")])
    with client(llm) as c:
        with c.websocket_connect("/ws/chat") as ws:
            ws.receive_json()
            say(ws, "hello")

        health = c.get("/health").json()
        assert health == {"status": "ok", "llm": {"provider": "scripted", "model": "scripted-model", "status": "ok"},
                          "knowledge_base": {"chunks": 0}, "voice": {"enabled": False}}
        assert c.get("/api/system").json()["llm_model"] == "scripted-model"

        metrics = c.get("/metrics")
        assert metrics.status_code == 200
        assert 'chat_turns_total{status="ok"}' in metrics.text

    assert sample("chat_turns_total", status="ok") == turns + 1
    assert sample("llm_first_token_seconds_count", model="scripted-model") == first_tokens + 1
    assert sample("http_request_duration_seconds_count", method="GET", path="/health", status="200") == health_requests + 1


def test_sources_are_sent_to_the_client():
    documents = [doc_chunk("s3/naming#0", "s3", "Bucket naming rules",
                           "Bucket names must be between 3 and 63 characters long.")]
    llm = ScriptedLLM(labels=[label(kind="question")], replies=["Between 3 and 63 characters [1]."])
    with client(llm, documents=documents) as c, c.websocket_connect("/ws/chat") as ws:
        conversation_id = ws.receive_json()["conversation_id"]
        events = say(ws, "how long can bucket names be, in characters?")
        event = next(e for e in events if e["type"] == "sources")
        assert event["kind"] == "cited" and event["documents"][0]["title"] == "Bucket naming rules"
        debug = c.get(f"/api/debug/conversations/{conversation_id}").json()
        assert debug["state"]["retrieved_documents"][0]["chunk_id"] == "s3/naming#0"
        assert c.get("/health").json()["knowledge_base"] == {"chunks": 1}


def test_evaluation_endpoints_serve_stored_runs():
    with client(ScriptedLLM()) as c:
        assert c.get("/api/evaluation/runs").json() == []
        assert c.get("/api/evaluation/runs/nope").status_code == 404
        store = c.app.state.store
        run = {"run_id": "r1", "started_at": "2026-10-01T10:00:00+00:00", "subset": "smoke", "metrics": {"scenarios": 1}}
        c.portal.call(store.save_evaluation, run, [{"scenario_id": "T-1", "passed": True}])
        assert c.get("/api/evaluation/runs").json()[0]["run_id"] == "r1"
        assert c.get("/api/evaluation/runs/r1").json()["results"] == [{"scenario_id": "T-1", "passed": True}]
    with client(ScriptedLLM(), debug_ui=False) as c:
        assert c.get("/api/evaluation/runs").status_code == 404
