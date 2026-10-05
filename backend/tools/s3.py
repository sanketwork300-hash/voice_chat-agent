from datetime import datetime
from fnmatch import fnmatchcase
from typing import Literal

from pydantic import BaseModel, Field

from backend.cloud.base import CloudProvider, Decision
from backend.tools.catalog import tool

BucketName = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", description="S3 bucket name")

ACCESS_ACTIONS = {"read": "s3:GetObject", "write": "s3:PutObject", "list": "s3:ListBucket"}


class NoInput(BaseModel):
    pass


class BucketInput(BaseModel):
    bucket: str = BucketName


class BucketSummary(BaseModel):
    name: str
    created: datetime | None = None


class BucketList(BaseModel):
    buckets: list[BucketSummary]


class BucketDetail(BaseModel):
    name: str
    exists: bool
    region: str | None


class ListObjectsInput(BucketInput):
    prefix: str = Field("", max_length=200, description="Only keys starting with this prefix")
    limit: int = Field(20, ge=1, le=50)


class ObjectSummary(BaseModel):
    key: str
    size: int
    last_modified: datetime | None = None


class ObjectList(BaseModel):
    bucket: str
    objects: list[ObjectSummary]
    truncated: bool


class ObjectInput(BucketInput):
    key: str = Field(min_length=1, max_length=1024, description="Object key (path inside the bucket)")


class ObjectMetadata(ObjectSummary):
    bucket: str
    content_type: str | None = None


class PolicyStatement(BaseModel):
    sid: str | None
    effect: str
    principal: str
    actions: list[str]
    resources: list[str]
    has_condition: bool


class BucketPolicy(BaseModel):
    bucket: str
    has_policy: bool
    statements: list[PolicyStatement]


class AccessInput(BucketInput):
    iam_user: str = Field(min_length=1, max_length=64, description="IAM user name making the request")
    action: Literal["read", "write", "list"] = Field(description="read = download, write = upload, list = list keys")
    key: str | None = Field(None, max_length=1024, description="Object key, if the request is about one object")


class AccessResult(BaseModel):
    bucket: str
    iam_user: str
    action: str
    decision: Decision
    identity_policy_decision: Decision
    bucket_policy_decision: Literal["allow", "deny", "no_match", "no_policy"]
    denied_by: Literal["bucket_policy", "identity_policy", "missing_permission"] | None
    note: str | None = None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _principal_text(principal) -> str:
    if principal is None:
        return "none"
    if isinstance(principal, dict):
        return ", ".join(v for values in principal.values() for v in _as_list(values))
    return str(principal)


def _statements(policy: dict | None) -> list[dict]:
    if not policy:
        return []
    statements = policy.get("Statement", [])
    return [statements] if isinstance(statements, dict) else statements


def _principal_matches(principal, user_arn: str) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, dict):
        account_root = user_arn.split(":user/")[0] + ":root"
        return any(v in ("*", user_arn, account_root) for v in _as_list(principal.get("AWS")))
    return False


def evaluate_bucket_policy(policy: dict | None, user_arn: str, action: str, resource_arn: str
                           ) -> tuple[Literal["allow", "deny", "no_match", "no_policy"], bool]:
    """Evaluate a bucket policy for one request.

    Returns the outcome and whether a matching statement had a Condition, which
    is not evaluated here.
    """
    if not policy:
        return "no_policy", False
    outcome, conditional = "no_match", False
    for statement in _statements(policy):
        if "NotAction" in statement or "NotResource" in statement or "NotPrincipal" in statement:
            conditional = True
            continue
        if not _principal_matches(statement.get("Principal"), user_arn):
            continue
        if not any(fnmatchcase(action.lower(), a.lower()) for a in _as_list(statement.get("Action"))):
            continue
        if not any(fnmatchcase(resource_arn, r) for r in _as_list(statement.get("Resource"))):
            continue
        if statement.get("Condition"):
            conditional = True
            continue
        if statement.get("Effect") == "Deny":
            return "deny", conditional
        outcome = "allow"
    return outcome, conditional


@tool(
    name="s3_list_buckets", service="s3", description="List all S3 buckets in the account.",
    input_model=NoInput, output_model=BucketList,
    activity="Looking up your buckets", summarize=lambda out: f"Found {len(out.buckets)} bucket(s)",
)
def list_buckets(cloud: CloudProvider, _: NoInput) -> BucketList:
    return BucketList(buckets=[BucketSummary(name=b.name, created=b.created) for b in cloud.list_buckets()])


