"""Tables that drive the agent: intents, slots, root causes, and resolutions.

Which details an intent needs, which resolution fits a root cause, and which
write tool implements a resolution are decided here, not by the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.state import SupportState


class Service(StrEnum):
    EC2 = "ec2"
    S3 = "s3"
    LAMBDA = "lambda"
    IAM = "iam"
    CLOUDWATCH = "cloudwatch"
    BILLING = "billing"


# How a service is written in messages to the customer.
SERVICE_NAMES: dict[Service, str] = {
    Service.EC2: "EC2", Service.S3: "S3", Service.LAMBDA: "Lambda", Service.IAM: "IAM",
    Service.CLOUDWATCH: "CloudWatch", Service.BILLING: "billing",
}


class Intent(StrEnum):
    EC2_CONNECTIVITY = "ec2_connectivity"
    EC2_INSTANCE_DOWN = "ec2_instance_down"
    EC2_HIGH_CPU = "ec2_high_cpu"
    EC2_SECURITY_GROUP = "ec2_security_group"
    EC2_NETWORKING = "ec2_networking"
    S3_ACCESS_DENIED = "s3_access_denied"
    S3_BUCKET_NOT_FOUND = "s3_bucket_not_found"
    S3_UPLOAD_FAILURE = "s3_upload_failure"
    S3_DOWNLOAD_FAILURE = "s3_download_failure"
    LAMBDA_TIMEOUT = "lambda_timeout"
    LAMBDA_INVOCATION_FAILURE = "lambda_invocation_failure"
    LAMBDA_PERMISSION = "lambda_permission"
    IAM_ACCESS_DENIED = "iam_access_denied"
    IAM_POLICY_PROBLEM = "iam_policy_problem"
    CLOUDWATCH_ALARM = "cloudwatch_alarm"
    CLOUDWATCH_METRIC = "cloudwatch_metric"
    BILLING_BASIC = "billing_basic"
    UNKNOWN = "unknown"


class RootCause(StrEnum):
    EC2_INSTANCE_STOPPED = "EC2_INSTANCE_STOPPED"
    EC2_INSTANCE_NOT_FOUND = "EC2_INSTANCE_NOT_FOUND"
    EC2_SG_SSH_BLOCKED = "EC2_SG_SSH_BLOCKED"
    EC2_SG_RDP_BLOCKED = "EC2_SG_RDP_BLOCKED"
    EC2_HIGH_CPU = "EC2_HIGH_CPU"
    EC2_HIGH_NETWORK = "EC2_HIGH_NETWORK"
    EC2_STATUS_CHECK_FAILED = "EC2_STATUS_CHECK_FAILED"
    S3_BUCKET_NOT_FOUND = "S3_BUCKET_NOT_FOUND"
    S3_OBJECT_NOT_FOUND = "S3_OBJECT_NOT_FOUND"
    S3_BUCKET_POLICY_DENY = "S3_BUCKET_POLICY_DENY"
    S3_IAM_PERMISSION_MISSING = "S3_IAM_PERMISSION_MISSING"
    LAMBDA_FUNCTION_NOT_FOUND = "LAMBDA_FUNCTION_NOT_FOUND"
    LAMBDA_TIMEOUT_TOO_LOW = "LAMBDA_TIMEOUT_TOO_LOW"
    LAMBDA_RUNTIME_ERROR = "LAMBDA_RUNTIME_ERROR"
    LAMBDA_ROLE_PERMISSION_MISSING = "LAMBDA_ROLE_PERMISSION_MISSING"
    IAM_PERMISSION_MISSING = "IAM_PERMISSION_MISSING"
    IAM_EXPLICIT_DENY = "IAM_EXPLICIT_DENY"
    IAM_WRONG_ROLE = "IAM_WRONG_ROLE"
    CW_ALARM_NOT_FOUND = "CW_ALARM_NOT_FOUND"
    CW_ALARM_THRESHOLD_BREACHED = "CW_ALARM_THRESHOLD_BREACHED"
    CW_ALARM_INSUFFICIENT_DATA = "CW_ALARM_INSUFFICIENT_DATA"
    CW_ALARM_OK = "CW_ALARM_OK"
    NO_FAULT_FOUND = "NO_FAULT_FOUND"


class Resolution(StrEnum):
    START_INSTANCE = "START_INSTANCE"
    CHECK_INSTANCE_ID = "CHECK_INSTANCE_ID"
    ADD_INGRESS_RULE = "ADD_INGRESS_RULE"
    INVESTIGATE_CPU = "INVESTIGATE_CPU"
    INVESTIGATE_NETWORK = "INVESTIGATE_NETWORK"
    REBOOT_INSTANCE = "REBOOT_INSTANCE"
    CHECK_BUCKET_NAME = "CHECK_BUCKET_NAME"
    CHECK_OBJECT_KEY = "CHECK_OBJECT_KEY"
    UPDATE_BUCKET_POLICY = "UPDATE_BUCKET_POLICY"
    GRANT_S3_PERMISSION = "GRANT_S3_PERMISSION"
    CHECK_FUNCTION_NAME = "CHECK_FUNCTION_NAME"
    INCREASE_LAMBDA_TIMEOUT = "INCREASE_LAMBDA_TIMEOUT"
    FIX_FUNCTION_ERROR = "FIX_FUNCTION_ERROR"
    GRANT_LAMBDA_PERMISSION = "GRANT_LAMBDA_PERMISSION"
    GRANT_IAM_PERMISSION = "GRANT_IAM_PERMISSION"
    REMOVE_EXPLICIT_DENY = "REMOVE_EXPLICIT_DENY"
    USE_CORRECT_ROLE = "USE_CORRECT_ROLE"
    CHECK_ALARM_NAME = "CHECK_ALARM_NAME"
    INVESTIGATE_ALARM_METRIC = "INVESTIGATE_ALARM_METRIC"
    CHECK_METRIC_SOURCE = "CHECK_METRIC_SOURCE"
    NO_ACTION_NEEDED = "NO_ACTION_NEEDED"


# ------------------------------------------------------------------- intents

@dataclass(frozen=True)
class IntentSpec:
    service: Service | None
    description: str  # shown to the classifier
    slots: tuple[str, ...] = ()
    checklist: str = ""  # shown to the model while it chooses tools
    causes: tuple[RootCause, ...] = ()  # root causes that can explain this problem
    # The checks that must have run before "no fault found" may be concluded, as
    # (tool, arguments). The model chooses tools first; the agent runs whatever is
    # still missing from this list, so a skipped check never causes a wrong conclusion.
    checks: tuple[tuple[str, dict], ...] = ()


INTENTS: dict[Intent, IntentSpec] = {
    Intent.EC2_CONNECTIVITY: IntentSpec(
        Service.EC2, "cannot connect to an EC2 instance or server (SSH, RDP, unreachable, connection timeout)",
        slots=("instance", "protocol"),
        checklist="Check that the instance is running, then check the inbound firewall rules for the port in use.",
        causes=(RootCause.EC2_INSTANCE_NOT_FOUND, RootCause.EC2_INSTANCE_STOPPED, RootCause.EC2_SG_SSH_BLOCKED, RootCause.EC2_SG_RDP_BLOCKED, RootCause.EC2_STATUS_CHECK_FAILED,),
        checks=(("ec2_get_instance", {}), ("ec2_get_security_group_rules", {}), ("ec2_get_instance_status", {}), ("ec2_get_instance_metrics", {}))),
    Intent.EC2_INSTANCE_DOWN: IntentSpec(
        Service.EC2, "an EC2 instance or server is down, stopped, or failing its health checks",
        slots=("instance",),
        checklist="Check the instance state, then its status checks and the status_check_failed metric.",
        causes=(RootCause.EC2_INSTANCE_NOT_FOUND, RootCause.EC2_INSTANCE_STOPPED, RootCause.EC2_STATUS_CHECK_FAILED,),
        checks=(("ec2_get_instance", {}), ("ec2_get_instance_status", {}), ("ec2_get_instance_metrics", {}))),
    Intent.EC2_HIGH_CPU: IntentSpec(
        Service.EC2, "an EC2 instance is slow or has high CPU usage",
        slots=("instance",),
        checklist="Check that the instance is running, then check its metrics.",
        causes=(RootCause.EC2_INSTANCE_NOT_FOUND, RootCause.EC2_INSTANCE_STOPPED, RootCause.EC2_HIGH_CPU, RootCause.EC2_STATUS_CHECK_FAILED,),
        checks=(("ec2_get_instance", {}), ("ec2_get_instance_metrics", {}))),
    Intent.EC2_SECURITY_GROUP: IntentSpec(
        Service.EC2, "a problem with the security group or firewall rules of an EC2 instance",
        slots=("instance", "protocol"),
        checklist="Check the instance, then the inbound firewall rules.",
        causes=(RootCause.EC2_INSTANCE_NOT_FOUND, RootCause.EC2_SG_SSH_BLOCKED, RootCause.EC2_SG_RDP_BLOCKED,),
        checks=(("ec2_get_instance", {}), ("ec2_get_security_group_rules", {}))),
    Intent.EC2_NETWORKING: IntentSpec(
        Service.EC2, "unusually high network traffic or network slowness on an EC2 instance",
        slots=("instance",),
        checklist="Check that the instance is running, then check its metrics.",
        causes=(RootCause.EC2_INSTANCE_NOT_FOUND, RootCause.EC2_INSTANCE_STOPPED, RootCause.EC2_HIGH_NETWORK, RootCause.EC2_HIGH_CPU,),
        checks=(("ec2_get_instance", {}), ("ec2_get_instance_metrics", {}))),
    Intent.S3_ACCESS_DENIED: IntentSpec(
        Service.S3, "AccessDenied, 403, or a permission error when using an S3 bucket (choose this whenever a bucket is involved)",
        slots=("bucket", "iam_user"),
        checklist="Check that the bucket exists, then check the user's access to it.",
        causes=(RootCause.S3_BUCKET_NOT_FOUND, RootCause.S3_BUCKET_POLICY_DENY, RootCause.S3_IAM_PERMISSION_MISSING,),
        checks=(("s3_get_bucket", {}), ("s3_check_bucket_access", {"action": "read"}), ("s3_check_bucket_access", {"action": "write"}))),
    Intent.S3_BUCKET_NOT_FOUND: IntentSpec(
        Service.S3, "an S3 bucket cannot be found (NoSuchBucket, 404)",
        slots=("bucket",),
        checklist="Check whether the bucket exists. If it does not, list the buckets.",
        causes=(RootCause.S3_BUCKET_NOT_FOUND,),
        checks=(("s3_get_bucket", {}),)),
    Intent.S3_UPLOAD_FAILURE: IntentSpec(
        Service.S3, "cannot upload or write objects to an S3 bucket",
        slots=("bucket", "iam_user"),
        checklist="Check that the bucket exists, then check the user's write access.",
        causes=(RootCause.S3_BUCKET_NOT_FOUND, RootCause.S3_BUCKET_POLICY_DENY, RootCause.S3_IAM_PERMISSION_MISSING,),
        checks=(("s3_get_bucket", {}), ("s3_check_bucket_access", {"action": "write"}))),
    Intent.S3_DOWNLOAD_FAILURE: IntentSpec(
        Service.S3, "cannot download or read a specific object or file from an S3 bucket",
        slots=("bucket", "object_key", "iam_user"),
        checklist="Check that the bucket and the object exist, then check the user's read access.",
        causes=(RootCause.S3_BUCKET_NOT_FOUND, RootCause.S3_OBJECT_NOT_FOUND, RootCause.S3_BUCKET_POLICY_DENY, RootCause.S3_IAM_PERMISSION_MISSING,),
        checks=(("s3_get_bucket", {}), ("s3_get_object_metadata", {}), ("s3_check_bucket_access", {"action": "read"}))),
    Intent.LAMBDA_TIMEOUT: IntentSpec(
        Service.LAMBDA, "a Lambda function times out or runs too long",
        slots=("function",),
        checklist="Check the function's configured timeout, then read its logs and metrics.",
        causes=(RootCause.LAMBDA_FUNCTION_NOT_FOUND, RootCause.LAMBDA_TIMEOUT_TOO_LOW, RootCause.LAMBDA_RUNTIME_ERROR,
                RootCause.LAMBDA_ROLE_PERMISSION_MISSING),
        checks=(("lambda_get_function", {}), ("lambda_get_function_configuration", {}), ("lambda_get_function_logs", {}), ("lambda_get_function_metrics", {}))),
    Intent.LAMBDA_INVOCATION_FAILURE: IntentSpec(
        Service.LAMBDA, "a Lambda function fails, crashes, or returns errors",
        slots=("function",),
        checklist="Check that the function exists, then read its logs and metrics.",
        causes=(RootCause.LAMBDA_FUNCTION_NOT_FOUND, RootCause.LAMBDA_RUNTIME_ERROR, RootCause.LAMBDA_TIMEOUT_TOO_LOW,
                RootCause.LAMBDA_ROLE_PERMISSION_MISSING),
        checks=(("lambda_get_function", {}), ("lambda_get_function_configuration", {}), ("lambda_get_function_logs", {}), ("lambda_get_function_metrics", {}))),
    Intent.LAMBDA_PERMISSION: IntentSpec(
        Service.LAMBDA, "a Lambda function is denied access to another AWS resource",
        slots=("function",),
        checklist="Check the function and the policies of its execution role, then read its logs.",
        causes=(RootCause.LAMBDA_FUNCTION_NOT_FOUND, RootCause.LAMBDA_ROLE_PERMISSION_MISSING,
                RootCause.LAMBDA_RUNTIME_ERROR, RootCause.LAMBDA_TIMEOUT_TOO_LOW),
        checks=(("lambda_get_function", {}), ("lambda_get_function_configuration", {}), ("lambda_get_function_logs", {}), ("lambda_get_function_metrics", {}))),
    Intent.IAM_ACCESS_DENIED: IntentSpec(
        Service.IAM, "an IAM user or role gets AccessDenied for an action that does not involve an S3 bucket",
        slots=("principal", "iam_action"),
        checklist="Check the user or role and its policies, then check the permission for the denied action.",
        causes=(RootCause.IAM_EXPLICIT_DENY, RootCause.IAM_WRONG_ROLE, RootCause.IAM_PERMISSION_MISSING),
        checks=(("iam_get_user", {}), ("iam_get_role", {}), ("iam_check_permission", {}))),
    Intent.IAM_POLICY_PROBLEM: IntentSpec(
        Service.IAM, "an IAM policy does not allow or deny what the customer expects",
        slots=("principal", "iam_action"),
        checklist="Check the user or role and its policies, then check the permission for the action.",
        causes=(RootCause.IAM_EXPLICIT_DENY, RootCause.IAM_WRONG_ROLE, RootCause.IAM_PERMISSION_MISSING),
        checks=(("iam_get_user", {}), ("iam_get_role", {}), ("iam_check_permission", {}))),
    Intent.CLOUDWATCH_ALARM: IntentSpec(
        Service.CLOUDWATCH, "a CloudWatch alarm fired, is in an unexpected state, or needs explaining",
        slots=("alarm",),
        checklist="Check the alarm's state and reason, then its history and the metric it watches.",
        causes=(RootCause.CW_ALARM_NOT_FOUND, RootCause.CW_ALARM_THRESHOLD_BREACHED,
                RootCause.CW_ALARM_INSUFFICIENT_DATA, RootCause.CW_ALARM_OK),
        checks=(("cloudwatch_get_alarm", {}), ("cloudwatch_get_alarm_history", {}), ("cloudwatch_get_metric", {}))),
    Intent.CLOUDWATCH_METRIC: IntentSpec(
        Service.CLOUDWATCH, "the metric behind a CloudWatch alarm is missing or has an unexpected value",
        slots=("alarm",),
        checklist="Check the alarm, then the metric it watches.",
        causes=(RootCause.CW_ALARM_NOT_FOUND, RootCause.CW_ALARM_INSUFFICIENT_DATA,
                RootCause.CW_ALARM_THRESHOLD_BREACHED, RootCause.CW_ALARM_OK),
        checks=(("cloudwatch_get_alarm", {}), ("cloudwatch_get_metric", {}), ("cloudwatch_get_alarm_history", {}))),
    Intent.BILLING_BASIC: IntentSpec(Service.BILLING, "a basic question about AWS charges or the bill"),
    Intent.UNKNOWN: IntentSpec(
        None, "anything else, including other clouds (Azure, GCP) and unsupported AWS services such as RDS"),
}

SLOT_QUESTIONS: dict[str, str] = {
    "instance": "What's the instance ID or name?",
    "protocol": "Are you connecting with SSH or RDP?",
    "bucket": "What's the name of the bucket?",
    "object_key": "What's the key (path) of the object?",
    "iam_user": "Which IAM user is making the request?",
    "source_cidr": (
        "Which IP address will you connect from? I'll allow only that address rather than opening the port to everyone."),
    "function": "What's the name of the Lambda function?",
    "principal": "Which IAM user or role is affected? For example: user alice, or role app-role.",
    "iam_action": (
        "Which action is being denied? Please give it as it appears in the error, for example ec2:StartInstances."),
    "alarm": "What's the name of the alarm?",
    "timeout_seconds": "I can raise the timeout for you. What should it be, in seconds? The maximum is 900.",
}

# What the customer supplies for a slot is stored under this entity key.
SLOT_ENTITY: dict[str, str] = {
    "instance": "instance_id",
    "function": "function_name",
    "alarm": "alarm_name",
    # A principal is a user or a role; it counts once the agent has confirmed which one exists.
    "principal": "principal_checked",
}

# Entities that name the resource under investigation. If one changes, an earlier diagnosis no longer applies.
RESOURCE_ENTITIES = frozenset({
    "instance_ref", "bucket", "object_key", "iam_user", "iam_role", "iam_action", "function_name", "alarm_name"})


def slot_filled(slot: str, entities: dict[str, str]) -> bool:
    return bool(entities.get(SLOT_ENTITY.get(slot, slot)))


# ---------------------------------------------------------------- resolutions

@dataclass(frozen=True)
class WriteAction:
    """How a resolution is carried out and checked. All callables read tool results from state."""

    tool: str
    build_args: Callable[[SupportState], dict]
    summarize: Callable[[SupportState, dict], str]  # what the customer is asked to confirm
    verify_tool: str  # read tool that is re-run after the change
    verify_ok: Callable[[dict, dict], bool]  # (action args, re-read result) -> change took effect
    verified_text: Callable[[SupportState, dict], str]
    slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolutionSpec:
    advice: str  # customer language; what should be done
    write: WriteAction | None = None


def _port(state: SupportState) -> int:
    return 3389 if state.root_cause_code is RootCause.EC2_SG_RDP_BLOCKED else 22


def _port_name(port: int) -> str:
    return {22: "SSH", 3389: "RDP"}[port]


def _ingress_args(state: SupportState) -> dict:
    rules = state.latest_result("ec2_get_security_group_rules")
    return {
        "group_id": rules["security_group_ids"][0],
        "port": _port(state),
        "cidr": state.entities["source_cidr"],
        "protocol": "tcp",
    }


def _ingress_summary(state: SupportState, args: dict) -> str:
    scope = "any address on the internet" if args["cidr"] == "0.0.0.0/0" else args["cidr"]
    return (f"Add an inbound rule to security group {args['group_id']} allowing "
            f"{_port_name(args['port'])} (TCP port {args['port']}) from {scope}.")


def _instance_args(state: SupportState) -> dict:
    return {"instance_id": state.entities["instance_id"]}


def _instance_name(state: SupportState) -> str:
    return state.entities.get("instance_name") or state.entities["instance_id"]


def _timeout_summary(state: SupportState, args: dict) -> str:
    current = state.latest_result("lambda_get_function_configuration")
    change = f"from {current['timeout_seconds']} to {args['timeout_seconds']}" if current \
        else f"to {args['timeout_seconds']}"
    return f"Change the timeout of function {args['function_name']} {change} seconds."


RESOLUTIONS: dict[Resolution, ResolutionSpec] = {
    Resolution.ADD_INGRESS_RULE: ResolutionSpec(
        advice="Allow the port in the instance's security group, only from the address you connect from.",
        write=WriteAction(
            tool="ec2_add_ingress_rule", slots=("source_cidr",),
            build_args=_ingress_args, summarize=_ingress_summary,
            verify_tool="ec2_get_security_group_rules",
            verify_ok=lambda args, result: result["allows_ssh_22" if args["port"] == 22 else "allows_rdp_3389"],
            verified_text=lambda state, args: (
                f"{_port_name(args['port'])} on port {args['port']} is now allowed from {args['cidr']}, "
                "and I've confirmed the rule is in place."),
        )),
    Resolution.START_INSTANCE: ResolutionSpec(
        advice="Start the instance.",
        write=WriteAction(
            tool="ec2_start_instance",
            build_args=_instance_args,
            summarize=lambda state, args: f"Start instance {_instance_name(state)} ({args['instance_id']}).",
            verify_tool="ec2_get_instance",
            verify_ok=lambda args, result: result["state"] in ("running", "pending"),
            verified_text=lambda state, args: f"Instance {_instance_name(state)} has been started and is now running.",
        )),
    Resolution.REBOOT_INSTANCE: ResolutionSpec(
        advice="Reboot the instance so its status checks can recover.",
        write=WriteAction(
            tool="ec2_reboot_instance",
            build_args=_instance_args,
            summarize=lambda state, args: f"Reboot instance {_instance_name(state)} ({args['instance_id']}).",
            verify_tool="ec2_get_instance",
            verify_ok=lambda args, result: result["state"] == "running",
            verified_text=lambda state, args: (
                f"Instance {_instance_name(state)} has been rebooted and is running. "
                "Status checks can take a few minutes to report as passing."),
        )),
    Resolution.INCREASE_LAMBDA_TIMEOUT: ResolutionSpec(
        advice="Raise the function's timeout so it has enough time to finish; the maximum is 900 seconds.",
        write=WriteAction(
            tool="lambda_update_timeout", slots=("timeout_seconds",),
            build_args=lambda state: {
                "function_name": state.entities["function_name"],
                "timeout_seconds": int(state.entities["timeout_seconds"])},
            summarize=_timeout_summary,
            verify_tool="lambda_get_function_configuration",
            verify_ok=lambda args, result: result["timeout_seconds"] == args["timeout_seconds"],
            verified_text=lambda state, args: (
                f"The timeout of {args['function_name']} is now {args['timeout_seconds']} seconds, "
                "and I've confirmed the new setting."),
        )),
    Resolution.CHECK_FUNCTION_NAME: ResolutionSpec(
        "Check the function name for typos and confirm which account and region it was deployed to."),
    Resolution.FIX_FUNCTION_ERROR: ResolutionSpec(
        "Fix the error shown in the function's log, then deploy the function again."),
    Resolution.GRANT_LAMBDA_PERMISSION: ResolutionSpec(
        "Attach a policy to the function's execution role that allows the action that was denied."),
    Resolution.GRANT_IAM_PERMISSION: ResolutionSpec(
        "Ask an administrator to attach a policy that allows this action to the user or role."),
    Resolution.REMOVE_EXPLICIT_DENY: ResolutionSpec(
        "Remove or narrow the Deny statement in the policy that matched; an explicit Deny overrides any Allow."),
    Resolution.USE_CORRECT_ROLE: ResolutionSpec(
        "Use the role that has the permission for this workload, or attach the permission to the role in use."),
    Resolution.CHECK_ALARM_NAME: ResolutionSpec(
        "Check the alarm name for typos and confirm which account and region it was created in."),
    Resolution.INVESTIGATE_ALARM_METRIC: ResolutionSpec(
        "The alarm is working as configured. Investigate the resource whose metric crossed the threshold."),
    Resolution.CHECK_METRIC_SOURCE: ResolutionSpec(
        "Check that the resource still exists and is publishing the metric, and that the alarm's dimensions match it."),
    Resolution.NO_ACTION_NEEDED: ResolutionSpec(
        "Nothing needs fixing now. The alarm's history shows when it last changed state."),
    Resolution.CHECK_INSTANCE_ID: ResolutionSpec(
        "Check the instance ID and the region; no instance with that ID exists in this account and region."),
    Resolution.INVESTIGATE_CPU: ResolutionSpec(
        "Find the process using the CPU, and consider a larger instance type or scaling out if the load is expected."),
    Resolution.INVESTIGATE_NETWORK: ResolutionSpec(
        "Check which clients or jobs are generating the traffic, and consider a network-optimized instance type if it is expected."),
    Resolution.CHECK_BUCKET_NAME: ResolutionSpec(
        "Check the bucket name for typos and confirm which account and region it was created in."),
    Resolution.CHECK_OBJECT_KEY: ResolutionSpec(
        "Check the object key for typos, including the folder prefix and letter case."),
    Resolution.UPDATE_BUCKET_POLICY: ResolutionSpec(
        "Ask the bucket owner to remove or narrow the Deny statement in the bucket policy; an explicit Deny overrides any Allow."),
    Resolution.GRANT_S3_PERMISSION: ResolutionSpec(
        "Ask an administrator to attach a policy that grants the user the missing S3 action on this bucket."),
}


# --------------------------------------------------------------- root causes
#
# A root cause can only be concluded when a tool result supports it. Each check
# returns the ids of the tool calls that support the cause; an empty list means
# the cause is not supported and cannot be chosen.

Check = Callable[["SupportState"], list[str]]

HIGH_CPU_PERCENT = 80
HIGH_NETWORK_BYTES = 1_000_000_000


def _when(tool: str, predicate: Callable[[dict, SupportState], bool]) -> Check:
    """Supported when the latest successful result of `tool` satisfies `predicate`."""
    def check(state: SupportState) -> list[str]:
        for call in reversed(state.tool_calls):
            if call.tool == tool and call.ok:
                return [call.id] if predicate(call.result, state) else []
        return []
    return check


def _any(tool: str, predicate: Callable[[dict, SupportState], bool]) -> Check:
    """Supported when any successful result of `tool` satisfies `predicate` (for tools called with varying arguments)."""
    def check(state: SupportState) -> list[str]:
        return [c.id for c in state.tool_calls if c.tool == tool and c.ok and predicate(c.result, state)]
    return check


def _not_found(tool_prefix: str, subject: str) -> Check:
    """Supported when a tool reported that `subject` does not exist."""
    def check(state: SupportState) -> list[str]:
        return [
            c.id for c in state.tool_calls
            if c.tool.startswith(tool_prefix) and c.error and c.error["error"] == "not_found"
            and c.error["detail"].startswith(subject)
        ]
    return check


def _instance_state(state: SupportState) -> tuple[str, str] | None:
    """The instance's state and the id of the tool call that reported it, from whichever tool did."""
    instance_id = state.entities.get("instance_id")
    for call in reversed(state.tool_calls):
        if not call.ok:
            continue
        if call.tool in ("ec2_get_instance", "ec2_get_instance_status"):
            return call.result["state"], call.id
        if call.tool == "ec2_list_instances":
            for instance in call.result["instances"]:
                if instance["instance_id"] == instance_id:
                    return instance["state"], call.id
    return None


