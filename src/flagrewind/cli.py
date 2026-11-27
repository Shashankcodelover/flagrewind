"""Typer CLI entrypoint for FlagRewind."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console

from flagrewind.approval import expected_undo_phrase, request_approval
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
from flagrewind.executor import (
    ExecutionResult,
    ExecutorError,
    PreconditionDriftError,
    execute_option,
)
from flagrewind.flagsmith_client import (
    FlagsmithAuthOrReachabilityError,
    FlagsmithClient,
    FlagsmithError,
)
from flagrewind.journal.audit_poll import AuditPoller
from flagrewind.journal.store import JournalStore
from flagrewind.journal.webhook import create_webhook_app
from flagrewind.llm_client import OpenAICompatibleLLMClient
from flagrewind.notifier import send_notify_webhook
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
from flagrewind.web_console import create_console_app

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


def _load_dotenv_if_local(config_path: Path) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    env_file = config_path.resolve().parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _resolve_env_key_lookup(cfg: FlagRewindConfig) -> dict[str, str]:
    fs_client = _build_flagsmith_from_config(cfg)
    if fs_client is None:
        return {}
    try:
        with fs_client:
            proj = fs_client.get_project_by_name(cfg.flagsmith.project)
            return {
                str(e["name"]).lower(): str(e["api_key"])
                for e in fs_client.list_environments(int(proj["id"]))
                if e.get("name") and e.get("api_key")
            }
    except FlagsmithError:
        return {}


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


def _run_investigation(
    runtime: RuntimeContext,
    *,
    symptom_summary: SymptomSummary,
    extra_notes: list[str] | None = None,
    notify_url: str | None = None,
) -> tuple[EvidenceBundle, RankingVerdict, MitigationPlan, str]:
    store = JournalStore(
        runtime.state_dir / "journal.jsonl",
        runtime.state_dir / "journal_heartbeat.json",
    )
    code_idx = CodeIndex.build(runtime.config_path.resolve().parent, runtime.config.code.paths)
    inc_id = _next_incident_id(runtime.state_dir)
    bundle = build_evidence_bundle(
        incident_id=inc_id,
        config=runtime.config,
        store=store,
        symptom=symptom_summary,
        code_index=code_idx,
        extra_notes=extra_notes,
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
    env_lookup = (
        {}
        if "PYTEST_CURRENT_TEST" in os.environ
        else _resolve_env_key_lookup(runtime.config)
    )
    mitigation_plan = build_mitigation_plan(
        incident_id=inc_id,
        bundle=bundle,
        verdict=verdict,
        events=events,
        config=runtime.config,
        env_key_lookup=env_lookup,
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
    send_notify_webhook(
        notify_url,
        {
            "event": "incident_brief",
            "incident_id": inc_id,
            "verdict": verdict.verdict,
            "recommended": verdict.recommended.model_dump(mode="json"),
        },
    )
    return bundle, verdict, mitigation_plan, mode


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
    _load_dotenv_if_local(config)
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
def journal(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Webhook server bind host.")] = "0.0.0.0",
) -> None:
    """Run the Flagsmith webhook receiver and audit-log poller."""
    runtime = _require_ctx(ctx)
    store = JournalStore(
        runtime.state_dir / "journal.jsonl",
        runtime.state_dir / "journal_heartbeat.json",
    )
    secret = resolve_env(runtime.config.journal.webhook_secret_env) or ""
    webhook_app = create_webhook_app(store, secret=secret)

    stop_event = threading.Event()
    fs_client = _build_flagsmith_from_config(runtime.config)

    def _poll_loop() -> None:
        if fs_client is None:
            return
        with fs_client:
            project_id: int | None = None
            org_id: int | None = None
            env_keys: dict[str, str] = {}
            env_ids: dict[str, int] = {}
            try:
                orgs = fs_client.list_organisations()
                if orgs and "id" in orgs[0]:
                    org_id = int(orgs[0]["id"])
                proj = fs_client.get_project_by_name(runtime.config.flagsmith.project)
                project_id = int(proj["id"])
                for env in fs_client.list_environments(project_id):
                    if env.get("name") and env.get("api_key"):
                        ename = str(env["name"]).lower()
                        env_keys[ename] = str(env["api_key"])
                        if "id" in env:
                            env_ids[ename] = int(env["id"])
            except FlagsmithError:
                pass

            poller = AuditPoller(
                fs_client,
                store,
                project_id=project_id,
                organisation_id=org_id,
                env_keys=env_keys,
                env_ids=env_ids,
            )
            while not stop_event.is_set():
                try:
                    poller.poll_once()
                except FlagsmithError as exc:
                    runtime.console.print(f"Audit poll warning: {exc}")
                stop_event.wait(runtime.config.journal.audit_poll_seconds)

    poller_thread = threading.Thread(target=_poll_loop, daemon=True)
    poller_thread.start()
    runtime.console.print(
        f"FlagRewind journal listening on http://{host}:{runtime.config.journal.webhook_port} "
        f"(audit poll every {runtime.config.journal.audit_poll_seconds}s)"
    )
    try:
        uvicorn.run(
            webhook_app,
            host=host,
            port=runtime.config.journal.webhook_port,
            log_level="warning",
        )
    finally:
        stop_event.set()
        poller_thread.join(timeout=2.0)


@app.command()
def watch(
    ctx: typer.Context,
    interval: Annotated[float, typer.Option("--interval", help="Polling interval seconds.")] = 5.0,
    once: Annotated[bool, typer.Option("--once", help="Stop after opening one incident.")] = True,
    max_polls: Annotated[
        int, typer.Option("--max-polls", min=0, help="0=unlimited, >0 stops after N polls.")
    ] = 0,
    notify_url: Annotated[
        str | None,
        typer.Option("--notify-url", help="Optional generic webhook URL for JSON status POST."),
    ] = None,
) -> None:
    """Poll symptom metrics and open read-only investigations on threshold breach."""
    runtime = _require_ctx(ctx)
    polls = 0
    runtime.console.print(
        f"Watching {runtime.config.symptoms.demo_url} "
        f"(threshold={runtime.config.symptoms.error_rate_threshold:.2%})..."
    )
    try:
        with DemoSymptomReader(runtime.config.symptoms) as reader:
            while max_polls == 0 or polls < max_polls:
                polls += 1
                summary = reader.fetch()
                if summary.breached:
                    _run_investigation(runtime, symptom_summary=summary, notify_url=notify_url)
                    if once:
                        return
                if max_polls == 0 or polls < max_polls:
                    time.sleep(interval)
    except SymptomError as exc:
        runtime.console.print(f"Symptom source error: {exc}")
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE)) from exc


@app.command()
def investigate(
    ctx: typer.Context,
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30m.")] = "30m",
    symptom: Annotated[
        str | None, typer.Option("--symptom", help="Optional symptom description.")
    ] = None,
    notify_url: Annotated[
        str | None,
        typer.Option("--notify-url", help="Optional generic webhook URL for JSON status POST."),
    ] = None,
) -> None:
    """Gather evidence, rank suspects, build dry-run plan, and render one-screen brief."""
    runtime = _require_ctx(ctx)
    try:
        since_min = _parse_since_minutes(since)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc

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

    notes = [f"Operator symptom note: {symptom}"] if symptom else []
    _run_investigation(
        runtime,
        symptom_summary=symptom_summary,
        extra_notes=notes,
        notify_url=notify_url,
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
            (incident_dir(runtime.state_dir, incident_id) / "execution.json").write_text(
                json.dumps(
                    {
                        "option_n": option,
                        "execution": exec_result.model_dump(mode="json"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
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
    send_notify_webhook(
        notify_url,
        {
            "event": "mitigation_applied",
            "incident_id": incident_id,
            "option_n": option,
            "recovered": verify_result.recovered,
        },
    )
    runtime.console.print(
        f"Applied option {option} | state_verified={verify_result.state_verified} "
        f"| recovered={verify_result.recovered} | report={md_path}"
    )
    if not verify_result.recovered:
        next_opt = next((o for o in loaded_plan.options if o.n > option), None)
        next_hint = (
            f" or try 'flagrewind apply {incident_id} --option {next_opt.n}'."
            if next_opt
            else "."
        )
        runtime.console.print(
            f"Recovery not confirmed. Run 'flagrewind undo {incident_id}' to restore state"
            + next_hint
        )
        raise typer.Exit(code=int(ExitCode.UNRESOLVED_OR_REJECTED))


@app.command()
def undo(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
) -> None:
    """Restore pre-mitigation Flagsmith state (requires fresh typed approval)."""
    runtime = _require_ctx(ctx)
    exec_file = incident_dir(runtime.state_dir, incident_id) / "execution.json"
    if not exec_file.exists():
        runtime.console.print(f"No prior execution recorded for {incident_id}.")
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))

    try:
        _, _, loaded_plan, _ = _load_saved_incident(runtime.state_dir, incident_id)
    except ConfigError as exc:
        runtime.console.print(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc

    exec_data = json.loads(exec_file.read_text(encoding="utf-8"))
    applied_opt_n = int(exec_data.get("option_n", 1))
    applied_opt = next((o for o in loaded_plan.options if o.n == applied_opt_n), None)
    if applied_opt is None or not applied_opt.inverse_operations:
        runtime.console.print(f"No inverse operation available to undo {incident_id}.")
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))

    undo_opt = applied_opt.model_copy(
        update={
            "operations": applied_opt.inverse_operations,
            "precondition": applied_opt.operations[-1].body,
        }
    )
    undo_plan = loaded_plan.model_copy(
        update={
            "options": [
                undo_opt if o.n == applied_opt_n else o for o in loaded_plan.options
            ]
        }
    )
    approval = request_approval(
        undo_plan,
        undo_opt,
        state_dir=runtime.state_dir,
        expected_phrase=expected_undo_phrase(undo_plan.plan_id),
    )
    audit = AuditTrail(runtime.state_dir / "audit.jsonl")
    audit.log(
        "undo_approval" if approval.approved else "undo_rejection",
        incident_id=incident_id,
        details=approval.model_dump(mode="json"),
    )
    if not approval.approved:
        runtime.console.print("Undo approval denied. Flagsmith state unchanged.")
        raise typer.Exit(code=int(ExitCode.UNRESOLVED_OR_REJECTED))

    fs_client = _build_flagsmith_from_config(runtime.config)
    if fs_client is None:
        runtime.console.print(f"Missing {runtime.config.flagsmith.admin_key_env}.")
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE))

    try:
        with fs_client:
            res: ExecutionResult = execute_option(
                client=fs_client,
                config=runtime.config,
                plan=undo_plan,
                option=undo_opt,
                approval=approval,
            )
            audit.log("undo_execute", incident_id=incident_id, details=res.model_dump(mode="json"))
    except PreconditionDriftError as exc:
        runtime.console.print(f"Undo precondition failed: {exc}")
        raise typer.Exit(code=int(ExitCode.PRECONDITION_FAILED)) from exc
    except FlagsmithAuthOrReachabilityError as exc:
        runtime.console.print(f"Flagsmith error: {exc}")
        raise typer.Exit(code=int(ExitCode.REMOTE_UNREACHABLE)) from exc
    except ExecutorError as exc:
        runtime.console.print(f"Undo blocked: {exc}")
        raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from exc

    runtime.console.print(f"Undo applied for {incident_id} (restored pre-mitigation state).")


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


@app.command()
def ui(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Bind host for local UI.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port for local UI.")] = 8788,
) -> None:
    """Launch local Incident Timeline, Blast-Radius Diff & Approval Console."""
    runtime = _require_ctx(ctx)
    console_app = create_console_app(runtime.config, runtime.state_dir)
    runtime.console.print(f"FlagRewind Local Console listening on http://{host}:{port}")
    uvicorn.run(console_app, host=host, port=port, log_level="warning")
