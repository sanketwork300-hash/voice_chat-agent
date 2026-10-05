"""The registered tool catalog. A name that is not in here cannot be called."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from backend.cloud.base import CloudProvider

RiskLevel = Literal["low", "medium", "high"]

# Input fields that identify a customer resource, and the conversation entity
# each one is filled from. The model never supplies these: small models miscopy
# long identifiers, so the gateway binds them from what the customer said.
BOUND_FIELDS: dict[str, str] = {
    "instance_id": "instance_id",
    "bucket": "bucket",
    "iam_user": "iam_user",
    "key": "object_key",
    "function_name": "function_name",
    "role_name": "iam_role",
    "iam_action": "iam_action",
    "alarm_name": "alarm_name",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    service: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    read_only: bool
    risk_level: RiskLevel
    run: Callable[[CloudProvider, Any], BaseModel]
    # Customer-language text: what the assistant is doing, and what it found.
    activity: str
    summarize: Callable[[Any], str]

    @property
    def bound(self) -> dict[str, str]:
        """Input field -> entity key, for the fields the gateway fills in."""
        return {f: BOUND_FIELDS[f] for f in self.input_model.model_fields if f in BOUND_FIELDS}

    def required_entities(self) -> set[str]:
        fields = self.input_model.model_fields
        return {entity for f, entity in self.bound.items() if fields[f].is_required()}

    def llm_schema(self) -> dict:
        """The function definition shown to the model, without the bound fields."""
        schema = self.input_model.model_json_schema()
        properties = {
            name: {k: v for k, v in prop.items() if k != "title"}
            for name, prop in schema.get("properties", {}).items()
            if name not in BOUND_FIELDS
        }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [r for r in schema.get("required", []) if r not in BOUND_FIELDS],
                },
            },
        }


CATALOG: dict[str, ToolSpec] = {}


def tool(*, name: str, description: str, service: str, input_model: type[BaseModel],
         output_model: type[BaseModel], activity: str, summarize: Callable[[Any], str],
         read_only: bool = True, risk_level: RiskLevel = "low"):
    """Register a function `(cloud, input) -> output` as a tool."""

    def register(run: Callable[[CloudProvider, Any], BaseModel]):
        if name in CATALOG:
            raise ValueError(f"duplicate tool name {name}")
        CATALOG[name] = ToolSpec(
            name=name, description=description, service=service, input_model=input_model,
            output_model=output_model, read_only=read_only, risk_level=risk_level, run=run,
            activity=activity, summarize=summarize,
        )
        return run

    return register


def read_tools(service: str | None, entities: dict[str, str]) -> list[ToolSpec]:
    """The tools the model may call right now.

    Only read tools of the identified service, and only those whose resource
    identifiers are already known. Write tools are never included.
    """
    if service is None:
        return []
    return [
        t for t in CATALOG.values()
        if t.service == service and t.read_only and all(entities.get(e) for e in t.required_entities())
    ]
