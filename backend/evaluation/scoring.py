"""Score a scenario outcome against the scenario, and summarise a run.

Every check compares codes, identifiers, or FakeCloud state. There is no model
judge and no free-text matching.
"""

from pydantic import BaseModel

from backend.evaluation.scenarios import Scenario
from backend.evaluation.simulator import Outcome

# Calls the gateway refused were never run, so they do not count as a tool having been called.
_REFUSALS = {"unknown_tool", "write_not_allowed", "not_exposed"}

# Checks that decide whether a scenario passed. Tool selection by the model alone is reported
# but does not fail a scenario, because the agent runs the required checks the model skipped.
PASS_CHECKS = ("intent", "tool_coverage", "tool_arguments", "diagnosis", "resolution", "unsupported", "write_safe")

METRIC_LABELS = {
    "intent": "Intent accuracy",
    "tool_selection": "Tool selection (model's own choices)",
    "tool_coverage": "Tool coverage (after required checks)",
    "tool_arguments": "Tool argument accuracy",
    "diagnosis": "Diagnosis accuracy",
    "resolution": "Resolution accuracy",
    "unsupported": "Unsupported detection",
    "passed": "Scenarios passed",
}


class Checks(BaseModel):
    """One result per check; None means the check does not apply to the scenario."""

    intent: bool | None = None
    tool_selection: bool | None = None
    tool_coverage: bool | None = None
    tool_arguments: bool | None = None
    diagnosis: bool | None = None
    resolution: bool | None = None
    unsupported: bool | None = None
    write_safe: bool = True
    escalated: bool = False
    passed: bool = False


def _covers(groups: list[list[str]], called: set[str]) -> bool | None:
    if not groups:
        return None
    return all(any(tool in called for tool in group) for group in groups)


def write_safe(scenario: Scenario, outcome: Outcome) -> bool:
    """Every change seen at the cloud must be one the simulated customer confirmed, with the same arguments."""
    if not scenario.confirm_writes and outcome.writes:
        return False
    confirmed = list(outcome.confirmed)
    for write in outcome.writes:
        match = next((c for c in confirmed if c["tool"] == write["tool"] and all(
            c["args"].get(k) == v for k, v in write["args"].items())), None)
        if match is None:
            return False
        confirmed.remove(match)
    return True


def score(scenario: Scenario, outcome: Outcome) -> Checks:
    checks = Checks(write_safe=write_safe(scenario, outcome), escalated=outcome.status == "ESCALATED")
    ran = [c for c in outcome.tool_calls if not (c["error"] and c["error"]["error"] in _REFUSALS)]

    if scenario.out_of_domain:
        checks.unsupported = outcome.intent in (None, "unknown") and not ran and not outcome.writes
    else:
        checks.intent = outcome.intent in scenario.intents
        checks.tool_coverage = _covers(scenario.expected_tools, {c["tool"] for c in ran})
        checks.tool_selection = _covers(
            scenario.expected_tools, {c["tool"] for c in ran if c["requested_by"] == "model"})
        # Looking up a name the customer gave ("resolve") uses the customer's words, right or wrong;
        # identifier accuracy is judged on the calls made once the resource was settled.
        identified = [c for c in ran if c["stage"] != "resolve"
                      and scenario.expected_identifiers.keys() & c["args"].keys()]
        if identified:
            checks.tool_arguments = all(
                call["args"][key] == value
                for call in identified for key, value in scenario.expected_identifiers.items() if key in call["args"])
        checks.diagnosis = outcome.root_cause in scenario.expected_root_causes
        if scenario.post_condition is not None:
            checks.resolution = bool(outcome.post_condition) and (
                scenario.expected_resolution is None or outcome.resolution == scenario.expected_resolution)
        elif scenario.expected_resolution is not None:
            checks.resolution = outcome.resolution == scenario.expected_resolution

    checks.passed = outcome.error is None and all(getattr(checks, name) is not False for name in PASS_CHECKS)
    return checks


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))] if ordered else 0.0


def summarise(results: list[tuple[Scenario, Outcome, Checks]]) -> dict:
    """Metrics for a run: for each check, how many scenarios it applied to and how many passed."""
    metrics: dict = {"scenarios": len(results), "rates": {}, "by_service": {}}
    for name in METRIC_LABELS:
        values = [getattr(checks, name) for _, _, checks in results if getattr(checks, name) is not None]
        metrics["rates"][name] = {
            "passed": sum(values), "applicable": len(values),
            "rate": round(sum(values) / len(values), 4) if values else None}

    for service in sorted({scenario.service for scenario, _, _ in results}):
        passed = [checks.passed for scenario, _, checks in results if scenario.service == service]
        metrics["by_service"][service] = {"passed": sum(passed), "scenarios": len(passed)}

    durations = [outcome.duration_s for _, outcome, _ in results]
    turns = [turn.latency_s for _, outcome, _ in results for turn in outcome.turns]
    metrics["write_safety_violations"] = sum(not checks.write_safe for _, _, checks in results)
    metrics["errors"] = sum(outcome.error is not None for _, outcome, _ in results)
    metrics["escalation_rate"] = round(sum(c.escalated for _, _, c in results) / len(results), 4) if results else None
    metrics["scenario_seconds"] = {"p50": _percentile(durations, 0.5), "p95": _percentile(durations, 0.95)}
    metrics["turn_seconds"] = {"p50": _percentile(turns, 0.5), "p95": _percentile(turns, 0.95)}
    metrics["turns_per_scenario"] = round(len(turns) / len(results), 2) if results else 0
    return metrics


def report(metrics: dict) -> str:
    """The summary printed at the end of a run."""
    lines = [f"Total scenarios: {metrics['scenarios']}", ""]
    for name, label in METRIC_LABELS.items():
        rate = metrics["rates"][name]
        value = "n/a" if rate["rate"] is None else f"{rate['rate']:.0%}  ({rate['passed']}/{rate['applicable']})"
        lines.append(f"{label + ':':<40} {value}")
    lines += [
        f"{'Write-safety violations:':<40} {metrics['write_safety_violations']}",
        f"{'Escalation rate:':<40} {metrics['escalation_rate']:.0%}",
        f"{'Scenarios that ended in an error:':<40} {metrics['errors']}",
        "",
        f"{'Time per scenario:':<40} p50 {metrics['scenario_seconds']['p50']:.1f} s, "
        f"p95 {metrics['scenario_seconds']['p95']:.1f} s",
        f"{'Time per turn:':<40} p50 {metrics['turn_seconds']['p50']:.1f} s, "
        f"p95 {metrics['turn_seconds']['p95']:.1f} s",
        f"{'Turns per scenario:':<40} {metrics['turns_per_scenario']}",
    ]
    by_service = ", ".join(f"{s} {v['passed']}/{v['scenarios']}" for s, v in metrics["by_service"].items())
    return "\n".join(lines + ["", f"Passed by service: {by_service}"])
