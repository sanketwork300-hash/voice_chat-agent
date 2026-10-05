"""The agent's behaviour, with a scripted model and an in-memory cloud."""

import asyncio
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agent import agent as agent_module
from backend.agent.agent import (
    ESCALATION_TEXT, NO_DOCUMENTATION_TEXT, NOT_INSPECTABLE_TEXT, SupportAgent, checkpoint_serde,
)
from backend.agent.state import ConversationStatus as Status
from backend.agent.taxonomy import RootCause
from backend.cloud.base import CloudError
from backend.memory.store import MemoryStore
from backend.tools.gateway import ToolGateway
from backend.rag.knowledge import MemoryKnowledgeBase
from tests.fakes import FakeCloud, FakeEmbedder, ScriptedLLM, calls, doc_chunk, label

WEB = "i-0aaaaaaaaaaaaaaa1"
CONNECTIVITY = label(intent="ec2_connectivity")
CHECK_INSTANCE_AND_RULES = calls("ec2_get_instance", "ec2_get_security_group_rules")


class Harness:
    def __init__(self, llm: ScriptedLLM, cloud: FakeCloud | None = None, documents: list | None = None,
                 channel: str = "chat"):
        self.cloud = cloud or web_server_cloud()
        self.llm = llm
        self.channel = channel
        self.store = MemoryStore()
        self.knowledge = MemoryKnowledgeBase(FakeEmbedder())
        self.knowledge.add(documents or [])
        self.agent = SupportAgent(
            llm, "scripted-model", ToolGateway(self.cloud, self.store, timeout_s=0.5), self.store,
            InMemorySaver(serde=checkpoint_serde()), self.knowledge, rag_min_score=0.3)
        self.events: list[dict] = []
        self.pending_id: str | None = None

    def say(self, text: str = "", *, event: str = "message", action_id: str | None = None) -> str:
        """Run one turn and return the reply text."""
        async def run():
            self.events = [e async for e in self.agent.handle_turn(
                "conv", channel=self.channel, event=event, text=text, action_id=action_id)]
        asyncio.run(run())
        for e in self.events:
            if e["type"] == "pending_action":
                self.pending_id = e["action_id"]
        return "".join(e["text"] for e in self.events if e["type"] == "token")

    @property
    def state(self):
        return asyncio.run(self.agent.get_state("conv"))

    def audit(self) -> list[str]:
        return [a["event"] for a in self.store.audit]

    def tools_called(self) -> list[str]:
        return [c.tool for c in self.state.tool_calls]


DOCS = [
    doc_chunk("ec2/authorize#0", "ec2", "Authorize inbound traffic > Add a rule for SSH",
              "To allow SSH access, add an inbound rule to the security group of the instance for port 22."),
    doc_chunk("ec2/stop#0", "ec2", "Stop and start your instance",
              "When you stop an instance, data on instance store volumes is lost. EBS volumes persist."),
    doc_chunk("s3/naming#0", "s3", "Bucket naming rules",
              "Bucket names must be between 3 and 63 characters long and can contain lowercase letters."),
    doc_chunk("lambda/timeout#0", "lambda", "Lambda timeout > Configuring function timeout",
              "If your Lambda function keeps timing out, increase the timeout setting of the function."),
    doc_chunk("billing/charges#0", "billing", "Avoiding unexpected charges",
              "To find unexpected charges on your bill this month, check Cost Explorer for services still running."),
]


def web_server_cloud() -> FakeCloud:
    """One running instance whose security group allows only HTTP."""
    cloud = FakeCloud()
    cloud.add_instance(WEB, "web-01", open_ports=(80,))
    return cloud


def ssh_harness(channel: str = "chat", **llm_kwargs) -> Harness:
    """A conversation taken up to the point where the fix is waiting for confirmation."""
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY], tool_rounds=[CHECK_INSTANCE_AND_RULES], **llm_kwargs),
                channel=channel)
    h.say("I can't SSH into my EC2 server.")
    h.say("web-01")
    h.say("203.0.113.10")
    assert h.state.status is Status.WAITING_FOR_CONFIRMATION
    return h


# ------------------------------------------------------------ the SSH scenario

