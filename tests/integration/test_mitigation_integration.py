"""Integration tests covering end-to-end watch/apply/undo, failure modes, and MCP reader."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from flagrewind.approval import expected_approval_phrase, expected_undo_phrase
from flagrewind.cli import app
from flagrewind.config import ExitCode
from flagrewind.journal.store import JournalStore
from flagrewind.mcp_reader import FlagsmithMCPReader, MCPReadOnlyError
from flagrewind.notifier import send_notify_webhook


def _prepare_env_and_journal(tmp_path: Path, repo_root: Path) -> Path:
    cfg_path = tmp_path / "flagrewind.yaml"
    cfg_path.write_text(
        (repo_root / "flagrewind.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state_dir = tmp_path / ".flagrewind"
    store = JournalStore(state_dir / "journal.jsonl", state_dir / "journal_heartbeat.json")
    now = datetime.now(UTC)
    for sec in range(0, 301, 30):
        store.record_heartbeat(
            source="audit", ok=True, now=now - timedelta(minutes=5) + timedelta(seconds=sec)
        )
    store.append(
        {
            "source": "webhook",
            "occurred_at": now - timedelta(minutes=2),
            "received_at": now - timedelta(minutes=2),
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
    return cfg_path


def _breached_metrics(now: datetime) -> dict[str, Any]:
    return {
        "windows": [
            {
                "window_start": (now - timedelta(seconds=20)).isoformat(),
                "epoch_s": int((now - timedelta(seconds=20)).timestamp()),
                "total": 20,
                "errors": 0,
                "error_rate": 0.0,
                "by_country": {"US": {"total": 10, "errors": 0}, "DE": {"total": 10, "errors": 0}},
                "by_plan": {"free": {"total": 20, "errors": 0}},
                "by_endpoint": {"/checkout": {"total": 20, "errors": 0}},
            },
            {
                "window_start": now.isoformat(),
                "epoch_s": int(now.timestamp()),
                "total": 20,
                "errors": 6,
                "error_rate": 0.3,
                "by_country": {"US": {"total": 10, "errors": 0}, "DE": {"total": 10, "errors": 6}},
                "by_plan": {"free": {"total": 20, "errors": 6}},
                "by_endpoint": {"/checkout": {"total": 20, "errors": 6}},
            },
        ]
    }


def _healthy_metrics(now: datetime) -> dict[str, Any]:
    return {
        "windows": [
            {
                "window_start": now.isoformat(),
                "epoch_s": int(now.timestamp()),
                "total": 20,
                "errors": 0,
                "error_rate": 0.0,
                "by_country": {},
                "by_plan": {},
                "by_endpoint": {},
            }
        ]
    }


@respx.mock
def test_integration_watch_apply_undo_and_failure_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg_path = _prepare_env_and_journal(tmp_path, repo_root)
    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/")
    monkeypatch.setenv("FLAGREWIND_ADMIN_KEY", "agent-key")
    monkeypatch.setattr("flagrewind.approval._stdin_is_interactive", lambda: True)
    monkeypatch.setattr("flagrewind.verifier.time.sleep", lambda _: None)

    now = datetime.now(UTC)
    errors_payload = {
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

    respx.get("http://localhost:8001/metrics").mock(
        side_effect=[
            httpx.Response(200, json=_breached_metrics(now)),  # watch opens INC-0001
            httpx.Response(200, json=_healthy_metrics(now)),  # verify window 1
            httpx.Response(200, json=_healthy_metrics(now)),  # verify window 2
            httpx.Response(200, json=_healthy_metrics(now)),  # verify window 3
        ]
    )
    respx.get("http://localhost:8001/errors").mock(
        return_value=httpx.Response(200, json=errors_payload)
    )
    notify_route = respx.post("http://localhost:9099/hook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    runner = CliRunner()
    watch_res = runner.invoke(
        app,
        [
            "--config",
            str(cfg_path),
            "--no-llm",
            "watch",
            "--once",
            "--max-polls",
            "1",
            "--notify-url",
            "http://localhost:9099/hook",
        ],
    )
    assert watch_res.exit_code == 0
    assert "INC-0001" in watch_res.stdout
    assert notify_route.call_count == 1

    # Mock Flagsmith state transitions for apply -> verify -> undo
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}environments/env-prod/featurestates/55/").mock(
        side_effect=[
            httpx.Response(200, json={"id": 55, "enabled": True}),   # apply precondition
            httpx.Response(200, json={"id": 55, "enabled": False}),  # apply verify
            httpx.Response(200, json={"id": 55, "enabled": False}),  # undo precondition
        ]
    )
    patch_route = respx.patch(f"{base}environments/env-prod/featurestates/55/").mock(
        side_effect=[
            httpx.Response(200, json={"id": 55, "enabled": False}),  # apply revert
            httpx.Response(200, json={"id": 55, "enabled": True}),   # undo restore
        ]
    )

    apply_input = f"Shashank\n{expected_approval_phrase('PLN-0001', 1)}\n"
    apply_res = runner.invoke(
        app,
        ["--config", str(cfg_path), "apply", "INC-0001", "--notify-url", "http://localhost:9099/hook"],
        input=apply_input,
    )
    assert apply_res.exit_code == 0
    assert "recovered=True" in apply_res.stdout
    assert notify_route.call_count == 2

    # Undo restores exact previous state (enabled=True) after fresh confirmation
    undo_res = runner.invoke(
        app,
        ["--config", str(cfg_path), "undo", "INC-0001"],
        input=f"{expected_undo_phrase('PLN-0001')}\n",
    )
    assert undo_res.exit_code == 0
    assert "Undo applied" in undo_res.stdout
    assert patch_route.call_count == 2
    assert json.loads(patch_route.calls[0].request.content) == {"enabled": False}
    assert json.loads(patch_route.calls[1].request.content) == {"enabled": True}

    # Verify Flagsmith down / bad key exits with code 3
    respx.get(f"{base}organisations/").mock(return_value=httpx.Response(401))
    doc_res = runner.invoke(app, ["--config", str(cfg_path), "doctor"])
    assert doc_res.exit_code == int(ExitCode.REMOTE_UNREACHABLE)


@respx.mock
def test_mcp_read_only_and_notifier() -> None:
    mcp_url = "http://localhost:8003/mcp"
    respx.post(mcp_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "list_project_features"},
                        {"name": "get_environment_feature_state"},
                        {"name": "update_environment_feature_state"},
                    ]
                },
            },
        )
    )
    with FlagsmithMCPReader(mcp_url, "agent-key") as mcp:
        tools = mcp.list_tools()
        assert tools == ["list_project_features", "get_environment_feature_state"]
        with pytest.raises(MCPReadOnlyError, match="Blocked non-read-only"):
            mcp.call_read_tool("update_environment_feature_state", {})

    # Unreachable notify URL fails safely without raising
    assert send_notify_webhook(None, {"a": 1}) is False