@tool(
    name="s3_get_bucket", service="s3",
    description="Check that an S3 bucket exists and get its region. Fails with not_found if it does not exist.",
    input_model=BucketInput, output_model=BucketDetail,
    activity="Checking the bucket", summarize=lambda out: f"Bucket {out.name} exists",
)
def get_bucket(cloud: CloudProvider, args: BucketInput) -> BucketDetail:
    bucket = cloud.get_bucket(args.bucket)
    return BucketDetail(name=bucket.name, exists=True, region=bucket.region)


@tool(
    name="s3_list_objects", service="s3", description="List object keys in an S3 bucket, optionally under a prefix.",
    input_model=ListObjectsInput, output_model=ObjectList,
    activity="Listing objects in the bucket", summarize=lambda out: f"Found {len(out.objects)} object(s)",
)
def list_objects(cloud: CloudProvider, args: ListObjectsInput) -> ObjectList:
    objects, truncated = cloud.list_objects(args.bucket, args.prefix, args.limit)
    return ObjectList(
        bucket=args.bucket, truncated=truncated,
        objects=[ObjectSummary(key=o.key, size=o.size, last_modified=o.last_modified) for o in objects],
    )


@tool(
    name="s3_get_object_metadata", service="s3",
    description="Check that one object exists in a bucket and get its size and type. Fails with not_found if it does not exist.",
    input_model=ObjectInput, output_model=ObjectMetadata,
    activity="Checking the object", summarize=lambda out: f"Object {out.key} exists",
)
def get_object_metadata(cloud: CloudProvider, args: ObjectInput) -> ObjectMetadata:
    obj = cloud.get_object_metadata(args.bucket, args.key)
    return ObjectMetadata(bucket=args.bucket, **obj.model_dump())


@tool(
    name="s3_get_bucket_policy", service="s3",
    description="Get the statements of a bucket's policy (who is allowed or denied which actions).",
    input_model=BucketInput, output_model=BucketPolicy,
    activity="Reading the bucket policy",
    summarize=lambda out: (
        f"Bucket policy has {len(out.statements)} statement(s)" if out.has_policy else "No bucket policy is set"),
)
def get_bucket_policy(cloud: CloudProvider, args: BucketInput) -> BucketPolicy:
    policy = cloud.get_bucket_policy(args.bucket)
    return BucketPolicy(
        bucket=args.bucket, has_policy=policy is not None,
        statements=[
            PolicyStatement(
                sid=s.get("Sid"), effect=s.get("Effect", ""), principal=_principal_text(s.get("Principal")),
                actions=_as_list(s.get("Action")), resources=_as_list(s.get("Resource")),
                has_condition=bool(s.get("Condition")),
            )
            for s in _statements(policy)
        ],
    )


def _access_summary(out: AccessResult) -> str:
    if out.decision == "allowed":
        return f"{out.iam_user} is allowed to {out.action}"
    reasons = {
        "bucket_policy": "the bucket policy denies it",
        "identity_policy": "the user's own policy denies it",
        "missing_permission": "no policy grants it",
    }
    return f"{out.iam_user} cannot {out.action}: {reasons[out.denied_by]}"


@tool(
    name="s3_check_bucket_access", service="s3",
    description=(
        "Check whether an IAM user is allowed to read, write, or list in a bucket, and if not, "
        "whether the bucket policy or the user's permissions are the reason."),
    input_model=AccessInput, output_model=AccessResult,
    activity="Checking access permissions", summarize=_access_summary,
)
def check_bucket_access(cloud: CloudProvider, args: AccessInput) -> AccessResult:
    cloud.get_bucket(args.bucket)  # a missing bucket is reported as not_found, not as a denial
    user_arn = cloud.get_user(args.iam_user).arn
    action = ACCESS_ACTIONS[args.action]
    resource = f"arn:aws:s3:::{args.bucket}" if args.action == "list" else f"arn:aws:s3:::{args.bucket}/{args.key or '*'}"

    identity = cloud.simulate_identity_policy(user_arn, action, resource).decision
    bucket_policy, conditional = evaluate_bucket_policy(cloud.get_bucket_policy(args.bucket), user_arn, action, resource)

    if bucket_policy == "deny":
        decision, denied_by = "explicit_deny", "bucket_policy"
    elif identity == "explicit_deny":
        decision, denied_by = "explicit_deny", "identity_policy"
    elif identity == "allowed" or bucket_policy == "allow":
        decision, denied_by = "allowed", None
    else:
        decision, denied_by = "implicit_deny", "missing_permission"

    return AccessResult(
        bucket=args.bucket, iam_user=args.iam_user, action=args.action, decision=decision,
        identity_policy_decision=identity, bucket_policy_decision=bucket_policy, denied_by=denied_by,
        note="The bucket policy has conditional statements that were not evaluated." if conditional else None,
    )