def test_ssh_scenario_collects_diagnoses_proposes_executes_and_verifies():
    h = Harness(ScriptedLLM(
        labels=[CONNECTIVITY], tool_rounds=[CHECK_INSTANCE_AND_RULES],
        replies=["Your instance is running but SSH is blocked."]))

    reply = h.say("I can't SSH into my EC2 server.")
    assert reply == "What's the instance ID or name?"
    assert h.state.status is Status.COLLECTING_INFORMATION
    assert h.state.entities == {"protocol": "ssh"}

    reply = h.say("web-01")
    state = h.state
    assert state.entities["instance_id"] == WEB
    # The model asked for two checks; the agent ran the rest of the intent's required checks.
    assert [(c.tool, c.requested_by) for c in state.tool_calls] == [
        ("ec2_list_instances", "agent"), ("ec2_get_instance", "model"), ("ec2_get_security_group_rules", "model"),
        ("ec2_get_instance_status", "agent"), ("ec2_get_instance_metrics", "agent")]
    assert state.root_cause_code is RootCause.EC2_SG_SSH_BLOCKED
    assert state.evidence == ["t3", "t4"]
    assert reply.startswith("Your instance is running but SSH is blocked.")
    assert reply.endswith("opening the port to everyone.")
    assert state.missing_slots == ["source_cidr"]
    assert h.cloud.writes == []

    reply = h.say("203.0.113.10")
    state = h.state
    group_id = h.cloud.instances[WEB].security_group_ids[0]
    assert state.status is Status.WAITING_FOR_CONFIRMATION
    assert state.pending_action.tool == "ec2_add_ingress_rule"
    assert state.pending_action.args == {"group_id": group_id, "port": 22, "cidr": "203.0.113.10/32", "protocol": "tcp"}
    assert "Shall I go ahead?" in reply
    assert h.cloud.writes == []

    reply = h.say("yes")
    state = h.state
    assert h.cloud.writes == [("add_ingress_rule", group_id, "tcp", 22, "203.0.113.10/32")]
    assert reply == "Done. SSH on port 22 is now allowed from 203.0.113.10/32, and I've confirmed the rule is in place."
    assert (state.status, state.outcome, state.pending_action) == (Status.RESOLVED, "fixed", None)
    assert [c.stage for c in state.tool_calls[-2:]] == ["execute", "verify"]
    assert h.audit() == [
        *["tool_call"] * 5, "action_proposed", "action_confirmed", "tool_call", "action_executed", "tool_call",
        "action_verified"]

    # Only the classifier and the two wording/tool-choice calls needed the model.
    assert [r["kind"] for r in h.llm.requests] == ["classify", "investigate", "stream"]


# ------------------------------------------------------------ voice behaviour

def test_voice_turns_announce_what_is_being_checked_and_applied():
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY], tool_rounds=[CHECK_INSTANCE_AND_RULES],
                            replies=["SSH is blocked."]), channel="voice")
    h.say("I can't SSH into my EC2 server.")
    h.say("web-01")
    assert [e["text"] for e in h.events if e["type"] == "interim"] == ["Let me check web-01."]
    h.say("203.0.113.10")
    h.say("yes")
    assert [e["text"] for e in h.events if e["type"] == "interim"] == ["Applying that now."]
    assert h.cloud.writes != []


def test_chat_turns_get_no_interim_events():
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY], tool_rounds=[CHECK_INSTANCE_AND_RULES], replies=["SSH is blocked."]))
    h.say("I can't SSH into my EC2 server.")
    h.say("web-01")
    assert [e for e in h.events if e["type"] == "interim"] == []


def test_voice_unclear_reply_to_a_proposal_is_reasked_once_then_withdrawn():
    h = ssh_harness(channel="voice")
    h.llm.labels.extend([label(kind="other"), label(kind="other")])
    reply = h.say("um, hold on a second")
    assert reply.startswith("Sorry, I didn't catch that.") and reply.endswith("Please say yes or no.")
    assert h.state.pending_action is not None and h.cloud.writes == []

    reply = h.say("hmm")
    assert reply.startswith("I couldn't tell whether that was a yes, so I've left it.")
    assert h.state.pending_action is None and h.cloud.writes == []
    assert (h.state.status, h.state.outcome) == (Status.RESOLVED, "declined")
    assert h.store.audit[-1]["data"]["via"] == "unclear"


def test_voice_question_about_the_proposal_is_answered_and_the_proposal_kept():
    h = ssh_harness(channel="voice", replies=["x", "No, only your address."])
    h.llm.labels.append(label(kind="question"))
    reply = h.say("wait, will that open it to everyone?")
    assert reply.startswith("No, only your address.") and "Would you like me to go ahead?" in reply
    assert h.state.pending_action is not None
    reply = h.say("yes")
    assert reply.startswith("Done.")


def test_voice_clear_yes_after_one_reask_still_applies_the_fix():
    h = ssh_harness(channel="voice")
    h.llm.labels.append(label(kind="other"))
    h.say("um")
    assert h.say("yes please").startswith("Done.")


def test_declining_the_fix_changes_nothing():
    h = ssh_harness()
    reply = h.say("no")
    assert h.cloud.writes == []
    assert reply.startswith("Okay, I haven't changed anything.")
    assert (h.state.status, h.state.outcome, h.state.pending_action) == (Status.RESOLVED, "declined", None)
    assert h.audit()[-2:] == ["action_proposed", "action_declined"]


def test_confirm_and_cancel_buttons():
    h = ssh_harness()
    h.say(event="confirm", action_id="000000000000")
    assert h.cloud.writes == [] and h.state.pending_action is not None

    h.say(event="confirm", action_id=h.pending_id)
    assert len(h.cloud.writes) == 1 and h.state.status is Status.RESOLVED

    h = ssh_harness()
    h.say(event="cancel", action_id=h.pending_id)
    assert h.cloud.writes == [] and h.state.outcome == "declined"


# ------------------------------------------------------- confirmation is strict

def test_yes_without_a_pending_action_executes_nothing():
    h = Harness(ScriptedLLM(labels=[label(kind="other")], replies=["There is no pending change."]))
    assert h.say("yes, do it") == "There is no pending change."
    assert h.cloud.writes == [] and h.state.tool_calls == []


