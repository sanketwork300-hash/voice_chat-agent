"""In-memory stand-ins for the cloud and the model, so agent logic is tested deterministically."""

import json
import math
import re
import time
import zlib
from collections import deque
from datetime import date, datetime, timezone

from backend.agent.prompts import CLASSIFY_SCHEMA
from backend.cloud.base import (
    Alarm, AlarmHistoryItem, Bucket, CloudError, Decision, Function, IngressRule, Instance, InstanceStatus, LogEvent,
    MetricSummary, Principal, S3Object, SecurityGroup, Simulation,
)
from backend.providers.llm import LLMError
from backend.providers.llm.base import Completion
from backend.rag.knowledge import DocChunk

NO_DATA = MetricSummary(datapoints=0, average=None, maximum=None)


class FakeEmbedder:
    """Bag-of-words vectors: similarity is word overlap, so tests can reason about what matches."""

    name = "fake-embedder"
    dimensions = 384

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in set(re.findall(r"[a-z0-9]+", text.lower())):
            vector[zlib.crc32(word.encode()) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def doc_chunk(chunk_id: str, service: str, section: str, content: str) -> DocChunk:
    return DocChunk(
        chunk_id=chunk_id, document_id=chunk_id.split("#")[0], position=0, service=service, category="test",
        topic=section.split(" > ")[0], section=section, source="aws_documentation",
        source_url=f"https://example.test/{chunk_id.split('#')[0]}.md", source_date=date(2023, 6, 15), content=content)


class FakeCloud:
    """A tiny AWS account held in dictionaries."""

    def __init__(self):
        self.instances: dict[str, Instance] = {}
        self.groups: dict[str, SecurityGroup] = {}
        self.metrics: dict[tuple[str, str], MetricSummary] = {}
        self.buckets: dict[str, dict[str, S3Object]] = {}
        self.policies: dict[str, dict] = {}
        self.users: dict[str, Decision] = {}  # user name -> identity-policy decision for any action
        self.roles: dict[str, set[str]] = {}  # role name -> actions its policies allow
        self.functions: dict[str, Function] = {}
        self.logs: dict[str, list[str]] = {}  # function name -> log lines
        self.alarms: dict[str, Alarm] = {}
        self.failure: CloudError | None = None  # raised by every call while set
        self.delay_s = 0.0
        self.writes: list[tuple] = []

    # --- builders

    def add_instance(self, instance_id: str, name: str, *, state: str = "running", open_ports=(80,)) -> str:
        group_id = f"sg-{len(self.groups):08x}"
        self.groups[group_id] = SecurityGroup(
            group_id=group_id, name=f"{name}-sg", description="",
            ingress=[IngressRule(protocol="tcp", from_port=p, to_port=p, sources=["0.0.0.0/0"]) for p in open_ports])
        self.instances[instance_id] = Instance(
            instance_id=instance_id, name=name, state=state, instance_type="t3.micro", public_ip="52.0.0.1",
            private_ip="172.31.0.1", availability_zone="us-east-1a", security_group_ids=[group_id])
        return group_id

    def set_metric(self, resource: str, metric: str, value: float) -> None:
        """A metric value for an instance ID or a function name."""
        self.metrics[(resource, metric)] = MetricSummary(datapoints=1, average=value, maximum=value, total=value)

    def add_function(self, name: str, *, timeout: int = 30, role: str = "fn-role", logs=()) -> None:
        self.roles.setdefault(role, set())
        self.functions[name] = Function(
            name=name, runtime="python3.12", handler="handler.handler", timeout_seconds=timeout, memory_mb=128,
            role_arn=f"arn:aws:iam::123456789012:role/{role}", state="Active", last_modified=None,
            environment_variables=[])
        self.logs[name] = list(logs)

    def add_alarm(self, name: str, state: str, *, instance_id: str = "i-0aaaaaaaaaaaaaaa1") -> None:
        self.alarms[name] = Alarm(
            name=name, state=state, state_reason="seeded", state_updated=None, description=None,
            namespace="AWS/EC2", metric="CPUUtilization", dimensions={"InstanceId": instance_id},
            statistic="Average", period_seconds=300, evaluation_periods=1, threshold=80.0,
            comparison="GreaterThanThreshold", actions_enabled=True)

    def add_bucket(self, name: str, keys=(), policy: dict | None = None) -> None:
        self.buckets[name] = {k: S3Object(key=k, size=10) for k in keys}
        if policy:
            self.policies[name] = policy

    # --- plumbing

    def _guard(self) -> None:
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.failure:
            raise self.failure

    def _instance(self, instance_id: str) -> Instance:
        self._guard()
        if instance_id not in self.instances:
            raise CloudError("not_found", f"instance {instance_id} does not exist")
        return self.instances[instance_id]

    def _bucket(self, bucket: str) -> dict[str, S3Object]:
        self._guard()
        if bucket not in self.buckets:
            raise CloudError("not_found", f"bucket {bucket} does not exist")
        return self.buckets[bucket]

    # --- CloudProvider

    def list_instances(self) -> list[Instance]:
        self._guard()
        return list(self.instances.values())

    def get_instance(self, instance_id: str) -> Instance:
        return self._instance(instance_id)

    def get_instance_status(self, instance_id: str) -> InstanceStatus:
        return InstanceStatus(state=self._instance(instance_id).state, system_status="ok", instance_status="ok")

    def get_security_groups(self, group_ids: list[str]) -> list[SecurityGroup]:
        self._guard()
        return [self.groups[g] for g in group_ids]

    def add_ingress_rule(self, group_id: str, protocol: str, port: int, cidr: str) -> None:
        self._guard()
        self.writes.append(("add_ingress_rule", group_id, protocol, port, cidr))
        self.groups[group_id].ingress.append(
            IngressRule(protocol=protocol, from_port=port, to_port=port, sources=[cidr]))

    def _set_state(self, operation: str, instance_id: str, state: str) -> None:
        instance = self._instance(instance_id)
        self.writes.append((operation, instance_id))
        self.instances[instance_id] = instance.model_copy(update={"state": state})

    def start_instance(self, instance_id: str) -> None:
        self._set_state("start_instance", instance_id, "running")

    def stop_instance(self, instance_id: str) -> None:
        self._set_state("stop_instance", instance_id, "stopped")

    def reboot_instance(self, instance_id: str) -> None:
        self._set_state("reboot_instance", instance_id, "running")

    def get_metric(self, namespace: str, metric: str, dimensions: dict[str, str], minutes: int) -> MetricSummary:
        self._guard()
        resource = dimensions.get("InstanceId") or dimensions.get("FunctionName") or ""
        return self.metrics.get((resource, metric), NO_DATA)

    def list_alarms(self, state: str | None) -> list[Alarm]:
        self._guard()
        return [a for a in self.alarms.values() if state in (None, a.state)]

    def get_alarm(self, alarm_name: str) -> Alarm:
        self._guard()
        if alarm_name not in self.alarms:
            raise CloudError("not_found", f"alarm {alarm_name} does not exist")
        return self.alarms[alarm_name]

    def get_alarm_history(self, alarm_name: str, limit: int) -> list[AlarmHistoryItem]:
        self._guard()
        return [AlarmHistoryItem(timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc), type="StateUpdate",
                                 summary=f"Alarm updated to {self.alarms[alarm_name].state}")]

    def list_functions(self) -> list[Function]:
        self._guard()
        return list(self.functions.values())

    def get_function(self, function_name: str) -> Function:
        self._guard()
        if function_name not in self.functions:
            raise CloudError("not_found", f"function {function_name} does not exist")
        return self.functions[function_name]

    def update_function_timeout(self, function_name: str, timeout_seconds: int) -> None:
        function = self.get_function(function_name)
        self.writes.append(("update_function_timeout", function_name, timeout_seconds))
        self.functions[function_name] = function.model_copy(update={"timeout_seconds": timeout_seconds})

    def get_log_events(self, log_group: str, minutes: int, limit: int) -> list[LogEvent]:
        self._guard()
        lines = self.logs.get(log_group.rsplit("/", 1)[-1], [])
        return [LogEvent(timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc), message=m) for m in lines[:limit]]

    def list_buckets(self) -> list[Bucket]:
        self._guard()
        return [Bucket(name=name) for name in self.buckets]

    def get_bucket(self, bucket: str) -> Bucket:
        self._bucket(bucket)
        return Bucket(name=bucket, region="us-east-1")

    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[list[S3Object], bool]:
        objects = [o for k, o in self._bucket(bucket).items() if k.startswith(prefix)]
        return objects[:limit], len(objects) > limit

    def get_object_metadata(self, bucket: str, key: str) -> S3Object:
        objects = self._bucket(bucket)
        if key not in objects:
            raise CloudError("not_found", f"object {key} does not exist")
        return objects[key]

    def get_bucket_policy(self, bucket: str) -> dict | None:
        self._bucket(bucket)
        return self.policies.get(bucket)

    def get_user(self, user_name: str) -> Principal:
        self._guard()
        if user_name not in self.users:
            raise CloudError("not_found", f"IAM user {user_name} does not exist")
        return Principal(kind="user", name=user_name, arn=f"arn:aws:iam::123456789012:user/{user_name}",
                         attached_policies=[], inline_policies=["policy"])

    def get_role(self, role_name: str) -> Principal:
        self._guard()
        if role_name not in self.roles:
            raise CloudError("not_found", f"IAM role {role_name} does not exist")
        return Principal(kind="role", name=role_name, arn=f"arn:aws:iam::123456789012:role/{role_name}",
                         attached_policies=[], inline_policies=["policy"] if self.roles[role_name] else [],
                         trusted_by=["lambda.amazonaws.com"])

    def list_role_names(self) -> list[str]:
        self._guard()
        return list(self.roles)

    def get_policy_document(self, principal: Principal, policy_name: str) -> dict:
        self._guard()
        return {"Statement": [{"Effect": "Allow", "Action": sorted(self.roles.get(principal.name, [])) or "*",
                               "Resource": "*"}]}

    def simulate_identity_policy(self, principal_arn: str, action: str, resource_arn: str) -> Simulation:
        self._guard()
        kind, name = principal_arn.split(":")[-1].split("/", 1)
        if kind == "user":
            decision = self.users[name]
        else:
            decision = "allowed" if action in self.roles[name] else "implicit_deny"
        return Simulation(decision=decision, matched_policies=[] if decision == "implicit_deny" else ["policy"])


