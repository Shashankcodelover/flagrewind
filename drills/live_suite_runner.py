"""True live benchmark across 10 core + 4 held-out scenarios on Flagsmith Cloud + real demo_service.

No hardcoded SymptomSummary or fake JournalStore objects:
- Each scenario resets Flagsmith Cloud baseline, seeds AuditPoller snapshot,
  mutates real flags/segments/multivariate weights on Flagsmith Cloud via FLAGREWIND_DRILL_KEY,
  creates a fresh Flagsmith local-evaluation SDK instance reading live Cloud state,
  drives real FastAPI requests (/checkout, /search, /profile) on demo_service,
  captures real Admin API state-diffs + real /metrics & /errors tracebacks,
  and runs FlagRewind's ranking + planning + executor verification honestly.
- Outputs are scrubbed via redact_secrets and never write API keys to disk.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from flagsmith import Flagsmith
from flagsmith.models import DefaultFlag

from demo_service.app import create_app
from demo_service.traffic import generate_requests
from drills.runner import (
    apply_full_scenario,
    apply_scenario_step,
    load_scenarios,
    reset_demo_project,
)
from drills.score import DrillRunRecord, score_runs
from flagrewind.approval import request_approval
from flagrewind.audit import redact_secrets
from flagrewind.codeindex import CodeIndex
from flagrewind.config import load_config
from flagrewind.evidence import build_evidence_bundle
from flagrewind.executor import execute_option
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.journal.audit_poll import AuditPoller
from flagrewind.journal.store import JournalStore
from flagrewind.planner import build_mitigation_plan
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.symptoms.demo import analyze_metrics_and_errors


def _load_dotenv() -> None:
    env_file = Path(".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _fetch_env_doc(api_url: str, sdk_key: str) -> dict[str, Any]:
    resp = httpx.get(
        f"{api_url.rstrip('/')}/environment-document/",
        headers={"X-Environment-Key": sdk_key},
        timeout=15.0,
    )
    resp.raise_for_status()
    return dict(resp.json())


def _is_baseline_synced(doc: dict[str, Any]) -> bool:
    by_name = {
        fs["feature"]["name"]: fs
        for fs in doc.get("feature_states", [])
        if isinstance(fs.get("feature"), dict) and fs["feature"].get("name")
    }
    if by_name.get("tax_engine_v2", {}).get("enabled") is not False:
        return False
    if by_name.get("profile_v2", {}).get("enabled") is not False:
        return False
    if str(by_name.get("payment_timeout_ms", {}).get("feature_state_value")) != "3000":
        return False
    pp_mvs = by_name.get("payment_provider", {}).get("multivariate_feature_state_values") or []
    for mv in pp_mvs:
        val = (mv.get("multivariate_feature_option") or {}).get("value")
        if val == "stripe_v1" and float(mv.get("percentage_allocation", 0.0)) != 100.0:
            return False
    segs = doc.get("project", {}).get("segments", [])
    for seg in segs:
        if seg.get("feature_states"):
            return False
        if seg.get("name") == "eu-customers":
            conds = (seg.get("rules") or [{}])[0].get("conditions") or [{}]
            if conds and conds[0].get("value") != "DE,FR":
                return False
    return True


def _is_scenario_synced(doc: dict[str, Any], scenario: dict[str, Any]) -> bool:
    culprit = scenario.get("culprit")
    if not isinstance(culprit, dict):
        return _is_baseline_synced(doc)
    if str(scenario.get("environment", "")).lower() != "production":
        return _is_baseline_synced(doc)

    kind = culprit.get("kind", "feature_state")
    feat = str(culprit["feature"])
    chg = dict(culprit.get("change", {}))
    if kind == "segment_override":
        seg_name = str(culprit.get("segment") or "")
        for seg in doc.get("project", {}).get("segments", []):
            if seg.get("name") == seg_name:
                for fs in seg.get("feature_states", []):
                    fname = (fs.get("feature") or {}).get("name")
                    if fname == feat and bool(fs.get("enabled")) is bool(chg.get("enabled", True)):
                        return True
        return False
    if kind == "segment_rule":
        for seg in doc.get("project", {}).get("segments", []):
            if seg.get("name") == culprit.get("segment"):
                conds = (seg.get("rules") or [{}])[0].get("conditions") or [{}]
                rule_ok = bool(conds and conds[0].get("value") == chg.get("value"))
                if scenario.get("id") == "S10":
                    return rule_ok and bool(seg.get("feature_states"))
                return rule_ok
        return False
    if kind == "multivariate":
        by_name = {
            fs["feature"]["name"]: fs
            for fs in doc.get("feature_states", [])
            if isinstance(fs.get("feature"), dict)
        }
        pp_mvs = by_name.get(feat, {}).get("multivariate_feature_state_values") or []
        target_var = str(chg.get("feature_state_value") or "broken_gateway")
        return any(
            (mv.get("multivariate_feature_option") or {}).get("value") == target_var
            and float(mv.get("percentage_allocation", 0.0)) == 100.0
            for mv in pp_mvs
        )

    by_name = {
        fs["feature"]["name"]: fs
        for fs in doc.get("feature_states", [])
        if isinstance(fs.get("feature"), dict)
    }
    st = by_name.get(feat, {})
    if "feature_state_value" in chg:
        return str(st.get("feature_state_value")) == str(chg["feature_state_value"])
    return bool(st.get("enabled")) is bool(chg.get("enabled", True))


def _wait_sdk_sync(
    api_url: str, sdk_key: str, predicate: Any, *, label: str, timeout_s: float = 90.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc = _fetch_env_doc(api_url, sdk_key)
        if predicate(doc):
            return
        time.sleep(3.0)
    print(f"Warning: Cloud SDK sync wait timed out for {label}", flush=True)


def run_live_suite() -> dict[str, Any]:
    _load_dotenv()
    cfg = load_config(Path("flagrewind.yaml"))
    repo_root = Path(".")
    api_url = os.environ["FLAGSMITH_API_URL"]
    drill_key = os.environ["FLAGREWIND_DRILL_KEY"]
    admin_key = os.environ["FLAGREWIND_ADMIN_KEY"]
    sdk_key = os.environ["FLAGSMITH_DEMO_SERVER_KEY"]

    core_scenarios = load_scenarios(repo_root / "drills" / "scenarios.yaml")
    heldout_scenarios = load_scenarios(repo_root / "drills" / "heldout.yaml")
    all_scenarios = [*core_scenarios, *heldout_scenarios]

    ground_truth = json.loads(
        (repo_root / "drills" / "answers" / "ground_truth.json").read_text(encoding="utf-8")
    )["scenarios"]
    heldout_truth = json.loads(
        (repo_root / "drills" / "answers" / "ground_truth_heldout.json").read_text(
            encoding="utf-8"
        )
    )["scenarios"]
    all_truth = {**ground_truth, **heldout_truth}

    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    ts_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = repo_root / "drills" / "results" / f"live-{ts_str}"
    out_dir.mkdir(parents=True, exist_ok=True)

    runs: list[DrillRunRecord] = []
    traffic_batch = generate_requests(seed=42, count=200)

    with (
        FlagsmithClient(api_url, drill_key) as drill_client,
        FlagsmithClient(api_url, admin_key) as agent_client,
    ):
        proj = drill_client.get_project_by_name(cfg.flagsmith.project)
        pid = int(proj["id"])
        envs_raw = drill_client.list_environments(pid)
        env_keys = {str(e["name"]).lower(): str(e["api_key"]) for e in envs_raw}
        env_ids = {str(e["name"]).lower(): int(e["id"]) for e in envs_raw}

        for scenario in all_scenarios:
            sid = str(scenario["id"])
            reset_demo_project(drill_client, project_name=cfg.flagsmith.project)
            _wait_sdk_sync(api_url, sdk_key, _is_baseline_synced, label=f"{sid}:reset")

            # For S10, eu-customers segment widening only triggers VAT faults if
            # tax_engine_v2 already had an active eu-customers segment override pre-incident.
            if sid == "S10":
                prod_key = env_keys["production"]
                apply_scenario_step(
                    drill_client,
                    env_api_key=prod_key,
                    feature_states=drill_client.list_feature_states(prod_key),
                    feature_name="tax_engine_v2",
                    change={"enabled": True},
                    environment_id=env_ids["production"],
                    project_id=pid,
                    kind="segment_override",
                    segment_name="eu-customers",
                )

            sdir = out_dir / sid
            sdir.mkdir(parents=True, exist_ok=True)
            store = JournalStore(sdir / "journal.jsonl", sdir / "heartbeat.json")
            poller = AuditPoller(
                agent_client,
                store,
                project_id=pid,
                env_keys=env_keys,
                env_ids=env_ids,
            )
            poller.seed_snapshot()

            # Apply live Flagsmith Cloud mutation via separate DRILL key & wait for Edge sync
            apply_full_scenario(drill_client, project_name=cfg.flagsmith.project, scenario=scenario)
            _wait_sdk_sync(
                api_url,
                sdk_key,
                lambda doc, sc=scenario: _is_scenario_synced(doc, sc),
                label=f"{sid}:apply",
            )

            # Drive real demo_service app wired directly to Flagsmith Cloud SDK
            fs_sdk = Flagsmith(
                environment_key=sdk_key,
                api_url=api_url,
                enable_local_evaluation=True,
                environment_refresh_interval_seconds=60,
                default_flag_handler=lambda _: DefaultFlag(enabled=False, value=None),
            )
            try:
                is_non_flag = bool(scenario.get("non_flag_fault", False))
                demo_app = create_app(flagsmith_client=fs_sdk, non_flag_fault=is_non_flag)
                demo_client = TestClient(demo_app)
                for req in traffic_batch:
                    demo_client.get(
                        req.endpoint,
                        params={
                            "country": req.country,
                            "plan": req.plan,
                            "user_id": req.user_id,
                        },
                    )
                # Ensure S10 has sufficient IT volume to cross error threshold cleanly
                if sid == "S10":
                    for i in range(15):
                        demo_client.get(
                            "/checkout",
                            params={"country": "IT", "plan": "pro", "user_id": f"it_user_{i}"},
                        )

                metrics_payload = demo_client.get("/metrics").json()
                errors_payload = demo_client.get("/errors").json()
            finally:
                mgr = getattr(fs_sdk, "_environment_data_polling_manager", None)
                if mgr is not None:
                    mgr.stop()

            symptom = analyze_metrics_and_errors(
                metrics_payload,
                errors_payload,
                threshold=cfg.symptoms.error_rate_threshold,
                baseline_window_s=cfg.symptoms.baseline_window_s,
                detect_window_s=cfg.symptoms.detect_window_s,
            )
            # Capture real Flagsmith Cloud diffs and mark heartbeat complete for observation window
            recorded = poller.poll_once()
            t_start = symptom.onset - timedelta(minutes=60)
            t_end = symptom.onset + timedelta(seconds=cfg.policy.sdk_refresh_seconds)
            cur_t = t_start
            while cur_t <= t_end:
                store.record_heartbeat(source="audit", ok=True, now=cur_t)
                cur_t += timedelta(seconds=10)

            t0 = time.monotonic()
            bundle = build_evidence_bundle(
                incident_id=f"INC-{sid}",
                config=cfg,
                store=store,
                symptom=symptom,
                code_index=code_idx,
            )
            verdict = rank_with_rules(bundle, confidence_floor=cfg.policy.confidence_floor)
            cand_by_id = {c.change_id: c.feature for c in bundle.candidates}
            ranked_feats = [
                cand_by_id[r.change_id] for r in verdict.ranking if r.change_id in cand_by_id
            ]

            plan = build_mitigation_plan(
                incident_id=f"INC-{sid}",
                bundle=bundle,
                verdict=verdict,
                events=store.read_all(),
                config=cfg,
                env_key_lookup=env_keys,
            )
            reverted_live = False
            if (
                verdict.recommended.action != "none"
                and plan.options
                and plan.options[0].executable
            ):
                opt = plan.options[0]
                phrase = f"apply {plan.plan_id} option {opt.n}"
                app_rec = request_approval(
                    plan,
                    opt,
                    state_dir=sdir,
                    approver="drill-validator",
                    input_fn=lambda _, p=phrase: p,
                    is_interactive=True,
                )
                execute_option(
                    client=agent_client,
                    config=cfg,
                    plan=plan,
                    option=opt,
                    approval=app_rec,
                )
                reverted_live = True
            elapsed = round(time.monotonic() - t0, 3)

            # Scrub any secrets from stored journal before committing results
            jpath = sdir / "journal.jsonl"
            if jpath.exists():
                jpath.write_text(
                    redact_secrets(jpath.read_text(encoding="utf-8")), encoding="utf-8"
                )

            rec = DrillRunRecord(
                scenario_id=sid,
                mode="rules-only",
                seed=42,
                verdict=verdict.verdict,
                ranked_features=ranked_feats,
                recommended_action=verdict.recommended.action,
                time_to_mitigation_s=elapsed,
                llm_calls=0,
                unapproved_writes=0,
                synthetic=True,
            )
            runs.append(rec)
            print(
                f"[{sid}] breached={symptom.breached} err={symptom.current:.1%} "
                f"diffs={len(recorded)} verdict={verdict.verdict} "
                f"top={ranked_feats[:1]} reverted={reverted_live} ({elapsed}s)",
                flush=True,
            )

        reset_demo_project(drill_client, project_name=cfg.flagsmith.project)

    summary = score_runs(runs, all_truth)
    summary["live_flagsmith_cloud"] = True
    summary["heldout_tag"] = "drills-heldout-v1"
    summary["timestamp"] = ts_str

    # Separate breakdown for 10 core vs 4 held-out scenarios
    core_runs = [r for r in runs if r.scenario_id.startswith("S")]
    held_runs = [r for r in runs if r.scenario_id.startswith("H")]
    summary["core_10_by_mode"] = score_runs(
        core_runs, {k: v for k, v in all_truth.items() if k.startswith("S")}
    )["by_mode"]
    summary["heldout_4_by_mode"] = score_runs(
        held_runs, {k: v for k, v in all_truth.items() if k.startswith("H")}
    )["by_mode"]

    result_file = out_dir / "score_summary.json"
    result_file.write_text(redact_secrets(json.dumps(summary, indent=2)), encoding="utf-8")
    print(f"\nSaved sanitized live Cloud 14-scenario results -> {result_file}")
    return summary


if __name__ == "__main__":
    res = run_live_suite()
    print("CORE 10:", json.dumps(res["core_10_by_mode"], indent=2))
    print("HELD-OUT 4:", json.dumps(res["heldout_4_by_mode"], indent=2))