def test_qualified_yes_does_not_confirm():
    h = ssh_harness()
    h.llm.labels.append(label(kind="question"))
    reply = h.say("yes but will that open it to everyone?")
    assert h.cloud.writes == []
    assert h.state.status is Status.WAITING_FOR_CONFIRMATION
    assert "Would you like me to go ahead?" in reply


def test_expired_action_cannot_be_confirmed(monkeypatch):
    h = ssh_harness()
    real_now = agent_module._now
    monkeypatch.setattr(agent_module, "_now", lambda: real_now() + timedelta(hours=1))
    h.llm.labels.append(label(kind="other"))
    h.say("yes")
    assert h.cloud.writes == [] and h.state.pending_action is None
    assert "action_expired" in h.audit()
    assert {"type": "action_resolved", "action_id": h.pending_id, "outcome": "expired"} in h.events


def test_new_problem_cancels_the_pending_action():
    h = ssh_harness()
    h.llm.labels.append(label(intent="s3_bucket_not_found"))
    h.say("actually forget that, my S3 bucket is missing")
    assert h.cloud.writes == [] and h.state.pending_action is None
    assert h.state.intent == "s3_bucket_not_found"
    assert "action_cancelled" in h.audit()


# ----------------------------------------- the model cannot step outside policy

def test_model_cannot_write_use_unknown_tools_or_other_services():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)], tool_rounds=[
        calls(("ec2_stop_instance", {"instance_id": WEB}), "delete_everything", "s3_list_buckets"),
        CHECK_INSTANCE_AND_RULES]))
    h.say(f"I can't ssh into {WEB}")
    refused = [(c.tool, c.error["error"]) for c in h.state.tool_calls if not c.ok]
    assert refused == [
        ("ec2_stop_instance", "write_not_allowed"), ("delete_everything", "unknown_tool"),
        ("s3_list_buckets", "not_exposed")]
    assert h.cloud.writes == []
    assert h.cloud.instances[WEB].state == "running"
    assert h.state.root_cause_code is RootCause.EC2_SG_SSH_BLOCKED
    # Refused calls are the model's mistakes and are not shown to the customer.
    shown = [e for e in h.events if e["type"] == "activity" and e["state"] != "running"]
    assert len(shown) == len([c for c in h.state.tool_calls if c.ok])


def test_identifiers_come_from_the_conversation_not_the_model():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)], tool_rounds=[
        calls(("ec2_get_instance", {"instance_id": "i-0bbbbbbbbbbbbbbb2"}), "ec2_get_security_group_rules")]))
    h.say(f"I can't ssh into {WEB}")
    assert h.state.tool_calls[0].args == {"instance_id": WEB}
    assert h.state.tool_calls[0].ok
    # The model is never shown an identifier parameter.
    tools = h.llm.requests_of("investigate")[0]["tools"]
    assert all("instance_id" not in t["function"]["parameters"]["properties"] for t in tools)


def test_details_the_customer_did_not_say_are_discarded():
    h = Harness(ScriptedLLM(labels=[label(intent="s3_access_denied", bucket="prod-secrets", iam_user="admin")]))
    reply = h.say("I get AccessDenied from S3")
    assert h.state.entities == {}
    assert reply == "What's the name of the bucket?"


# ----------------------------------------------------------- other diagnoses

def test_stopped_instance_is_started_after_confirmation():
    cloud = FakeCloud()
    cloud.add_instance(WEB, "batch-01", state="stopped", open_ports=(22,))
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_instance_down", instance="batch-01")],
                            tool_rounds=[calls("ec2_get_instance")]), cloud)
    reply = h.say("my server batch-01 is down")
    assert h.state.root_cause_code is RootCause.EC2_INSTANCE_STOPPED
    assert f"I can do this for you: Start instance batch-01 ({WEB}). Shall I go ahead?" in reply
    assert cloud.instances[WEB].state == "stopped"

    assert h.say("go ahead") == "Done. Instance batch-01 has been started and is now running."
    assert cloud.instances[WEB].state == "running"


def test_model_breaks_the_tie_when_two_causes_are_supported():
    cloud = FakeCloud()
    cloud.add_instance(WEB, "api-01")
    cloud.set_metric(WEB, "CPUUtilization", 97.0)
    cloud.set_metric(WEB, "StatusCheckFailed", 1.0)
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_high_cpu", instance="api-01")],
                            tool_rounds=[calls("ec2_get_instance", "ec2_get_instance_metrics")],
                            cause="EC2_HIGH_CPU"), cloud)
    h.say("api-01 is really slow")
    choice = h.llm.requests_of("diagnose")[0]
    assert choice["json_schema"]["properties"]["root_cause_code"]["enum"] == ["EC2_HIGH_CPU", "EC2_STATUS_CHECK_FAILED"]
    assert h.state.root_cause_code is RootCause.EC2_HIGH_CPU
    assert (h.state.status, h.state.outcome) == (Status.RESOLVED, "advice_given")
    assert cloud.writes == []


