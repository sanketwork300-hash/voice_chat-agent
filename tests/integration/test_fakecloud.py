"""The boto3 provider, the fixtures, and the tools against a running FakeCloud.

Not part of the default test run, because these tests wipe the simulated
account: run them with `make test-fakecloud`, which reseeds the demo afterwards.
"""

import asyncio

import httpx
import pytest

from backend.cloud.base import CloudError
from backend.cloud.boto3_provider import Boto3CloudProvider
from backend.config import Settings
from backend.evaluation import fixtures
from backend.memory.store import MemoryStore
from backend.tools.gateway import CallContext, ToolGateway

SETTINGS = Settings(_env_file=None)


def _reachable() -> bool:
    try:
        return httpx.get(f"{SETTINGS.aws_endpoint_url}/_fakecloud/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.fakecloud, pytest.mark.skipif(not _reachable(), reason="FakeCloud is not running")]


@pytest.fixture
def cloud():
    fixtures.reset(SETTINGS.aws_endpoint_url)
    yield Boto3CloudProvider(SETTINGS)
    fixtures.reset(SETTINGS.aws_endpoint_url)


def read(cloud, service: str, entities: dict, tool: str, args: dict | None = None):
    gateway = ToolGateway(cloud, MemoryStore())
    return asyncio.run(gateway.call_read(CallContext("c", 1, service, entities), "t1", tool, args or {}, "investigate"))


def test_ec2_reads_and_writes(cloud):
    web = fixtures.ec2_instance(cloud.client, name="web-01", open_ports=(80,), cpu_percent=96.0)
    entities = {"instance_id": web["instance_id"]}

    instance = read(cloud, "ec2", entities, "ec2_get_instance").result
    assert (instance["name"], instance["state"], instance["security_group_ids"]) == ("web-01", "running", [web["group_id"]])

    rules = read(cloud, "ec2", entities, "ec2_get_security_group_rules").result
    assert (rules["allows_ssh_22"], rules["allows_rdp_3389"]) == (False, False)
    assert read(cloud, "ec2", entities, "ec2_get_instance_metrics").result["cpu_utilization_percent"]["average"] == 96.0

    cloud.add_ingress_rule(web["group_id"], "tcp", 22, "203.0.113.10/32")
    assert read(cloud, "ec2", entities, "ec2_get_security_group_rules").result["allows_ssh_22"] is True

    cloud.stop_instance(web["instance_id"])
    assert cloud.get_instance(web["instance_id"]).state in ("stopping", "stopped")

    missing = read(cloud, "ec2", {"instance_id": "i-0123456789abcdef0"}, "ec2_get_instance")
    assert missing.error == {"error": "not_found", "detail": "instance i-0123456789abcdef0 does not exist"}


def test_s3_access_checks(cloud):
    fixtures.s3_bucket(cloud.client, name="reports", objects=("q3.csv",), deny_downloads=True)
    fixtures.s3_bucket(cloud.client, name="logs", objects=("app.log",))
    fixtures.iam_user(cloud.client, name="analyst", s3_access=True)
    fixtures.iam_user(cloud.client, name="intern", s3_access=False)

    def access(bucket: str, user: str, action: str) -> dict:
        return read(cloud, "s3", {"bucket": bucket, "iam_user": user}, "s3_check_bucket_access", {"action": action}).result

    assert access("reports", "analyst", "read")["denied_by"] == "bucket_policy"
    assert access("reports", "analyst", "write")["decision"] == "allowed"
    assert access("logs", "intern", "read")["denied_by"] == "missing_permission"

    assert read(cloud, "s3", {"bucket": "nope"}, "s3_get_bucket").error["detail"] == "bucket nope does not exist"
    gone = read(cloud, "s3", {"bucket": "logs", "object_key": "gone.log"}, "s3_get_object_metadata")
    assert gone.error["detail"] == "object gone.log does not exist"
    assert cloud.get_bucket_policy("logs") is None
    with pytest.raises(CloudError, match="bucket nope does not exist"):
        cloud.get_bucket_policy("nope")


def test_unreachable_endpoint_is_reported_as_unavailable():
    dead = Boto3CloudProvider(Settings(_env_file=None, aws_endpoint_url="http://127.0.0.1:9"))
    record = read(dead, "ec2", {}, "ec2_list_instances")
    assert record.error["error"] == "unavailable"


def test_lambda_reads_and_timeout_change(cloud):
    role = fixtures.iam_principal(cloud.client, kind="role", name="fn-role", allow=("logs:*",))
    fixtures.lambda_function(
        cloud.client, name="reporter", role_arn=role, timeout=3, invocations=4, errors=4, max_duration_ms=3000,
        log_lines=("START", "Task timed out after 3.00 seconds", "[ERROR] KeyError: 'x'",
                   "AccessDenied: not authorized to perform: s3:PutObject"))
    entities = {"function_name": "reporter"}

    function = read(cloud, "lambda", entities, "lambda_get_function").result
    assert (function["role_name"], function["role_inline_policies"]) == ("fn-role", ["allow"])
    assert read(cloud, "lambda", entities, "lambda_get_function_configuration").result["timeout_seconds"] == 3

    logs = read(cloud, "lambda", entities, "lambda_get_function_logs").result
    assert (logs["timeouts"], logs["errors"], logs["access_denied"], len(logs["events"])) == (1, 1, 1, 4)
    metrics = read(cloud, "lambda", entities, "lambda_get_function_metrics").result
    assert (metrics["invocations"], metrics["errors"], metrics["duration_ms"]["maximum"]) == (4, 4, 3000)

    cloud.update_function_timeout("reporter", 60)
    assert cloud.get_function("reporter").timeout_seconds == 60

    missing = read(cloud, "lambda", {"function_name": "ghost"}, "lambda_get_function_logs")
    assert missing.error == {"error": "not_found", "detail": "function ghost does not exist"}


def test_iam_permission_checks(cloud):
    fixtures.iam_principal(cloud.client, kind="user", name="developer", allow=("s3:ListAllMyBuckets",))
    fixtures.iam_principal(cloud.client, kind="user", name="contractor", allow=("*",), deny=("ec2:*",))
    fixtures.iam_principal(cloud.client, kind="role", name="app-role", allow=("logs:*",))
    fixtures.iam_principal(cloud.client, kind="role", name="data-role", allow=("dynamodb:GetItem",))

    def check(entities: dict) -> dict:
        return read(cloud, "iam", entities, "iam_check_permission").result

    missing = check({"iam_user": "developer", "iam_action": "ec2:StartInstances"})
    assert (missing["decision"], missing["matched_policies"]) == ("implicit_deny", [])
    denied = check({"iam_user": "contractor", "iam_action": "ec2:StartInstances"})
    assert (denied["decision"], denied["matched_policies"]) == ("explicit_deny", ["deny"])
    wrong_role = check({"iam_role": "app-role", "iam_action": "dynamodb:GetItem"})
    assert (wrong_role["decision"], wrong_role["other_roles_allowed"]) == ("implicit_deny", ["data-role"])
    assert check({"iam_role": "data-role", "iam_action": "dynamodb:GetItem"})["decision"] == "allowed"

    policies = read(cloud, "iam", {"iam_user": "contractor"}, "iam_get_policy").result
    assert {p["policy_name"]: p["statements"][0]["effect"] for p in policies["policies"]} == {"allow": "Allow", "deny": "Deny"}
    assert read(cloud, "iam", {"iam_role": "app-role"}, "iam_get_role").result["trusted_by"] == ["lambda.amazonaws.com"]
    assert read(cloud, "iam", {"iam_user": "nobody"}, "iam_get_user").error["detail"] == "IAM user nobody does not exist"


def test_cloudwatch_alarm_states(cloud):
    busy = fixtures.ec2_instance(cloud.client, name="busy")["instance_id"]
    quiet = fixtures.ec2_instance(cloud.client, name="quiet")["instance_id"]
    fixtures.cpu_alarm(cloud.client, name="busy-cpu", instance_id=busy, cpu_percent=95.0)
    fixtures.cpu_alarm(cloud.client, name="quiet-cpu", instance_id=quiet, cpu_percent=5.0)
    fixtures.cpu_alarm(cloud.client, name="silent-cpu", instance_id="i-0123456789abcdef0")

    def alarm(name: str, tool: str = "cloudwatch_get_alarm"):
        return read(cloud, "cloudwatch", {"alarm_name": name}, tool)

    assert [alarm(n).result["state"] for n in ("busy-cpu", "quiet-cpu", "silent-cpu")] == [
        "ALARM", "OK", "INSUFFICIENT_DATA"]
    metric = alarm("busy-cpu", "cloudwatch_get_metric").result
    assert (metric["metric"], metric["values"]["average"], metric["threshold"]) == ("CPUUtilization", 95.0, 80.0)
    assert alarm("silent-cpu", "cloudwatch_get_metric").result["values"]["datapoints"] == 0
    history = alarm("busy-cpu", "cloudwatch_get_alarm_history").result["items"]
    assert "to ALARM" in history[0]["summary"]
    assert len(read(cloud, "cloudwatch", {}, "cloudwatch_list_alarms").result["alarms"]) == 3
    assert alarm("ghost").error == {"error": "not_found", "detail": "alarm ghost does not exist"}
    assert alarm("ghost", "cloudwatch_get_alarm_history").error["error"] == "not_found"


def test_every_scenario_template_builds_its_fixture_and_fills_its_placeholders(cloud):
    from backend.evaluation import generator
    from backend.evaluation.runner import fakecloud_prepare

    prepare = fakecloud_prepare(cloud, SETTINGS.aws_endpoint_url)
    first_of_each_template = {s.scenario_id.rsplit("-", 1)[0]: s for s in reversed(generator.generate())}
    assert len(first_of_each_template) == 27   # 26 templates plus the out-of-domain group
    for scenario in list(first_of_each_template.values()) + generator.generate_conversations():
        prepared = prepare(scenario)
        assert "{" not in prepared.model_dump_json(exclude={"fixture"}).replace('{"', "").replace("{}", ""), scenario.scenario_id
        if prepared.post_condition:
            # Before any conversation, an "applied" post-condition must not already hold, and a "declined" one must.
            holds = fixtures.post_condition_holds(cloud, prepared.post_condition)
            always_true = prepared.expected_resolution == "REBOOT_INSTANCE"   # running before and after
            assert holds == (not prepared.confirm_writes or always_true), scenario.scenario_id
