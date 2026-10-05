from typing import Literal

from pydantic import BaseModel, Field

from backend.cloud.base import CloudProvider, IngressRule, MetricSummary
from backend.tools.catalog import tool

SSH_PORT = 22
RDP_PORT = 3389
ANYWHERE = "0.0.0.0/0"

InstanceId = Field(pattern=r"^i-[0-9a-f]{8,17}$", description="EC2 instance ID, e.g. i-0abc1234def567890")


class NoInput(BaseModel):
    pass


class InstanceInput(BaseModel):
    instance_id: str = InstanceId


class InstanceSummary(BaseModel):
    instance_id: str
    name: str | None
    state: str
    instance_type: str


class InstanceList(BaseModel):
    instances: list[InstanceSummary]


class InstanceDetail(InstanceSummary):
    public_ip: str | None
    private_ip: str | None
    availability_zone: str | None
    security_group_ids: list[str]


class InstanceStatusOutput(BaseModel):
    instance_id: str
    state: str
    system_status: str
    instance_status: str


class MetricsInput(InstanceInput):
    minutes: int = Field(60, ge=5, le=1440, description="How far back to look")


class InstanceMetrics(BaseModel):
    instance_id: str
    minutes: int
    cpu_utilization_percent: MetricSummary
    network_in_bytes: MetricSummary
    network_out_bytes: MetricSummary
    status_check_failed: MetricSummary


class GroupSummary(BaseModel):
    group_id: str
    name: str
    description: str


class GroupList(BaseModel):
    instance_id: str
    groups: list[GroupSummary]


class GroupRule(IngressRule):
    group_id: str


class GroupRules(BaseModel):
    instance_id: str
    security_group_ids: list[str]
    inbound_rules: list[GroupRule]
    allows_ssh_22: bool
    allows_rdp_3389: bool


