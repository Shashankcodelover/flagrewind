"""Interactive stopwatch helper for the B1 manual triage baseline (n=3, indicative only).

Launches blinded seeded scenarios against live Flagsmith + demo_service, prints nothing
about the culprit, and records exact human alert -> revert-applied timing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from drills.runner import (
    apply_full_scenario,
    load_scenarios,
    reset_demo_project,
    select_scenario_order,
)
from flagrewind.flagsmith_client import FlagsmithClient


def _load_dotenv() -> None:
    env_file = Path(".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Blind manual triage stopwatch (B1, n=3).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("drills/results/manual_baseline_n3.json"),
    )
    args = parser.parse_args(argv)

    if not sys.stdin.isatty():
        print("Error: manual stopwatch requires an interactive terminal.", file=sys.stderr)
        return 1

    api_url = os.environ.get("FLAGSMITH_API_URL", "https://api.flagsmith.com/api/v1/")
    drill_key = os.environ.get("FLAGREWIND_DRILL_KEY")
    if not drill_key:
        print("Error: FLAGREWIND_DRILL_KEY must be set in .env.", file=sys.stderr)
        return 2

    all_scenarios = load_scenarios(Path("drills/scenarios.yaml"))
    eligible = [
        s for s in all_scenarios if not s.get("non_flag_fault") and s["environment"] == "production"
    ]
    order = select_scenario_order(eligible, seed=args.seed)[: args.count]
    truth = json.loads(Path("drills/answers/ground_truth.json").read_text(encoding="utf-8"))[
        "scenarios"
    ]

    results = []
    with FlagsmithClient(api_url, drill_key) as client:
        for idx, sid in enumerate(order, start=1):
            reset_demo_project(client, project_name="Acme Checkout")
            scenario = next(s for s in eligible if s["id"] == sid)
            print(f"\n=== Blind Manual Drill {idx}/{len(order)} ===", flush=True)
            print("Injecting hidden fault into Flagsmith now...", flush=True)
            apply_full_scenario(client, project_name="Acme Checkout", scenario=scenario)
            input("ALERT: Error rate breached >5%! Press ENTER when timer starts -> ")
            t_start = time.monotonic()
            print("Open Flagsmith UI + demo /metrics & /errors and revert culprit.", flush=True)
            guess = input(
                "Press ENTER as soon as you applied the revert (or type feature name): "
            ).strip()
            t_revert = round(time.monotonic() - t_start, 2)
            expected = truth[sid]["culprit_feature"]
            results.append(
                {
                    "trial": idx,
                    "scenario_id": sid,
                    "alert_to_revert_applied_s": t_revert,
                    "operator_note": guess,
                    "expected_culprit": expected,
                }
            )
            print(f"Recorded: scenario={sid}, alert_to_revert_applied_s={t_revert}s")
        reset_demo_project(client, project_name="Acme Checkout")

    payload = {
        "synthetic": True,
        "indicative_only": True,
        "n": len(results),
        "seed": args.seed,
        "recorded_at": datetime.now(UTC).isoformat(),
        "runs": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved indicative B1 manual baseline (n={len(results)}) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
