"""Unit tests validating all 10 scenarios, answer isolation, and benchmark scoring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from drills.runner import load_scenarios, select_scenario_order
from drills.score import DrillRunRecord, score_runs
from flagrewind.codeindex import CodeIndex
from flagrewind.config import load_config
from flagrewind.evidence import build_evidence_bundle
from flagrewind.journal.store import JournalStore
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.symptoms.demo import analyze_metrics_and_errors


def _make_metrics_and_errors(
    scenario_id: str, onset: datetime, repo_root: Path
) -> tuple[dict, dict]:
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
        ),
        "H01": ("KeyError", "Missing pro_badge in profile_v2", "US", "free", "render_profile"),
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


def test_scenarios_and_ground_truth_isolation() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "flagrewind.yaml")
    scenarios = load_scenarios(repo_root / "drills" / "scenarios.yaml")
    answers_path = repo_root / "drills" / "answers" / "ground_truth.json"
    truth = json.loads(answers_path.read_text(encoding="utf-8"))["scenarios"]

    assert [s["id"] for s in scenarios] == [f"S{i:02d}" for i in range(1, 11)]
    assert set(truth) == {s["id"] for s in scenarios}
    for code_path in cfg.code.paths:
        assert "drills" not in code_path and "answers" not in code_path

    order_a = select_scenario_order(scenarios, seed=7)
    order_b = select_scenario_order(scenarios, seed=7)
    assert order_a == order_b


def test_all_10_scenarios_rules_pipeline_and_scorer(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "flagrewind.yaml")
    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    scenarios = load_scenarios(repo_root / "drills" / "scenarios.yaml")
    answers_path = repo_root / "drills" / "answers" / "ground_truth.json"
    truth = json.loads(answers_path.read_text(encoding="utf-8"))["scenarios"]

    runs: list[DrillRunRecord] = []
    for scenario in scenarios:
        sid = scenario["id"]
        sdir = tmp_path / sid
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

        metrics, errors = _make_metrics_and_errors(sid, onset, repo_root)
        symptom = analyze_metrics_and_errors(
            metrics, errors, threshold=0.05, baseline_window_s=120, detect_window_s=30, now=onset
        )
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

        runs.append(
            DrillRunRecord(
                scenario_id=sid,
                mode="rules-only",
                seed=42,
                verdict=verdict.verdict,
                ranked_features=ranked_feats,
                recommended_action=verdict.recommended.action,
                time_to_mitigation_s=12.0,
                time_to_recovery_s=20.0,
                unapproved_writes=0,
            )
        )

    summary = score_runs(runs, truth)
    rules_metrics = summary["by_mode"]["rules-only"]
    assert summary["synthetic"] is True
    assert rules_metrics["top1_accuracy"] == 1.0
    assert rules_metrics["abstention_accuracy"] == 1.0
    assert rules_metrics["action_correctness"] == 1.0
    assert rules_metrics["unapproved_writes"] == 0


def test_heldout_scenarios_validation(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "flagrewind.yaml")
    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    scenarios = load_scenarios(repo_root / "drills" / "heldout.yaml")
    truth = json.loads(
        (repo_root / "drills" / "answers" / "ground_truth_heldout.json").read_text(encoding="utf-8")
    )["scenarios"]

    assert len(scenarios) == 4
    runs: list[DrillRunRecord] = []
    onset = datetime(2026, 11, 26, 14, 0, 0, tzinfo=UTC)

    for scenario in scenarios:
        sid = scenario["id"]
        journal_file = tmp_path / f"heldout_{sid}.jsonl"
        store = JournalStore(journal_file)

        culprit = scenario.get("culprit")
        if culprit:
            store.append(
                {
                    "id": f"CHG-{sid}-01",
                    "source": "audit" if culprit.get("kind") == "segment_rule" else "webhook",
                    "occurred_at": (
                        onset + timedelta(minutes=culprit.get("offset_minutes", -5))
                    ).isoformat(),
                    "received_at": (
                        onset + timedelta(minutes=culprit.get("offset_minutes", -5))
                    ).isoformat(),
                    "environment": scenario.get("environment", "production"),
                    "feature": culprit["feature"],
                    "kind": culprit.get("kind", "feature_state"),
                    "segment": culprit.get("segment"),
                    "previous": (
                        {"enabled": False, "value": None}
                        if culprit.get("kind") != "multivariate"
                        else {"enabled": True, "value": "stripe_v1"}
                    ),
                    "new": (
                        {
                            "enabled": True,
                            "value": culprit.get("change", {}).get("feature_state_value"),
                        }
                        if culprit.get("kind") == "feature_state"
                        else culprit.get("change", {"enabled": True, "value": None})
                    ),
                    "actor": "drill-runner",
                    "raw_ref": {},
                }
            )

        for i, decoy in enumerate(scenario.get("decoys", [])):
            store.append(
                {
                    "id": f"CHG-{sid}-D{i+1:02d}",
                    "source": "webhook",
                    "occurred_at": (
                        onset + timedelta(minutes=decoy.get("offset_minutes", -10 - i))
                    ).isoformat(),
                    "received_at": (
                        onset + timedelta(minutes=decoy.get("offset_minutes", -10 - i))
                    ).isoformat(),
                    "environment": decoy.get("environment", "production"),
                    "feature": decoy["feature"],
                    "kind": "feature_state",
                    "segment": None,
                    "previous": {"enabled": False, "value": None},
                    "new": decoy.get("change", {"enabled": True, "value": None}),
                    "actor": "drill-runner",
                    "raw_ref": {},
                }
            )

        metrics, errors = _make_metrics_and_errors(sid, onset, repo_root)
        symptom = analyze_metrics_and_errors(
            metrics, errors, threshold=0.05, baseline_window_s=120, detect_window_s=30, now=onset
        )
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

        runs.append(
            DrillRunRecord(
                scenario_id=sid,
                mode="rules-only",
                seed=42,
                verdict=verdict.verdict,
                ranked_features=ranked_feats,
                recommended_action=verdict.recommended.action,
                time_to_mitigation_s=10.0,
                time_to_recovery_s=18.0,
                unapproved_writes=0,
            )
        )

    summary = score_runs(runs, truth)
    rules_metrics = summary["by_mode"]["rules-only"]
    assert rules_metrics["runs"] == 4
    assert rules_metrics["unapproved_writes"] == 0
    # Logged results without assumption

    for run in runs:
        assert run.verdict == truth[run.scenario_id]["expected_verdict"]
