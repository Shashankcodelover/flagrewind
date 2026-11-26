"""Typer CLI entrypoint for FlagRewind."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from flagrewind.approval import request_approval
from flagrewind.audit import AuditTrail
from flagrewind.capabilities import run_doctor
from flagrewind.codeindex import CodeIndex
from flagrewind.config import (
    ConfigError,
    ExitCode,
    FlagRewindConfig,
    load_config,
    resolve_env,
    state_dir_for,
)
from flagrewind.evidence import EvidenceBundle, build_evidence_bundle
from flagrewind.executor import ExecutorError, PreconditionDriftError, execute_option
from flagrewind.flagsmith_client import FlagsmithAuthOrReachabilityError, FlagsmithClient
from flagrewind.journal.store import JournalStore
from flagrewind.llm_client import OpenAICompatibleLLMClient
from flagrewind.planner import MitigationPlan, build_mitigation_plan
from flagrewind.ranking.llm import rank_with_llm
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.ranking.schema import RankingVerdict
from flagrewind.report import (
    incident_dir,
    render_brief,
    render_plan_table,
    save_incident_state,
    write_incident_report,
)
from flagrewind.symptoms.demo import (
    DemoSymptomReader,
    DimensionBreakdown,
    SymptomError,
    SymptomSummary,
)
from flagrewind.verifier import verify_mitigation

app = typer.Typer(
    name="flagrewind",
    help=(
        "Human-approved on-call agent that identifies Flagsmith changes behind incidents, "
        "prepares the exact undo, and verifies recovery."
    ),
    no_args_is_help=True,
)


@dataclass(slots=True)
class RuntimeContext:
    """Shared CLI runtime options passed via Typer context."""

    config_path: Path
    config: FlagRewindConfig
    state_dir: Path
    no_llm: bool
    console: Console


def _make_console() -> Console:
    """Create a Rich console honouring NO_COLOR and safe 80-column wrapping."""
    return Console(width=80)


def _require_ctx(ctx: typer.Context) -> RuntimeContext:
    if not isinstance(ctx.obj, RuntimeContext):
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))
    return ctx.obj


def _next_incident_id(state_dir: Path) -> str:
    inc_root = state_dir / "incidents"
    if not inc_root.exists():
        return "INC-0001"
    nums = []
    for child in inc_root.iterdir():
        if child.is_dir() and child.name.startswith("INC-"):
            try:
                nums.append(int(child.name.split("-")[1]))
            except (IndexError, ValueError):
                continue
    return f"INC-{(max(nums) + 1) if nums else 1:04d}"


def _parse_since_minutes(since: str) -> int:
    m = re.match(r"^\s*(\d+)\s*([mh]?)\s*$", since.lower())
    if not m:
        raise ConfigError(f"Invalid --since duration: '{since}'. Use e.g. 30m or 1h.")
    val = int(m.group(1))
    unit = m.group(2) or "m"
    return val * 60 if unit == "h" else val


def _build_flagsmith_from_config(cfg: FlagRewindConfig) -> FlagsmithClient | None:
    api_url = resolve_env(cfg.flagsmith.api_url_env) or "http://localhost:8000/api/v1/"
    key = resolve_env(cfg.flagsmith.admin_key_env)
    if not key:
        return None
    return FlagsmithClient(api_url, key)


def _load_saved_incident(
    state_dir: Path, incident_id: str
) -> tuple[EvidenceBundle, RankingVerdict, MitigationPlan, str]:
    target = incident_dir(state_dir, incident_id)
    if not (target / "evidence.json").exists():
        raise ConfigError(f"Incident '{incident_id}' not found under {target}.")
    bundle = EvidenceBundle.model_validate_json(
        (target / "evidence.json").read_text(encoding="utf-8")
    )
    verdict_raw = json.loads((target / "verdict.json").read_text(encoding="utf-8"))
    mode = str(verdict_raw.pop("mode", "rules-only"))
    verdict = RankingVerdict.model_validate(verdict_raw)
    plan = MitigationPlan.model_validate_json((target / "plan.json").read_text(encoding="utf-8"))
    return bundle, verdict, plan, mode


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option("--config", help="Path to flagrewind.yaml configuration file."),
    ] = Path("flagrewind.yaml"),
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Force deterministic rules-only ranking (skip LLM)."),
    ] = False,
) -> None:
    """Load and validate configuration before running any subcommand."""
    console = _make_console()
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc

    ctx.obj = RuntimeContext(
        config_path=config,
        config=loaded,
        state_dir=state_dir_for(config),
        no_llm=no_llm,
        console=console,
    )


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Verify Flagsmith API, key, environments, journal, webhook port, and LLM access."""
    runtime = _require_ctx(ctx)
    ok, _ = run_doctor(runtime.config, runtime.state_dir, console=runtime.console)
    if not ok:
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE))