def label(kind: str = "problem", intent: str = "unknown", **details: str) -> dict:
    """A classifier output."""
    return {"kind": kind, "intent": intent, "instance": "", "bucket": "", "object_key": "", "iam_user": "",
            "iam_role": "", "function": "", "alarm": "", **details}


def calls(*names_or_pairs) -> list[dict]:
    """Tool calls the model makes in one reply. Items are a name or (name, arguments)."""
    result = []
    for i, item in enumerate(names_or_pairs):
        name, arguments = (item, {}) if isinstance(item, str) else item
        result.append({"id": f"call_{i}", "name": name, "arguments": arguments})
    return result


class ScriptedLLM:
    """Plays back prepared model outputs, one queue per kind of call."""

    name = "scripted"

    def __init__(self, *, labels=(), tool_rounds=(), replies=(), cause: str | None = None):
        self.labels = deque(labels)
        self.tool_rounds = deque(tool_rounds)
        self.replies = deque(replies)
        self.cause = cause
        self.requests: list[dict] = []  # every call, for assertions

    async def list_models(self):
        return ["scripted-model"]

    async def loaded_memory_bytes(self):
        return 0

    async def aclose(self):
        pass

    async def complete(self, messages, *, model, tools=None, json_schema=None, max_tokens=None) -> Completion:
        if json_schema == CLASSIFY_SCHEMA:
            kind = "classify"
        elif json_schema is not None:
            kind = "diagnose"
        else:
            kind = "investigate"
        self.requests.append({"kind": kind, "messages": list(messages), "tools": tools, "json_schema": json_schema})

        if kind == "classify":
            if not self.labels:
                raise AssertionError("the model was asked to classify, but no label was scripted")
            return Completion(json.dumps(self.labels.popleft()))
        if kind == "diagnose":
            return Completion(json.dumps({"root_cause_code": self.cause}))
        if self.tool_rounds:
            return Completion("", self.tool_rounds.popleft())
        return Completion("DONE")

    async def stream_chat(self, messages, *, model):
        self.requests.append({"kind": "stream", "messages": list(messages)})
        reply = self.replies.popleft() if self.replies else "Here is what I found."
        if isinstance(reply, Exception):
            raise reply
        for i, word in enumerate(reply.split(" ")):
            yield word if i == 0 else " " + word

    def requests_of(self, kind: str) -> list[dict]:
        return [r for r in self.requests if r["kind"] == kind]


class BrokenLLM(ScriptedLLM):
    async def complete(self, *args, **kwargs):
        raise LLMError("model exploded")
