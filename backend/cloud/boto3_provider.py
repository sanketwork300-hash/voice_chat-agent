import json
from datetime import datetime, timedelta, timezone
from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from backend.cloud.base import (
    Alarm,
    AlarmHistoryItem,
    Bucket,
    CloudError,
    Decision,
    Function,
    IngressRule,
    Instance,
    InstanceStatus,
    LogEvent,
    MetricSummary,
    Principal,
    S3Object,
    SecurityGroup,
    Simulation,
)
from backend.config import Settings

_NOT_FOUND_MARKERS = ("NotFound", "NoSuch", "404")
_DECISIONS: dict[str, Decision] = {
    "allowed": "allowed", "explicitDeny": "explicit_deny", "implicitDeny": "implicit_deny"}


def translate(error: Exception) -> CloudError:
    if isinstance(error, ClientError):
        code = error.response["Error"].get("Code", "")
        message = error.response["Error"].get("Message", "")
        if any(marker in code for marker in _NOT_FOUND_MARKERS):
            return CloudError("not_found", message or code)
        if "AccessDenied" in code or "UnauthorizedOperation" in code:
            return CloudError("access_denied", message or code)
        return CloudError("invalid", f"{code}: {message}")
    if isinstance(error, ReadTimeoutError):
        return CloudError("timeout", "the cloud API did not answer in time")
    if isinstance(error, (EndpointConnectionError, ConnectTimeoutError)):
        return CloudError("unavailable", "the cloud API is not reachable")
    return CloudError("unavailable", str(error))


def _call(operation, *, missing: str | None = None, **kwargs):
    """Run a boto3 operation, translating its errors.

    `missing` names the resource the call is about ("instance i-123"); a
    not-found error is then reported as "<missing> does not exist", so callers
    can tell which resource was missing whatever wording the API used.
    """
    try:
        return operation(**kwargs)
    except (ClientError, BotoCoreError) as e:
        error = translate(e)
        if missing and error.kind == "not_found":
            error = CloudError("not_found", f"{missing} does not exist")
        raise error from e


def _instance(raw: dict) -> Instance:
    tags = {t["Key"]: t["Value"] for t in raw.get("Tags", [])}
    return Instance(
        instance_id=raw["InstanceId"],
        name=tags.get("Name"),
        state=raw["State"]["Name"],
        instance_type=raw.get("InstanceType", "unknown"),
        public_ip=raw.get("PublicIpAddress"),
        private_ip=raw.get("PrivateIpAddress"),
        availability_zone=raw.get("Placement", {}).get("AvailabilityZone"),
        security_group_ids=[g["GroupId"] for g in raw.get("SecurityGroups", [])],
    )


def _ingress(permission: dict) -> IngressRule:
    protocol = permission.get("IpProtocol", "-1")
    sources = [r["CidrIp"] for r in permission.get("IpRanges", [])]
    sources += [r["CidrIpv6"] for r in permission.get("Ipv6Ranges", [])]
    sources += [g["GroupId"] for g in permission.get("UserIdGroupPairs", [])]
    return IngressRule(
        protocol="all" if protocol == "-1" else protocol,
        from_port=permission.get("FromPort"),
        to_port=permission.get("ToPort"),
        sources=sources,
    )


def _trusted_principals(trust_policy) -> list[str]:
    """Who a role's trust policy lets assume the role."""
    if not isinstance(trust_policy, dict):
        return []
    statements = trust_policy.get("Statement", [])
    trusted: list[str] = []
    for statement in [statements] if isinstance(statements, dict) else statements:
        principal = statement.get("Principal") or {}
        if isinstance(principal, str):
            trusted.append(principal)
            continue
        for values in principal.values():
            trusted.extend([values] if isinstance(values, str) else values)
    return trusted