def test_bucket_policy_deny_is_diagnosed_with_advice_only():
    cloud = FakeCloud()
    cloud.add_bucket("reports", keys=("q3.csv",), policy={"Statement": [{
        "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::reports/*"}]})
    cloud.users["analyst"] = "allowed"
    h = Harness(ScriptedLLM(
        labels=[label(intent="s3_download_failure", bucket="reports", object_key="q3.csv", iam_user="analyst")],
        tool_rounds=[calls("s3_get_bucket", ("s3_check_bucket_access", {"action": "read"}))]), cloud)
    h.say("analyst gets AccessDenied downloading q3.csv from reports")
    state = h.state
    assert state.root_cause_code is RootCause.S3_BUCKET_POLICY_DENY
    assert state.tool_calls[1].args == {"bucket": "reports", "iam_user": "analyst", "action": "read", "key": "q3.csv"}
    assert (state.status, state.outcome, state.pending_action) == (Status.RESOLVED, "advice_given", None)


def test_unknown_instance_name_lists_what_exists():
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY]))
    h.say("I can't SSH into my EC2 server.")
    reply = h.say("database-9")
    assert reply == "I couldn't find an instance named database-9. The instances I can see are: web-01. Which one is it?"
    assert h.state.missing_slots == ["instance"]


def test_missing_instance_id_is_reported_as_not_found():
    missing = "i-0cccccccccccccc03"
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=missing)],
                            tool_rounds=[calls("ec2_get_instance")]))
    h.say(f"can't ssh to {missing}")
    assert h.state.root_cause_code is RootCause.EC2_INSTANCE_NOT_FOUND
    assert h.state.status is Status.RESOLVED


# ---------------------------------------------------------------- escalation

def test_no_fault_found_escalates():
    cloud = FakeCloud()
    cloud.add_instance(WEB, "web-01", open_ports=(22,))
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")],
                            tool_rounds=[CHECK_INSTANCE_AND_RULES]), cloud)
    reply = h.say("I can't ssh into web-01")
    assert h.state.root_cause_code is RootCause.NO_FAULT_FOUND
    assert h.state.status is Status.ESCALATED
    assert reply == ESCALATION_TEXT


def test_unreachable_cloud_escalates():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)],
                            tool_rounds=[calls("ec2_get_instance")]))
    h.cloud.failure = CloudError("unavailable", "connection refused")
    h.say(f"I can't ssh into {WEB}")
    assert h.state.status is Status.ESCALATED
    assert h.state.escalation_reason == "the cloud API is unreachable"


def test_tool_timeout_is_recorded_and_does_not_hang():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)],
                            tool_rounds=[calls("ec2_get_instance")]))
    h.cloud.delay_s = 1.0
    h.say(f"I can't ssh into {WEB}")
    assert h.state.tool_calls[0].error["error"] == "timeout"
    assert h.state.status is Status.ESCALATED  # nothing could be checked, so no cause was found


def test_failed_write_escalates_and_does_not_claim_success():
    h = ssh_harness()
    h.cloud.failure = CloudError("access_denied", "not authorized")
    reply = h.say("yes")
    assert "Done" not in reply and reply.startswith("I wasn't able to make that change.")
    assert h.state.status is Status.ESCALATED


def test_unverified_write_escalates(monkeypatch):
    monkeypatch.setattr(agent_module, "VERIFY_ATTEMPTS", 1)
    h = ssh_harness()
    h.cloud.add_ingress_rule = lambda *args: None  # the change silently does nothing
    reply = h.say("yes")
    assert "couldn't confirm" in reply and "Done" not in reply
    assert h.state.escalation_reason == "a confirmed change could not be verified"


def test_asking_for_a_human_escalates_without_the_model():
    h = Harness(ScriptedLLM())
    assert h.say("let me talk to a human please") == ESCALATION_TEXT
    assert h.state.status is Status.ESCALATED and h.llm.requests == []


def test_escalation_records_a_handoff_for_the_engineer():
    cloud = FakeCloud()
    cloud.add_instance(WEB, "web-01", open_ports=(22,))
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")],
                            tool_rounds=[CHECK_INSTANCE_AND_RULES]), cloud)
    h.say("I can't ssh into web-01")
    assert {"type": "escalated", "trigger": "no_fault_found", "reason": "the checks that ran did not find the cause"} in h.events

    (row,) = h.store.escalation_rows
    assert (row["conversation_id"], row["trigger"], row["channel"], row["service"], row["intent"]) == (
        "conv", "no_fault_found", "chat", "ec2", "ec2_connectivity")
    handoff = row["handoff"]
    assert handoff["problem_statement"] == "I can't ssh into web-01"
    assert handoff["entities"]["instance_id"] == WEB and handoff["root_cause"] == "NO_FAULT_FOUND"
    assert [c["tool"] for c in handoff["checks"]][:3] == [
        "ec2_list_instances", "ec2_get_instance", "ec2_get_security_group_rules"]
    assert all(c["ok"] and c["finding"] for c in handoff["checks"])
    assert h.audit()[-1] == "escalated" and h.store.audit[-1]["data"]["trigger"] == "no_fault_found"


def test_escalation_button_hands_over_and_cancels_a_pending_change():
    h = ssh_harness()
    reply = h.say(event="escalate")
    assert reply == ESCALATION_TEXT and h.state.status is Status.ESCALATED
    assert h.state.pending_action is None and h.cloud.writes == []
    assert h.store.escalation_rows[-1]["trigger"] == "customer_request"
    assert h.store.escalation_rows[-1]["handoff"]["proposed_resolution"] == "ADD_INGRESS_RULE"


