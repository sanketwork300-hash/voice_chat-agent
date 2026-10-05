"""FakeCloud fidelity probe (SPEC section 7).

For every root-cause code in the taxonomy, seed the condition in FakeCloud and
read it back with the same AWS calls the tools will use. Prints a pass/fail
table. A root cause that fails here cannot be used in scenarios.

Usage: python scripts/probe_fakecloud.py [--json]
"""

import argparse
import io
import json
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from backend.config import get_settings

SETTINGS = get_settings()
RUN = uuid.uuid4().hex[:8]
AMI = "ami-0abcdef1234567890"
RESET_SERVICES = ["ec2", "s3", "lambda", "iam", "cloudwatch", "logs"]

Check = Callable[[], str]
CHECKS: list[tuple[str, str, Check]] = []


class Unsupported(Exception):
    """The condition could not be made observable."""


def check(code: str, what: str):
    def register(fn: Check) -> Check:
        CHECKS.append((code, what, fn))
        return fn
    return register


def client(service: str, key: str | None = None, secret: str | None = None):
    return boto3.client(
        service,
        endpoint_url=SETTINGS.aws_endpoint_url,
        region_name=SETTINGS.aws_region,
        aws_access_key_id=key or SETTINGS.aws_access_key_id,
        aws_secret_access_key=secret or SETTINGS.aws_secret_access_key,
        config=Config(retries={"max_attempts": 1}, connect_timeout=3, read_timeout=30),
    )


def error_code(fn: Callable, *args, **kwargs) -> str | None:
    try:
        fn(*args, **kwargs)
    except ClientError as e:
        return e.response["Error"]["Code"]
    return None


def wait_for(read: Callable[[], str], wanted: str, seconds: float = 15) -> str:
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value == wanted or time.monotonic() > deadline:
            return value
        time.sleep(0.5)


# ---------------------------------------------------------------- seed helpers

def security_group(ports: list[int]) -> str:
    ec2 = client("ec2")
    sg = ec2.create_security_group(
        GroupName=f"probe-{uuid.uuid4().hex[:8]}", Description="fidelity probe"
    )["GroupId"]
    for port in ports:
        ec2.authorize_security_group_ingress(
            GroupId=sg, IpProtocol="tcp", FromPort=port, ToPort=port, CidrIp="0.0.0.0/0"
        )
    return sg


def allows(sg: str, port: int) -> bool:
    groups = client("ec2").describe_security_groups(GroupIds=[sg])["SecurityGroups"]
    return any(
        p.get("IpProtocol") in ("tcp", "-1")
        and (p.get("IpProtocol") == "-1" or p["FromPort"] <= port <= p["ToPort"])
        for p in groups[0]["IpPermissions"]
    )


def instance(sg: str | None = None) -> str:
    kwargs = {"SecurityGroupIds": [sg]} if sg else {}
    return client("ec2").run_instances(
        ImageId=AMI, InstanceType="t3.micro", MinCount=1, MaxCount=1, **kwargs
    )["Instances"][0]["InstanceId"]


def instance_state(instance_id: str) -> str:
    res = client("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"]
    return res[0]["Instances"][0]["State"]["Name"]


def seed_metric(namespace: str, name: str, dims: dict[str, str], value: float, unit: str) -> float:
    """Publish one datapoint and return the average read back.

    The datapoint is stamped "now": alarms only evaluate datapoints inside their
    current period, so a backdated one never moves an alarm.
    """
    cw = client("cloudwatch")
    now = datetime.now(timezone.utc)
    dimensions = [{"Name": k, "Value": v} for k, v in dims.items()]
    cw.put_metric_data(Namespace=namespace, MetricData=[{
        "MetricName": name, "Dimensions": dimensions,
        "Timestamp": now, "Value": value, "Unit": unit,
    }])
    points = cw.get_metric_statistics(
        Namespace=namespace, MetricName=name, Dimensions=dimensions,
        StartTime=now - timedelta(minutes=15), EndTime=now + timedelta(minutes=1),
        Period=300, Statistics=["Average"],
    )["Datapoints"]
    if not points:
        raise Unsupported(f"{namespace}/{name}: published but no datapoints read back")
    return max(p["Average"] for p in points)


