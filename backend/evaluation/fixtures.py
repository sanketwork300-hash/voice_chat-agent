"""Builders that put FakeCloud into a known state.

Used by the demo seed and by tests. They call the AWS API directly with admin
credentials; the agent never uses them.
"""

import io
import json
import re
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

AMI = "ami-0abcdef1234567890"
RESET_SERVICES = ("ec2", "s3", "lambda", "iam", "cloudwatch", "logs")

Clients = Callable[[str], Any]  # service name -> boto3 client


def reset(endpoint_url: str, services: tuple[str, ...] = RESET_SERVICES) -> None:
    """Wipe FakeCloud state for the given services."""
    for service in services:
        httpx.post(f"{endpoint_url}/_fakecloud/reset/{service}", timeout=10).raise_for_status()


def publish_metric(clients: Clients, namespace: str, metric: str, dimensions: dict[str, str],
                   value: float, unit: str) -> None:
    """Publish one datapoint stamped now. FakeCloud keeps metrics only in memory."""
    clients("cloudwatch").put_metric_data(Namespace=namespace, MetricData=[{
        "MetricName": metric, "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
        "Timestamp": datetime.now(timezone.utc), "Value": value, "Unit": unit,
    }])


def ec2_instance(clients: Clients, *, name: str, state: str = "running", open_ports: tuple[int, ...] = (80, 443),
                 cpu_percent: float | None = None, network_in_bytes: float | None = None,
                 status_check_failed: bool = False) -> dict[str, str]:
    """An instance behind its own security group. Returns its instance_id and group_id."""
    ec2 = clients("ec2")
    group_id = ec2.create_security_group(GroupName=f"{name}-sg", Description=f"Security group for {name}")["GroupId"]
    for port in open_ports:
        ec2.authorize_security_group_ingress(
            GroupId=group_id, IpProtocol="tcp", FromPort=port, ToPort=port, CidrIp="0.0.0.0/0")
    instance_id = ec2.run_instances(
        ImageId=AMI, InstanceType="t3.micro", MinCount=1, MaxCount=1, SecurityGroupIds=[group_id],
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}],
    )["Instances"][0]["InstanceId"]
    if state == "stopped":
        ec2.stop_instances(InstanceIds=[instance_id])

    dimensions = {"InstanceId": instance_id}
    if cpu_percent is not None:
        publish_metric(clients, "AWS/EC2", "CPUUtilization", dimensions, cpu_percent, "Percent")
    if network_in_bytes is not None:
        publish_metric(clients, "AWS/EC2", "NetworkIn", dimensions, network_in_bytes, "Bytes")
    if status_check_failed:
        publish_metric(clients, "AWS/EC2", "StatusCheckFailed", dimensions, 1, "Count")
    return {"instance_id": instance_id, "group_id": group_id}