def test_unidentified_problem_escalates_after_two_clarifications():
    h = Harness(ScriptedLLM(labels=[label()] * 3))
    for expected in (Status.IDENTIFYING, Status.IDENTIFYING, Status.ESCALATED):
        h.say("something is wrong with AWS")
        assert h.state.status is expected


def test_stalled_conversation_escalates(monkeypatch):
    monkeypatch.setattr(agent_module, "STALLED_TURNS", 2)
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY, label(kind="question"), label(kind="question")]))
    h.say("I can't SSH into my EC2 server.")
    h.say("why do you need that?")
    assert h.state.status is Status.COLLECTING_INFORMATION
    h.say("but why?")
    assert h.state.status is Status.ESCALATED


# ------------------------------------------------------------------- hygiene

def test_secrets_in_model_output_are_redacted():
    h = Harness(ScriptedLLM(labels=[label(kind="other")],
                            replies=["Your key is AKIAABCDEFGHIJKLMNOP and password=hunter2 ok"]))
    reply = h.say("hello")
    assert "AKIA" not in reply and "hunter2" not in reply
    assert "AKIA" not in h.state.messages[-1].text


def test_problem_on_a_service_without_tools_is_answered_from_documentation_or_not_at_all():
    h = Harness(ScriptedLLM(labels=[label(intent="billing_basic")]))
    assert h.say("my bill has unexpected charges") == NOT_INSPECTABLE_TEXT.format(service="billing")
    assert h.state.tool_calls == [] and h.llm.requests_of("stream") == []

    h = Harness(ScriptedLLM(labels=[label(intent="billing_basic")], replies=["Check Cost Explorer [1]."]),
                documents=DOCS)
    assert h.say("my bill has unexpected charges this month") == (
        "Check Cost Explorer [1].\n\n"
        "I couldn't check your billing resources myself; this guidance comes from the documentation.")
    prompt = h.llm.requests_of("stream")[0]["messages"][-1]["content"]
    assert "Give troubleshooting guidance for it from the excerpts" in prompt and "[1] Avoiding unexpected charges" in prompt
    assert h.state.retrieved_documents[0]["cited"] is True


@pytest.mark.parametrize("turns", [1, 3])
def test_state_survives_between_turns(turns):
    h = Harness(ScriptedLLM(labels=[CONNECTIVITY]))
    h.say("I can't SSH into my EC2 server.")
    for _ in range(turns - 1):
        h.llm.labels.append(label(kind="question"))
        h.say("what is an instance id?")
    assert h.state.turn == turns
    assert h.state.intent == "ec2_connectivity"
    assert len(h.state.messages) == 2 * turns


def test_my_alarm_is_not_an_alarm_name():
    h = Harness(ScriptedLLM(labels=[label(intent="cloudwatch_alarm", alarm="my alarm")]))
    assert h.say("my alarm is acting weird") == "What's the name of the alarm?"


def test_a_quoted_state_or_error_name_is_not_a_resource_name():
    h = Harness(ScriptedLLM(labels=[label(intent="cloudwatch_alarm", alarm="INSUFFICIENT_DATA")]))
    assert h.say("My alarm shows INSUFFICIENT_DATA.") == "What's the name of the alarm?"
    h = Harness(ScriptedLLM(labels=[label(intent="s3_bucket_not_found", bucket="NoSuchBucket")]))
    assert h.say("I'm getting a NoSuchBucket error.") == "What's the name of the bucket?"


def test_category_words_are_not_taken_as_resource_names():
    h = Harness(ScriptedLLM(labels=[label(intent="s3_upload_failure", bucket="bucket", iam_user="my user")]))
    assert h.say("my user cannot upload to my bucket") == "What's the name of the bucket?"
    assert h.state.entities == {}