def trust_policy(service: str) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]})


def policy(effect: str, action: str, resource: str = "*") -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": effect, "Action": action, "Resource": resource}]})


def role(name: str) -> str:
    return client("iam").create_role(
        RoleName=name, AssumeRolePolicyDocument=trust_policy("lambda.amazonaws.com")
    )["Role"]["Arn"]


def user_with_key(name: str) -> tuple[str, str, str]:
    iam = client("iam")
    arn = iam.create_user(UserName=name)["User"]["Arn"]
    key = iam.create_access_key(UserName=name)["AccessKey"]
    return arn, key["AccessKeyId"], key["SecretAccessKey"]


def simulate(principal_arn: str, action: str, resource: str = "*") -> str:
    res = client("iam").simulate_principal_policy(
        PolicySourceArn=principal_arn, ActionNames=[action], ResourceArns=[resource]
    )
    return res["EvaluationResults"][0]["EvalDecision"]


@cache
def bucket() -> str:
    name = f"probe-{RUN}"
    s3 = client("s3")
    s3.create_bucket(Bucket=name)
    s3.put_object(Bucket=name, Key="report.csv", Body=b"a,b\n1,2\n")
    return name


@cache
def function() -> tuple[str, str]:
    """A Lambda function with a 3 second timeout and a role that has no policies."""
    name = f"probe-fn-{RUN}"
    role_arn = role(f"probe-fn-role-{RUN}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("handler.py", "def handler(event, context):\n    return 'ok'\n")
    client("lambda").create_function(
        FunctionName=name, Runtime="python3.12", Role=role_arn, Handler="handler.handler",
        Code={"ZipFile": buf.getvalue()}, Timeout=3,
    )
    return name, role_arn


def seed_log(function_name: str, message: str) -> int:
    """Write one Lambda log line and return how many events a filter for it finds."""
    logs = client("logs")
    group, stream = f"/aws/lambda/{function_name}", f"probe/{uuid.uuid4().hex[:8]}"
    if error_code(logs.create_log_group, logGroupName=group) not in (None, "ResourceAlreadyExistsException"):
        raise Unsupported("cannot create log group")
    logs.create_log_stream(logGroupName=group, logStreamName=stream)
    logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=[
        {"timestamp": int(time.time() * 1000), "message": message}])
    needle = message.split(":")[0]
    events = logs.filter_log_events(logGroupName=group, filterPattern=f'"{needle}"')["events"]
    return len(events)


def alarm(name: str, metric: str, dims: dict[str, str]) -> None:
    client("cloudwatch").put_metric_alarm(
        AlarmName=name, Namespace="AWS/EC2", MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        Statistic="Average", Period=60, EvaluationPeriods=1, Threshold=80,
        ComparisonOperator="GreaterThanThreshold",
    )


