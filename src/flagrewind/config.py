"""Strict Pydantic v2 configuration loader and exit codes for FlagRewind."""

from __future__ import annotations

import os
from enum import IntEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ExitCode(IntEnum):
    """Canonical FlagRewind CLI exit codes."""

    OK = 0
    UNRESOLVED_OR_REJECTED = 1
    CONFIG_ERROR = 2
    REMOTE_UNREACHABLE = 3
    INTERNAL_ERROR = 4
    PRECONDITION_FAILED = 5


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


class StrictModel(BaseModel):
    """Base Pydantic model forbidding unexpected YAML keys."""

    model_config = ConfigDict(extra="forbid")


class FlagsmithConfig(StrictModel):
    """Flagsmith API endpoint and environment-variable references."""

    api_url_env: str = Field(min_length=1)
    admin_key_env: str = Field(min_length=1)
    project: str = Field(min_length=1)


class EnvironmentPolicy(StrictModel):
    """Write policy for a single Flagsmith environment."""

    writable: bool = False


class JournalConfig(StrictModel):
    """Webhook receiver and audit-log poller configuration."""

    webhook_port: int = Field(ge=1, le=65535)
    webhook_secret_env: str = Field(min_length=1)
    audit_poll_seconds: int = Field(ge=1, le=3600)


class SymptomsConfig(StrictModel):
    """Symptom source polling and breach thresholds."""

    source: Literal["demo", "prometheus"] = "demo"
    demo_url: str = Field(min_length=1)
    error_rate_threshold: float = Field(gt=0.0, le=1.0)
    baseline_window_s: int = Field(ge=10, le=3600)
    detect_window_s: int = Field(ge=5, le=600)


class CodeConfig(StrictModel):
    """Source code directories indexed for error stack-frame correlation."""

    paths: list[str] = Field(min_length=1)


class LLMConfig(StrictModel):
    """OpenAI-compatible LLM endpoint configuration."""

    base_url_env: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    model_env: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    max_calls_per_incident: int = Field(ge=1, le=10)
    timeout_s: int = Field(ge=1, le=300)


class PolicyConfig(StrictModel):
    """Autonomy, verification, and allowed-action policy."""

    allowed_actions: list[Literal["revert_change", "disable_for_segment"]] = Field(min_length=1)
    confidence_floor: float = Field(ge=0.0, le=1.0)
    verify_seconds: int = Field(ge=5, le=600)
    sdk_refresh_seconds: int = Field(ge=1, le=300)


class FlagRewindConfig(StrictModel):
    """Root validated configuration loaded from flagrewind.yaml."""

    version: Literal[1]
    flagsmith: FlagsmithConfig
    environments: dict[str, EnvironmentPolicy] = Field(min_length=1)
    journal: JournalConfig
    symptoms: SymptomsConfig
    code: CodeConfig
    llm: LLMConfig
    policy: PolicyConfig

    @field_validator("environments")
    @classmethod
    def _non_empty_env_names(
        cls, value: dict[str, EnvironmentPolicy]
    ) -> dict[str, EnvironmentPolicy]:
        for name in value:
            if not name.strip():
                raise ValueError("Environment names must be non-empty.")
        return value

    def writable_environments(self) -> set[str]:
        """Return environment names explicitly permitted for writes."""
        return {name for name, policy in self.environments.items() if policy.writable}


def resolve_env(env_name: str, *, required: bool = False) -> str | None:
    """Resolve an environment variable value without logging secrets."""
    val = os.environ.get(env_name)
    if val is not None:
        val = val.strip()
    if not val:
        if required:
            raise ConfigError(f"Required environment variable '{env_name}' is not set.")
        return None
    return val


def load_config(path: Path) -> FlagRewindConfig:
    """Load and strictly validate flagrewind.yaml, raising ConfigError on any flaw."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config root in {path} must be a YAML mapping.")

    try:
        return FlagRewindConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Config validation failed in {path}:\n{exc}") from exc


def state_dir_for(config_path: Path) -> Path:
    """Return runtime state directory (.flagrewind) anchored alongside config file."""
    return config_path.resolve().parent / ".flagrewind"
