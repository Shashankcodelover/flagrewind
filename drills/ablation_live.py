"""Run live LLM vs rules-only ablation across all 14 scenarios (10 core + 4 held-out, SYNTHETIC)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from drills.runner import load_scenarios
from drills.score import DrillRunRecord, score_runs
from flagrewind.audit import AuditTrail
from flagrewind.codeindex import CodeIndex
from flagrewind.config import load_config
from flagrewind.evidence import build_evidence_bundle
from flagrewind.journal.store import JournalStore
from flagrewind.llm_client import OpenAICompatibleLLMClient
from flagrewind.ranking.llm import rank_with_llm
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.symptoms.demo import analyze_metrics_and_errors


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _scenario_symptoms(scenario_id: str, onset: datetime, repo_root: Path) -> tuple[dict, dict]:
    base_win = {
        "window_start": (onset - timedelta(seconds=20)).isoformat(),
        "epoch_s": int((onset - timedelta(seconds=20)).timestamp()),
        "total": 20,
        "errors": 0,
        "error_rate": 0.0,
        "by_country": {"US": {"total": 10, "errors": 0}, "DE": {"total": 10, "errors": 0}},
        "by_plan": {"free": {"total": 10, "errors": 0}, "pro": {"total": 10, "errors": 0}},
        "by_endpoint": {"/checkout": {"total": 20, "errors": 0}},
    }
    fault_map = {
        "S01": (
            "VATCalculationError",
            "VAT calculation failed under tax_engine_v2",
            "DE",
            "pro",
            "apply_tax_engine",
        ),
        "S02": (
            "PaymentTimeoutError",
            "Timed out at payment_timeout_ms=300",
            "US",
            "pro",
            "check_payment_timeout",
        ),
        "S03": ("KeyError", "Missing pro_badge in profile_v2", "US", "free", "render_profile"),
        "S04": (
            "VATCalculationError",
            "VAT calculation failed under tax_engine_v2",
            "DE",
            "pro",
            "apply_tax_engine",
        ),
        "S05": (
            "VATCalculationError",
            "VAT calculation failed under tax_engine_v2",
            "FR",
            "pro",
            "apply_tax_engine",
        ),
        "S06": (
            "VATCalculationError",
            "VAT calculation failed under tax_engine_v2",
            "DE",
            "pro",
            "apply_tax_engine",
        ),
        "S07": (
            "DatabaseConnectionError",
            "Database connection pool exhausted",
            "US",
            "pro",
            "charge_payment_provider",
        ),
        "S08": (
            "PaymentGatewayError",
            "payment_provider=broken_gateway rejected",
            "US",
            "pro",
            "charge_payment_provider",
        ),
        "S09": (
            "PaymentTimeoutError",
            "Timed out at payment_timeout_ms=300",
            "US",
            "pro",
            "check_payment_timeout",
        ),
        "S10": (
            "VATCalculationError",
            "VAT calculation failed under tax_engine_v2",
            "IT",
            "pro",
            "apply_tax_engine",
            "faults.py",
        ),
        "H01": (
            "KeyError",
            "Missing pro_badge in profile_v2",
            "US",
            "free",
            "render_profile",
            "faults.py",
        ),
        "H02": (
            "MemoryExhaustionError",
            "Host memory exhaustion in external service",
            "US",
            "pro",
            "apply_external_host_check",
            "external_infra.py",
        ),
        "H03": (
            "PaymentTimeoutError",
            "Timed out at payment_timeout_ms=150",
            "US",
            "pro",
            "check_payment_timeout",
            "faults.py",
        ),
        "H04": (
            "PaymentGatewayError",
            "payment_provider=broken_gateway rejected",
            "US",
            "pro",
            "charge_payment_provider",
            "faults.py",
        ),
    }
    item = fault_map[scenario_id]
    err_type, msg, cc, plan, fn = item[0], item[1], item[2], item[3], item[4]
    frame_file = item[5] if len(item) > 5 else "faults.py"
    breach_win = {
        "window_start": onset.isoformat(),
        "epoch_s": int(onset.timestamp()),
        "total": 20,
        "errors": 6,
        "error_rate": 0.3,
        "by_country": {
            "US": {"total": 10, "errors": 0 if cc != "US" else 6},
            cc: {"total": 10, "errors": 6},
        },
        "by_plan": {
            "pro": {"total": 10, "errors": 0 if plan != "pro" else 6},
            plan: {"total": 10, "errors": 6},
        },
        "by_endpoint": {"/checkout": {"total": 20, "errors": 6}},
    }
    errors = {
        "errors": [
            {
                "type": err_type,
                "message": msg,
                "frames": [
                    {
                        "file": str(repo_root / "demo_service" / frame_file),
                        "line": 63,
                        "function": fn,
                    }
                ],
            }
        ]
    }
    return {"windows": [base_win, breach_win]}, errors


def run_ablation(repo_root: Path, out_path: Path) -> dict[str, Any]:
    """Run all 14 scenarios (10 core + 4 held-out) in rules-only and live LLM modes."""
    _load_dotenv(repo_root / ".env")
    cfg = load_config(repo_root / "flagrewind.yaml")
    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    scenarios = load_scenarios(repo_root / "drills" / "scenarios.yaml")
    heldout_path = repo_root / "drills" / "heldout.yaml"
    if heldout_path.exists():
        scenarios.extend(load_scenarios(heldout_path))

    truth = json.loads((repo_root / "drills" / "answers" / "ground_truth.json").read_text())[
        "scenarios"
    ]
    heldout_truth_path = repo_root / "drills" / "answers" / "ground_truth_heldout.json"
    if heldout_truth_path.exists():
        truth.update(json.loads(heldout_truth_path.read_text())["scenarios"])
    llm_client = OpenAICompatibleLLMClient.from_config(cfg.llm, min_interval_s=4.5)
    if llm_client is None:
        raise RuntimeError("LLM env vars not configured; cannot run live LLM ablation.")

    runs: list[DrillRunRecord] = []
    per_scenario_llm: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for scenario in scenarios:
            sid = scenario["id"]
            sdir = tmp_dir / sid
            store = JournalStore(sdir / "journal.jsonl", sdir / "heartbeat.json")
            onset = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
            for sec in range(0, 901, 30):
                store.record_heartbeat(
                    source="audit",
                    ok=True,
                    now=onset - timedelta(minutes=15) + timedelta(seconds=sec),
                )
            for decoy in scenario.get("decoys", []):
                off = int(decoy["offset_minutes"])
                prev_decoy = not decoy["change"].get("enabled", True)
                store.append(
                    {
                        "source": "webhook",
                        "occurred_at": onset + timedelta(minutes=off),
                        "received_at": onset + timedelta(minutes=off),
                        "environment": decoy["environment"],
                        "feature": decoy["feature"],
                        "kind": "feature_state",
                        "segment": None,
                        "previous": {"enabled": prev_decoy, "value": None},
                        "new": {"enabled": decoy["change"].get("enabled", True), "value": None},
                        "actor": "drill@example.com",
                        "raw_ref": {},
                    }
                )
            culprit = scenario.get("culprit")
            if isinstance(culprit, dict):
                off = int(culprit["offset_minutes"])
                chg = culprit["change"]
                prev_val = 3000 if culprit["feature"] == "payment_timeout_ms" else None
                store.append(
                    {
                        "source": "audit" if culprit["kind"] == "segment_rule" else "webhook",
                        "occurred_at": onset + timedelta(minutes=off),
                        "received_at": onset + timedelta(minutes=off),
                        "environment": scenario["environment"],
                        "feature": culprit["feature"],
                        "kind": culprit["kind"],
                        "segment": culprit.get("segment"),
                        "previous": {"enabled": False, "value": prev_val},
                        "new": {
                            "enabled": bool(chg.get("enabled", True)),
                            "value": chg.get("feature_state_value"),
                        },
                        "actor": "drill@example.com",
                        "raw_ref": {},
                    }
                )

            metrics, errors = _scenario_symptoms(sid, onset, repo_root)
            symptom = analyze_metrics_and_errors(
                metrics,
                errors,
                threshold=0.05,
                baseline_window_s=120,
                detect_window_s=30,
                now=onset,
            )
            bundle = build_evidence_bundle(
                incident_id=f"INC-{sid}",
                config=cfg,
                store=store,
                symptom=symptom,
                code_index=code_idx,
            )
            cand_by_id = {c.change_id: c.feature for c in bundle.candidates}

            t0 = time.monotonic()
            r_verdict = rank_with_rules(bundle, confidence_floor=cfg.policy.confidence_floor)
            r_elapsed = round(time.monotonic() - t0, 3)
            runs.append(
                DrillRunRecord(
                    scenario_id=sid,
                    mode="rules-only",
                    seed=42,
                    verdict=r_verdict.verdict,
                    ranked_features=[
                        cand_by_id[r.change_id]
                        for r in r_verdict.ranking
                        if r.change_id in cand_by_id
                    ],
                    recommended_action=r_verdict.recommended.action,
                    time_to_mitigation_s=r_elapsed,
                    llm_calls=0,
                    unapproved_writes=0,
                )
            )

            audit = AuditTrail(sdir / "audit.jsonl")
            t1 = time.monotonic()
            l_verdict, l_mode, l_meta = rank_with_llm(
                bundle, config=cfg, llm_client=llm_client, audit_trail=audit
            )
            l_elapsed = round(time.monotonic() - t1, 3)
            print(
                f"[{sid}] mode={l_mode} verdict={l_verdict.verdict} "
                f"calls={l_meta.get('calls')} tokens={l_meta.get('tokens')} ({l_elapsed}s)",
                flush=True,
            )
            ranked_llm = [
                cand_by_id[r.change_id]
                for r in l_verdict.ranking
                if r.change_id in cand_by_id
            ]
            truth_row = truth[sid]
            exp_culprit = truth_row["culprit_feature"]
            should_abstain = bool(truth_row["should_abstain"])
            top1_ok = (
                l_verdict.recommended.action == "none"
                if should_abstain
                else bool(ranked_llm and ranked_llm[0] == exp_culprit)
            )
            abstain_ok = (l_verdict.recommended.action == "none") if should_abstain else None
            per_scenario_llm.append(
                {
                    "scenario_id": sid,
                    "model": llm_client.model,
                    "mode": l_mode,
                    "top1_correct": top1_ok,
                    "abstention_correct": abstain_ok,
                    "action_correct": l_verdict.recommended.action == truth_row["expected_action"],
                    "tokens": l_meta.get("tokens"),
                    "latency_s": l_elapsed,
                    "llm_calls": int(l_meta.get("calls") or 0),
                    "fallback_fired": l_mode != f"llm ({llm_client.model})",
                }
            )
            runs.append(
                DrillRunRecord(
                    scenario_id=sid,
                    mode=l_mode,
                    seed=42,
                    verdict=l_verdict.verdict,
                    ranked_features=ranked_llm,
                    recommended_action=l_verdict.recommended.action,
                    time_to_mitigation_s=l_elapsed,
                    llm_calls=int(l_meta.get("calls") or 0),
                    tokens=l_meta.get("tokens"),
                    unapproved_writes=0,
                )
            )

    summary = score_runs(runs, truth)
    summary["live_llm_model"] = llm_client.model
    summary["live_flagsmith_cloud"] = False
    summary["per_scenario_llm"] = per_scenario_llm
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    res = run_ablation(root, root / "drills" / "results" / "llm_vs_rules_synthetic.json")
    print(json.dumps(res["by_mode"], indent=2))
