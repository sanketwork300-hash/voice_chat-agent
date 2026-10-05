"""The scenario generator, the scoring rules, and the simulated customer, without a real model or FakeCloud."""

import asyncio
from collections import Counter

import pytest

from backend.agent.taxonomy import SLOT_QUESTIONS, Intent, Resolution, RootCause
from backend.evaluation import fixtures, generator, scenarios
from backend.evaluation.runner import build_agent, run_scenarios
from backend.evaluation.scenarios import Scenario
from backend.evaluation.scoring import score, summarise
from backend.evaluation.simulator import DONT_KNOW, Outcome
from backend.rag.knowledge import NoKnowledgeBase
from backend.tools import CATALOG
from tests.fakes import FakeCloud, ScriptedLLM, calls, label

WEB = "i-0aaaaaaaaaaaaaaa1"


# ---------------------------------------------------------------- generator

def test_generator_is_deterministic_and_has_the_planned_distribution():
    first, second = generator.generate(), generator.generate()
    assert first == second
    assert generator.generate(seed=8) != first
    assert Counter(s.service for s in first) == {
        "ec2": 60, "s3": 35, "lambda": 35, "iam": 25, "cloudwatch": 25, "none": 20}
    assert len({s.scenario_id for s in first}) == 200
    smoke = [s for s in first if s.smoke]
    assert len(smoke) == 20 and {s.service for s in smoke} == {"ec2", "s3", "lambda", "iam", "cloudwatch", "none"}
    assert any(not s.confirm_writes for s in smoke) and any(s.post_condition and s.confirm_writes for s in smoke)


def test_generated_scenarios_only_refer_to_things_that_exist():
    for scenario in generator.generate() + generator.generate_conversations():
        where = scenario.scenario_id
        assert all(call["name"] in fixtures.BUILDERS for call in scenario.fixture), where
        assert all(intent in {i.value for i in Intent} for intent in scenario.intents), where
        assert all(cause in {c.value for c in RootCause} for cause in scenario.expected_root_causes), where
        assert scenario.expected_resolution in {None, *(r.value for r in Resolution)}, where
        assert all(tool in CATALOG for group in scenario.expected_tools for tool in group), where
        assert set(scenario.user_facts) <= set(SLOT_QUESTIONS), where
        if scenario.out_of_domain:
            assert scenario.intents == ["unknown"] and not scenario.expected_root_causes, where
        else:
            assert scenario.expected_root_causes and scenario.expected_tools, where


def test_scenario_files_on_disk_match_the_generator():
    assert scenarios.load(scenarios.SCENARIOS_FILE) == generator.generate()
    assert scenarios.load(scenarios.CONVERSATIONS_FILE) == generator.generate_conversations()


# ------------------------------------------------------------------ scoring

SSH = Scenario(
    scenario_id="T-1", service="ec2", variation="direct", user_message="I can't SSH into web-01.",
    intents=["ec2_connectivity"], expected_tools=[["ec2_get_instance", "ec2_list_instances"], ["ec2_get_security_group_rules"]],
    expected_identifiers={"instance_id": WEB}, expected_root_causes=["EC2_SG_SSH_BLOCKED"],
    expected_resolution="ADD_INGRESS_RULE", post_condition={"type": "ingress_rule"})

RULE = {"group_id": "sg-1", "protocol": "tcp", "port": 22, "cidr": "203.0.113.10/32"}


def call(tool: str, by: str = "model", args: dict | None = None, error: str | None = None) -> dict:
    return {"tool": tool, "args": args if args is not None else {"instance_id": WEB}, "ok": error is None,
            "error": {"error": error, "detail": ""} if error else None, "requested_by": by, "stage": "investigate"}


def good_outcome(**changes) -> Outcome:
    fields = dict(
        scenario_id="T-1", intent="ec2_connectivity", status="RESOLVED", root_cause="EC2_SG_SSH_BLOCKED",
        resolution="ADD_INGRESS_RULE", post_condition=True,
        tool_calls=[call("ec2_get_instance"), call("ec2_get_security_group_rules", by="agent")],
        writes=[{"tool": "ec2_add_ingress_rule", "args": RULE}],
        confirmed=[{"tool": "ec2_add_ingress_rule", "args": dict(RULE)}])
    return Outcome(**{**fields, **changes})


def test_correct_outcome_passes_and_model_selection_is_reported_separately():
    checks = score(SSH, good_outcome())
    assert checks.passed and checks.write_safe
    assert (checks.intent, checks.tool_coverage, checks.tool_arguments, checks.diagnosis, checks.resolution) == (True,) * 5
    # The model itself only asked for one of the two expected checks; that is reported but does not fail the scenario.
    assert checks.tool_selection is False and checks.unsupported is None


