"""Typer CLI entrypoint for FlagRewind."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from flagrewind.config import ConfigError, ExitCode, FlagRewindConfig, load_config, state_dir_for

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
    """Extract validated RuntimeContext or exit cleanly."""
    if not isinstance(ctx.obj, RuntimeContext):
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))
    return ctx.obj


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
    runtime.console.print("FlagRewind doctor is initialized; capability checks arrive in Module 5.")


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
    mode = "rules-only" if runtime.no_llm else "llm+rules"
    runtime.console.print(
        f"Investigate ready ({mode}, since={since}, symptom={symptom or 'auto'})."
    )


@app.command()
def plan(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident identifier, e.g. INC-0001.")],
) -> None:
    """Show the deterministic dry-run plan and inverse operations for an incident."""
    runtime = _require_ctx(ctx)
    runtime.console.print(f"Plan preview requested for {incident_id}.")


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
    runtime.console.print(f"Apply gate prepared for {incident_id} option {option}.")


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
    runtime.console.print(f"Report generator prepared for {incident_id}.")