class IngressInput(BaseModel):
    group_id: str = Field(pattern=r"^sg-[0-9a-f]{8,17}$")
    port: int = Field(ge=1, le=65535)
    cidr: str = Field(pattern=r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", description="Source range, e.g. 203.0.113.10/32")
    protocol: Literal["tcp", "udp"] = "tcp"


class IngressAdded(BaseModel):
    group_id: str
    protocol: str
    port: int
    cidr: str


class StateChange(BaseModel):
    instance_id: str
    previous_state: str
    requested: Literal["start", "stop", "reboot"]


def allows_port(rules: list[IngressRule], port: int) -> bool:
    """Whether any inbound rule admits TCP traffic on `port`."""
    for rule in rules:
        if rule.protocol == "all":
            return True
        if rule.protocol == "tcp" and rule.from_port is not None and rule.to_port is not None \
                and rule.from_port <= port <= rule.to_port:
            return True
    return False


def _label(instance: InstanceSummary) -> str:
    return instance.name or instance.instance_id


@tool(
    name="ec2_list_instances", service="ec2",
    description="List all EC2 instances in the account with their names and states.",
    input_model=NoInput, output_model=InstanceList,
    activity="Looking up your instances",
    summarize=lambda out: f"Found {len(out.instances)} instance(s)",
)
def list_instances(cloud: CloudProvider, _: NoInput) -> InstanceList:
    return InstanceList(instances=[
        InstanceSummary(instance_id=i.instance_id, name=i.name, state=i.state, instance_type=i.instance_type)
        for i in cloud.list_instances()
    ])


@tool(
    name="ec2_get_instance", service="ec2",
    description="Get one EC2 instance: whether it is running or stopped, its addresses, and its security groups.",
    input_model=InstanceInput, output_model=InstanceDetail,
    activity="Checking the instance",
    summarize=lambda out: f"Instance {_label(out)} is {out.state}",
)
def get_instance(cloud: CloudProvider, args: InstanceInput) -> InstanceDetail:
    return InstanceDetail(**cloud.get_instance(args.instance_id).model_dump())


@tool(
    name="ec2_get_instance_status", service="ec2",
    description="Get the system and instance status checks of one EC2 instance.",
    input_model=InstanceInput, output_model=InstanceStatusOutput,
    activity="Checking instance health checks",
    summarize=lambda out: (
        "Status checks are passing"
        if (out.system_status, out.instance_status) == ("ok", "ok")
        else f"Status checks: system {out.system_status}, instance {out.instance_status}"),
)
def get_instance_status(cloud: CloudProvider, args: InstanceInput) -> InstanceStatusOutput:
    status = cloud.get_instance_status(args.instance_id)
    return InstanceStatusOutput(instance_id=args.instance_id, **status.model_dump())


def _metrics_summary(out: InstanceMetrics) -> str:
    cpu = out.cpu_utilization_percent
    if cpu.average is None:
        return "No recent CPU data"
    return f"Average CPU is {cpu.average:.0f}%"


@tool(
    name="ec2_get_instance_metrics", service="ec2",
    description=(
        "Get recent CloudWatch metrics for one EC2 instance: CPU utilization, network traffic in and out, "
        "and failed status checks (status_check_failed maximum of 1 means a check failed)."),
    input_model=MetricsInput, output_model=InstanceMetrics,
    activity="Checking instance metrics",
    summarize=_metrics_summary,
)
def get_instance_metrics(cloud: CloudProvider, args: MetricsInput) -> InstanceMetrics:
    def metric(name: str) -> MetricSummary:
        return cloud.get_metric("AWS/EC2", name, {"InstanceId": args.instance_id}, args.minutes)

    return InstanceMetrics(
        instance_id=args.instance_id, minutes=args.minutes,
        cpu_utilization_percent=metric("CPUUtilization"),
        network_in_bytes=metric("NetworkIn"),
        network_out_bytes=metric("NetworkOut"),
        status_check_failed=metric("StatusCheckFailed"),
    )


@tool(
    name="ec2_get_security_groups", service="ec2",
    description="List the security groups attached to one EC2 instance.",
    input_model=InstanceInput, output_model=GroupList,
    activity="Looking up the instance's security groups",
    summarize=lambda out: f"{len(out.groups)} security group(s) attached",
)
def get_security_groups(cloud: CloudProvider, args: InstanceInput) -> GroupList:
    instance = cloud.get_instance(args.instance_id)
    groups = cloud.get_security_groups(instance.security_group_ids)
    return GroupList(instance_id=args.instance_id, groups=[
        GroupSummary(group_id=g.group_id, name=g.name, description=g.description) for g in groups])


def _rules_summary(out: GroupRules) -> str:
    ssh = "allowed" if out.allows_ssh_22 else "blocked"
    rdp = "allowed" if out.allows_rdp_3389 else "blocked"
    return f"SSH (port 22) is {ssh}, RDP (port 3389) is {rdp}"


@tool(
    name="ec2_get_security_group_rules", service="ec2",
    description=(
        "Get the inbound firewall rules of the security groups attached to one EC2 instance, "
        "and whether SSH (22) and RDP (3389) are allowed."),
    input_model=InstanceInput, output_model=GroupRules,
    activity="Checking the firewall rules",
    summarize=_rules_summary,
)
def get_security_group_rules(cloud: CloudProvider, args: InstanceInput) -> GroupRules:
    instance = cloud.get_instance(args.instance_id)
    groups = cloud.get_security_groups(instance.security_group_ids)
    rules = [GroupRule(group_id=g.group_id, **r.model_dump()) for g in groups for r in g.ingress]
    return GroupRules(
        instance_id=args.instance_id, security_group_ids=instance.security_group_ids, inbound_rules=rules,
        allows_ssh_22=allows_port(rules, SSH_PORT), allows_rdp_3389=allows_port(rules, RDP_PORT),
    )


@tool(
    name="ec2_add_ingress_rule", service="ec2",
    description="Add an inbound rule to a security group.",
    input_model=IngressInput, output_model=IngressAdded,
    read_only=False, risk_level="medium",
    activity="Adding the firewall rule",
    summarize=lambda out: f"Added {out.protocol.upper()} port {out.port} from {out.cidr}",
)
def add_ingress_rule(cloud: CloudProvider, args: IngressInput) -> IngressAdded:
    cloud.add_ingress_rule(args.group_id, args.protocol, args.port, args.cidr)
    return IngressAdded(**args.model_dump())


def _state_change(cloud: CloudProvider, instance_id: str, requested: Literal["start", "stop", "reboot"]) -> StateChange:
    previous = cloud.get_instance(instance_id).state
    {"start": cloud.start_instance, "stop": cloud.stop_instance, "reboot": cloud.reboot_instance}[requested](instance_id)
    return StateChange(instance_id=instance_id, previous_state=previous, requested=requested)


@tool(
    name="ec2_start_instance", service="ec2", description="Start a stopped EC2 instance.",
    input_model=InstanceInput, output_model=StateChange, read_only=False, risk_level="medium",
    activity="Starting the instance", summarize=lambda out: "Start requested",
)
def start_instance(cloud: CloudProvider, args: InstanceInput) -> StateChange:
    return _state_change(cloud, args.instance_id, "start")


@tool(
    name="ec2_stop_instance", service="ec2", description="Stop a running EC2 instance.",
    input_model=InstanceInput, output_model=StateChange, read_only=False, risk_level="high",
    activity="Stopping the instance", summarize=lambda out: "Stop requested",
)
def stop_instance(cloud: CloudProvider, args: InstanceInput) -> StateChange:
    return _state_change(cloud, args.instance_id, "stop")


@tool(
    name="ec2_reboot_instance", service="ec2", description="Reboot an EC2 instance.",
    input_model=InstanceInput, output_model=StateChange, read_only=False, risk_level="medium",
    activity="Rebooting the instance", summarize=lambda out: "Reboot requested",
)
def reboot_instance(cloud: CloudProvider, args: InstanceInput) -> StateChange:
    return _state_change(cloud, args.instance_id, "reboot")