def test_a_lookup_already_done_this_turn_is_not_repeated():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")], tool_rounds=[
        calls("ec2_list_instances"), CHECK_INSTANCE_AND_RULES]))
    h.say("I can't ssh into web-01")
    assert h.tools_called().count("ec2_list_instances") == 1
    # The model still receives the earlier result as the answer to its repeated call.
    tool_message = h.llm.requests_of("investigate")[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "web-01" in tool_message["content"]


def test_access_error_that_names_a_bucket_is_treated_as_an_s3_problem():
    cloud = FakeCloud()
    cloud.add_bucket("reports", keys=("q3.csv",))
    cloud.users["analyst"] = "implicit_deny"
    h = Harness(ScriptedLLM(
        labels=[label(intent="iam_access_denied", bucket="reports", object_key="q3.csv", iam_user="analyst")],
        tool_rounds=[calls(("s3_check_bucket_access", {"action": "read"}))]), cloud)
    h.say("analyst gets AccessDenied downloading q3.csv from reports")
    assert h.state.intent == "s3_download_failure"
    assert h.state.root_cause_code is RootCause.S3_IAM_PERMISSION_MISSING


def test_causes_are_recognised_from_whichever_tool_reported_the_fact():
    # The model listed buckets instead of checking the one bucket.
    cloud = FakeCloud()
    cloud.add_bucket("logs")
    h = Harness(ScriptedLLM(labels=[label(intent="s3_bucket_not_found", bucket="archive")],
                            tool_rounds=[calls("s3_list_buckets")]), cloud)
    h.say("I get NoSuchBucket for archive")
    assert h.state.root_cause_code is RootCause.S3_BUCKET_NOT_FOUND



def test_agent_runs_the_required_checks_the_model_skipped():
    # The model looks at the instance and stops; the firewall rules still get checked.
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)],
                            tool_rounds=[calls("ec2_get_instance")]))
    h.say(f"I can't ssh into {WEB}")
    assert [(c.tool, c.requested_by) for c in h.state.tool_calls][:2] == [
        ("ec2_get_instance", "model"), ("ec2_get_security_group_rules", "agent")]
    assert h.state.root_cause_code is RootCause.EC2_SG_SSH_BLOCKED

    # Even a model that calls nothing at all does not cause a wrong "no fault found".
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance=WEB)]))
    h.say(f"I can't ssh into {WEB}")
    assert h.state.root_cause_code is RootCause.EC2_SG_SSH_BLOCKED
    assert {c.requested_by for c in h.state.tool_calls} == {"agent"}


# ------------------------------------------------------------ knowledge base

def sources(h: Harness) -> dict | None:
    return next((e for e in h.events if e["type"] == "sources"), None)


def test_documentation_question_is_answered_from_excerpts_with_citations():
    h = Harness(ScriptedLLM(labels=[label(kind="question")],
                            replies=["Add an inbound rule for port 22 to the security group [1]."]), documents=DOCS)
    reply = h.say("How do I allow SSH access to my instance in a security group?")
    assert reply == "Add an inbound rule for port 22 to the security group [1]."

    request = h.llm.requests_of("stream")[0]["messages"]
    assert "using only the documentation excerpts" in request[0]["content"]
    assert "[1] Authorize inbound traffic > Add a rule for SSH\nTo allow SSH access" in request[-1]["content"]

    retrieved = h.state.retrieved_documents
    assert retrieved[0]["chunk_id"] == "ec2/authorize#0" and retrieved[0]["cited"] is True
    assert all(d["cited"] is False for d in retrieved[1:])
    event = sources(h)
    assert event["kind"] == "cited"
    assert event["documents"][0] == {
        "n": 1, "title": "Authorize inbound traffic", "section": "Authorize inbound traffic > Add a rule for SSH",
        "url": "https://example.test/ec2/authorize.md", "source_date": "2023-06-15", "cited": True}
    assert "retrieve" in h.state.latency_ms and h.state.tool_calls == []


def test_question_the_knowledge_base_does_not_cover_is_not_answered_from_memory():
    h = Harness(ScriptedLLM(labels=[label(kind="question")]), documents=DOCS)
    assert h.say("How do I configure an Azure virtual network?") == NO_DOCUMENTATION_TEXT
    assert h.llm.requests_of("stream") == []          # the model was never asked
    assert h.state.retrieved_documents == [] and sources(h) is None


def test_model_can_decline_excerpts_that_do_not_answer_the_question():
    h = Harness(ScriptedLLM(labels=[label(kind="question")], replies=["NO_ANSWER"]), documents=DOCS)
    reply = h.say("How long can a security group name for SSH on an instance be?")
    assert reply == NO_DOCUMENTATION_TEXT and "NO_ANSWER" not in str(h.events)
    assert h.state.retrieved_documents and not any(d["cited"] for d in h.state.retrieved_documents)
    assert sources(h) is None


def test_uncited_answer_is_discarded_and_cited_answer_is_delivered_in_full():
    question = "Is SSH access allowed by a security group of an instance by default?"
    # Answering from memory without citing an excerpt is not allowed through.
    h = Harness(ScriptedLLM(labels=[label(kind="question")], replies=["Yes, port 22 is always open."]),
                documents=DOCS)
    assert h.say(question) == NO_DOCUMENTATION_TEXT and "always open" not in str(h.events)
    assert [e for e in h.events if e["type"] == "activity"][-1]["detail"] == "The pages found do not answer this"
    # A citation of an excerpt that was not provided does not count either.
    h = Harness(ScriptedLLM(labels=[label(kind="question")], replies=["Yes, it is open [7]."]), documents=DOCS)
    assert h.say(question) == NO_DOCUMENTATION_TEXT

    h = Harness(ScriptedLLM(labels=[label(kind="question")],
                            replies=["No inbound rule exists by default [1]. Add one for port 22."]), documents=DOCS)
    assert h.say(question) == "No inbound rule exists by default [1]. Add one for port 22."
    searching = [e for e in h.events if e["type"] == "activity"]
    assert [e["state"] for e in searching] == ["running", "ok"] and "relevant section" in searching[1]["detail"]


def test_question_about_the_conversation_is_answered_from_context_when_no_page_matches():
    h = ssh_harness(replies=["x", "No, only your address."])
    h.llm.labels.append(label(kind="question"))
    reply = h.say("will that be open to everyone?")
    assert reply.startswith("No, only your address.") and "Would you like me to go ahead?" in reply
    assert "Proposed change awaiting" in h.llm.requests_of("stream")[-1]["messages"][-1]["content"]