@app.command()
def journal(ctx: typer.Context) -> None:
    """Run the Flagsmith webhook receiver and audit-log poller."""
    runtime = _require_ctx(ctx)
    runtime.console.print(
        f"Journal listener configured on port {runtime.config.journal.webhook_port}."
    )


@app.command()
def watch(ctx: typer.Context) -> None:
    """Poll symptom metrics and open read-only investigations on threshold breach."""
    runtime = _require_ctx(ctx)
    runtime.console.print(
        f"Watch configured for {runtime.config.symptoms.demo_url} "
        f"(threshold={runtime.config.symptoms.error_rate_threshold:.2%})."
    )


@app.command()
def investigate(
    ctx: typer.Context,
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30m.")] = "30m",
    symptom: Annotated[
        str | None, typer.Option("--symptom", help="Optional symptom description.")
    ] = None,
) -> None:
    """Gather evidence, rank suspects, build dry-run plan, and render one-screen brief."""
    runtime = _require_ctx(ctx)
    try:
        since_min = _parse_since_minutes(since)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc

    store = JournalStore(
        runtime.state_dir / "journal.jsonl",
        runtime.state_dir / "journal_heartbeat.json",
    )
    try:
        with DemoSymptomReader(runtime.config.symptoms) as reader:
            symptom_summary = reader.fetch()
    except SymptomError as exc:
        if not symptom:
            runtime.console.print(f"Symptom source error: {exc}")
            raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE)) from exc
        now_dt = datetime.now(UTC)
        symptom_summary = SymptomSummary(
            onset=now_dt - timedelta(minutes=min(since_min, 5)),
            breached=True,
            baseline=0.0,
            current=runtime.config.symptoms.error_rate_threshold,
            by_dimension=DimensionBreakdown(),
            error_samples=[],
        )

    code_idx = CodeIndex.build(runtime.config_path.resolve().parent, runtime.config.code.paths)
    inc_id = _next_incident_id(runtime.state_dir)
    notes = [f"Operator symptom note: {symptom}"] if symptom else []
    bundle = build_evidence_bundle(
        incident_id=inc_id,
        config=runtime.config,
        store=store,
        symptom=symptom_summary,
        code_index=code_idx,
        extra_notes=notes,
    )
    audit = AuditTrail(runtime.state_dir / "audit.jsonl")
    if runtime.no_llm:
        verdict = rank_with_rules(bundle, confidence_floor=runtime.config.policy.confidence_floor)
        mode = "rules-only"
        llm_meta: dict[str, object] = {"calls": 0, "tokens": None, "model": None}
    else:
        llm_client = OpenAICompatibleLLMClient.from_config(runtime.config.llm)
        verdict, mode, llm_meta = rank_with_llm(
            bundle,
            config=runtime.config,
            llm_client=llm_client,
            audit_trail=audit,
        )

    events = store.read_all()
    mitigation_plan = build_mitigation_plan(
        incident_id=inc_id,
        bundle=bundle,
        verdict=verdict,
        events=events,
        config=runtime.config,
    )
    save_incident_state(
        state_dir=runtime.state_dir,
        bundle=bundle,
        verdict=verdict,
        plan=mitigation_plan,
        ranking_mode=mode,
    )
    write_incident_report(
        state_dir=runtime.state_dir,
        bundle=bundle,
        verdict=verdict,
        plan=mitigation_plan,
        ranking_mode=mode,
        llm_meta=llm_meta,
    )
    audit.log(
        "investigate",
        incident_id=inc_id,
        details={"mode": mode, "verdict": verdict.verdict, "candidates": len(bundle.candidates)},
    )
    render_brief(
        console=runtime.console,
        bundle=bundle,
        verdict=verdict,
        plan=mitigation_plan,
        ranking_mode=mode,
    )


@app.command()
def plan(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
) -> None:
    """Show the deterministic dry-run plan and inverse operations for an incident."""
    runtime = _require_ctx(ctx)
    try:
        _, _, loaded_plan, _ = _load_saved_incident(runtime.state_dir, incident_id)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc
    render_plan_table(runtime.console, loaded_plan)


