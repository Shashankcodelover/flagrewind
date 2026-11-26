"""Capability diagnostics (`flagrewind doctor`) writing .flagrewind/capabilities.json."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from flagrewind.config import FlagRewindConfig, resolve_env
from flagrewind.flagsmith_client import FlagsmithClient, FlagsmithError


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def run_doctor(
    config: FlagRewindConfig,
    state_dir: Path,
    *,
    console: Console,
    fs_client: FlagsmithClient | None = None,
    llm_checker: Any = None,
    mcp_checker: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Run all capability checks without mutating Flagsmith; write capabilities.json."""
    checks: list[dict[str, Any]] = []
    api_url = resolve_env(config.flagsmith.api_url_env) or "http://localhost:8000/api/v1/"
    admin_key = resolve_env(config.flagsmith.admin_key_env)
    project_id: int | None = None
    env_map: dict[str, str] = {}
    all_ok = True

    client: FlagsmithClient | None = fs_client
    owns_client = False
    if client is None and admin_key:
        client = FlagsmithClient(api_url, admin_key)
        owns_client = True

    try:
        if client is None:
            checks.append(
                {
                    "check": "flagsmith_key",
                    "status": "FAIL",
                    "detail": f"Missing {config.flagsmith.admin_key_env}",
                }
            )
            all_ok = False
        else:
            try:
                orgs = client.list_organisations()
                checks.append(
                    {"check": "flagsmith_api", "status": "PASS", "detail": f"{len(orgs)} org(s)"}
                )
                proj = client.get_project_by_name(config.flagsmith.project)
                project_id = int(proj["id"])
                envs = client.list_environments(project_id)
                env_map = {str(e["name"]).lower(): str(e["api_key"]) for e in envs}
                checks.append(
                    {
                        "check": "project_envs",
                        "status": "PASS",
                        "detail": ", ".join(sorted(env_map)),
                    }
                )
                audits = client.list_audit_logs(project_id=project_id, page_size=5)
                checks.append(
                    {
                        "check": "audit_read",
                        "status": "PASS",
                        "detail": f"{len(audits)} entries",
                    }
                )

                writable_ok = all(w.lower() in env_map for w in config.writable_environments())
                if writable_ok:
                    first_w = next(iter(config.writable_environments())).lower()
                    states = client.list_feature_states(env_map[first_w])
                    checks.append(
                        {
                            "check": "dry_run_write_scope",
                            "status": "PASS",
                            "detail": f"{first_w} reachable ({len(states)} states; 0 writes)",
                        }
                    )
                else:
                    checks.append(
                        {
                            "check": "dry_run_write_scope",
                            "status": "FAIL",
                            "detail": "Configured writable env not found in Flagsmith",
                        }
                    )
                    all_ok = False
            except FlagsmithError as exc:
                checks.append({"check": "flagsmith_api", "status": "FAIL", "detail": str(exc)[:80]})
                all_ok = False
    finally:
        if owns_client and client is not None:
            client.close()

    journal_path = state_dir / "journal.jsonl"
    checks.append(
        {
            "check": "journal_file",
            "status": "PASS" if journal_path.exists() else "WARN",
            "detail": str(journal_path) if journal_path.exists() else "Not created yet",
        }
    )

    wh_up = _port_open(config.journal.webhook_port)
    checks.append(
        {
            "check": "webhook_receiver",
            "status": "PASS" if wh_up else "WARN",
            "detail": (
                f"Listening on :{config.journal.webhook_port}"
                if wh_up
                else f"Port :{config.journal.webhook_port} not listening"
            ),
        }
    )

    if llm_checker is not None:
        llm_status = llm_checker()
        checks.append(llm_status)
    else:
        has_llm = bool(resolve_env(config.llm.model_env) and resolve_env(config.llm.base_url_env))
        checks.append(
            {
                "check": "llm",
                "status": "PASS" if has_llm else "WARN",
                "detail": "Env configured" if has_llm else "rules-only (LLM env unset)",
            }
        )

    if mcp_checker is not None:
        checks.append(mcp_checker())

    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "ok": all_ok,
        "checks": checks,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "capabilities.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    table = Table(title="FlagRewind Capabilities (doctor)", width=min(80, console.width))
    table.add_column("Check", width=22)
    table.add_column("Status", width=8)
    table.add_column("Detail")
    for item in checks:
        table.add_row(item["check"], item["status"], item["detail"])
    console.print(table)
    return all_ok, report