def test_diagnosis_points_to_related_documentation_without_feeding_it_to_the_model():
    h = Harness(ScriptedLLM(labels=[label(intent="ec2_connectivity", instance="web-01")],
                            tool_rounds=[CHECK_INSTANCE_AND_RULES]), documents=DOCS)
    h.say("I can't ssh into web-01")
    event = sources(h)
    assert event["kind"] == "related" and event["documents"][0]["title"] == "Authorize inbound traffic"
    assert "Documentation excerpts" not in h.llm.requests_of("stream")[0]["messages"][-1]["content"]


# ------------------------------------------------- Lambda, IAM, and CloudWatch

FN = "report-generator"


def lambda_cloud(*, timeout: int = 3, logs=("START", "Task timed out after 3.00 seconds")) -> FakeCloud:
    cloud = FakeCloud()
    cloud.add_function(FN, timeout=timeout, logs=logs)
    return cloud


def test_lambda_timeout_is_raised_after_the_customer_chooses_a_value_and_confirms():
    cloud = lambda_cloud()
    h = Harness(ScriptedLLM(labels=[label(intent="lambda_timeout", function=FN)],
                            tool_rounds=[calls("lambda_get_function_logs")]), cloud)
    reply = h.say(f"my lambda {FN} keeps timing out")
    state = h.state
    assert state.root_cause_code is RootCause.LAMBDA_TIMEOUT_TOO_LOW
    # Evidence is the log that shows the timeout plus the configuration that shows the limit.
    assert {c.tool for c in state.tool_calls if c.id in state.evidence} == {
        "lambda_get_function_logs", "lambda_get_function_configuration"}
    assert reply.endswith("What should it be, in seconds? The maximum is 900.")
    assert state.missing_slots == ["timeout_seconds"]

    reply = h.say("2 minutes")
    assert f"Change the timeout of function {FN} from 3 to 120 seconds. Shall I go ahead?" in reply
    assert h.state.pending_action.args == {"function_name": FN, "timeout_seconds": 120}
    assert cloud.writes == []

    assert h.say("yes") == f"Done. The timeout of {FN} is now 120 seconds, and I've confirmed the new setting."
    assert cloud.writes == [("update_function_timeout", FN, 120)]
    assert h.state.outcome == "fixed"


def test_timeout_outside_the_allowed_range_is_asked_for_again():
    h = Harness(ScriptedLLM(labels=[label(intent="lambda_timeout", function=FN), label(intent="lambda_timeout")],
                            tool_rounds=[calls("lambda_get_function_logs")]), lambda_cloud())
    h.say(f"my lambda {FN} keeps timing out")
    reply = h.say("5000")
    assert h.state.pending_action is None and "The maximum is 900" in reply


@pytest.mark.parametrize("logs, expected", [
    (("[ERROR] KeyError: 'image_key'", "Traceback (most recent call last):"), RootCause.LAMBDA_RUNTIME_ERROR),
    (("[ERROR] ClientError: An error occurred (AccessDenied) when calling the PutObject operation: User is not "
      "authorized to perform: s3:PutObject",), RootCause.LAMBDA_ROLE_PERMISSION_MISSING),
])
def test_lambda_failures_are_told_apart_by_the_log(logs, expected):
    h = Harness(ScriptedLLM(labels=[label(intent="lambda_invocation_failure", function=FN)]),
                lambda_cloud(timeout=30, logs=logs))
    h.say(f"my function {FN} fails")
    assert h.state.root_cause_code is expected
    assert (h.state.status, h.state.outcome, h.state.pending_action) == (Status.RESOLVED, "advice_given", None)


def test_missing_function_stops_the_remaining_checks():
    h = Harness(ScriptedLLM(labels=[label(intent="lambda_invocation_failure", function="ghost-fn")]), lambda_cloud())
    h.say("my function ghost-fn fails")
    assert h.state.root_cause_code is RootCause.LAMBDA_FUNCTION_NOT_FOUND
    assert h.tools_called() == ["lambda_get_function"]   # one not-found is enough


def iam_cloud() -> FakeCloud:
    cloud = FakeCloud()
    cloud.users.update(developer="implicit_deny", contractor="explicit_deny", admin="allowed")
    cloud.roles.update({"app-role": {"logs:PutLogEvents"}, "data-role": {"dynamodb:GetItem"}})
    return cloud


@pytest.mark.parametrize("message, details, expected", [
    ("user developer gets AccessDenied for ec2:StartInstances", {"iam_user": "developer"},
     RootCause.IAM_PERMISSION_MISSING),
    ("contractor is not authorized to perform ec2:StartInstances", {"iam_role": "contractor"},   # mislabelled a role
     RootCause.IAM_EXPLICIT_DENY),
    ("role app-role is denied dynamodb:GetItem", {"iam_role": "app-role"}, RootCause.IAM_WRONG_ROLE),
    ("role data-role is denied s3:PutObject", {"iam_role": "data-role"}, RootCause.IAM_PERMISSION_MISSING),
])
def test_iam_denials_are_classified_by_the_permission_check(message, details, expected):
    h = Harness(ScriptedLLM(labels=[label(intent="iam_access_denied", **details)]), iam_cloud())
    h.say(message)
    assert h.state.root_cause_code is expected
    assert h.state.outcome == "advice_given" and h.cloud.writes == []
    check = h.state.latest_result("iam_check_permission")
    assert check["action"] == message.split()[-1] and check["principal"] == next(iter(details.values()))


