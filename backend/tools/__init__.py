"""Importing this package registers every tool in the catalog."""

from backend.tools import cloudwatch, ec2, iam, lambda_, s3  # noqa: F401
from backend.tools.catalog import CATALOG, ToolSpec, read_tools

__all__ = ["CATALOG", "ToolSpec", "read_tools"]
