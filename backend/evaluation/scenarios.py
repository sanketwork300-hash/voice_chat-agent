"""The scenario format used by the evaluation, and loading and saving scenario files."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

SCENARIOS_FILE = Path("datasets/scenarios/scenarios.json")
CONVERSATIONS_FILE = Path("datasets/conversations/conversations.json")


class Scenario(BaseModel):
    """One simulated support conversation and what a correct outcome looks like.

    Strings may contain "{handle.field}" placeholders that refer to what the
    fixture created, for example "{web.instance_id}".
    """

    scenario_id: str
    service: Literal["ec2", "s3", "lambda", "iam", "cloudwatch", "none"]
    variation: str            # how the customer behaves: direct, missing_details, declined, ...
    smoke: bool = False       # part of the quick subset

    fixture: list[dict] = []  # builder calls: {"name", "handle", "params"}

    # The simulated customer.
    preamble: str | None = None     # an earlier statement of a different problem, dropped in favour of user_message
    user_message: str
    user_facts: dict[str, str | list[str]] = {}  # detail the agent may ask for -> answer (a list is used in order)
    interjection: str | None = None  # an unrelated question asked once, when the agent first asks for a detail
    confirm_writes: bool = True
    confirm_via: Literal["message", "button"] = "message"

    # What a correct outcome is.
    intents: list[str]                        # any of these is correct; ["unknown"] for out-of-domain
    expected_tools: list[list[str]] = []      # for each group, at least one of its tools must have been called
    expected_identifiers: dict[str, str] = {}  # tool argument -> the value every call must have used
    expected_root_causes: list[str] = []      # any of these is correct; empty for out-of-domain
    expected_resolution: str | None = None    # None when more than one root cause is acceptable
    post_condition: dict | None = None        # checked in FakeCloud after the conversation
    out_of_domain: bool = False


def load(path: Path) -> list[Scenario]:
    return [Scenario.model_validate(item) for item in json.loads(path.read_text())]


def save(path: Path, scenarios: list[Scenario]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.model_dump(exclude_defaults=True) for s in scenarios], indent=2) + "\n")