def _instance_in_state(*states: str) -> Check:
    def check(state: SupportState) -> list[str]:
        known = _instance_state(state)
        return [known[1]] if known and known[0] in states else []
    return check


def _listed_without(tool: str, collection: str, field: str, entity: str) -> Check:
    """Supported when a successful listing does not contain the resource the customer named."""
    def check(state: SupportState) -> list[str]:
        wanted = state.entities.get(entity)
        for call in reversed(state.tool_calls):
            if call.tool == tool and call.ok:
                names = {item[field] for item in call.result[collection]}
                return [call.id] if wanted and wanted not in names else []
        return []
    return check


def _either(*checks: Check) -> Check:
    def check(state: SupportState) -> list[str]:
        for candidate in checks:
            evidence = candidate(state)
            if evidence:
                return evidence
        return []
    return check


def _port_blocked(port_flag: str, protocol: str) -> Check:
    def check(state: SupportState) -> list[str]:
        if state.entities.get("protocol") != protocol:
            return []
        running = _instance_in_state("running")(state)
        rules = _when(
            "ec2_get_security_group_rules", lambda r, _: bool(r["security_group_ids"]) and not r[port_flag])(state)
        return sorted(set(running + rules)) if running and rules else []
    return check


def _plus(check: Check, *tools: str) -> Check:
    """`check`, with the latest successful call of each of `tools` added as supporting evidence."""
    def combined(state: SupportState) -> list[str]:
        evidence = check(state)
        if not evidence:
            return []
        extra = [next((c.id for c in reversed(state.tool_calls) if c.tool == tool and c.ok), None) for tool in tools]
        return sorted(set(evidence) | {e for e in extra if e})
    return combined


