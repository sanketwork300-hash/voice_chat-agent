"""Reset FakeCloud and create the demo environment used in the README walkthrough.

Safe to run repeatedly: it wipes the simulated account first. Run it again after
FakeCloud restarts (all state is in memory) and when the seeded metrics are more
than an hour old.

Usage: python scripts/seed_fakecloud.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cloud.boto3_provider import Boto3CloudProvider
from backend.config import get_settings
from backend.evaluation import fixtures


def main() -> int:
    settings = get_settings()
    if not settings.aws_endpoint_url:
        print("AWS_ENDPOINT_URL is not set. Refusing to seed a real AWS account.", file=sys.stderr)
        return 1
    clients = Boto3CloudProvider(settings).client

    fixtures.reset(settings.aws_endpoint_url)
    web = fixtures.ec2_instance(clients, name="demo-web-01", open_ports=(80, 443))
    batch = fixtures.ec2_instance(clients, name="demo-batch-01", state="stopped", open_ports=(22,))
    api = fixtures.ec2_instance(clients, name="demo-api-01", open_ports=(22, 443))
    fixtures.s3_bucket(clients, name="demo-reports", objects=("2026/q3-summary.csv",), deny_downloads=True)
    fixtures.s3_bucket(clients, name="demo-logs", objects=("app/2026-10-01.log",))
    fixtures.iam_user(clients, name="demo-analyst", s3_access=True)
    fixtures.iam_user(clients, name="demo-intern", s3_access=False)

    # IAM: a user with no EC2 permission, a user with an explicit Deny, and a pair of roles
    # where the permission sits on the role the workload is not using.
    fixtures.iam_principal(clients, kind="user", name="demo-developer", allow=("s3:ListAllMyBuckets",))
    fixtures.iam_principal(clients, kind="user", name="demo-contractor", allow=("*",), deny=("ec2:*",))
    app_role = fixtures.iam_principal(clients, kind="role", name="demo-app-role", allow=("logs:*",))
    fixtures.iam_principal(clients, kind="role", name="demo-data-role", allow=("logs:*", "dynamodb:GetItem"))
    lambda_role = fixtures.iam_principal(clients, kind="role", name="demo-lambda-role", allow=("logs:*", "s3:*"))

    fixtures.lambda_function(
        clients, name="demo-report-generator", role_arn=lambda_role, timeout=3, invocations=12, errors=12,
        max_duration_ms=3000, log_lines=(
            "START RequestId: 7c1f Version: $LATEST",
            "Generating report for 2026-09",
            "2026-10-01T10:00:03.004Z 7c1f Task timed out after 3.00 seconds",
            "END RequestId: 7c1f",
        ))
    fixtures.lambda_function(
        clients, name="demo-thumbnailer", role_arn=lambda_role, timeout=30, invocations=40, errors=9,
        max_duration_ms=180, log_lines=(
            "START RequestId: 91ab Version: $LATEST",
            "[ERROR] KeyError: 'image_key'",
            "Traceback (most recent call last):\n  File \"/var/task/handler.py\", line 12, in handler\n    key = event['image_key']",
            "END RequestId: 91ab",
        ))
    fixtures.lambda_function(
        clients, name="demo-uploader", role_arn=app_role, timeout=30, invocations=5, errors=5,
        max_duration_ms=220, log_lines=(
            "START RequestId: 33de Version: $LATEST",
            "[ERROR] ClientError: An error occurred (AccessDenied) when calling the PutObject operation: "
            "User: arn:aws:sts::123456789012:assumed-role/demo-app-role/demo-uploader is not authorized to "
            "perform: s3:PutObject on resource: arn:aws:s3:::demo-logs/uploads/a.png",
            "END RequestId: 33de",
        ))

    fixtures.cpu_alarm(clients, name="demo-api-high-cpu", instance_id=api["instance_id"], cpu_percent=96.0)
    fixtures.cpu_alarm(clients, name="demo-batch-high-cpu", instance_id=batch["instance_id"])
    fixtures.cpu_alarm(clients, name="demo-web-high-cpu", instance_id=web["instance_id"], cpu_percent=12.0)

    print(f"Demo environment seeded in {settings.aws_endpoint_url}\n")
    rows = [
        ("EC2 demo-web-01", web["instance_id"], "running; SSH (22) not allowed"),
        ("EC2 demo-batch-01", batch["instance_id"], "stopped"),
        ("EC2 demo-api-01", api["instance_id"], "running; CPU at 96%"),
        ("S3 demo-reports", "", "bucket policy denies downloads"),
        ("S3 demo-logs", "", "no bucket policy"),
        ("IAM demo-analyst", "", "full S3 access"),
        ("IAM demo-intern", "", "no S3 permissions"),
        ("IAM demo-developer", "", "user; no EC2 permissions"),
        ("IAM demo-contractor", "", "user; explicit Deny on ec2:*"),
        ("IAM demo-app-role", "", "role; lacks dynamodb:GetItem, which demo-data-role has"),
        ("Lambda demo-report-generator", "", "timeout 3 s; log shows it timing out"),
        ("Lambda demo-thumbnailer", "", "log shows a KeyError in its code"),
        ("Lambda demo-uploader", "", "role demo-app-role is denied s3:PutObject"),
        ("Alarm demo-api-high-cpu", "", "ALARM (CPU 96% on demo-api-01)"),
        ("Alarm demo-batch-high-cpu", "", "INSUFFICIENT_DATA (instance is stopped)"),
        ("Alarm demo-web-high-cpu", "", "OK (CPU 12% on demo-web-01)"),
    ]
    for name, identifier, note in rows:
        print(f"  {name:<29} {identifier:<22} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
