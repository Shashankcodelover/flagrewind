"""Scripted 10-scenario drill runner against Flagsmith Admin API (SYNTHETIC).

Uses FLAGREWIND_DRILL_KEY (kept separate from the agent key) so drill mutations and
agent mitigations remain distinct in Flagsmith's audit log.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path
from typing import Any

import yaml

from flagrewind.flagsmith_client import FlagsmithClient, FlagsmithError


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    """Load frozen synthetic drill definitions from drills/scenarios.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("scenarios"), list):
        raise ValueError(f"Invalid scenarios file: {path}")
    return list(raw["scenarios"])


def select_scenario_order(scenarios: list[dict[str, Any]], *, seed: int) -> list[str]:
    """Return a seed-randomized scenario ID permutation for blind/manual baseline runs."""
    rng = random.Random(seed)
    ids = [str(s["id"]) for s in scenarios]
    rng.shuffle(ids)
    return ids


def apply_scenario_step(
    client: FlagsmithClient,
    *,
    env_api_key: str,
    feature_states: list[dict[str, Any]],
    feature_name: str,
    change: dict[str, Any],
) -> dict[str, Any]:
    """Find matching Flagsmith feature state and apply synthetic drill mutation."""
    for state in feature_states:
        feat = state.get("feature")
        name = feat.get("name") if isinstance(feat, dict) else None
        if name == feature_name:
            return client.patch_feature_state(env_api_key, int(state["id"]), change)
    raise FlagsmithError(f"Feature '{feature_name}' not found in environment states.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FlagRewind synthetic drill scenario.")
    parser.add_argument("--scenarios", type=Path, default=Path("drills/scenarios.yaml"))
    parser.add_argument("--scenario", help="Scenario ID (e.g. S01) to execute.")
    parser.add_argument("--project", default="Acme Checkout")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print drill order without calling API."
    )
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if args.dry_run:
        order = select_scenario_order(scenarios, seed=args.seed)
        print(f"SYNTHETIC drill dry-run order (seed={args.seed}): {', '.join(order)}")
        return 0

    api_url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/")
    drill_key = os.environ.get("FLAGREWIND_DRILL_KEY")
    if not drill_key:
        print("Error: set FLAGREWIND_DRILL_KEY before running live drills.", file=sys.stderr)
        return 2

    target = next((s for s in scenarios if s["id"] == args.scenario), scenarios[0])
    with FlagsmithClient(api_url, drill_key) as client:
        project = client.get_project_by_name(args.project)
        envs = {
            str(e["name"]).lower(): str(e["api_key"])
            for e in client.list_environments(int(project["id"]))
        }
        env_key = envs[str(target["environment"]).lower()]
        states = client.list_feature_states(env_key)
        for decoy in target.get("decoys", []):
            d_key = envs.get(str(decoy["environment"]).lower(), env_key)
            d_states = states if d_key == env_key else client.list_feature_states(d_key)
            apply_scenario_step(
                client,
                env_api_key=d_key,
                feature_states=d_states,
                feature_name=str(decoy["feature"]),
                change=dict(decoy["change"]),
            )
        culprit = target.get("culprit")
        if isinstance(culprit, dict) and culprit.get("kind") != "segment_rule":
            apply_scenario_step(
                client,
                env_api_key=env_key,
                feature_states=states,
                feature_name=str(culprit["feature"]),
                change=dict(culprit["change"]),
            )
    print(f"SYNTHETIC drill {target['id']} applied cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