@app.command()
def apply(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
    option: Annotated[int, typer.Option("--option", min=1, help="Plan option number.")] = 1,
    notify_url: Annotated[
        str | None,
        typer.Option("--notify-url", help="Optional generic webhook URL for JSON status POST."),
    ] = None,
) -> None:
    """Require typed human approval, execute inverse on Flagsmith, verify recovery, and report."""
    runtime = _require_ctx(ctx)
    _ = notify_url
    try:
        bundle, verdict, loaded_plan, mode = _load_saved_incident(runtime.state_dir, incident_id)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc

    if not loaded_plan.options:
        runtime.console.print(
            f"No mitigation options available for {incident_id} ({verdict.verdict})."
        )
        raise typer.Exit(code=int(ExitCode.UNRESOLVED_OR_REJECTED))

    chosen = next((o for o in loaded_plan.options if o.n == option), None)
    if chosen is None:
        runtime.console.print(f"Option {option} not found in {loaded_plan.plan_id}.")
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))

    render_brief(
        console=runtime.console,
        bundle=bundle,
        verdict=verdict,
        plan=loaded_plan,
        ranking_mode=mode,
    )
    approval = request_approval(loaded_plan, chosen, state_dir=runtime.state_dir)
    audit = AuditTrail(runtime.state_dir / "audit.jsonl")
    audit.log(
        "approval" if approval.approved else "rejection",
        incident_id=incident_id,
        details=approval.model_dump(mode="json"),
    )
    if not approval.approved:
        runtime.console.print(
            "Approval denied or non-interactive session. No Flagsmith write made."
        )
        write_incident_report(
            state_dir=runtime.state_dir,
            bundle=bundle,
            verdict=verdict,
            plan=loaded_plan,
            ranking_mode=mode,
            approval=approval,
        )
        raise typer.Exit(code=int(ExitCode.UNRESOLVED_OR_REJECTED))

    fs_client = _build_flagsmith_from_config(runtime.config)
    if fs_client is None:
        runtime.console.print(f"Missing {runtime.config.flagsmith.admin_key_env}.")
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE))

    try:
        with fs_client, DemoSymptomReader(runtime.config.symptoms) as symptom_reader:
            exec_result = execute_option(
                client=fs_client,
                config=runtime.config,
                plan=loaded_plan,
                option=chosen,
                approval=approval,
            )
            audit.log(
                "execute",
                incident_id=incident_id,
                details=exec_result.model_dump(mode="json"),
            )
            verify_result = verify_mitigation(
                client=fs_client,
                symptom_reader=symptom_reader,
                option=chosen,
                config=runtime.config,
            )
            audit.log(
                "verify",
                incident_id=incident_id,
                details=verify_result.model_dump(mode="json"),
            )
    except PreconditionDriftError as exc:
        runtime.console.print(f"Precondition failed: {exc}")
        raise typer.Exit(code=int(ExitCode.PRECONDITION_FAILED)) from exc
    except FlagsmithAuthOrReachabilityError as exc:
        runtime.console.print(f"Flagsmith error: {exc}")
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE)) from exc
    except ExecutorError as exc:
        runtime.console.print(f"Executor blocked operation: {exc}")
        raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from exc

    md_path, _ = write_incident_report(
        state_dir=runtime.state_dir,
        bundle=bundle,
        verdict=verdict,
        plan=loaded_plan,
        ranking_mode=mode,
        approval=approval,
        execution=exec_result,
        verification=verify_result,
    )
    runtime.console.print(
        f"Applied option {option} | state_verified={verify_result.state_verified} "
        f"| recovered={verify_result.recovered} | report={md_path}"
    )
    if not verify_result.recovered:
        raise typer.Exit(code=int(ExitCode.UNRESOLVED_OR_REJECTED))


@app.command()
def undo(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
) -> None:
    """Restore pre-mitigation Flagsmith state (requires fresh typed approval)."""
    runtime = _require_ctx(ctx)
    runtime.console.print(f"Undo gate prepared for {incident_id}.")


@app.command()
def report(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
) -> None:
    """Write Markdown and JSON incident reports to .flagrewind/incidents/<id>/."""
    runtime = _require_ctx(ctx)
    try:
        bundle, verdict, loaded_plan, mode = _load_saved_incident(runtime.state_dir, incident_id)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc
    md_path, json_path = write_incident_report(
        state_dir=runtime.state_dir,
        bundle=bundle,
        verdict=verdict,
        plan=loaded_plan,
        ranking_mode=mode,
    )
    runtime.console.print(f"Wrote reports: {md_path} and {json_path}")