def alarm_state(name: str) -> str:
    return client("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]


# ------------------------------------------------------------------------- EC2

@check("EC2_INSTANCE_STOPPED", "stop an instance, read state")
def _():
    iid = instance()
    client("ec2").stop_instances(InstanceIds=[iid])
    state = wait_for(lambda: instance_state(iid), "stopped")
    if state != "stopped":
        raise Unsupported(f"state is {state!r}")
    return f"{iid} state=stopped"


@check("EC2_INSTANCE_NOT_FOUND", "describe an unknown instance id")
def _():
    code = error_code(client("ec2").describe_instances, InstanceIds=["i-0123456789abcdef0"])
    if code != "InvalidInstanceID.NotFound":
        raise Unsupported(f"error code {code!r}")
    return code


@check("EC2_SG_SSH_BLOCKED", "security group without port 22")
def _():
    sg = security_group([80])
    iid = instance(sg)
    groups = client("ec2").describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["SecurityGroups"]
    if sg not in [g["GroupId"] for g in groups]:
        raise Unsupported("instance does not report its security group")
    if allows(sg, 22) or not allows(sg, 80):
        raise Unsupported("rules read back wrong")
    return f"{iid} → {sg}: 80 open, 22 closed"


@check("EC2_SG_RDP_BLOCKED", "security group without port 3389")
def _():
    sg = security_group([22])
    if allows(sg, 3389) or not allows(sg, 22):
        raise Unsupported("rules read back wrong")
    return f"{sg}: 22 open, 3389 closed"


@check("EC2_HIGH_CPU", "seed AWS/EC2 CPUUtilization")
def _():
    avg = seed_metric("AWS/EC2", "CPUUtilization", {"InstanceId": instance()}, 97.0, "Percent")
    return f"average read back = {avg}"


@check("EC2_HIGH_NETWORK", "seed AWS/EC2 NetworkIn")
def _():
    avg = seed_metric("AWS/EC2", "NetworkIn", {"InstanceId": instance()}, 9.5e9, "Bytes")
    return f"average read back = {avg:.3g}"


@check("EC2_STATUS_CHECK_FAILED", "impaired status via DescribeInstanceStatus")
def _():
    iid = instance()
    statuses = client("ec2").describe_instance_status(InstanceIds=[iid], IncludeAllInstances=True)["InstanceStatuses"]
    status = statuses[0]["InstanceStatus"]["Status"] if statuses else "missing"
    raise Unsupported(f"status is {status!r}; no API to make it impaired")


@check("EC2_STATUS_CHECK_FAILED (metric)", "seed AWS/EC2 StatusCheckFailed = 1")
def _():
    avg = seed_metric("AWS/EC2", "StatusCheckFailed", {"InstanceId": instance()}, 1.0, "Count")
    return f"average read back = {avg}"


# -------------------------------------------------------------------------- S3

@check("S3_BUCKET_NOT_FOUND", "head a missing bucket")
def _():
    code = error_code(client("s3").head_bucket, Bucket=f"probe-missing-{RUN}")
    if code not in ("404", "NoSuchBucket", "NotFound"):
        raise Unsupported(f"error code {code!r}")
    return f"error code {code}"


@check("S3_OBJECT_NOT_FOUND", "head a missing object")
def _():
    code = error_code(client("s3").head_object, Bucket=bucket(), Key="missing.csv")
    if code not in ("404", "NoSuchKey", "NotFound"):
        raise Unsupported(f"error code {code!r}")
    return f"error code {code}"


@check("S3_BUCKET_POLICY_DENY", "bucket policy with explicit Deny, enforced")
def _():
    name = f"probe-deny-{RUN}"
    s3 = client("s3")
    s3.create_bucket(Bucket=name)
    s3.put_object(Bucket=name, Key="report.csv", Body=b"x")
    s3.put_bucket_policy(Bucket=name, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{name}/*"}]}))
    if '"Deny"' not in s3.get_bucket_policy(Bucket=name)["Policy"].replace(" ", ""):
        raise Unsupported("policy not read back")
    user = f"probe-s3-allowed-{RUN}"
    _, key, secret = user_with_key(user)
    client("iam").put_user_policy(UserName=user, PolicyName="s3-all", PolicyDocument=policy("Allow", "s3:*"))
    code = error_code(client("s3", key, secret).get_object, Bucket=name, Key="report.csv")
    return f"policy read back; GetObject as IAM user → {code or 'allowed (not enforced)'}"


@check("S3_IAM_PERMISSION_MISSING", "IAM user with no S3 permission")
def _():
    user = f"probe-s3-nopolicy-{RUN}"
    _, key, secret = user_with_key(user)
    iam = client("iam")
    attached = iam.list_attached_user_policies(UserName=user)["AttachedPolicies"]
    inline = iam.list_user_policies(UserName=user)["PolicyNames"]
    if attached or inline:
        raise Unsupported("new user unexpectedly has policies")
    code = error_code(client("s3", key, secret).list_objects_v2, Bucket=bucket())
    return f"no policies read back; ListObjects as user → {code or 'allowed (not enforced)'}"


# ---------------------------------------------------------------------- Lambda

@check("LAMBDA_FUNCTION_NOT_FOUND", "get an unknown function")
def _():
    code = error_code(client("lambda").get_function, FunctionName=f"probe-missing-{RUN}")
    if code != "ResourceNotFoundException":
        raise Unsupported(f"error code {code!r}")
    return code


@check("LAMBDA_TIMEOUT_TOO_LOW", "3 s timeout plus a timed-out log line")
def _():
    name, _ = function()
    timeout = client("lambda").get_function_configuration(FunctionName=name)["Timeout"]
    found = seed_log(name, "Task timed out after 3.00 seconds: RequestId 1f0e")
    if timeout != 3 or not found:
        raise Unsupported(f"timeout={timeout}, log events found={found}")
    return f"Timeout=3, {found} matching log event(s)"


@check("LAMBDA_RUNTIME_ERROR", "error log line plus AWS/Lambda Errors metric")
def _():
    name, _ = function()
    found = seed_log(name, "[ERROR] KeyError: 'user_id' Traceback (most recent call last)")
    if not found:
        raise Unsupported("log line not read back")
    avg = seed_metric("AWS/Lambda", "Errors", {"FunctionName": name}, 12.0, "Count")
    return f"{found} log event(s); Errors average = {avg}"


@check("LAMBDA_ROLE_PERMISSION_MISSING", "execution role with no policies")
def _():
    name, role_arn = function()
    configured = client("lambda").get_function_configuration(FunctionName=name)["Role"]
    role_name = role_arn.rsplit("/", 1)[1]
    iam = client("iam")
    attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
    inline = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    if configured != role_arn or attached or inline:
        raise Unsupported(f"role={configured}, attached={attached}, inline={inline}")
    return f"role {role_name} has no policies"


# ------------------------------------------------------------------------- IAM

@check("IAM_PERMISSION_MISSING", "user without the needed permission")
def _():
    arn, _, _ = user_with_key(f"probe-iam-missing-{RUN}")
    return f"SimulatePrincipalPolicy(ec2:StartInstances) → {simulate(arn, 'ec2:StartInstances')}"


@check("IAM_EXPLICIT_DENY", "user with an explicit Deny policy")
def _():
    user = f"probe-iam-deny-{RUN}"
    arn, _, _ = user_with_key(user)
    iam = client("iam")
    iam.put_user_policy(UserName=user, PolicyName="allow-all", PolicyDocument=policy("Allow", "*"))
    iam.put_user_policy(UserName=user, PolicyName="deny-s3", PolicyDocument=policy("Deny", "s3:*"))
    document = iam.get_user_policy(UserName=user, PolicyName="deny-s3")["PolicyDocument"]
    if "Deny" not in json.dumps(document):
        raise Unsupported("Deny policy not read back")
    decision = error_code(simulate, arn, "s3:GetObject") or simulate(arn, "s3:GetObject")
    return f"Deny policy read back; SimulatePrincipalPolicy(s3:GetObject) → {decision}"


@check("IAM_WRONG_ROLE", "permission attached to a different role")
def _():
    iam = client("iam")
    used, other = f"probe-used-{RUN}", f"probe-other-{RUN}"
    role(used)
    role(other)
    iam.put_role_policy(RoleName=other, PolicyName="s3-read", PolicyDocument=policy("Allow", "s3:GetObject"))
    used_policies = iam.list_role_policies(RoleName=used)["PolicyNames"]
    other_policies = iam.list_role_policies(RoleName=other)["PolicyNames"]
    if used_policies or other_policies != ["s3-read"]:
        raise Unsupported(f"used={used_policies}, other={other_policies}")
    return "policies of both roles read back"


# ------------------------------------------------------------------ CloudWatch

@check("CW_ALARM_THRESHOLD_BREACHED", "alarm goes to ALARM on seeded data")
def _():
    # FakeCloud evaluates alarms when data is published, so the alarm must exist first.
    dims = {"InstanceId": instance()}
    name = f"probe-high-cpu-{RUN}"
    alarm(name, "CPUUtilization", dims)
    seed_metric("AWS/EC2", "CPUUtilization", dims, 95.0, "Percent")
    state, how = wait_for(lambda: alarm_state(name), "ALARM", 5), "evaluated from data"
    if state != "ALARM":
        client("cloudwatch").set_alarm_state(AlarmName=name, StateValue="ALARM", StateReason="probe")
        state, how = alarm_state(name), "only via SetAlarmState"
    if state != "ALARM":
        raise Unsupported(f"state is {state!r}")
    history = client("cloudwatch").describe_alarm_history(AlarmName=name)["AlarmHistoryItems"]
    return f"ALARM ({how}); {len(history)} history item(s)"


@check("CW_ALARM_INSUFFICIENT_DATA", "alarm on a metric with no data")
def _():
    name = f"probe-no-data-{RUN}"
    alarm(name, "CPUUtilization", {"InstanceId": "i-0nodata000000000"})
    time.sleep(3)
    state = alarm_state(name)
    if state != "INSUFFICIENT_DATA":
        raise Unsupported(f"state is {state!r}")
    return state


# ---------------------------------------------------------------- write tools

@check("WRITE ec2_add_ingress_rule", "add port 22, read it back")
def _():
    sg = security_group([80])
    client("ec2").authorize_security_group_ingress(
        GroupId=sg, IpProtocol="tcp", FromPort=22, ToPort=22, CidrIp="203.0.113.10/32")
    if not allows(sg, 22):
        raise Unsupported("new rule not read back")
    return f"{sg}: 22 now open"


@check("WRITE ec2_stop/start/reboot_instance", "state transitions")
def _():
    iid, ec2 = instance(), client("ec2")
    ec2.stop_instances(InstanceIds=[iid])
    stopped = wait_for(lambda: instance_state(iid), "stopped")
    ec2.start_instances(InstanceIds=[iid])
    started = wait_for(lambda: instance_state(iid), "running")
    ec2.reboot_instances(InstanceIds=[iid])
    rebooted = wait_for(lambda: instance_state(iid), "running")
    if (stopped, started, rebooted) != ("stopped", "running", "running"):
        raise Unsupported(f"stop→{stopped}, start→{started}, reboot→{rebooted}")
    return "stopped → running → running"


@check("WRITE lambda_update_timeout", "raise timeout, read it back")
def _():
    name, _ = function()
    lam = client("lambda")
    lam.update_function_configuration(FunctionName=name, Timeout=30)
    timeout = lam.get_function_configuration(FunctionName=name)["Timeout"]
    if timeout != 30:
        raise Unsupported(f"timeout read back as {timeout}")
    return "Timeout=30"


# ------------------------------------------------------------------- run/reset

def run_checks() -> list[dict]:
    rows = []
    for code, what, fn in CHECKS:
        try:
            rows.append({"code": code, "check": what, "ok": True, "detail": fn()})
        except Unsupported as e:
            rows.append({"code": code, "check": what, "ok": False, "detail": str(e)})
        except Exception as e:  # the probe exists to surface whatever FakeCloud does
            rows.append({"code": code, "check": what, "ok": False, "detail": f"{type(e).__name__}: {e}"[:160]})
    return rows


def reset() -> dict:
    """Reset every service the probe touched; doubles as the check for the reset endpoint."""
    statuses = {
        s: httpx.post(f"{SETTINGS.aws_endpoint_url}/_fakecloud/reset/{s}", timeout=10).status_code
        for s in RESET_SERVICES
    }
    left = sum(len(r["Instances"]) for r in client("ec2").describe_instances()["Reservations"])
    ok = all(code < 300 for code in statuses.values()) and left == 0
    return {"code": "RESET", "check": "POST /_fakecloud/reset/{service}", "ok": ok,
            "detail": f"{statuses}; instances left = {left}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print rows as JSON")
    args = parser.parse_args()

    try:
        health = httpx.get(f"{SETTINGS.aws_endpoint_url}/_fakecloud/health", timeout=5).json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"FakeCloud is not reachable at {SETTINGS.aws_endpoint_url}: {e}", file=sys.stderr)
        return 1

    rows = run_checks()
    rows.append(reset())

    if args.json:
        print(json.dumps({"fakecloud_version": health.get("version"), "rows": rows}, indent=2))
        return 0

    print(f"FakeCloud {health.get('version')} at {SETTINGS.aws_endpoint_url}\n")
    width = max(len(r["code"]) for r in rows)
    for r in rows:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['code']:<{width}}  {r['detail']}")
    passed = sum(r["ok"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