def _alarm(raw: dict) -> Alarm:
    return Alarm(
        name=raw["AlarmName"], state=raw["StateValue"], state_reason=raw.get("StateReason"),
        state_updated=raw.get("StateUpdatedTimestamp"), description=raw.get("AlarmDescription"),
        namespace=raw.get("Namespace"), metric=raw.get("MetricName"),
        dimensions={d["Name"]: d["Value"] for d in raw.get("Dimensions", [])},
        statistic=raw.get("Statistic"), period_seconds=raw.get("Period"),
        evaluation_periods=raw.get("EvaluationPeriods"), threshold=raw.get("Threshold"),
        comparison=raw.get("ComparisonOperator"), actions_enabled=raw.get("ActionsEnabled", True),
    )


def _function(raw: dict) -> Function:
    return Function(
        name=raw["FunctionName"], runtime=raw.get("Runtime"), handler=raw.get("Handler"),
        timeout_seconds=raw["Timeout"], memory_mb=raw["MemorySize"], role_arn=raw["Role"],
        state=raw.get("State"), last_modified=raw.get("LastModified"),
        environment_variables=sorted((raw.get("Environment") or {}).get("Variables", {})),
    )


class Boto3CloudProvider:
    """AWS API access through boto3. `endpoint_url` selects FakeCloud; None means real AWS."""

    def __init__(self, settings: Settings):
        self._session = boto3.session.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        self._endpoint_url = settings.aws_endpoint_url
        self._config = Config(retries={"max_attempts": 2}, connect_timeout=3, read_timeout=8)
        self.client = cache(self._client)

    def _client(self, service: str):
        return self._session.client(service, endpoint_url=self._endpoint_url, config=self._config)

    # --- EC2

    def list_instances(self) -> list[Instance]:
        reservations = _call(self.client("ec2").describe_instances)["Reservations"]
        return [_instance(i) for r in reservations for i in r["Instances"]]

    def get_instance(self, instance_id: str) -> Instance:
        reservations = _call(
            self.client("ec2").describe_instances, missing=f"instance {instance_id}", InstanceIds=[instance_id],
        )["Reservations"]
        if not reservations or not reservations[0]["Instances"]:
            raise CloudError("not_found", f"instance {instance_id} does not exist")
        return _instance(reservations[0]["Instances"][0])

    def get_instance_status(self, instance_id: str) -> InstanceStatus:
        statuses = _call(
            self.client("ec2").describe_instance_status, missing=f"instance {instance_id}",
            InstanceIds=[instance_id], IncludeAllInstances=True,
        )["InstanceStatuses"]
        if not statuses:
            raise CloudError("not_found", f"instance {instance_id} does not exist")
        status = statuses[0]
        return InstanceStatus(
            state=status["InstanceState"]["Name"],
            system_status=status["SystemStatus"]["Status"],
            instance_status=status["InstanceStatus"]["Status"],
        )

    def get_security_groups(self, group_ids: list[str]) -> list[SecurityGroup]:
        if not group_ids:
            return []
        groups = _call(self.client("ec2").describe_security_groups, GroupIds=group_ids)["SecurityGroups"]
        return [
            SecurityGroup(
                group_id=g["GroupId"], name=g.get("GroupName", ""), description=g.get("Description", ""),
                ingress=[_ingress(p) for p in g.get("IpPermissions", [])],
            )
            for g in groups
        ]

    def add_ingress_rule(self, group_id: str, protocol: str, port: int, cidr: str) -> None:
        _call(
            self.client("ec2").authorize_security_group_ingress,
            GroupId=group_id, IpProtocol=protocol, FromPort=port, ToPort=port, CidrIp=cidr,
        )

    def start_instance(self, instance_id: str) -> None:
        _call(self.client("ec2").start_instances, missing=f"instance {instance_id}", InstanceIds=[instance_id])

    def stop_instance(self, instance_id: str) -> None:
        _call(self.client("ec2").stop_instances, missing=f"instance {instance_id}", InstanceIds=[instance_id])

    def reboot_instance(self, instance_id: str) -> None:
        _call(self.client("ec2").reboot_instances, missing=f"instance {instance_id}", InstanceIds=[instance_id])

    # --- CloudWatch

    def get_metric(self, namespace: str, metric: str, dimensions: dict[str, str], minutes: int) -> MetricSummary:
        now = datetime.now(timezone.utc)
        points = _call(
            self.client("cloudwatch").get_metric_statistics,
            Namespace=namespace, MetricName=metric,
            Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
            StartTime=now - timedelta(minutes=minutes), EndTime=now + timedelta(minutes=1),
            Period=300, Statistics=["Average", "Maximum", "Sum"],
        )["Datapoints"]
        if not points:
            return MetricSummary(datapoints=0, average=None, maximum=None)
        return MetricSummary(
            datapoints=len(points),
            average=round(sum(p["Average"] for p in points) / len(points), 2),
            maximum=round(max(p["Maximum"] for p in points), 2),
            total=round(sum(p["Sum"] for p in points), 2),
        )

    def list_alarms(self, state: str | None) -> list[Alarm]:
        kwargs = {"StateValue": state} if state else {}
        return [_alarm(a) for a in _call(self.client("cloudwatch").describe_alarms, **kwargs)["MetricAlarms"]]

    def get_alarm(self, alarm_name: str) -> Alarm:
        alarms = _call(self.client("cloudwatch").describe_alarms, AlarmNames=[alarm_name])["MetricAlarms"]
        if not alarms:
            raise CloudError("not_found", f"alarm {alarm_name} does not exist")
        return _alarm(alarms[0])

    def get_alarm_history(self, alarm_name: str, limit: int) -> list[AlarmHistoryItem]:
        items = _call(
            self.client("cloudwatch").describe_alarm_history, AlarmName=alarm_name, MaxRecords=limit,
        )["AlarmHistoryItems"]
        return [
            AlarmHistoryItem(timestamp=i["Timestamp"], type=i["HistoryItemType"], summary=i["HistorySummary"])
            for i in items]

    # --- Lambda

    def list_functions(self) -> list[Function]:
        return [_function(f) for f in _call(self.client("lambda").list_functions)["Functions"]]

    def get_function(self, function_name: str) -> Function:
        return _function(_call(
            self.client("lambda").get_function_configuration, missing=f"function {function_name}",
            FunctionName=function_name))

    def update_function_timeout(self, function_name: str, timeout_seconds: int) -> None:
        _call(self.client("lambda").update_function_configuration, missing=f"function {function_name}",
              FunctionName=function_name, Timeout=timeout_seconds)

    def get_log_events(self, log_group: str, minutes: int, limit: int) -> list[LogEvent]:
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        try:
            events = _call(
                self.client("logs").filter_log_events, logGroupName=log_group,
                startTime=int(start.timestamp() * 1000), limit=limit)["events"]
        except CloudError as e:
            if e.kind == "not_found":  # the function has never logged anything
                return []
            raise
        return [
            LogEvent(timestamp=datetime.fromtimestamp(e["timestamp"] / 1000, timezone.utc), message=e["message"])
            for e in events]

    # --- S3

    def list_buckets(self) -> list[Bucket]:
        buckets = _call(self.client("s3").list_buckets).get("Buckets", [])
        return [Bucket(name=b["Name"], created=b.get("CreationDate")) for b in buckets]

    def get_bucket(self, bucket: str) -> Bucket:
        head = _call(self.client("s3").head_bucket, missing=f"bucket {bucket}", Bucket=bucket)
        return Bucket(name=bucket, region=head.get("BucketRegion"))

    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[list[S3Object], bool]:
        page = _call(
            self.client("s3").list_objects_v2, missing=f"bucket {bucket}", Bucket=bucket, Prefix=prefix, MaxKeys=limit)
        objects = [
            S3Object(key=o["Key"], size=o["Size"], last_modified=o.get("LastModified"))
            for o in page.get("Contents", [])
        ]
        return objects, bool(page.get("IsTruncated"))

    def get_object_metadata(self, bucket: str, key: str) -> S3Object:
        self.get_bucket(bucket)  # tell a missing bucket apart from a missing object
        head = _call(self.client("s3").head_object, missing=f"object {key}", Bucket=bucket, Key=key)
        return S3Object(
            key=key, size=head["ContentLength"], last_modified=head.get("LastModified"),
            content_type=head.get("ContentType"),
        )

    def get_bucket_policy(self, bucket: str) -> dict | None:
        try:
            return json.loads(_call(self.client("s3").get_bucket_policy, Bucket=bucket)["Policy"])
        except CloudError as e:
            if e.kind != "not_found":
                raise
            # NoSuchBucketPolicy means the bucket has no policy; NoSuchBucket means there is no bucket.
            if "policy" in e.detail.lower():
                return None
            raise CloudError("not_found", f"bucket {bucket} does not exist") from e

    # --- IAM

    def get_user(self, user_name: str) -> Principal:
        iam = self.client("iam")
        user = _call(iam.get_user, missing=f"IAM user {user_name}", UserName=user_name)["User"]
        return Principal(
            kind="user", name=user["UserName"], arn=user["Arn"],
            attached_policies=[
                p["PolicyName"] for p in _call(iam.list_attached_user_policies, UserName=user_name)["AttachedPolicies"]],
            inline_policies=_call(iam.list_user_policies, UserName=user_name)["PolicyNames"],
        )

    def get_role(self, role_name: str) -> Principal:
        iam = self.client("iam")
        role = _call(iam.get_role, missing=f"IAM role {role_name}", RoleName=role_name)["Role"]
        return Principal(
            kind="role", name=role["RoleName"], arn=role["Arn"],
            trusted_by=_trusted_principals(role.get("AssumeRolePolicyDocument")),
            attached_policies=[
                p["PolicyName"] for p in _call(iam.list_attached_role_policies, RoleName=role_name)["AttachedPolicies"]],
            inline_policies=_call(iam.list_role_policies, RoleName=role_name)["PolicyNames"],
        )

    def list_role_names(self) -> list[str]:
        roles = _call(self.client("iam").list_roles)["Roles"]
        # Service-linked roles belong to AWS services, not to the customer's workloads.
        return [r["RoleName"] for r in roles if not r.get("Path", "/").startswith("/aws-service-role/")
                and not r["RoleName"].startswith("AWSServiceRole")]

    def get_policy_document(self, principal: Principal, policy_name: str) -> dict:
        iam = self.client("iam")
        key = {"UserName": principal.name} if principal.kind == "user" else {"RoleName": principal.name}
        if policy_name in principal.inline_policies:
            get_inline = iam.get_user_policy if principal.kind == "user" else iam.get_role_policy
            return _call(get_inline, PolicyName=policy_name, **key)["PolicyDocument"]
        list_attached = iam.list_attached_user_policies if principal.kind == "user" else iam.list_attached_role_policies
        for attached in _call(list_attached, **key)["AttachedPolicies"]:
            if attached["PolicyName"] == policy_name:
                version = _call(iam.get_policy, PolicyArn=attached["PolicyArn"])["Policy"]["DefaultVersionId"]
                return _call(
                    iam.get_policy_version, PolicyArn=attached["PolicyArn"], VersionId=version,
                )["PolicyVersion"]["Document"]
        raise CloudError("not_found", f"policy {policy_name} is not attached to {principal.kind} {principal.name}")

    def simulate_identity_policy(self, principal_arn: str, action: str, resource_arn: str) -> Simulation:
        result = _call(
            self.client("iam").simulate_principal_policy,
            PolicySourceArn=principal_arn, ActionNames=[action], ResourceArns=[resource_arn],
        )["EvaluationResults"][0]
        # Inline policies are reported as "<user|role>_<principal name>_<policy name>".
        kind, name = principal_arn.split(":")[-1].split("/", 1)
        prefix = f"{kind}_{name}_"
        return Simulation(
            decision=_DECISIONS[result["EvalDecision"]],
            matched_policies=[
                m["SourcePolicyId"].removeprefix(prefix) for m in result.get("MatchedStatements", [])],
        )