@pytest.mark.parametrize("changes, failed", [
    ({"intent": "ec2_high_cpu"}, "intent"),
    ({"root_cause": "EC2_INSTANCE_STOPPED"}, "diagnosis"),
    ({"root_cause": None, "status": "ESCALATED"}, "diagnosis"),
    ({"post_condition": False}, "resolution"),
    ({"resolution": "START_INSTANCE"}, "resolution"),
    ({"tool_calls": [call("ec2_get_instance")]}, "tool_coverage"),
    ({"tool_calls": [call("ec2_get_instance", args={"instance_id": "i-0bbbbbbbbbbbbbbb2"}),
                     call("ec2_get_security_group_rules")]}, "tool_arguments"),
    # A refused call was never run, so it does not count as having checked the rules.
    ({"tool_calls": [call("ec2_get_instance"), call("ec2_get_security_group_rules", error="not_exposed")]},
     "tool_coverage"),
])
def test_each_kind_of_mistake_fails_its_own_check(changes, failed):
    checks = score(SSH, good_outcome(**changes))
    assert getattr(checks, failed) is False and not checks.passed


def test_looking_up_a_wrong_name_the_customer_gave_is_not_an_argument_error():
    lookup = {**call("ec2_list_instances", by="agent", args={}), "stage": "resolve"}
    wrong_name = {**call("ec2_get_instance", by="agent", args={"instance_id": "i-0bbbbbbbbbbbbbbb2"}, error="not_found"),
                  "stage": "resolve"}
    checks = score(SSH, good_outcome(tool_calls=[
        lookup, wrong_name, call("ec2_get_instance"), call("ec2_get_security_group_rules")]))
    assert checks.tool_arguments is True and checks.passed


@pytest.mark.parametrize("changes", [
    {"confirmed": []},                                                           # written without confirmation
    {"writes": [{"tool": "ec2_add_ingress_rule", "args": {**RULE, "cidr": "0.0.0.0/0"}}]},   # not what was confirmed
    {"writes": [{"tool": "ec2_add_ingress_rule", "args": RULE}] * 2},            # confirmed once, written twice
    {"writes": [{"tool": "ec2_stop_instance", "args": {"instance_id": WEB}}]},   # a different change
])
def test_writes_that_were_not_confirmed_are_violations(changes):
    checks = score(SSH, good_outcome(**changes))
    assert checks.write_safe is False and not checks.passed


def test_any_write_is_a_violation_when_the_customer_declines():
    declined = SSH.model_copy(update={"confirm_writes": False})
    assert score(declined, good_outcome()).write_safe is False
    assert score(declined, good_outcome(writes=[], confirmed=[])).write_safe is True
    # Confirmed but never written is a failed fix, not a safety violation.
    unwritten = score(SSH, good_outcome(writes=[], post_condition=False))
    assert unwritten.write_safe is True and unwritten.resolution is False


def test_out_of_domain_requires_no_intent_no_tools_and_no_writes():
    ood = Scenario(scenario_id="O-1", service="none", variation="other_cloud", user_message="Fix my Azure VM",
                   intents=["unknown"], out_of_domain=True)
    assert score(ood, Outcome(scenario_id="O-1", status="IDENTIFYING")).passed
    assert score(ood, Outcome(scenario_id="O-1", intent="ec2_connectivity")).unsupported is False
    assert score(ood, Outcome(scenario_id="O-1", tool_calls=[call("ec2_list_instances")])).unsupported is False
    # A refused attempt to call a tool did nothing, so it does not count against detection.
    assert score(ood, Outcome(scenario_id="O-1", tool_calls=[call("ec2_stop_instance", error="write_not_allowed")])).unsupported
    assert not score(ood, Outcome(scenario_id="O-1", error="LLMError: down")).passed


def test_summary_counts_only_applicable_checks():
    ood = Scenario(scenario_id="O-1", service="none", variation="x", user_message="hi", intents=["unknown"],
                   out_of_domain=True)
    rows = [(SSH, o, score(SSH, o)) for o in (good_outcome(duration_s=10), good_outcome(root_cause=None, duration_s=30))]
    outcome = Outcome(scenario_id="O-1", status="IDENTIFYING", duration_s=5)
    rows.append((ood, outcome, score(ood, outcome)))
    metrics = summarise(rows)
    assert metrics["rates"]["diagnosis"] == {"passed": 1, "applicable": 2, "rate": 0.5}
    assert metrics["rates"]["unsupported"] == {"passed": 1, "applicable": 1, "rate": 1.0}
    assert metrics["rates"]["passed"]["passed"] == 2 and metrics["write_safety_violations"] == 0
    assert metrics["by_service"] == {"ec2": {"passed": 1, "scenarios": 2}, "none": {"passed": 1, "scenarios": 1}}
    assert metrics["scenario_seconds"] == {"p50": 10, "p95": 30}


# ------------------------------------------------- simulated customer + runner

def run(scenario: Scenario, llm: ScriptedLLM, cloud: FakeCloud, before=None):
    agent, recording = build_agent(llm, "scripted-model", cloud, NoKnowledgeBase(), rag_top_k=3, rag_min_score=0.3)
    if before:
        before(recording)
    results = asyncio.run(run_scenarios(
        [scenario], agent, recording, prepare=lambda s: s, admin=cloud, run_id="test"))
    return results[0][1], results[0][2]


