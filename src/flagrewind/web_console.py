"""P2 local Incident Timeline, Counterfactual Blast-Radius & Approval Console."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from flagrewind.approval import ApprovalRecord, expected_approval_phrase
from flagrewind.audit import AuditTrail
from flagrewind.config import FlagRewindConfig, resolve_env
from flagrewind.evidence import EvidenceBundle
from flagrewind.executor import ExecutorError, PreconditionDriftError, execute_option
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.planner import MitigationPlan, compute_plan_hash
from flagrewind.ranking.schema import RankingVerdict
from flagrewind.report import incident_dir


def compute_blast_radius_diff(bundle: EvidenceBundle, plan: MitigationPlan) -> dict[str, Any]:
    """Estimate affected traffic share before vs after mitigation from symptom dimensions."""
    by_dim = bundle.symptom.get("by_dimension", {})
    countries = by_dim.get("country", {})
    plans = by_dim.get("plan", {})
    total_reqs = sum(int(v.get("total", 0)) for v in countries.values()) or 1
    total_errs = sum(int(v.get("errors", 0)) for v in countries.values())

    failing_countries = [k for k, v in countries.items() if int(v.get("errors", 0)) > 0]
    failing_plans = [k for k, v in plans.items() if int(v.get("errors", 0)) > 0]
    affected_reqs = sum(int(countries[c].get("total", 0)) for c in failing_countries)

    return {
        "total_requests": total_reqs,
        "total_errors": total_errs,
        "current_error_rate_pct": round((total_errs / total_reqs) * 100, 2),
        "failing_countries": failing_countries,
        "failing_plans": failing_plans,
        "estimated_protected_share_pct": round((affected_reqs / total_reqs) * 100, 1),
        "planned_options": len(plan.options),
    }


def _load_incidents(state_dir: Path) -> list[dict[str, Any]]:
    inc_root = state_dir / "incidents"
    if not inc_root.exists():
        return []
    items: list[dict[str, Any]] = []
    for child in sorted(inc_root.iterdir(), reverse=True):
        if not child.is_dir() or not (child / "evidence.json").exists():
            continue
        bundle = EvidenceBundle.model_validate_json((child / "evidence.json").read_text("utf-8"))
        verdict_raw = json.loads((child / "verdict.json").read_text("utf-8"))
        mode = str(verdict_raw.pop("mode", "rules-only"))
        verdict = RankingVerdict.model_validate(verdict_raw)
        plan = MitigationPlan.model_validate_json((child / "plan.json").read_text("utf-8"))
        items.append(
            {
                "bundle": bundle,
                "verdict": verdict,
                "plan": plan,
                "mode": mode,
                "blast": compute_blast_radius_diff(bundle, plan),
            }
        )
    return items


def create_console_app(
    config: FlagRewindConfig,
    state_dir: Path,
    *,
    client_factory: Any = None,
) -> FastAPI:
    """Create local FastAPI UI serving incident timeline and approval gate."""
    app = FastAPI(title="FlagRewind Console (Local)", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        incidents = _load_incidents(state_dir)
        cards: list[str] = []
        for item in incidents:
            bundle: EvidenceBundle = item["bundle"]
            verdict: RankingVerdict = item["verdict"]
            plan: MitigationPlan = item["plan"]
            blast: dict[str, Any] = item["blast"]
            opt = plan.options[0] if plan.options else None
            phrase = expected_approval_phrase(plan.plan_id, opt.n) if opt else ""
            controls = ""
            if opt and verdict.recommended.action != "none":
                esc_id = html.escape(bundle.incident_id)
                controls = (
                    f"<form method='post' action='/incidents/{esc_id}/approve'>"
                    f"<input type='hidden' name='option_n' value='{opt.n}'/>"
                    f"<input name='approver' placeholder='Your name' required/> "
                    f"<input name='phrase' placeholder='{html.escape(phrase)}' required/> "
                    f"<button type='submit'>Approve & Apply</button></form>"
                    f"<form method='post' action='/incidents/{esc_id}/reject'>"
                    f"<input type='hidden' name='option_n' value='{opt.n}'/>"
                    f"<button type='submit'>Reject</button></form>"
                )
            countries_txt = html.escape(", ".join(blast["failing_countries"]) or "none")
            cards.append(
                f"<section style='border:1px solid #334;padding:12px;margin:12px 0'>"
                f"<h3>{html.escape(bundle.incident_id)} — {html.escape(verdict.verdict)} "
                f"({html.escape(item['mode'])})</h3>"
                f"<p>Error rate: {blast['current_error_rate_pct']}% | "
                f"Affected countries: {countries_txt} | "
                f"Estimated protected share: {blast['estimated_protected_share_pct']}%</p>"
                f"<p>Recommendation: {html.escape(verdict.recommended.why)}</p>"
                f"{controls}</section>"
            )
        body = (
            "".join(cards)
            or "<p>No incidents recorded yet. Run <code>flagrewind investigate</code>.</p>"
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>FlagRewind Local Console</title></head>"
            "<body style='font-family:system-ui;max-width:860px;margin:24px auto'>"
            "<h1>FlagRewind — Incident Timeline & Approval Gate</h1>"
            f"{body}</body></html>"
        )

    @app.get("/api/incidents")
    def list_incidents_json() -> dict[str, Any]:
        incidents = _load_incidents(state_dir)
        return {
            "incidents": [
                {
                    "incident_id": it["bundle"].incident_id,
                    "verdict": it["verdict"].verdict,
                    "mode": it["mode"],
                    "blast_radius_diff": it["blast"],
                }
                for it in incidents
            ]
        }

    async def _read_form_or_json(request: Request) -> dict[str, str]:
        raw = (await request.body()).decode("utf-8")
        if not raw.strip():
            return {}
        if "application/json" in request.headers.get("content-type", ""):
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()}
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: vals[0] for k, vals in parsed.items() if vals}

    @app.post("/incidents/{inc_id}/reject")
    async def reject_incident(inc_id: str, request: Request) -> dict[str, Any]:
        fields = await _read_form_or_json(request)
        option_n = int(fields.get("option_n", "1"))
        approver = fields.get("approver", "web-operator")
        target = incident_dir(state_dir, inc_id)
        if not (target / "plan.json").exists():
            raise HTTPException(status_code=404, detail="Incident not found")
        plan = MitigationPlan.model_validate_json((target / "plan.json").read_text("utf-8"))
        record = ApprovalRecord(
            plan_id=plan.plan_id,
            plan_hash=compute_plan_hash(plan, option_n),
            option_n=option_n,
            approver=approver,
            approved=False,
            phrase_entered="REJECTED_IN_UI",
            expected_phrase=expected_approval_phrase(plan.plan_id, option_n),
            decided_at=datetime.now(UTC),
        )
        AuditTrail(state_dir / "audit.jsonl").log(
            "rejection", incident_id=inc_id, details=record.model_dump(mode="json")
        )
        return {"status": "rejected", "incident_id": inc_id}

    @app.post("/incidents/{inc_id}/approve")
    async def approve_and_apply(inc_id: str, request: Request) -> dict[str, Any]:
        fields = await _read_form_or_json(request)
        option_n = int(fields.get("option_n", "1"))
        approver = fields.get("approver", "")
        phrase = fields.get("phrase", "")
        target = incident_dir(state_dir, inc_id)
        if not (target / "plan.json").exists():
            raise HTTPException(status_code=404, detail="Incident not found")
        plan = MitigationPlan.model_validate_json((target / "plan.json").read_text("utf-8"))
        opt = next((o for o in plan.options if o.n == option_n), None)
        if opt is None:
            raise HTTPException(status_code=400, detail="Invalid option")

        expected = expected_approval_phrase(plan.plan_id, option_n)
        approved = phrase.strip() == expected
        record = ApprovalRecord(
            plan_id=plan.plan_id,
            plan_hash=compute_plan_hash(plan, option_n),
            option_n=option_n,
            approver=approver.strip() or "web-operator",
            approved=approved,
            phrase_entered=phrase.strip(),
            expected_phrase=expected,
            decided_at=datetime.now(UTC),
        )
        audit = AuditTrail(state_dir / "audit.jsonl")
        audit.log(
            "approval" if approved else "rejection",
            incident_id=inc_id,
            details=record.model_dump(mode="json"),
        )
        if not approved:
            raise HTTPException(status_code=403, detail=f"Phrase mismatch; expected '{expected}'")

        api_url = resolve_env(config.flagsmith.api_url_env) or "http://localhost:8000/api/v1/"
        admin_key = resolve_env(config.flagsmith.admin_key_env)
        if client_factory is not None:
            fs_client = client_factory()
        elif admin_key:
            fs_client = FlagsmithClient(api_url, admin_key)
        else:
            raise HTTPException(status_code=503, detail="Flagsmith admin key unset")

        with fs_client:
            try:
                res = execute_option(
                    client=fs_client,
                    config=config,
                    plan=plan,
                    option=opt,
                    approval=record,
                )
            except PreconditionDriftError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ExecutorError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        audit.log("execute", incident_id=inc_id, details=res.model_dump(mode="json"))
        return {"status": "applied", "operations_applied": res.operations_applied}

    return app