def test_iam_principal_and_action_are_collected_one_at_a_time():
    h = Harness(ScriptedLLM(labels=[label(intent="iam_access_denied")]), iam_cloud())
    assert h.say("my app gets access denied from IAM").startswith("Which IAM user or role is affected?")

    reply = h.say("role app-role")
    assert reply.startswith("Which action is being denied?")
    assert h.state.entities["iam_role"] == "app-role" and h.state.entities["principal_checked"] == "role"

    h.say("dynamodb:GetItem")
    assert h.state.root_cause_code is RootCause.IAM_WRONG_ROLE
    assert h.state.latest_result("iam_check_permission")["other_roles_allowed"] == ["data-role"]
    # The two answers were taken without the model; it was used to classify, choose tools, and explain.
    assert {r["kind"] for r in h.llm.requests} == {"classify", "investigate", "stream"}
    assert len(h.llm.requests_of("classify")) == 1


def test_unknown_principal_is_asked_for_again_and_a_bare_name_is_tried_as_user_then_role():
    h = Harness(ScriptedLLM(labels=[label(intent="iam_access_denied", iam_user="ghost")]), iam_cloud())
    reply = h.say("user ghost gets AccessDenied for s3:GetObject")
    assert reply == "I couldn't find an IAM user or role named ghost. Which user or role is it?"
    assert h.state.missing_slots == ["principal"] and h.state.root_cause_code is None
    # Looking for the principal is not shown to the customer as a failed check.
    assert [e for e in h.events if e["type"] == "activity"] == []

    h.say("data-role")
    assert h.state.entities["iam_role"] == "data-role" and "iam_user" not in h.state.entities
    assert h.state.root_cause_code is RootCause.IAM_PERMISSION_MISSING


def test_allowed_permission_is_no_fault_and_escalates():
    h = Harness(ScriptedLLM(labels=[label(intent="iam_access_denied", iam_user="admin")]), iam_cloud())
    h.say("user admin gets AccessDenied for ec2:StartInstances")
    assert h.state.root_cause_code is RootCause.NO_FAULT_FOUND and h.state.status is Status.ESCALATED


@pytest.mark.parametrize("alarm_state, expected", [
    ("ALARM", RootCause.CW_ALARM_THRESHOLD_BREACHED),
    ("INSUFFICIENT_DATA", RootCause.CW_ALARM_INSUFFICIENT_DATA),
    ("OK", RootCause.CW_ALARM_OK),
])
def test_alarm_is_explained_from_its_state(alarm_state, expected):
    cloud = FakeCloud()
    cloud.add_alarm("high-cpu", alarm_state)
    h = Harness(ScriptedLLM(labels=[label(intent="cloudwatch_alarm", alarm="high-cpu")]), cloud)
    h.say("why is my alarm high-cpu in this state?")
    assert h.state.root_cause_code is expected
    assert h.tools_called() == ["cloudwatch_get_alarm", "cloudwatch_get_alarm_history", "cloudwatch_get_metric"]
    assert h.state.outcome == "advice_given"


def test_missing_alarm_and_alarm_name_collected_by_question():
    cloud = FakeCloud()
    cloud.add_alarm("high-cpu", "ALARM")
    h = Harness(ScriptedLLM(labels=[label(intent="cloudwatch_alarm")]), cloud)
    assert h.say("my alarm went off") == "What's the name of the alarm?"
    h.say("low-disk")
    assert h.state.root_cause_code is RootCause.CW_ALARM_NOT_FOUND


def test_question_that_names_a_customer_resource_is_investigated_not_looked_up_in_the_docs():
    cloud = FakeCloud()
    cloud.add_alarm("high-cpu", "OK")
    h = Harness(ScriptedLLM(labels=[label(kind="question", intent="cloudwatch_alarm", alarm="high-cpu")]), cloud)
    h.say("did my alarm high-cpu go off?")
    assert h.state.message_kind == "problem" and h.state.root_cause_code is RootCause.CW_ALARM_OK


def test_naming_a_different_resource_discards_the_earlier_diagnosis():
    cloud = lambda_cloud()
    cloud.add_function("thumbnailer", timeout=30, logs=("[ERROR] KeyError: 'k'",))
    h = Harness(ScriptedLLM(labels=[label(intent="lambda_timeout", function=FN),
                                    label(intent="lambda_timeout", function="thumbnailer")]), cloud)
    h.say(f"my lambda {FN} keeps timing out")
    assert h.state.root_cause_code is RootCause.LAMBDA_TIMEOUT_TOO_LOW
    h.say("sorry, I meant the thumbnailer function")
    state = h.state
    assert state.entities["function_name"] == "thumbnailer"
    assert state.root_cause_code is RootCause.LAMBDA_RUNTIME_ERROR
    assert all(c.args.get("function_name") == "thumbnailer" for c in state.tool_calls)
