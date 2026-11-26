"""Unit tests for rules ranking, planning, approval, executor, verifier, doctor, and CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from rich.console import Console
from typer.testing import CliRunner

from flagrewind.approval import request_approval
from flagrewind.capabilities import run_doctor
from flagrewind.cli import app
from flagrewind.codeindex import CodeIndex
from flagrewind.config import ExitCode, load_config
from flagrewind.evidence import build_evidence_bundle
from flagrewind.executor import ExecutorError, PreconditionDriftError, execute_option
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.journal.store import JournalStore
from flagrewind.planner import ApiOperation, build_mitigation_plan, compute_plan_hash
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.report import render_brief, write_incident_report
from flagrewind.symptoms.demo import DemoSymptomReader, analyze_metrics_and_errors
from flagrewind.verifier import verify_mitigation


def _seed_scenario1(tmp_path: Path, repo_root: Path):
    cfg = load_config(repo_root / "flagrewind.yaml")
    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")
    onset = datetime(2026, 11, 26, 9, 30, tzinfo=UTC)
    for sec in range(0, 301, 30):
        store.record_heartbeat(
            source="audit", ok=True, now=onset - timedelta(minutes=5) + timedelta(seconds=sec)
        )
    ev = store.append(
        {
            "source": "webhook",
            "occurred_at": onset - timedelta(minutes=4),
            "received_at": onset - timedelta(minutes=4),
            "environment": "production",
            "feature": "tax_engine_v2",
            "kind": "segment_override",
            "segment": "eu-customers",
            "previous": {"enabled": False, "value": None},
            "new": {"enabled": True, "value": None},
            "actor": "drill@example.com",
            "raw_ref": {"feature_state_id": 55, "environment_api_key": "env-prod"},
        }
    )
    metrics = {
        "windows": [
            {
                "window_start": (onset - timedelta(seconds=20)).isoformat(),
                "epoch_s": int((onset - timedelta(seconds=20)).timestamp()),
                "total": 20,
                "errors": 0,
                "error_rate": 0.0,
                "by_country": {"US": {"total": 10, "errors": 0}, "DE": {"total": 10, "errors": 0}},
                "by_plan": {"free": {"total": 20, "errors": 0}},
                "by_endpoint": {"/checkout": {"total": 20, "errors": 0}},
            },
            {
                "window_start": onset.isoformat(),
                "epoch_s": int(onset.timestamp()),
                "total": 20,
                "errors": 6,
                "error_rate": 0.3,
                "by_country": {"US": {"total": 10, "errors": 0}, "DE": {"total": 10, "errors": 6}},
                "by_plan": {"free": {"total": 20, "errors": 6}},
                "by_endpoint": {"/checkout": {"total": 20, "errors": 6}},
            },
        ]
    }
    errors = {
        "errors": [
            {
                "type": "VATCalculationError",
                "message": "VAT calculation failed under tax_engine_v2 for country=DE",
                "frames": [
                    {
                        "file": str(repo_root / "demo_service" / "faults.py"),
                        "line": 63,
                        "function": "apply_tax_engine",
                    }
                ],
            }
        ]
    }
    symptom = analyze_metrics_and_errors(
        metrics, errors, threshold=0.05, baseline_window_s=120, detect_window_s=30, now=onset
    )
    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    bundle = build_evidence_bundle(
        incident_id="INC-0001",
        config=cfg,
        store=store,
        symptom=symptom,
        code_index=code_idx,
    )
    return cfg, store, ev, bundle


def test_rules_ranking_plan_approval_and_brief(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg, store, _, bundle = _seed_scenario1(tmp_path, repo_root)
    verdict = rank_with_rules(bundle, confidence_floor=cfg.policy.confidence_floor)
    assert verdict.verdict == "flag_change"
    assert verdict.recommended.change_id == "CHG-000001"
    assert verdict.recommended.action == "revert_change"

    plan = build_mitigation_plan(
        incident_id="INC-0001",
        bundle=bundle,
        verdict=verdict,
        events=store.read_all(),
        config=cfg,
    )
    assert len(plan.options) == 2
    assert plan.options[0].action == "revert_change"
    assert plan.options[0].operations[0].method == "PATCH"
    assert plan.options[0].operations[0].body == {"enabled": False}

    # Non-interactive approval always rejects
    denied = request_approval(plan, plan.options[0], state_dir=tmp_path, is_interactive=False)
    assert denied.approved is False

    # Wrong phrase rejects; exact phrase approves
    bad_answers = iter(["Alice", "wrong phrase"])
    bad = request_approval(
        plan,
        plan.options[0],
        state_dir=tmp_path,
        input_fn=lambda _: next(bad_answers),
        is_interactive=True,
    )
    assert bad.approved is False

    good_answers = iter([f"apply {plan.plan_id} option 1"])
    good = request_approval(
        plan,
        plan.options[0],
        state_dir=tmp_path,
        input_fn=lambda _: next(good_answers),
        is_interactive=True,
    )
    assert good.approved is True
    assert good.approver == "Alice"
    assert good.plan_hash == compute_plan_hash(plan, 1)

    console = Console(width=80, no_color=True, record=True)
    render_brief(
        console=console, bundle=bundle, verdict=verdict, plan=plan, ranking_mode="rules-only"
    )
    rendered = console.export_text()
    assert "INC-0001" in rendered
    assert "apply PLN-0001 option 1" in rendered
    md_path, json_path = write_incident_report(
        state_dir=tmp_path,
        bundle=bundle,
        verdict=verdict,
        plan=plan,
        ranking_mode="rules-only",
        approval=good,
    )
    assert md_path.exists() and json_path.exists()


@respx.mock
def test_executor_drift_allowlist_and_verifier_recovery(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg, store, _, bundle = _seed_scenario1(tmp_path, repo_root)
    verdict = rank_with_rules(bundle, confidence_floor=cfg.policy.confidence_floor)
    plan = build_mitigation_plan(
        incident_id="INC-0001",
        bundle=bundle,
        verdict=verdict,
        events=store.read_all(),
        config=cfg,
    )
    opt = plan.options[0]
    approval = request_approval(
        plan,
        opt,
        state_dir=tmp_path,
        approver="Shashank",
        input_fn=lambda _: f"apply {plan.plan_id} option 1",
        is_interactive=True,
    )

    base = "http://localhost:8000/api/v1/"
    # Drift detection: state already reverted (enabled=False instead of expected True)
    respx.get(f"{base}environments/env-prod/featurestates/55/").mock(
        return_value=httpx.Response(200, json={"id": 55, "enabled": False})
    )
    with FlagsmithClient(base, "agent-key") as fs:
        with pytest.raises(PreconditionDriftError):
            execute_option(client=fs, config=cfg, plan=plan, option=opt, approval=approval)

        # Reject non-writable env & DELETE
        staging_opt = opt.model_copy(update={"environment": "staging"})
        staging_plan = plan.model_copy(update={"options": [staging_opt]})
        staging_app = approval.model_copy(update={"plan_hash": compute_plan_hash(staging_plan, 1)})
        with pytest.raises(ExecutorError, match="non-writable"):
            execute_option(
                client=fs, config=cfg, plan=staging_plan, option=staging_opt, approval=staging_app
            )

        bad_op_opt = opt.model_copy(
            update={
                "operations": [
                    ApiOperation.model_construct(method="DELETE", path="x", body={})
                ]
            }
        )
        bad_plan = plan.model_copy(update={"options": [bad_op_opt]})
        bad_app = approval.model_copy(update={"plan_hash": compute_plan_hash(bad_plan, 1)})
        with pytest.raises(ExecutorError, match="HTTP method"):
            execute_option(
                client=fs, config=cfg, plan=bad_plan, option=bad_op_opt, approval=bad_app
            )

    # Normal execution + 3-window verification recovery
    respx.get(f"{base}environments/env-prod/featurestates/55/").mock(
        side_effect=[
            httpx.Response(200, json={"id": 55, "enabled": True}),
            httpx.Response(200, json={"id": 55, "enabled": False}),
        ]
    )
    respx.patch(f"{base}environments/env-prod/featurestates/55/").mock(
        return_value=httpx.Response(200, json={"id": 55, "enabled": False})
    )
    healthy_metrics = {
        "windows": [
            {
                "window_start": "2026-11-26T09:31:00Z",
                "epoch_s": 1000,
                "total": 20,
                "errors": 0,
                "error_rate": 0.0,
                "by_country": {},
                "by_plan": {},
                "by_endpoint": {},
            }
        ]
    }
    respx.get("http://localhost:8001/metrics").mock(
        return_value=httpx.Response(200, json=healthy_metrics)
    )
    respx.get("http://localhost:8001/errors").mock(
        return_value=httpx.Response(200, json={"errors": []})
    )

    with (
        FlagsmithClient(base, "agent-key") as fs,
        DemoSymptomReader(cfg.symptoms) as sym,
    ):
        res = execute_option(client=fs, config=cfg, plan=plan, option=opt, approval=approval)
        assert res.operations_applied == 1
        ver = verify_mitigation(
            client=fs,
            symptom_reader=sym,
            option=opt,
            config=cfg,
            poll_interval_s=0.0,
            sleep_fn=lambda _: None,
        )
        assert ver.state_verified is True
        assert ver.recovered is True
        assert ver.consecutive_healthy_windows == 3


@respx.mock
def test_doctor_and_cli_investigate_non_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg_src = (repo_root / "flagrewind.yaml").read_text(encoding="utf-8")
    cfg_path = tmp_path / "flagrewind.yaml"
    cfg_path.write_text(cfg_src, encoding="utf-8")

    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/")
    monkeypatch.setenv("FLAGREWIND_ADMIN_KEY", "agent-key")
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}organisations/").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(f"{base}projects/").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "name": "Acme Checkout"}])
    )
    respx.get(f"{base}environments/?project=10").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "production", "api_key": "env-prod"},
                {"name": "staging", "api_key": "env-stg"},
            ],
        )
    )
    respx.get(f"{base}audit/?page_size=5&project=10").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{base}environments/env-prod/featurestates/").mock(
        return_value=httpx.Response(200, json=[])
    )

    cfg = load_config(cfg_path)
    ok, report = run_doctor(cfg, tmp_path / ".flagrewind", console=Console(width=80, no_color=True))
    assert ok is True
    assert (tmp_path / ".flagrewind" / "capabilities.json").exists()
    assert any(
        c["check"] == "dry_run_write_scope" and c["status"] == "PASS" for c in report["checks"]
    )

    # CLI investigate against empty journal + non-flag demo error
    respx.get("http://localhost:8001/metrics").mock(
        return_value=httpx.Response(200, json={"windows": []})
    )
    respx.get("http://localhost:8001/errors").mock(
        return_value=httpx.Response(200, json={"errors": []})
    )
    runner = CliRunner()
    res = runner.invoke(app, ["--config", str(cfg_path), "--no-llm", "investigate"])
    assert res.exit_code == 0
    assert "NOT_FLAG_RELATED" in res.stdout

    # Non-interactive apply must reject with exit code 1
    apply_res = runner.invoke(app, ["--config", str(cfg_path), "apply", "INC-0001"])
    assert apply_res.exit_code == int(ExitCode.UNRESOLVED_OR_REJECTED)