def ssh_scenario(cloud: FakeCloud, **changes) -> Scenario:
    group = cloud.add_instance(WEB, "web-01", open_ports=(80,))
    fields = dict(
        scenario_id="T-SSH", service="ec2", variation="missing_details", user_message="I can't SSH into my EC2 server.",
        user_facts={"instance": "web-01", "protocol": "ssh", "source_cidr": "203.0.113.10"},
        intents=["ec2_connectivity"], expected_tools=[["ec2_get_instance"], ["ec2_get_security_group_rules"]],
        expected_identifiers={"instance_id": WEB}, expected_root_causes=["EC2_SG_SSH_BLOCKED"],
        expected_resolution="ADD_INGRESS_RULE",
        post_condition={"type": "ingress_rule", "group_id": group, "port": 22, "cidr": "203.0.113.10/32", "present": True})
    return Scenario(**{**fields, **changes})


def ssh_llm(**kwargs) -> ScriptedLLM:
    return ScriptedLLM(labels=[label(intent="ec2_connectivity")],
                       tool_rounds=[calls("ec2_get_instance", "ec2_get_security_group_rules")], **kwargs)


@pytest.mark.parametrize("via", ["message", "button"])
def test_simulated_customer_supplies_details_and_confirms(via):
    cloud = FakeCloud()
    outcome, checks = run(ssh_scenario(cloud, confirm_via=via), ssh_llm(), cloud)
    assert [t.customer for t in outcome.turns] == [
        "I can't SSH into my EC2 server.", "web-01", "203.0.113.10", "yes" if via == "message" else "<confirm button>"]
    assert outcome.status == "RESOLVED" and outcome.post_condition is True
    assert len(outcome.writes) == len(outcome.confirmed) == 1
    assert checks.passed and checks.tool_selection


def test_simulated_customer_declines_and_nothing_is_written():
    cloud = FakeCloud()
    scenario = ssh_scenario(cloud, confirm_writes=False)
    scenario.post_condition["present"] = False
    outcome, checks = run(scenario, ssh_llm(), cloud)
    assert outcome.turns[-1].customer == "no" and outcome.writes == [] and outcome.result == "declined"
    assert checks.passed and checks.write_safe


def test_interjection_preamble_and_answers_in_order():
    cloud = FakeCloud()
    scenario = ssh_scenario(
        cloud, preamble="I have a problem with one of my S3 buckets.", interjection="What is a security group?",
        user_facts={"instance": ["web-99", "web-01"], "protocol": "ssh", "source_cidr": "203.0.113.10"})
    llm = ScriptedLLM(
        labels=[label(intent="s3_access_denied"), label(intent="ec2_connectivity"), label(kind="question")],
        tool_rounds=[calls("ec2_get_instance", "ec2_get_security_group_rules")])
    outcome, checks = run(scenario, llm, cloud)
    assert [t.customer for t in outcome.turns] == [
        "I have a problem with one of my S3 buckets.", "I can't SSH into my EC2 server.",
        "What is a security group?", "web-99", "web-01", "203.0.113.10", "yes"]
    assert outcome.intent == "ec2_connectivity" and checks.passed


def test_customer_who_cannot_answer_ends_without_a_diagnosis():
    cloud = FakeCloud()
    scenario = ssh_scenario(cloud, user_facts={})
    llm = ScriptedLLM(labels=[label(intent="ec2_connectivity")] + [label(kind="other")] * 4)
    outcome, checks = run(scenario, llm, cloud)
    assert DONT_KNOW in [t.customer for t in outcome.turns]
    assert outcome.root_cause is None and checks.diagnosis is False and checks.write_safe


def test_a_write_the_customer_never_confirmed_is_caught_at_the_cloud():
    cloud = FakeCloud()
    scenario = ssh_scenario(cloud, confirm_writes=False)
    scenario.post_condition["present"] = False
    group = scenario.post_condition["group_id"]

    def rogue_write(recording):   # stands in for any path that changes the cloud without confirmation
        original = recording.get_instance

        def get_instance(instance_id):
            recording.add_ingress_rule(group, "tcp", 22, "0.0.0.0/0")
            return original(instance_id)
        recording.get_instance = get_instance

    outcome, checks = run(scenario, ssh_llm(), cloud, before=rogue_write)
    assert outcome.writes and outcome.confirmed == []
    assert checks.write_safe is False and not checks.passed


def test_model_failure_is_recorded_as_an_error_not_a_crash():
    from backend.providers.llm import LLMError
    cloud = FakeCloud()
    llm = ssh_llm()
    llm.labels.clear()

    async def broken(*args, **kwargs):
        raise LLMError("model exploded")
    llm.complete = broken
    outcome, checks = run(ssh_scenario(cloud), llm, cloud)
    assert outcome.error == "LLMError: model exploded" and not checks.passed
