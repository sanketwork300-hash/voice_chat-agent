from datetime import datetime

from pydantic import BaseModel, Field

from backend.cloud.base import CloudError, CloudProvider, MetricSummary
from backend.tools.catalog import tool

AlarmName = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f]+$", description="CloudWatch alarm name")


class NoInput(BaseModel):
    pass


class AlarmInput(BaseModel):
    alarm_name: str = AlarmName


class AlarmSummary(BaseModel):
    name: str
    state: str
    metric: str | None
    namespace: str | None


class AlarmList(BaseModel):
    alarms: list[AlarmSummary]


class AlarmDetail(BaseModel):
    name: str
    state: str
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


class HistoryInput(AlarmInput):
    limit: int = Field(10, ge=1, le=50)


class HistoryItem(BaseModel):
    timestamp: datetime
    type: str
    summary: str


class AlarmHistory(BaseModel):
    alarm_name: str
    items: list[HistoryItem]


class MetricInput(AlarmInput):
    minutes: int = Field(60, ge=5, le=1440, description="How far back to look")


class AlarmMetric(BaseModel):
    alarm_name: str
    namespace: str
    metric: str
    dimensions: dict[str, str]
    minutes: int
    values: MetricSummary
    threshold: float | None
    comparison: str | None


_STATE_TEXT = {"ALARM": "in the ALARM state", "OK": "in the OK state", "INSUFFICIENT_DATA": "missing data"}


@tool(
    name="cloudwatch_list_alarms", service="cloudwatch",
    description="List all CloudWatch alarms with their states.",
    input_model=NoInput, output_model=AlarmList,
    activity="Looking up your alarms", summarize=lambda out: f"Found {len(out.alarms)} alarm(s)",
)
def list_alarms(cloud: CloudProvider, _: NoInput) -> AlarmList:
    return AlarmList(alarms=[
        AlarmSummary(name=a.name, state=a.state, metric=a.metric, namespace=a.namespace)
        for a in cloud.list_alarms(None)])


@tool(
    name="cloudwatch_get_alarm", service="cloudwatch",
    description="Get one alarm: its state, why it is in that state, and the metric and threshold it watches.",
    input_model=AlarmInput, output_model=AlarmDetail,
    activity="Checking the alarm", summarize=lambda out: f"Alarm {out.name} is {_STATE_TEXT.get(out.state, out.state)}",
)
def get_alarm(cloud: CloudProvider, args: AlarmInput) -> AlarmDetail:
    return AlarmDetail(**cloud.get_alarm(args.alarm_name).model_dump())


@tool(
    name="cloudwatch_get_alarm_history", service="cloudwatch",
    description="Get the recent state changes and configuration changes of one alarm, newest first.",
    input_model=HistoryInput, output_model=AlarmHistory,
    activity="Reading the alarm's history", summarize=lambda out: f"{len(out.items)} history entry(ies)",
)
def get_alarm_history(cloud: CloudProvider, args: HistoryInput) -> AlarmHistory:
    cloud.get_alarm(args.alarm_name)  # a missing alarm is reported as not_found, not as an empty history
    items = cloud.get_alarm_history(args.alarm_name, args.limit)
    return AlarmHistory(alarm_name=args.alarm_name, items=[HistoryItem(**i.model_dump()) for i in items])


def _metric_summary(out: AlarmMetric) -> str:
    if out.values.average is None:
        return f"No recent data for {out.metric}"
    return f"{out.metric} averages {out.values.average:g} (threshold {out.threshold:g})" \
        if out.threshold is not None else f"{out.metric} averages {out.values.average:g}"


@tool(
    name="cloudwatch_get_metric", service="cloudwatch",
    description="Get the recent values of the metric that the alarm watches, next to the alarm's threshold.",
    input_model=MetricInput, output_model=AlarmMetric,
    activity="Checking the alarm's metric", summarize=_metric_summary,
)
def get_metric(cloud: CloudProvider, args: MetricInput) -> AlarmMetric:
    alarm = cloud.get_alarm(args.alarm_name)
    if not alarm.namespace or not alarm.metric:
        raise CloudError("invalid", f"alarm {args.alarm_name} does not watch a single metric")
    return AlarmMetric(
        alarm_name=alarm.name, namespace=alarm.namespace, metric=alarm.metric, dimensions=alarm.dimensions,
        minutes=args.minutes, values=cloud.get_metric(alarm.namespace, alarm.metric, alarm.dimensions, args.minutes),
        threshold=alarm.threshold, comparison=alarm.comparison,
    )
