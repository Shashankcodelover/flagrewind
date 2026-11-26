"""Rich 80-column brief renderer and Markdown/JSON incident report writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from flagrewind.approval import ApprovalRecord, expected_approval_phrase
from flagrewind.evidence import EvidenceBundle
from flagrewind.executor import ExecutionResult
from flagrewind.planner import MitigationPlan
from flagrewind.ranking.schema import RankingVerdict
from flagrewind.verifier import VerificationResult


def render_brief(
    *,
    console: Console,
    bundle: EvidenceBundle,
    verdict: RankingVerdict,
    plan: MitigationPlan,
    ranking_mode: str,
) -> None:
    """Render a concise one-screen incident brief readable at 80 columns without colour."""
    top = verdict.ranking[0] if verdict.ranking else None
    lines: list[str] = [
        f"Incident: {bundle.incident_id} | Mode: {ranking_mode}",
        f"Verdict: {verdict.verdict.upper()} | Journal complete: {bundle.journal_complete}",
        (
            f"Error rate: {bundle.symptom['baseline']:.1%} -> {bundle.symptom['current']:.1%} "
            f"(onset {bundle.onset.isoformat()})"
        ),
    ]
    if top is not None:
        lines.append(f"Top suspect: {top.change_id} (confidence {top.confidence:.2f})")
        for bullet in top.evidence[:3]:
            lines.append(f"  * {bullet[:72]}")
        if top.counter_evidence:
            lines.append(f"  - Counter: {top.counter_evidence[0][:65]}")
    else:
        lines.append("Top suspect: none")

    if verdict.recommended.action != "none" and plan.options:
        opt = plan.options[0]
        lines.append(f"Action [option {opt.n}]: {opt.action} -> {opt.preview[:70]}")
        lines.append(f"Blast radius: {opt.blast_radius[:64]}")
        phrase = expected_approval_phrase(plan.plan_id, opt.n)
        lines.append(f"Confirm with: flagrewind apply {bundle.incident_id} (type '{phrase}')")
    else:
        lines.append(f"Recommendation: NO WRITE ({verdict.recommended.why[:60]})")
        if verdict.questions:
            lines.append(f"Question: {verdict.questions[0][:68]}")

    panel = Panel(
        "\n".join(lines),
        title=f"FlagRewind Brief [{bundle.incident_id}]",
        width=min(80, console.width),
        expand=False,
    )
    console.print(panel)


def render_plan_table(console: Console, plan: MitigationPlan) -> None:
    """Render deterministic dry-run plan options."""
    table = Table(
        title=f"Dry-Run Plan {plan.plan_id} ({plan.incident_id})",
        width=min(80, console.width),
    )
    table.add_column("Opt", width=4)
    table.add_column("Action", width=18)
    table.add_column("Blast Radius & Preview")
    for opt in plan.options:
        status = "READY" if opt.executable else "BLOCKED"
        table.add_row(str(opt.n), f"{opt.action}\n[{status}]", f"{opt.blast_radius}\n{opt.preview}")
    console.print(table)


def incident_dir(state_dir: Path, incident_id: str) -> Path:
    """Return directory holding artifacts for an incident."""
    return state_dir / "incidents" / incident_id


def save_incident_state(
    *,
    state_dir: Path,
    bundle: EvidenceBundle,
    verdict: RankingVerdict,
    plan: MitigationPlan,
    ranking_mode: str,
) -> Path:
    """Persist evidence, verdict, and plan JSON files for later plan/apply/undo/report."""
    target = incident_dir(state_dir, bundle.incident_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / "evidence.json").write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    (target / "verdict.json").write_text(
        json.dumps({"mode": ranking_mode, **verdict.model_dump(mode="json")}, indent=2),
        encoding="utf-8",
    )
    (target / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return target


def write_incident_report(
    *,
    state_dir: Path,
    bundle: EvidenceBundle,
    verdict: RankingVerdict,
    plan: MitigationPlan,
    ranking_mode: str,
    approval: ApprovalRecord | None = None,
    execution: ExecutionResult | None = None,
    verification: VerificationResult | None = None,
    llm_meta: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write Markdown and JSON incident reports to .flagrewind/incidents/<id>/."""
    target = incident_dir(state_dir, bundle.incident_id)
    target.mkdir(parents=True, exist_ok=True)

    summary_line = (
        f"{bundle.incident_id}: {verdict.verdict} | {verdict.recommended.why} "
        f"| mode={ranking_mode}"
    )
    report_payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "incident_id": bundle.incident_id,
        "summary": summary_line,
        "ranking_mode": ranking_mode,
        "journal_complete": bundle.journal_complete,
        "notes": bundle.notes,
        "symptom": bundle.symptom,
        "onset": bundle.onset.isoformat(),
        "candidates": [c.model_dump(mode="json") for c in bundle.candidates],
        "verdict": verdict.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "approval": approval.model_dump(mode="json") if approval else None,
        "execution": execution.model_dump(mode="json") if execution else None,
        "verification": verification.model_dump(mode="json") if verification else None,
        "llm": llm_meta or {"calls": 0, "tokens": None, "model": None},
    }

    json_path = target / "report.json"
    json_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    md_lines = [
        f"# Incident Report: {bundle.incident_id} (SYNTHETIC)",
        "",
        f"**Summary:** {summary_line}",
        f"- **Onset:** `{bundle.onset.isoformat()}`",
        (
            f"- **Error Rate:** `{bundle.symptom['baseline']:.2%}` -> "
            f"`{bundle.symptom['current']:.2%}`"
        ),
        f"- **Journal Complete:** `{bundle.journal_complete}`",
        f"- **Mode:** `{ranking_mode}`",
        "",
        "## Change Timeline vs Onset",
    ]
    if bundle.candidates:
        for cand in bundle.candidates:
            md_lines.append(
                f"- `{cand.change_id}` ({cand.minutes_before_onset:+.1f}m): "
                f"`{cand.feature}` ({cand.kind}) in `{cand.environment}` "
                f"— score `{cand.rules_score:.2f}`"
            )
    else:
        md_lines.append("- No Flagsmith changes recorded in window.")

    md_lines.extend(["", "## Ranked Suspects"])
    for suspect in verdict.ranking:
        md_lines.append(f"- **`{suspect.change_id}`** (confidence `{suspect.confidence:.2f}`)")
        for ev in suspect.evidence:
            md_lines.append(f"  - Evidence: {ev}")
        for ctr in suspect.counter_evidence:
            md_lines.append(f"  - Counter-evidence: {ctr}")

    md_lines.extend(["", "## Action, Approval & Verification"])
    if approval:
        md_lines.append(
            f"- Approval: `{'APPROVED' if approval.approved else 'REJECTED'}` "
            f"by `{approval.approver}` at `{approval.decided_at.isoformat()}`"
        )
    else:
        md_lines.append("- Approval: none recorded (read-only investigation)")
    if execution:
        md_lines.append(
            f"- Execution: applied `{execution.operations_applied}` ops for "
            f"option `{execution.option_n}`"
        )
    if verification:
        md_lines.append(
            f"- Verification: state_verified=`{verification.state_verified}`, "
            f"recovered=`{verification.recovered}`, "
            f"rates=`{verification.observed_error_rates}`"
        )
    llm_info = report_payload["llm"]
    md_lines.extend(
        [
            "",
            "## Model Metadata",
            f"- Model: `{llm_info.get('model')}` | Calls: `{llm_info.get('calls')}` "
            f"| Tokens: `{llm_info.get('tokens')}`",
        ]
    )

    md_path = target / "report.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return md_path, json_path
