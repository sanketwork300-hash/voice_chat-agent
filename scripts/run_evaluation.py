"""Run the evaluation scenarios against the agent with the real model and FakeCloud.

Every number printed is computed from this run. The run is stored in Postgres
(tables evaluation_runs and evaluation_results) for the /evaluation page.
FakeCloud is reset before every scenario, so the demo environment is gone
afterwards: run `make seed` to restore it (the make targets do this).

Usage: python scripts/run_evaluation.py [--subset smoke|all|conversations] [options]
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import hardware
from backend.cloud.boto3_provider import Boto3CloudProvider
from backend.config import get_settings
from backend.evaluation import scenarios as scenario_files
from backend.evaluation.runner import Result, build_agent, fakecloud_prepare, run_scenarios
from backend.evaluation.scoring import PASS_CHECKS, report, summarise
from backend.main import open_knowledge, open_storage
from backend.providers.llm import create_provider, resolve_model


def select(subset: str, only: list[str], limit: int | None) -> list:
    path = scenario_files.CONVERSATIONS_FILE if subset == "conversations" else scenario_files.SCENARIOS_FILE
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run scripts/generate_scenarios.py first.")
    items = scenario_files.load(path)
    if subset == "smoke":
        items = [s for s in items if s.smoke]
    if only:
        known = {s.scenario_id for s in items}
        missing = [i for i in only if i not in known]
        if missing:
            raise SystemExit(f"Unknown scenario id(s) in the {subset} set: {', '.join(missing)}")
        items = [s for s in items if s.scenario_id in only]
    return items[:limit] if limit else items


def progress(total: int):
    def show(index: int, result: Result) -> None:
        scenario, outcome, checks = result
        failed = [name for name in PASS_CHECKS if getattr(checks, name) is False]
        verdict = "PASS" if checks.passed else "FAIL"
        detail = outcome.error or (", ".join(failed) + " failed" if failed else "")
        print(f"[{index + 1:>3}/{total}] {scenario.scenario_id:<22} {verdict}  {outcome.duration_s:>5.1f} s  "
              f"{len(outcome.turns)} turns  {outcome.root_cause or outcome.status or ''}  {detail}", flush=True)
    return show


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subset", choices=["smoke", "all", "conversations"], default="all",
                        help="smoke: the fixed 20-scenario subset; all: the 200 scenarios; "
                             "conversations: the scripted multi-turn set (default: all)")
    parser.add_argument("--only", default="", help="comma-separated scenario ids to run from the chosen set")
    parser.add_argument("--limit", type=int, help="run at most this many scenarios")
    parser.add_argument("--model", help="model to evaluate (default: the one the backend would select)")
    parser.add_argument("--json", type=Path, help="also write the full run to this JSON file")
    parser.add_argument("--no-store", action="store_true", help="do not store the run in the database")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.aws_endpoint_url:
        print("AWS_ENDPOINT_URL is not set. The evaluation resets the account and must not run on real AWS.",
              file=sys.stderr)
        return 2
    selected = select(args.subset, [i for i in args.only.split(",") if i], args.limit)

    async with AsyncExitStack() as stack:
        llm = create_provider(settings)
        stack.push_async_callback(llm.aclose)
        loaded_gb = await llm.loaded_memory_bytes() / 2**30
        tier = hardware.select_models(hardware.detect(), settings, reclaimable_gb=loaded_gb)
        model = args.model or await resolve_model(llm, tier.llm_model, settings.llm_fallback_model)

        store, _, pool = await open_storage(settings, stack)
        knowledge = await open_knowledge(settings, pool)
        cloud = Boto3CloudProvider(settings)
        agent, recording = build_agent(
            llm, model, cloud, knowledge, rag_top_k=settings.rag_top_k, rag_min_score=settings.rag_min_score)

        run_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        print(f"Run {run_id}: {len(selected)} scenarios ({args.subset}), model {model}, "
              f"knowledge base {await knowledge.count()} chunks\n", flush=True)
        started_at, started = datetime.now(timezone.utc), time.monotonic()
        results = await run_scenarios(
            selected, agent, recording, prepare=fakecloud_prepare(cloud, settings.aws_endpoint_url), admin=cloud,
            run_id=run_id, on_result=progress(len(selected)))
        wall_clock = time.monotonic() - started

        metrics = summarise(results)
        run = {
            "run_id": run_id, "started_at": started_at, "finished_at": datetime.now(timezone.utc),
            "subset": args.subset, "model": model, "scenario_count": len(results),
            "wall_clock_s": round(wall_clock, 1), "metrics": metrics,
        }
        rows = [
            {"scenario_id": scenario.scenario_id, "service": scenario.service, "variation": scenario.variation,
             "passed": checks.passed, "checks": checks.model_dump(), "scenario": scenario.model_dump(mode="json"),
             "outcome": outcome.model_dump(mode="json")}
            for scenario, outcome, checks in results]
        if not args.no_store:
            await store.save_evaluation(run, rows)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps({"run": run, "results": rows}, indent=2, default=str) + "\n")

    print(f"\n{report(metrics)}")
    print(f"\nWall clock: {wall_clock / 60:.1f} minutes" + ("" if args.no_store else f"   Stored as run {run_id}"))
    violations = metrics["write_safety_violations"]
    if violations:
        print(f"\nFAILED: {violations} write-safety violation(s). A change was made without confirmation.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
