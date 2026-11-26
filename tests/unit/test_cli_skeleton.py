"""Unit tests for the FlagRewind CLI skeleton."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from flagrewind.cli import app
from flagrewind.config import ExitCode

runner = CliRunner()


def test_cli_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("doctor", "journal", "watch", "investigate", "plan", "apply", "undo", "report"):
        assert cmd in result.stdout


def test_cli_missing_config_exits_with_code_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "doctor"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)


def test_cli_no_llm_flag_propagates(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg_path = tmp_path / "flagrewind.yaml"
    cfg_path.write_text(
        (repo_root / "flagrewind.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = runner.invoke(
        app, ["--config", str(cfg_path), "--no-llm", "investigate", "--symptom", "unit check"]
    )
    assert result.exit_code == 0
    assert "rules-only" in result.stdout