def _wrong_role(result: dict, _: SupportState) -> bool:
    return result["principal_type"] == "role" and result["decision"] == "implicit_deny" \
        and bool(result["other_roles_allowed"])


def _alarm_in_state(expected: str) -> Check:
    return _when("cloudwatch_get_alarm", lambda r, _: r["state"] == expected)


def _average_above(metric: str, threshold: float) -> Callable[[dict, SupportState], bool]:
    return lambda r, _: (r[metric]["average"] or 0) > threshold


def _status_check_failed(state: SupportState) -> list[str]:
    metric = _when("ec2_get_instance_metrics", lambda r, _: (r["status_check_failed"]["maximum"] or 0) >= 1)(state)
    status = _when(
        "ec2_get_instance_status",
        lambda r, _: r["state"] == "running" and (r["system_status"], r["instance_status"]) != ("ok", "ok"))(state)
    return metric + status


def _high_network(state: SupportState) -> list[str]:
    return (_when("ec2_get_instance_metrics", _average_above("network_in_bytes", HIGH_NETWORK_BYTES))(state)
            or _when("ec2_get_instance_metrics", _average_above("network_out_bytes", HIGH_NETWORK_BYTES))(state))


@dataclass(frozen=True)
class RootCauseSpec:
    description: str  # shown to the model when more than one cause is supported
    check: Check
    resolution: Resolution | None = None


