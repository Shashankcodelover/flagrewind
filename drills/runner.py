"""Scripted drill runner and reset tool against Flagsmith Admin API (SYNTHETIC).

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

BASELINE_DEFAULTS: dict[str, dict[str, Any]] = {
    "tax_engine_v2": {"enabled": False, "feature_state_value": None},
    "payment_timeout_ms": {"enabled": True, "feature_state_value": "3000"},
    "profile_v2": {"enabled": False, "feature_state_value": None},
    "search_ranker_v2": {"enabled": False, "feature_state_value": None},
    "checkout_layout": {"enabled": False, "feature_state_value": None},
    "maintenance_banner": {"enabled": False, "feature_state_value": None},
    "export_csv": {"enabled": False, "feature_state_value": None},
    "dark_mode": {"enabled": False, "feature_state_value": None},
    "payment_provider": {"enabled": True, "feature_state_value": "stripe_v1"},
}


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


def _state_feature_name(state: dict[str, Any], feature_map: dict[int, str] | None) -> str | None:
    feat = state.get("feature")
    if isinstance(feat, dict):
        return str(feat.get("name")) if feat.get("name") else None
    if feature_map and feat is not None:
        return feature_map.get(int(feat))
    return None


def reset_demo_project(client: FlagsmithClient, *, project_name: str = "Acme Checkout") -> None:
    """Restore all 9 flags, segment rules, and segment overrides to clean baseline."""
    project = client.get_project_by_name(project_name)
    pid = int(project["id"])
    envs = client.list_environments(pid)
    features = client.list_features(pid)
    feat_map = {int(f["id"]): str(f["name"]) for f in features if "id" in f and "name" in f}
    feat_by_name = {str(f["name"]): f for f in features if "name" in f}

    for env in envs:
        env_id = int(env["id"])
        env_key = str(env["api_key"])
        # Remove temporary segment overrides cleanly (1 bulk query finds affected features)
        all_env_states = client.list_feature_all_states(env_id)
        fids_with_ov = {
            int(st["feature"])
            for st in all_env_states
            if st.get("feature_segment") is not None and st.get("feature") is not None
        }
        for fid in fids_with_ov:
            for fseg in client.list_feature_segments(env_id, fid):
                if "id" in fseg:
                    client.delete_feature_segment(int(fseg["id"]))

        # Reset default feature states only if drifted from BASELINE_DEFAULTS
        for state in client.list_feature_states(env_key):
            if state.get("feature_segment"):
                continue
            fname = _state_feature_name(state, feat_map)
            if not fname or fname not in BASELINE_DEFAULTS:
                continue
            d = BASELINE_DEFAULTS[fname]
            if fname == "payment_provider":
                pp = feat_by_name["payment_provider"]
                pp_opts = {
                    str(o["string_value"]): int(o["id"])
                    for o in pp.get("multivariate_options") or []
                    if o.get("string_value") and "id" in o
                }
                target_opt = pp_opts.get("stripe_v1")
                mv_vals = state.get("multivariate_feature_state_values") or []
                needs_mv_reset = (
                    state.get("enabled") != d["enabled"]
                    or state.get("feature_state_value") != d["feature_state_value"]
                    or any(
                        float(mv.get("percentage_allocation", 0.0))
                        != (100.0 if mv["multivariate_feature_option"] == target_opt else 0.0)
                        for mv in mv_vals
                    )
                )
                if needs_mv_reset:
                    body = dict(d)
                    body["feature"] = int(pp["id"])
                    body["multivariate_feature_state_values"] = [
                        {
                            "id": mv["id"],
                            "multivariate_feature_option": mv["multivariate_feature_option"],
                            "percentage_allocation": (
                                100.0 if mv["multivariate_feature_option"] == target_opt else 0.0
                            ),
                        }
                        for mv in mv_vals
                    ]
                    client.put_feature_state(env_key, int(state["id"]), body)
            elif (
                state.get("enabled") != d["enabled"]
                or state.get("feature_state_value") != d["feature_state_value"]
            ):
                client.patch_feature_state(env_key, int(state["id"]), dict(d))

    # Restore eu-customers segment condition back to country IN DE,FR if altered
    for seg in client.list_segments(pid):
        if seg.get("name") == "eu-customers":
            conds = (seg.get("rules") or [{}])[0].get("conditions") or [{}]
            if conds and conds[0].get("value") != "DE,FR":
                client.patch_segment(
                    pid,
                    int(seg["id"]),
                    {
                        "name": "eu-customers",
                        "description": seg.get("description") or "",
                        "rules": [
                            {
                                "type": "ALL",
                                "rules": [],
                                "conditions": [
                                    {"operator": "IN", "property": "country", "value": "DE,FR"}
                                ],
                            }
                        ],
                    },
                )


def apply_scenario_step(
    client: FlagsmithClient,
    *,
    env_api_key: str,
    feature_states: list[dict[str, Any]],
    feature_name: str,
    change: dict[str, Any],
    feature_map: dict[int, str] | None = None,
    environment_id: int | None = None,
    project_id: int | None = None,
    kind: str = "feature_state",
    segment_name: str | None = None,
) -> dict[str, Any]:
    """Find matching Flagsmith feature/segment and apply synthetic drill mutation."""
    if kind == "segment_rule" and project_id is not None:
        segs = {s["name"]: s for s in client.list_segments(project_id) if "name" in s}
        seg = segs.get(segment_name or feature_name)
        if not seg:
            raise FlagsmithError(f"Segment '{segment_name or feature_name}' not found.")
        return client.patch_segment(
            project_id,
            int(seg["id"]),
            {
                "name": seg["name"],
                "description": seg.get("description") or "",
                "rules": [{"type": "ALL", "rules": [], "conditions": [change]}],
            },
        )

    if kind == "segment_override" and environment_id is not None and project_id is not None:
        feats = {f["name"]: f for f in client.list_features(project_id) if "name" in f}
        segs = {s["name"]: s for s in client.list_segments(project_id) if "name" in s}
        feat = feats.get(feature_name)
        seg = segs.get(segment_name or "")
        if not feat or not seg:
            raise FlagsmithError(
                f"Cannot resolve feature '{feature_name}' or segment '{segment_name}'."
            )
        fid = int(feat["id"])
        sid = int(seg["id"])
        existing_fsegs = client.list_feature_segments(environment_id, fid)
        existing_binding = next(
            (fs for fs in existing_fsegs if int(fs.get("segment", 0)) == sid), None
        )
        if existing_binding is None:
            existing_binding = client.create_feature_segment(
                feature_id=fid, segment_id=sid, environment_id=environment_id
            )
        fseg_id = int(existing_binding["id"])
        all_states = client.list_feature_all_states(environment_id, fid)
        ov_state = next((s for s in all_states if s.get("feature_segment") == fseg_id), None)
        if ov_state is not None:
            return client.patch_feature_state(env_api_key, int(ov_state["id"]), change)
        return client.create_segment_override(
            env_api_key,
            {
                "feature": fid,
                "feature_segment": fseg_id,
                "enabled": bool(change.get("enabled", True)),
                "feature_state_value": change.get("feature_state_value"),
            },
        )

    if kind == "multivariate" and project_id is not None:
        feats = {f["name"]: f for f in client.list_features(project_id) if "name" in f}
        feat = feats.get(feature_name)
        target_variant = str(change.get("feature_state_value") or "broken_gateway")
        if feat:
            opts = {
                str(o["string_value"]): int(o["id"])
                for o in feat.get("multivariate_options") or []
                if o.get("string_value") and "id" in o
            }
            target_opt_id = opts.get(target_variant)
            for state in feature_states:
                if _state_feature_name(state, feature_map) == feature_name and not state.get(
                    "feature_segment"
                ):
                    mv_vals = state.get("multivariate_feature_state_values") or []
                    body = {
                        "feature": int(feat["id"]),
                        "enabled": bool(change.get("enabled", True)),
                        "feature_state_value": target_variant,
                        "multivariate_feature_state_values": [
                            {
                                "id": mv["id"],
                                "multivariate_feature_option": mv["multivariate_feature_option"],
                                "percentage_allocation": (
                                    100.0
                                    if mv["multivariate_feature_option"] == target_opt_id
                                    else 0.0
                                ),
                            }
                            for mv in mv_vals
                        ],
                    }
                    return client.put_feature_state(env_api_key, int(state["id"]), body)

    for state in feature_states:
        if _state_feature_name(state, feature_map) == feature_name:
            return client.patch_feature_state(env_api_key, int(state["id"]), change)
    raise FlagsmithError(f"Feature '{feature_name}' not found in environment states.")


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


def apply_full_scenario(
    client: FlagsmithClient, *, project_name: str, scenario: dict[str, Any]
) -> None:
    """Apply all decoys and culprit for a synthetic scenario on live Flagsmith."""
    project = client.get_project_by_name(project_name)
    pid = int(project["id"])
    env_objs = {
        str(e["name"]).lower(): e for e in client.list_environments(pid) if e.get("name")
    }
    target_env_name = str(scenario["environment"]).lower()
    target_env = env_objs[target_env_name]
    env_key = str(target_env["api_key"])
    env_id = int(target_env["id"])
    states = client.list_feature_states(env_key)
    features = client.list_features(pid)
    feat_map = {int(f["id"]): str(f["name"]) for f in features if "id" in f and "name" in f}

    for decoy in scenario.get("decoys", []):
        d_env_name = str(decoy["environment"]).lower()
        d_env = env_objs.get(d_env_name, target_env)
        d_key = str(d_env["api_key"])
        d_states = states if d_key == env_key else client.list_feature_states(d_key)
        apply_scenario_step(
            client,
            env_api_key=d_key,
            feature_states=d_states,
            feature_name=str(decoy["feature"]),
            change=dict(decoy["change"]),
            feature_map=feat_map,
            environment_id=int(d_env["id"]),
            project_id=pid,
        )
    culprit = scenario.get("culprit")
    if isinstance(culprit, dict):
        apply_scenario_step(
            client,
            env_api_key=env_key,
            feature_states=states,
            feature_name=str(culprit["feature"]),
            change=dict(culprit["change"]),
            feature_map=feat_map,
            environment_id=env_id,
            project_id=pid,
            kind=str(culprit.get("kind", "feature_state")),
            segment_name=culprit.get("segment"),
        )


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Run FlagRewind synthetic drill scenario.")
    parser.add_argument("--scenarios", type=Path, default=Path("drills/scenarios.yaml"))
    parser.add_argument("--scenario", help="Scenario ID (e.g. S01) to execute.")
    parser.add_argument("--project", default="Acme Checkout")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--reset", action="store_true", help="Reset Flagsmith demo project to healthy baseline."
    )
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

    with FlagsmithClient(api_url, drill_key) as client:
        if args.reset:
            reset_demo_project(client, project_name=args.project)
            print("SYNTHETIC Flagsmith project restored to clean healthy baseline.")
            return 0
        target = next((s for s in scenarios if s["id"] == args.scenario), scenarios[0])
        apply_full_scenario(client, project_name=args.project, scenario=target)
    print(f"SYNTHETIC drill {target['id']} applied cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
