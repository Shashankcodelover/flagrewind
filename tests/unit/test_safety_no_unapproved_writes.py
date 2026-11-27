"""Safety test asserting zero code paths write to Flagsmith without recorded approval."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from flagrewind.approval import ApprovalRecord
from flagrewind.cli import app
from flagrewind.config import ExitCode, load_config
from flagrewind.executor import ExecutorError, execute_option
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.journal.store import JournalStore
from flagrewind.planner import ApiOperation, MitigationPlan, PlanOption


def test_no_unapproved_write_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []

    def _forbidden_patch(*args: object, **kwargs: object) -> dict[str, object]:
        writes.append("patch")
        raise AssertionError("Unapproved Flagsmith PATCH attempted!")

    def _forbidden_post(*args: object, **kwargs: object) -> dict[str, object]:
        writes.append("post")
        raise AssertionError("Unapproved Flagsmith POST attempted!")

    monkeypatch.setattr(FlagsmithClient, "patch_feature_state", _forbidden_patch)
    monkeypatch.setattr(FlagsmithClient, "create_segment_override", _forbidden_post)

    repo_root = Path(__file__).resolve().parents[2]
    cfg_path = tmp_path / "flagrewind.yaml"
    cfg_path.write_text(
        (repo_root / "flagrewind.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    cfg = load_config(cfg_path)

    # Direct executor rejection on unapproved ApprovalRecord
    opt = PlanOption(
        n=1,
        action="revert_change",
        change_id="CHG-000001",
        environment="production",
        environment_api_key="env-prod",
        feature="tax_engine_v2",
        feature_state_id=55,
        operations=[
            ApiOperation(
                method="PATCH",
                path="environments/env-prod/featurestates/55/",
                body={"enabled": False},
            )
        ],
        precondition={"enabled": True},
        inverse_operations=[],
        blast_radius="production / tax_engine_v2",
        preview="enabled True -> False",
    )
    plan = MitigationPlan(
        plan_id="PLN-0001",
        incident_id="INC-0001",
        created_at=datetime.now(UTC),
        options=[opt],
    )
    denied = ApprovalRecord(
        plan_id="PLN-0001",
        plan_hash="bad",
        option_n=1,
        approver="tester",
        approved=False,
        phrase_entered="no",
        expected_phrase="apply PLN-0001 option 1",
        decided_at=datetime.now(UTC),
    )

    with (
        FlagsmithClient("http://localhost:8000/api/v1/", "agent-key") as fs,
        pytest.raises(ExecutorError, match="not granted"),
    ):
        execute_option(client=fs, config=cfg, plan=plan, option=opt, approval=denied)

    # Seed journal & run read-only CLI flow + rejected apply
    state_dir = tmp_path / ".flagrewind"
    store = JournalStore(state_dir / "journal.jsonl", state_dir / "journal_heartbeat.json")
    now = datetime.now(UTC)
    store.record_heartbeat(source="audit", ok=True, now=now)
    store.append(
        {
            "source": "webhook",
            "occurred_at": now,
            "received_at": now,
            "environment": "production",
            "feature": "tax_engine_v2",
            "kind": "feature_state",
            "segment": None,
            "previous": {"enabled": False, "value": None},
            "new": {"enabled": True, "value": None},
            "actor": "drill@example.com",
            "raw_ref": {"feature_state_id": 55, "environment_api_key": "env-prod"},
        }
    )

    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/")
    monkeypatch.setenv("FLAGREWIND_ADMIN_KEY", "agent-key")

    with respx.mock:
        respx.get("http://localhost:8000/api/v1/organisations/").mock(
            return_value=httpx.Response(200, json=[{"id": 1}])
        )
        respx.get("http://localhost:8000/api/v1/projects/").mock(
            return_value=httpx.Response(200, json=[{"id": 10, "name": "Acme Checkout"}])
        )
        respx.get("http://localhost:8000/api/v1/environments/?project=10").mock(
            return_value=httpx.Response(
                200, json=[{"name": "production", "api_key": "env-prod"}]
            )
        )
        respx.get("http://localhost:8000/api/v1/projects/10/audit/?page_size=5").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("http://localhost:8000/api/v1/audit/?page_size=5&project=10").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("http://localhost:8000/api/v1/environments/env-prod/featurestates/").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("http://localhost:8001/metrics").mock(
            return_value=httpx.Response(200, json={"windows": []})
        )
        respx.get("http://localhost:8001/errors").mock(
            return_value=httpx.Response(200, json={"errors": []})
        )

        runner = CliRunner()
        assert runner.invoke(app, ["--config", str(cfg_path), "doctor"]).exit_code == 0
        assert (
            runner.invoke(app, ["--config", str(cfg_path), "--no-llm", "investigate"]).exit_code
            == 0
        )
        assert runner.invoke(app, ["--config", str(cfg_path), "plan", "INC-0001"]).exit_code == 0
        assert (
            runner.invoke(app, ["--config", str(cfg_path), "apply", "INC-0001"]).exit_code
            == int(ExitCode.UNRESOLVED_OR_REJECTED)
        )

    assert writes == []
