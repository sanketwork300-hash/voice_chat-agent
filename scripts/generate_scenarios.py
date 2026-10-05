"""Write the evaluation scenarios and the scripted multi-turn conversations.

Generation is deterministic: the same seed always produces the same files.

Usage: python scripts/generate_scenarios.py [--seed N] [--check]
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.evaluation import generator, scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=generator.DEFAULT_SEED,
                        help=f"random seed (default {generator.DEFAULT_SEED})")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if the files on disk differ from what would be generated")
    args = parser.parse_args()

    outputs = {
        scenarios.SCENARIOS_FILE: generator.generate(args.seed),
        scenarios.CONVERSATIONS_FILE: generator.generate_conversations(args.seed),
    }
    if args.check:
        stale = [str(path) for path, items in outputs.items()
                 if not path.exists() or scenarios.load(path) != items]
        if stale:
            print("Out of date, run scripts/generate_scenarios.py: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("Scenario files are up to date.")
        return 0

    for path, items in outputs.items():
        scenarios.save(path, items)
        per_service = Counter(item.service for item in items)
        print(f"{path}: {len(items)} scenarios  " + ", ".join(f"{k} {v}" for k, v in sorted(per_service.items())))
    smoke = sum(item.smoke for item in outputs[scenarios.SCENARIOS_FILE])
    print(f"Smoke subset: {smoke} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
