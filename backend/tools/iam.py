from typing import Literal

from pydantic import BaseModel, Field

from backend.cloud.base import CloudError, CloudProvider, Decision, Principal
from backend.tools.catalog import tool

MAX_ROLES_COMPARED = 25
MAX_POLICIES_READ = 10

PRINCIPAL_PATTERN = r"^[\w+=,.@-]{1,64}$"
PrincipalName = Field(pattern=PRINCIPAL_PATTERN)
Action = Field(pattern=r"^[a-z][a-z0-9-]{1,30}:([A-Za-z0-9]+\*?|\*)$", description="IAM action, e.g. s3:GetObject")


class UserInput(BaseModel):
    iam_user: str = PrincipalName


class RoleInput(BaseModel):
    role_name: str = PrincipalName


class PrincipalInput(BaseModel):
    """The user or the role under discussion; the gateway fills in whichever one is known."""

    iam_user: str | None = Field(None, pattern=PRINCIPAL_PATTERN)
    role_name: str | None = Field(None, pattern=PRINCIPAL_PATTERN)


class UserDetail(BaseModel):
    user_name: str
    arn: str
    attached_policies: list[str]
    inline_policies: list[str]


class RoleDetail(BaseModel):
    role_name: str
    arn: str
    trusted_by: list[str]
    attached_policies: list[str]
    inline_policies: list[str]


class Statement(BaseModel):
    effect: str
    actions: list[str]
    resources: list[str]
    has_condition: bool


class Policy(BaseModel):
    policy_name: str
    statements: list[Statement]


class PolicyDetail(BaseModel):
    attached_to: str
    policies: list[Policy]
    truncated: bool


class PermissionInput(PrincipalInput):
    iam_action: str = Action


class PermissionResult(BaseModel):
    principal: str
    principal_type: Literal["user", "role"]
    action: str
    decision: Decision
    matched_policies: list[str]   # policies whose statements decided the result
    other_roles_allowed: list[str]  # for a denied role: other roles that are allowed this action


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _principal(cloud: CloudProvider, args: PrincipalInput) -> Principal:
    if args.iam_user:
        return cloud.get_user(args.iam_user)
    if args.role_name:
        return cloud.get_role(args.role_name)
    raise CloudError("invalid", "no IAM user or role is known yet")


def _policy_count(out) -> str:
    count = len(out.attached_policies) + len(out.inline_policies)
    return f"{count} policy(ies) attached" if count else "No policies attached"


@tool(
    name="iam_get_user", service="iam", description="Get an IAM user and the names of the policies attached to it.",
    input_model=UserInput, output_model=UserDetail,
    activity="Checking the IAM user", summarize=_policy_count,
)
def get_user(cloud: CloudProvider, args: UserInput) -> UserDetail:
    user = cloud.get_user(args.iam_user)
    return UserDetail(user_name=user.name, arn=user.arn, attached_policies=user.attached_policies,
                      inline_policies=user.inline_policies)


@tool(
    name="iam_get_role", service="iam",
    description="Get an IAM role, who is trusted to assume it, and the names of the policies attached to it.",
    input_model=RoleInput, output_model=RoleDetail,
    activity="Checking the IAM role", summarize=_policy_count,
)
def get_role(cloud: CloudProvider, args: RoleInput) -> RoleDetail:
    role = cloud.get_role(args.role_name)
    return RoleDetail(role_name=role.name, arn=role.arn, trusted_by=role.trusted_by,
                      attached_policies=role.attached_policies, inline_policies=role.inline_policies)


def _statements(document: dict) -> list[Statement]:
    statements = document.get("Statement", [])
    return [
        Statement(effect=s.get("Effect", ""), actions=_as_list(s.get("Action")),
                  resources=_as_list(s.get("Resource")), has_condition=bool(s.get("Condition")))
        for s in ([statements] if isinstance(statements, dict) else statements)
    ]


@tool(
    name="iam_get_policy", service="iam",
    description="Get the statements of the policies attached to the user or role: which actions each allows or denies.",
    input_model=PrincipalInput, output_model=PolicyDetail,
    activity="Reading the policies", summarize=lambda out: f"Read {len(out.policies)} policy(ies)",
)
def get_policy(cloud: CloudProvider, args: PrincipalInput) -> PolicyDetail:
    # The model is not asked for a policy name: small models miscopy names, so all policies are returned.
    principal = _principal(cloud, args)
    names = principal.inline_policies + principal.attached_policies
    return PolicyDetail(
        attached_to=f"{principal.kind} {principal.name}",
        policies=[
            Policy(policy_name=name, statements=_statements(cloud.get_policy_document(principal, name)))
            for name in names[:MAX_POLICIES_READ]],
        truncated=len(names) > MAX_POLICIES_READ,
    )


def _permission_summary(out: PermissionResult) -> str:
    if out.decision == "allowed":
        return f"{out.principal} is allowed {out.action}"
    if out.decision == "explicit_deny":
        return f"{out.principal} is explicitly denied {out.action}"
    if out.other_roles_allowed:
        return f"{out.principal} lacks {out.action}; another role has it"
    return f"{out.principal} has no permission for {out.action}"


@tool(
    name="iam_check_permission", service="iam",
    description=(
        "Check whether the user or role is allowed the action the customer was denied, which policies decide "
        "that, and for a role, whether a different role has the permission instead."),
    input_model=PermissionInput, output_model=PermissionResult,
    activity="Checking the permission", summarize=_permission_summary,
)
def check_permission(cloud: CloudProvider, args: PermissionInput) -> PermissionResult:
    principal = _principal(cloud, args)
    result = cloud.simulate_identity_policy(principal.arn, args.iam_action, "*")

    other_roles: list[str] = []
    if principal.kind == "role" and result.decision == "implicit_deny":
        for name in cloud.list_role_names()[:MAX_ROLES_COMPARED]:
            if name != principal.name and cloud.simulate_identity_policy(
                    cloud.get_role(name).arn, args.iam_action, "*").decision == "allowed":
                other_roles.append(name)

    return PermissionResult(
        principal=principal.name, principal_type=principal.kind, action=args.iam_action,
        decision=result.decision, matched_policies=result.matched_policies, other_roles_allowed=other_roles,
    )
