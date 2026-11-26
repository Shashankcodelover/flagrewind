"""Unit tests for strict configuration validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from flagrewind.config import ConfigError, load_config, resolve_env


def test_default_flagrewind_yaml_is_valid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "flagrewind.yaml")
    assert cfg.version == 1
    assert cfg.flagsmith.project == "Acme Checkout"
    assert cfg.writable_environments() == {"production"}
    assert cfg.policy.confidence_floor == 0.6


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    bad_cfg = tmp_path / "flagrewind.yaml"
    source = Path(__file__).resolve().parents[2] / "flagrewind.yaml"
    bad_cfg.write_text(
        source.read_text(encoding="utf-8") + "\nunexpected_extra_key: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unexpected_extra_key"):
        load_config(bad_cfg)


def test_missing_config_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_resolve_env_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_MISSING_ENV", raising=False)
    assert resolve_env("TEST_MISSING_ENV") is None
    with pytest.raises(ConfigError, match="TEST_MISSING_ENV"):
        resolve_env("TEST_MISSING_ENV", required=True)
    monkeypatch.setenv("TEST_MISSING_ENV", "  ok-val  ")
    assert resolve_env("TEST_MISSING_ENV", required=True) == "ok-val"
