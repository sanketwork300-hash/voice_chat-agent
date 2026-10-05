"""The cloud operations the tools need, independent of where they run.

Tools depend only on `CloudProvider`. The single implementation talks to the AWS
API through boto3, which FakeCloud also speaks, so pointing it at real AWS is a
configuration change.
"""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

ErrorKind = Literal["not_found", "access_denied", "timeout", "unavailable", "invalid"]
Decision = Literal["allowed", "explicit_deny", "implicit_deny"]


class CloudError(Exception):
    def __init__(self, kind: ErrorKind, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class Instance(BaseModel):
    instance_id: str
    name: str | None
    state: str
    instance_type: str
    public_ip: str | None
    private_ip: str | None
    availability_zone: str | None
    security_group_ids: list[str]


class InstanceStatus(BaseModel):
    state: str
    system_status: str
    instance_status: str


class MetricSummary(BaseModel):
    """`average`, `maximum`, and `total` are None when the metric has no datapoints in the window."""

    datapoints: int
    average: float | None
    maximum: float | None
    total: float | None = None


class IngressRule(BaseModel):
    protocol: str  # "tcp", "udp", "icmp", or "all"
    from_port: int | None
    to_port: int | None
    sources: list[str]


class SecurityGroup(BaseModel):
    group_id: str
    name: str
    description: str
    ingress: list[IngressRule]


class Bucket(BaseModel):
    name: str
    region: str | None = None
    created: datetime | None = None


class S3Object(BaseModel):
    key: str
    size: int
    last_modified: datetime | None = None
    content_type: str | None = None


class Function(BaseModel):
    name: str
    runtime: str | None
    handler: str | None
    timeout_seconds: int
    memory_mb: int
    role_arn: str
    state: str | None
    last_modified: str | None
    environment_variables: list[str]  # names only; values may be secrets


class LogEvent(BaseModel):
    timestamp: datetime
    message: str


class Principal(BaseModel):
    kind: Literal["user", "role"]
    name: str
    arn: str
    attached_policies: list[str]
    inline_policies: list[str]
    trusted_by: list[str] = []  # roles only: who may assume the role


class Simulation(BaseModel):
    decision: Decision
    matched_policies: list[str]  # the policies whose statements decided the result


class Alarm(BaseModel):
    name: str
    state: str  # OK, ALARM, or INSUFFICIENT_DATA
    state_reason: str | None
    state_updated: datetime | None
    description: str | None
    namespace: str | None
    metric: str | None
    dimensions: dict[str, str]
    statistic: str | None
    period_seconds: int | None
    evaluation_periods: int | None
    threshold: float | None
    comparison: str | None
    actions_enabled: bool


class AlarmHistoryItem(BaseModel):
    timestamp: datetime
    type: str
    summary: str


class CloudProvider(Protocol):
    # EC2
    def list_instances(self) -> list[Instance]: ...
    def get_instance(self, instance_id: str) -> Instance: ...
    def get_instance_status(self, instance_id: str) -> InstanceStatus: ...
    def get_security_groups(self, group_ids: list[str]) -> list[SecurityGroup]: ...
    def add_ingress_rule(self, group_id: str, protocol: str, port: int, cidr: str) -> None: ...
    def start_instance(self, instance_id: str) -> None: ...
    def stop_instance(self, instance_id: str) -> None: ...
    def reboot_instance(self, instance_id: str) -> None: ...

    # CloudWatch
    def get_metric(self, namespace: str, metric: str, dimensions: dict[str, str], minutes: int) -> MetricSummary: ...
    def list_alarms(self, state: str | None) -> list[Alarm]: ...
    def get_alarm(self, alarm_name: str) -> Alarm: ...
    def get_alarm_history(self, alarm_name: str, limit: int) -> list[AlarmHistoryItem]: ...

    # Lambda
    def list_functions(self) -> list[Function]: ...
    def get_function(self, function_name: str) -> Function: ...
    def update_function_timeout(self, function_name: str, timeout_seconds: int) -> None: ...
    def get_log_events(self, log_group: str, minutes: int, limit: int) -> list[LogEvent]: ...

    # S3
    def list_buckets(self) -> list[Bucket]: ...
    def get_bucket(self, bucket: str) -> Bucket: ...
    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[list[S3Object], bool]: ...
    def get_object_metadata(self, bucket: str, key: str) -> S3Object: ...
    def get_bucket_policy(self, bucket: str) -> dict | None: ...

    # IAM
    def get_user(self, user_name: str) -> Principal: ...
    def get_role(self, role_name: str) -> Principal: ...
    def list_role_names(self) -> list[str]: ...
    def get_policy_document(self, principal: Principal, policy_name: str) -> dict: ...
    def simulate_identity_policy(self, principal_arn: str, action: str, resource_arn: str) -> Simulation: ...
