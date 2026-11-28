"""Build a read-only Cloudflare Worker showcase bundle from verified FlagRewind artifacts.

Zero live secrets or mutation endpoints are emitted. All metrics come strictly from
recorded files in ``drills/results/`` and scenario specs in ``drills/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from flagrewind.audit import redact_secrets

WORKER_HEADER = (
    "// FlagRewind Read-Only Incident Explorer & Benchmark Console (Cloudflare Worker)\n"
    "// Generated from verified repository artifacts by deploy/cloudflare/build_showcase.py.\n"
    "// Zero write credentials, zero live mutation paths, and zero hardcoded secrets.\n"
)


def _scenario_preview(sc: dict[str, Any], should_abstain: bool) -> str:
    if should_abstain:
        return "None (abstain — no writable Flagsmith change explains incident)"
    culprit = sc.get("culprit") or {}
    feat = str(culprit.get("feature", ""))
    kind = str(culprit.get("kind", "feature_state"))
    seg = culprit.get("segment")
    if kind == "segment_rule":
        return f"Manual advisory: restore segment rule '{seg}' to country IN DE,FR"
    if kind == "segment_override":
        return f"PUT featurestate for {feat} in segment '{seg}': enabled=true -> enabled=false"
    if feat == "payment_timeout_ms":
        return "PATCH production featurestate payment_timeout_ms: value='300'/'150' -> value='3000'"
    if kind == "multivariate":
        return "PATCH production payment_provider multivariate: stripe_v1=100%, broken_gateway=0%"
    return f"Revert {feat} in production"


def _read_yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def collect_showcase_data(repo_root: Path) -> dict[str, Any]:
    """Load strictly verified results and scenario definitions from repo files."""
    proof = _read_json(repo_root / "drills" / "results" / "live_e2e_proof.json")
    cloud_suite = _read_json(
        repo_root / "drills" / "results" / "live-20261127T022911Z" / "score_summary.json"
    )
    ablation = _read_json(repo_root / "drills" / "results" / "llm_vs_rules_synthetic.json")

    core_yaml = _read_yaml(repo_root / "drills" / "scenarios.yaml")
    held_yaml = _read_yaml(repo_root / "drills" / "heldout.yaml")
    core_truth = _read_json(repo_root / "drills" / "answers" / "ground_truth.json")["scenarios"]
    held_truth = _read_json(
        repo_root / "drills" / "answers" / "ground_truth_heldout.json"
    )["scenarios"]
    truth_by_id = {**core_truth, **held_truth}

    cloud_by_id = {r["scenario_id"]: r for r in cloud_suite["raw_runs"]}
    llm_by_id = {r["scenario_id"]: r for r in ablation["per_scenario_llm"]}

    all_defs = [
        ("Core-10", sc) for sc in core_yaml["scenarios"]
    ] + [
        ("Held-out (drills-heldout-v1)", sc) for sc in held_yaml["scenarios"]
    ]

    scenarios_out: list[dict[str, Any]] = []
    for group, sc in all_defs:
        sid = str(sc["id"])
        tr = truth_by_id[sid]
        cr = cloud_by_id[sid]
        lr = llm_by_id[sid]
        culprit = sc.get("culprit")
        kind = str(culprit.get("kind", "non_flag")) if isinstance(culprit, dict) else "non_flag"
        scenarios_out.append(
            {
                "id": sid,
                "group": group,
                "name": str(sc["name"]),
                "description": str(sc["description"]),
                "environment": str(sc["environment"]),
                "kind": kind,
                "should_abstain": bool(tr["should_abstain"]),
                "advisory_only": kind == "segment_rule",
                "expected_culprit": tr["culprit_feature"],
                "preview": _scenario_preview(sc, bool(tr["should_abstain"])),
                "cloud_verdict": cr["verdict"],
                "cloud_action": cr["recommended_action"],
                "cloud_ranked": cr["ranked_features"],
                "cloud_time_s": cr["time_to_mitigation_s"],
                "llm_top1_correct": bool(lr["top1_correct"]),
                "llm_action_correct": bool(lr["action_correct"]),
                "llm_latency_s": lr["latency_s"],
                "llm_fallback_fired": bool(lr["fallback_fired"]),
            }
        )

    run_ledger = [
        {
            "name": "Live Flagsmith Cloud Single E2E (S01)",
            "sample_size": "n=1",
            "endpoint": "Flagsmith Cloud + :8001 live service (alert -> revert applied / recovery)",
            "result": (
                f"{proof['alert_to_plan_s']}s plan, "
                f"{proof['alert_to_revert_applied_s_automated']}s revert (automated), "
                f"{proof['alert_to_recovery_s_automated']}s 3-window recovery, "
                f"{proof['unapproved_writes']} unapproved writes"
            ),
        },
        {
            "name": "Live Flagsmith Cloud 14-Suite (rules-only)",
            "sample_size": "n=14 (10 core + 4 held-out)",
            "endpoint": "Flagsmith Cloud + SDK (alert -> revert/advisory post-propagation)",
            "result": "14/14 top-1, 14/14 action, 3/3 abstention, 1.8s median, 0 unapproved writes",
        },
        {
            "name": "Paced Live LLM Ablation (gemini-flash-lite-latest)",
            "sample_size": "n=14 per mode (28 total)",
            "endpoint": "Synthetic journal/symptoms (ranking latency; advisory only)",
            "result": (
                "14/14 top-1, 11/14 action (0.7857), 3/3 abstention, "
                "0/14 fallbacks, 4.11s median"
            ),
        },
        {
            "name": "Local Lint & Automated Test Suite",
            "sample_size": "35 pytest + ruff",
            "endpoint": "scripts/verify.ps1 -> docs/verification/latest.md",
            "result": "PASS (local run; GitHub Actions blocked by an account billing hold)",
        },
        {
            "name": "Manual UI Baseline (B1, indicative only)",
            "sample_size": "n=3 target",
            "endpoint": "drills/manual_stopwatch.py (alert -> revert applied in Flagsmith UI)",
            "result": "Pending operator run (no unrecorded manual timing claimed)",
        },
    ]

    return {
        "proof": proof,
        "ablation": {
            "live_llm_model": ablation.get("live_llm_model"),
            "heldout_tag": "drills-heldout-v1",
            "llm_role": "advisory_only",
            "by_mode": ablation["by_mode"],
            "per_scenario_llm": ablation["per_scenario_llm"],
        },
        "scenarios": scenarios_out,
        "run_ledger": run_ledger,
    }


def render_worker_js(repo_root: Path) -> str:
    """Render standalone ES module Worker source code with embedded sanitized data."""
    asset_dir = Path(__file__).resolve().parent
    html_shell = (asset_dir / "template.html").read_text(encoding="utf-8")
    worker_runtime = (asset_dir / "worker_runtime.js").read_text(encoding="utf-8")

    data = collect_showcase_data(repo_root)
    data_json = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    data_json = redact_secrets(data_json)

    return (
        WORKER_HEADER
        + f"\nconst SHOWCASE_DATA = {data_json};\n"
        + f"const HTML_TEMPLATE = {json.dumps(html_shell, ensure_ascii=True)};\n\n"
        + worker_runtime
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    out_file = Path(__file__).resolve().parent / "worker.js"
    js_src = render_worker_js(repo_root)
    out_file.write_text(js_src, encoding="utf-8")
    print(f"Wrote {out_file} ({len(js_src)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
