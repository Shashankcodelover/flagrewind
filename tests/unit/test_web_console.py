"""Unit tests for the local Incident Timeline & Approval Console (P2)."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from flagrewind.approval import expected_approval_phrase
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.planner import build_mitigation_plan
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.report import save_incident_state
from flagrewind.web_console import create_console_app
from tests.unit.test_core_mitigation import _seed_scenario1


@respx.mock
def test_web_console_enforces_same_approval_phrase_gate(tmp_path: Path) -> None:
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
    state_dir = tmp_path / ".flagrewind"
    save_incident_state(
        state_dir=state_dir,
        bundle=bundle,
        verdict=verdict,
        plan=plan,
        ranking_mode="rules-only",
    )

    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}environments/env-prod/featurestates/55/").mock(
        return_value=httpx.Response(200, json={"id": 55, "enabled": True})
    )
    respx.patch(f"{base}environments/env-prod/featurestates/55/").mock(
        return_value=httpx.Response(200, json={"id": 55, "enabled": False})
    )

    ui_app = create_console_app(
        cfg,
        state_dir,
        client_factory=lambda: FlagsmithClient(base, "agent-key"),
    )
    client = TestClient(ui_app)

    home = client.get("/")
    assert home.status_code == 200
    assert "INC-0001" in home.text
    assert "Estimated protected share" in home.text

    # Wrong phrase is rejected (403) without writing to Flagsmith
    bad = client.post(
        "/incidents/INC-0001/approve",
        data={"option_n": "1", "approver": "Shashank", "phrase": "yes"},
    )
    assert bad.status_code == 403

    # Exact phrase applies cleanly through executor
    ok = client.post(
        "/incidents/INC-0001/approve",
        data={
            "option_n": "1",
            "approver": "Shashank",
            "phrase": expected_approval_phrase("PLN-0001", 1),
        },
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "applied"
