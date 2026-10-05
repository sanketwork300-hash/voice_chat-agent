import re
from datetime import datetime

from pydantic import BaseModel, Field

from backend.cloud.base import CloudProvider, MetricSummary
from backend.tools.catalog import tool

MAX_TIMEOUT_SECONDS = 900

FunctionName = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Lambda function name")

_TIMED_OUT = re.compile(r"Task timed out after")
_ACCESS_DENIED = re.compile(r"AccessDenied|Access Denied|is not authorized to perform", re.I)
_ERROR = re.compile(r"\[ERROR\]|Traceback|errorMessage|Exception|Error:")


class NoInput(BaseModel):
    pass


class FunctionInput(BaseModel):
    function_name: str = FunctionName


class FunctionSummary(BaseModel):
    name: str
    runtime: str | None
    timeout_seconds: int
    memory_mb: int


class FunctionList(BaseModel):
    functions: list[FunctionSummary]


class FunctionDetail(BaseModel):
    name: str
    runtime: str | None
    handler: str | None
    state: str | None
    last_modified: str | None
    role_name: str
    role_attached_policies: list[str]
    role_inline_policies: list[str]


class FunctionConfiguration(BaseModel):
    name: str
    timeout_seconds: int
    memory_mb: int
    runtime: str | None
    handler: str | None
    environment_variables: list[str]


class LogsInput(FunctionInput):
    minutes: int = Field(60, ge=5, le=1440, description="How far back to look")
    limit: int = Field(20, ge=1, le=50)


class LogLine(BaseModel):
    timestamp: datetime
    message: str


class FunctionLogs(BaseModel):
    function_name: str
    minutes: int
    events: list[LogLine]
    timeouts: int        # lines saying the task timed out
    access_denied: int   # lines saying a call was denied
    errors: int          # other error lines


class MetricsInput(FunctionInput):
    minutes: int = Field(60, ge=5, le=1440, description="How far back to look")


class FunctionMetrics(BaseModel):
    function_name: str
    minutes: int
    invocations: float | None
    errors: float | None
    throttles: float | None
    duration_ms: MetricSummary


class TimeoutInput(FunctionInput):
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)


class TimeoutChanged(BaseModel):
    function_name: str
    previous_timeout_seconds: int
    timeout_seconds: int


@tool(
    name="lambda_list_functions", service="lambda", description="List all Lambda functions in the account.",
    input_model=NoInput, output_model=FunctionList,
    activity="Looking up your functions", summarize=lambda out: f"Found {len(out.functions)} function(s)",
)
def list_functions(cloud: CloudProvider, _: NoInput) -> FunctionList:
    return FunctionList(functions=[
        FunctionSummary(name=f.name, runtime=f.runtime, timeout_seconds=f.timeout_seconds, memory_mb=f.memory_mb)
        for f in cloud.list_functions()])


@tool(
    name="lambda_get_function", service="lambda",
    description="Get a Lambda function's state, runtime, and its execution role with the policies attached to that role.",
    input_model=FunctionInput, output_model=FunctionDetail,
    activity="Checking the function", summarize=lambda out: f"Function {out.name} exists and is {out.state or 'active'}",
)
def get_function(cloud: CloudProvider, args: FunctionInput) -> FunctionDetail:
    function = cloud.get_function(args.function_name)
    role = cloud.get_role(function.role_arn.rsplit("/", 1)[-1])
    return FunctionDetail(
        name=function.name, runtime=function.runtime, handler=function.handler, state=function.state,
        last_modified=function.last_modified, role_name=role.name,
        role_attached_policies=role.attached_policies, role_inline_policies=role.inline_policies,
    )