def s3_bucket(clients: Clients, *, name: str, objects: tuple[str, ...] = (), deny_downloads: bool = False) -> None:
    """A bucket with some objects, optionally with a policy that denies every download."""
    s3 = clients("s3")
    s3.create_bucket(Bucket=name)
    for key in objects:
        s3.put_object(Bucket=name, Key=key, Body=b"sample data\n", ContentType="text/plain")
    if deny_downloads:
        s3.put_bucket_policy(Bucket=name, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "DenyDownloads", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{name}/*"}]}))


def iam_user(clients: Clients, *, name: str, s3_access: bool) -> None:
    """An IAM user, with or without permission to use S3."""
    iam = clients("iam")
    iam.create_user(UserName=name)
    if s3_access:
        iam.put_user_policy(UserName=name, PolicyName="s3-full-access", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))


def _policy(effect: str, actions: tuple[str, ...]) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": effect, "Action": list(actions), "Resource": "*"}]})


def iam_principal(clients: Clients, *, kind: str, name: str, allow: tuple[str, ...] = (),
                  deny: tuple[str, ...] = (), trusted_service: str = "lambda.amazonaws.com") -> str:
    """An IAM user or role with optional inline policies allowing and denying actions. Returns its ARN."""
    iam = clients("iam")
    if kind == "user":
        arn = iam.create_user(UserName=name)["User"]["Arn"]
        put_policy, key = iam.put_user_policy, {"UserName": name}
    else:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": trusted_service}, "Action": "sts:AssumeRole"}]}),
        )["Role"]["Arn"]
        put_policy, key = iam.put_role_policy, {"RoleName": name}
    if allow:
        put_policy(PolicyName="allow", PolicyDocument=_policy("Allow", allow), **key)
    if deny:
        put_policy(PolicyName="deny", PolicyDocument=_policy("Deny", deny), **key)
    return arn


def lambda_function(clients: Clients, *, name: str, role_arn: str, timeout: int = 30, memory: int = 128,
                    log_lines: tuple[str, ...] = (), errors: int = 0, invocations: int = 0,
                    max_duration_ms: float | None = None) -> None:
    """A Lambda function with seeded logs and metrics. FakeCloud never invokes it."""
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("handler.py", "def handler(event, context):\n    return 'ok'\n")
    clients("lambda").create_function(
        FunctionName=name, Runtime="python3.12", Role=role_arn, Handler="handler.handler",
        Code={"ZipFile": package.getvalue()}, Timeout=timeout, MemorySize=memory)

    if log_lines:
        logs = clients("logs")
        group, stream = f"/aws/lambda/{name}", "2026/10/01/[$LATEST]seeded"
        logs.create_log_group(logGroupName=group)
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
        now = int(time.time() * 1000)
        logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=[
            {"timestamp": now - 1000 * (len(log_lines) - i), "message": line} for i, line in enumerate(log_lines)])

    dimensions = {"FunctionName": name}
    if invocations:
        publish_metric(clients, "AWS/Lambda", "Invocations", dimensions, invocations, "Count")
    if errors:
        publish_metric(clients, "AWS/Lambda", "Errors", dimensions, errors, "Count")
    if max_duration_ms is not None:
        publish_metric(clients, "AWS/Lambda", "Duration", dimensions, max_duration_ms, "Milliseconds")


def cpu_alarm(clients: Clients, *, name: str, instance_id: str, threshold: float = 80,
              cpu_percent: float | None = None) -> None:
    """A CPU alarm on an instance. With `cpu_percent`, a datapoint is published so the alarm leaves INSUFFICIENT_DATA."""
    dimensions = {"InstanceId": instance_id}
    clients("cloudwatch").put_metric_alarm(
        AlarmName=name, AlarmDescription=f"CPU above {threshold:g}% on {instance_id}",
        Namespace="AWS/EC2", MetricName="CPUUtilization",
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        Statistic="Average", Period=300, EvaluationPeriods=1, Threshold=threshold,
        ComparisonOperator="GreaterThanThreshold")
    if cpu_percent is None:
        return
    # FakeCloud evaluates an alarm when a datapoint arrives, so the alarm has to exist first.
    # Once during development an alarm stayed in INSUFFICIENT_DATA after its datapoint was
    # published, so the state is checked and the datapoint published again if needed.
    for attempt in range(3):
        publish_metric(clients, "AWS/EC2", "CPUUtilization", dimensions, cpu_percent, "Percent")
        state = clients("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]
        if state != "INSUFFICIENT_DATA":
            return
        time.sleep(1)
    raise RuntimeError(f"alarm {name} did not evaluate its metric (still INSUFFICIENT_DATA)")


# ------------------------------------------------------- scenario fixtures
#
# A scenario lists builder calls by name. Each call may be given a handle; later
# calls, the customer's messages, and the expected values can refer to what the
# builder returned as "{handle.field}" (for example "{web.instance_id}").

def _named(builder: Callable[..., Any]) -> Callable[..., dict]:
    """Wrap a builder so it returns a dict of handles that always includes the resource name."""
    def build(clients: Clients, **params: Any) -> dict:
        result = builder(clients, **params)
        handles = dict(result) if isinstance(result, dict) else {"arn": result} if isinstance(result, str) else {}
        return {"name": params["name"], **handles}
    return build


BUILDERS: dict[str, Callable[..., dict]] = {
    "ec2_instance": _named(ec2_instance),
    "s3_bucket": _named(s3_bucket),
    "iam_user": _named(iam_user),
    "iam_principal": _named(iam_principal),
    "lambda_function": _named(lambda_function),
    "cpu_alarm": _named(cpu_alarm),
}

_PLACEHOLDER = re.compile(r"\{(\w+)\.(\w+)\}")


def fill(value: Any, handles: dict[str, dict]) -> Any:
    """Replace "{handle.field}" placeholders in strings, recursively through lists and dicts."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: str(handles[m.group(1)][m.group(2)]), value)
    if isinstance(value, list):
        return [fill(v, handles) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, handles) for k, v in value.items()}
    return value


def apply(clients: Clients, calls: list[dict]) -> dict[str, dict]:
    """Run a scenario's builder calls in order and return what each one created, by handle."""
    handles: dict[str, dict] = {}
    for call in calls:
        handles[call.get("handle", call["name"])] = BUILDERS[call["name"]](clients, **fill(call["params"], handles))
    return handles


def post_condition_holds(cloud: Any, condition: dict) -> bool:
    """Read FakeCloud directly (not through the agent) to see whether a fix was, or was not, applied.

    `cloud` is a CloudProvider with admin credentials. `condition` has a "type" and
    the values to compare; "{handle.field}" placeholders must already be filled in.
    """
    kind = condition["type"]
    if kind == "ingress_rule":
        group = cloud.get_security_groups([condition["group_id"]])[0]
        present = any(
            rule.protocol == "tcp" and rule.from_port == condition["port"] == rule.to_port
            and condition["cidr"] in rule.sources
            for rule in group.ingress)
        return present == condition["present"]
    if kind == "instance_state":
        return cloud.get_instance(condition["instance_id"]).state in condition["states"]
    if kind == "lambda_timeout":
        return cloud.get_function(condition["function_name"]).timeout_seconds == condition["seconds"]
    raise ValueError(f"unknown post-condition type {kind!r}")
