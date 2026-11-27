"""Unit tests for Flagsmith client, webhook/audit journal, symptoms, code index, and evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from flagrewind.codeindex import CodeIndex
from flagrewind.config import load_config
from flagrewind.evidence import build_evidence_bundle
from flagrewind.flagsmith_client import FlagsmithAuthOrReachabilityError, FlagsmithClient
from flagrewind.journal.audit_poll import AuditPoller
from flagrewind.journal.store import JournalStore
from flagrewind.journal.webhook import create_webhook_app, verify_flagsmith_signature
from flagrewind.symptoms.demo import analyze_metrics_and_errors


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_webhook_hmac_verification_and_missing_previous_state(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")
    app = create_webhook_app(store, secret="top-secret")
    client = TestClient(app)

    payload = {
        "event_type": "FLAG_UPDATED",
        "timestamp": "2026-11-26T09:00:00Z",
        "data": {
            "changed_by": "drill@flagrewind.local",
            "previous_state": None,
            "new_state": {
                "id": 77,
                "enabled": True,
                "feature_state_value": None,
                "environment": {"id": 2, "name": "production"},
                "feature": {"id": 5, "name": "tax_engine_v2", "type": "STANDARD"},
                "feature_segment": {"segment": {"id": 1, "name": "eu-customers"}},
            },
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    assert verify_flagsmith_signature(raw, _sign(raw, "top-secret"), "top-secret")
    assert not verify_flagsmith_signature(raw, "bad-sig", "top-secret")

    bad_resp = client.post(
        "/webhooks/flagsmith",
        content=raw,
        headers={"X-Flagsmith-Signature": "bad", "Content-Type": "application/json"},
    )
    assert bad_resp.status_code == 401

    ok_resp = client.post(
        "/webhooks/flagsmith",
        content=raw,
        headers={
            "X-Flagsmith-Signature": _sign(raw, "top-secret"),
            "Content-Type": "application/json",
        },
    )
    assert ok_resp.status_code == 200
    events = store.read_all()
    assert len(events) == 1
    assert events[0].id == "CHG-000001"
    assert events[0].kind == "segment_override"
    assert events[0].segment == "eu-customers"
    assert events[0].previous is None


@respx.mock
def test_audit_poller_captures_segment_rule_and_client_handles_401(tmp_path: Path) -> None:
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}audit/?page_size=50").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 401,
                        "created_date": "2026-11-26T09:05:00Z",
                        "log": "Segment rules updated for: eu-customers",
                        "author": {"email": "drill@flagrewind.local"},
                        "environment": None,
                        "related_object_type": "SEGMENT",
                        "related_object_id": 1,
                    }
                ]
            },
        )
    )
    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")
    with FlagsmithClient(base, "agent-key") as fs:
        poller = AuditPoller(fs, store)
        recorded = poller.poll_once()
        # Second poll deduplicates the same audit id
        recorded_again = poller.poll_once()

    assert len(recorded) == 1
    assert recorded[0].kind == "segment_rule"
    assert recorded[0].segment == "eu-customers"
    assert recorded_again[0].id == recorded[0].id

    respx.get(f"{base}organisations/").mock(
        return_value=httpx.Response(401, json={"detail": "bad"})
    )
    with FlagsmithClient(base, "bad-key") as bad_fs:
        try:
            bad_fs.list_organisations()
            raise AssertionError("Expected auth error")
        except FlagsmithAuthOrReachabilityError:
            pass


def test_journal_gap_detection(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")
    t0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    store.record_heartbeat(source="audit", ok=True, now=t0)
    store.record_heartbeat(source="audit", ok=True, now=t0 + timedelta(minutes=10))
    ok, notes = store.check_completeness(t0, t0 + timedelta(minutes=10), expected_poll_seconds=15)
    assert not ok
    assert any("gap" in note.lower() for note in notes)


def test_evidence_bundle_staging_env_zero_and_code_index(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "flagrewind.yaml")
    code_idx = CodeIndex.build(repo_root, cfg.code.paths)
    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")

    onset = datetime(2026, 11, 26, 9, 10, tzinfo=UTC)
    for sec in range(0, 301, 30):
        store.record_heartbeat(
            source="audit", ok=True, now=onset - timedelta(minutes=5) + timedelta(seconds=sec)
        )

    # Staging change (env=0) vs production culprit (5 min before onset) vs post-window change
    store.append(
        {
            "source": "webhook",
            "occurred_at": onset - timedelta(minutes=2),
            "received_at": onset - timedelta(minutes=2),
            "environment": "staging",
            "feature": "tax_engine_v2",
            "kind": "segment_override",
            "segment": "eu-customers",
            "previous": {"enabled": False, "value": None},
            "new": {"enabled": True, "value": None},
            "actor": "dev@example.com",
            "raw_ref": {},
        }
    )
    store.append(
        {
            "source": "webhook",
            "occurred_at": onset - timedelta(minutes=5),
            "received_at": onset - timedelta(minutes=5),
            "environment": "production",
            "feature": "tax_engine_v2",
            "kind": "segment_override",
            "segment": "eu-customers",
            "previous": {"enabled": False, "value": None},
            "new": {"enabled": True, "value": None},
            "actor": "drill@example.com",
            "raw_ref": {"feature_state_id": 42, "environment_api_key": "env-prod"},
        }
    )
    store.append(
        {
            "source": "webhook",
            "occurred_at": onset + timedelta(minutes=5),
            "received_at": onset + timedelta(minutes=5),
            "environment": "production",
            "feature": "dark_mode",
            "kind": "feature_state",
            "segment": None,
            "previous": {"enabled": False, "value": None},
            "new": {"enabled": True, "value": None},
            "actor": "other@example.com",
            "raw_ref": {},
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
    bundle = build_evidence_bundle(
        incident_id="INC-0001",
        config=cfg,
        store=store,
        symptom=symptom,
        code_index=code_idx,
    )

    assert bundle.journal_complete is True
    assert len(bundle.candidates) == 2  # post-window dark_mode excluded
    assert bundle.candidates[0].environment == "production"
    assert bundle.candidates[0].features.code == 1.0
    assert bundle.candidates[0].features.scope == 1.0
    assert bundle.candidates[1].environment == "staging"
    assert bundle.candidates[1].features.env == 0.0
    assert bundle.candidates[1].rules_score == 0.0


@respx.mock
def test_audit_poller_snapshot_diff_fallback_when_audit_empty(tmp_path: Path) -> None:
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}projects/10/audit/?page_size=50").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{base}projects/10/features/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 5, "name": "tax_engine_v2"}]})
    )
    respx.get(f"{base}projects/10/segments/").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": 1, "name": "eu-customers", "rules": "DE,FR"}]}
        )
    )
    respx.get(f"{base}environments/env-prod/featurestates/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"id": 77, "feature": 5, "enabled": False}]},
            ),
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 77,
                            "feature": 5,
                            "enabled": True,
                            "updated_at": "2026-11-26T09:10:00Z",
                        }
                    ]
                },
            ),
        ]
    )

    store = JournalStore(tmp_path / "journal.jsonl", tmp_path / "heartbeat.json")
    with FlagsmithClient(base, "agent-key") as fs:
        poller = AuditPoller(
            fs,
            store,
            project_id=10,
            env_keys={"production": "env-prod"},
        )
        # First poll seeds baseline snapshot cleanly (0 events emitted)
        first = poller.poll_once()
        # Second poll detects enabled False -> True diff cleanly without storing env key
        second = poller.poll_once()

    assert first == []
    assert len(second) == 1
    assert second[0].feature == "tax_engine_v2"
    assert second[0].previous is not None and second[0].previous.enabled is False
    assert second[0].new is not None and second[0].new.enabled is True
    assert "environment_api_key" not in second[0].raw_ref