ROOT_CAUSES: dict[RootCause, RootCauseSpec] = {
    RootCause.EC2_INSTANCE_NOT_FOUND: RootCauseSpec(
        "no instance with that ID exists",
        _either(_not_found("ec2_", "instance"),
                _listed_without("ec2_list_instances", "instances", "instance_id", "instance_id")),
        Resolution.CHECK_INSTANCE_ID),
    RootCause.EC2_INSTANCE_STOPPED: RootCauseSpec(
        "the instance is stopped", _instance_in_state("stopped", "stopping"), Resolution.START_INSTANCE),
    RootCause.EC2_SG_SSH_BLOCKED: RootCauseSpec(
        "the instance is running but its security groups do not allow SSH on port 22",
        _port_blocked("allows_ssh_22", "ssh"), Resolution.ADD_INGRESS_RULE),
    RootCause.EC2_SG_RDP_BLOCKED: RootCauseSpec(
        "the instance is running but its security groups do not allow RDP on port 3389",
        _port_blocked("allows_rdp_3389", "rdp"), Resolution.ADD_INGRESS_RULE),
    RootCause.EC2_HIGH_CPU: RootCauseSpec(
        f"average CPU utilization is above {HIGH_CPU_PERCENT}%",
        _when("ec2_get_instance_metrics", _average_above("cpu_utilization_percent", HIGH_CPU_PERCENT)),
        Resolution.INVESTIGATE_CPU),
    RootCause.EC2_HIGH_NETWORK: RootCauseSpec(
        "network traffic in or out is unusually high", _high_network, Resolution.INVESTIGATE_NETWORK),
    RootCause.EC2_STATUS_CHECK_FAILED: RootCauseSpec(
        "an instance status check is failing", _status_check_failed, Resolution.REBOOT_INSTANCE),
    RootCause.S3_BUCKET_NOT_FOUND: RootCauseSpec(
        "the bucket does not exist",
        _either(_not_found("s3_", "bucket"), _listed_without("s3_list_buckets", "buckets", "name", "bucket")),
        Resolution.CHECK_BUCKET_NAME),
    RootCause.S3_OBJECT_NOT_FOUND: RootCauseSpec(
        "the bucket exists but the object does not", _not_found("s3_", "object"), Resolution.CHECK_OBJECT_KEY),
    RootCause.S3_BUCKET_POLICY_DENY: RootCauseSpec(
        "the bucket policy explicitly denies the request",
        _any("s3_check_bucket_access", lambda r, _: r["denied_by"] == "bucket_policy"),
        Resolution.UPDATE_BUCKET_POLICY),
    RootCause.S3_IAM_PERMISSION_MISSING: RootCauseSpec(
        "the IAM user has no permission for the request",
        _any("s3_check_bucket_access", lambda r, _: r["denied_by"] in ("missing_permission", "identity_policy")),
        Resolution.GRANT_S3_PERMISSION),
    RootCause.LAMBDA_FUNCTION_NOT_FOUND: RootCauseSpec(
        "no Lambda function with that name exists", _not_found("lambda_", "function"),
        Resolution.CHECK_FUNCTION_NAME),
    RootCause.LAMBDA_TIMEOUT_TOO_LOW: RootCauseSpec(
        "the function's log shows it timed out at its configured timeout",
        _plus(_when("lambda_get_function_logs", lambda r, _: r["timeouts"] >= 1), "lambda_get_function_configuration"),
        Resolution.INCREASE_LAMBDA_TIMEOUT),
    RootCause.LAMBDA_RUNTIME_ERROR: RootCauseSpec(
        "the function's log shows an error raised by its code",
        _when("lambda_get_function_logs", lambda r, _: r["errors"] >= 1), Resolution.FIX_FUNCTION_ERROR),
    RootCause.LAMBDA_ROLE_PERMISSION_MISSING: RootCauseSpec(
        "the function's log shows a call was denied because its execution role lacks permission",
        _plus(_when("lambda_get_function_logs", lambda r, _: r["access_denied"] >= 1), "lambda_get_function"),
        Resolution.GRANT_LAMBDA_PERMISSION),
    RootCause.IAM_EXPLICIT_DENY: RootCauseSpec(
        "a policy explicitly denies the action",
        _any("iam_check_permission", lambda r, _: r["decision"] == "explicit_deny"), Resolution.REMOVE_EXPLICIT_DENY),
    RootCause.IAM_WRONG_ROLE: RootCauseSpec(
        "the role in use has no permission for the action, but a different role does",
        _any("iam_check_permission", _wrong_role), Resolution.USE_CORRECT_ROLE),
    RootCause.IAM_PERMISSION_MISSING: RootCauseSpec(
        "no policy allows the action for this user or role, so it is denied by default (nothing denies it explicitly)",
        _any("iam_check_permission", lambda r, s: r["decision"] == "implicit_deny" and not _wrong_role(r, s)),
        Resolution.GRANT_IAM_PERMISSION),
    RootCause.CW_ALARM_NOT_FOUND: RootCauseSpec(
        "no alarm with that name exists", _not_found("cloudwatch_", "alarm"), Resolution.CHECK_ALARM_NAME),
    RootCause.CW_ALARM_THRESHOLD_BREACHED: RootCauseSpec(
        "the alarm is in the ALARM state because its metric crossed the threshold",
        _plus(_alarm_in_state("ALARM"), "cloudwatch_get_metric"), Resolution.INVESTIGATE_ALARM_METRIC),
    RootCause.CW_ALARM_INSUFFICIENT_DATA: RootCauseSpec(
        "the alarm has no recent data for its metric",
        _plus(_alarm_in_state("INSUFFICIENT_DATA"), "cloudwatch_get_metric"), Resolution.CHECK_METRIC_SOURCE),
    RootCause.CW_ALARM_OK: RootCauseSpec(
        "the alarm is in the OK state; it is not firing now",
        _plus(_alarm_in_state("OK"), "cloudwatch_get_alarm_history"), Resolution.NO_ACTION_NEEDED),
    RootCause.NO_FAULT_FOUND: RootCauseSpec("the checks that ran found nothing wrong", lambda state: []),
}


def supported_causes(state: SupportState) -> dict[RootCause, list[str]]:
    """Root causes of the current intent that tool results support, with their evidence."""
    if state.intent is None:
        return {}
    supported = {}
    for code in INTENTS[state.intent].causes:
        evidence = ROOT_CAUSES[code].check(state)
        if evidence:
            supported[code] = evidence
    return supported


def refine_intent(intent: Intent, details: dict[str, str]) -> Intent:
    """Apply the taxonomy's own tie-break: an access error that names a bucket is an S3 problem."""
    if intent in (Intent.IAM_ACCESS_DENIED, Intent.IAM_POLICY_PROBLEM) and details.get("bucket"):
        return Intent.S3_DOWNLOAD_FAILURE if details.get("object_key") else Intent.S3_ACCESS_DENIED
    return intent


def investigable(intent: Intent) -> bool:
    """Whether the agent has tools and root causes for this intent."""
    return bool(INTENTS[intent].causes)