@tool(
    name="lambda_get_function_configuration", service="lambda",
    description="Get a Lambda function's timeout, memory size, and the names of its environment variables.",
    input_model=FunctionInput, output_model=FunctionConfiguration,
    activity="Checking the function's configuration",
    summarize=lambda out: f"Timeout is {out.timeout_seconds} s, memory is {out.memory_mb} MB",
)
def get_function_configuration(cloud: CloudProvider, args: FunctionInput) -> FunctionConfiguration:
    function = cloud.get_function(args.function_name)
    return FunctionConfiguration(
        name=function.name, timeout_seconds=function.timeout_seconds, memory_mb=function.memory_mb,
        runtime=function.runtime, handler=function.handler, environment_variables=function.environment_variables,
    )


def _logs_summary(out: FunctionLogs) -> str:
    if not out.events:
        return "No recent log entries"
    found = [f"{count} {label}" for count, label in (
        (out.timeouts, "timeout(s)"), (out.access_denied, "access-denied error(s)"), (out.errors, "other error(s)"),
    ) if count]
    return f"{len(out.events)} recent log entries: {', '.join(found)}" if found \
        else f"{len(out.events)} recent log entries, no errors"


@tool(
    name="lambda_get_function_logs", service="lambda",
    description=(
        "Get a Lambda function's recent log lines, with counts of timeouts, access-denied errors, and other errors."),
    input_model=LogsInput, output_model=FunctionLogs,
    activity="Reading the function's logs", summarize=_logs_summary,
)
def get_function_logs(cloud: CloudProvider, args: LogsInput) -> FunctionLogs:
    cloud.get_function(args.function_name)  # a missing function is reported as not_found, not as "no logs"
    events = cloud.get_log_events(f"/aws/lambda/{args.function_name}", args.minutes, args.limit)
    timeouts = [e for e in events if _TIMED_OUT.search(e.message)]
    denied = [e for e in events if e not in timeouts and _ACCESS_DENIED.search(e.message)]
    errors = [e for e in events if e not in timeouts and e not in denied and _ERROR.search(e.message)]
    return FunctionLogs(
        function_name=args.function_name, minutes=args.minutes,
        events=[LogLine(timestamp=e.timestamp, message=e.message[:500]) for e in events],
        timeouts=len(timeouts), access_denied=len(denied), errors=len(errors),
    )


def _metrics_summary(out: FunctionMetrics) -> str:
    if out.invocations is None:
        return "No recent invocations recorded"
    return f"{out.invocations:.0f} invocation(s), {out.errors or 0:.0f} error(s)"


@tool(
    name="lambda_get_function_metrics", service="lambda",
    description="Get a Lambda function's recent invocation count, error count, throttles, and duration.",
    input_model=MetricsInput, output_model=FunctionMetrics,
    activity="Checking the function's metrics", summarize=_metrics_summary,
)
def get_function_metrics(cloud: CloudProvider, args: MetricsInput) -> FunctionMetrics:
    cloud.get_function(args.function_name)

    def metric(name: str) -> MetricSummary:
        return cloud.get_metric("AWS/Lambda", name, {"FunctionName": args.function_name}, args.minutes)

    return FunctionMetrics(
        function_name=args.function_name, minutes=args.minutes,
        invocations=metric("Invocations").total, errors=metric("Errors").total,
        throttles=metric("Throttles").total, duration_ms=metric("Duration"),
    )


@tool(
    name="lambda_update_timeout", service="lambda", description="Change a Lambda function's timeout.",
    input_model=TimeoutInput, output_model=TimeoutChanged, read_only=False, risk_level="medium",
    activity="Changing the function's timeout",
    summarize=lambda out: f"Timeout changed from {out.previous_timeout_seconds} s to {out.timeout_seconds} s",
)
def update_timeout(cloud: CloudProvider, args: TimeoutInput) -> TimeoutChanged:
    previous = cloud.get_function(args.function_name).timeout_seconds
    cloud.update_function_timeout(args.function_name, args.timeout_seconds)
    return TimeoutChanged(
        function_name=args.function_name, previous_timeout_seconds=previous, timeout_seconds=args.timeout_seconds)
