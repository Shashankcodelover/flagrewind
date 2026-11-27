"""Post-execution Flagsmith state and error-rate recovery verifier."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from flagrewind.config import FlagRewindConfig
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.planner import PlanOption
from flagrewind.symptoms.demo import DemoSymptomReader


class VerificationResult(BaseModel):
    """Outcome of post-mitigation Flagsmith state + symptom recovery check."""

    state_verified: bool
    recovered: bool
    consecutive_healthy_windows: int
    observed_error_rates: list[float] = Field(default_factory=list)
    final_state: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


def _state_matches_target(actual: dict[str, Any], target_body: dict[str, Any]) -> bool:
    for key in ("enabled", "feature_state_value"):
        if key in target_body and actual.get(key) != target_body[key]:
            return False
    if "multivariate_feature_state_values" in target_body:
        expected_mv = {
            int(item["multivariate_feature_option"]): float(item["percentage_allocation"])
            for item in target_body["multivariate_feature_state_values"]
        }
        actual_mv = {
            int(item["multivariate_feature_option"]): float(item["percentage_allocation"])
            for item in actual.get("multivariate_feature_state_values") or []
        }
        if expected_mv != actual_mv:
            return False
    return True


def verify_mitigation(
    *,
    client: FlagsmithClient,
    symptom_reader: DemoSymptomReader,
    option: PlanOption,
    config: FlagRewindConfig,
    poll_interval_s: float = 5.0,
    required_healthy_windows: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> VerificationResult:
    """Re-read Flagsmith state and verify error rate stays below threshold."""
    if not option.environment_api_key or option.feature_state_id is None or not option.operations:
        return VerificationResult(
            state_verified=False,
            recovered=False,
            consecutive_healthy_windows=0,
            notes="Option lacks target state identifiers for verification.",
        )

    final_state = client.get_feature_state(option.environment_api_key, option.feature_state_id)
    target_body = option.operations[-1].body
    state_ok = _state_matches_target(final_state, target_body)
    if not state_ok:
        return VerificationResult(
            state_verified=False,
            recovered=False,
            consecutive_healthy_windows=0,
            final_state=final_state,
            notes="Flagsmith state did not match planned target after execution.",
        )

    threshold = config.symptoms.error_rate_threshold
    deadline = time.monotonic() + float(config.policy.verify_seconds)
    healthy = 0
    rates: list[float] = []

    while True:
        summary = symptom_reader.fetch()
        rates.append(summary.current)
        if summary.current < threshold:
            healthy += 1
        else:
            healthy = 0

        if healthy >= required_healthy_windows:
            return VerificationResult(
                state_verified=True,
                recovered=True,
                consecutive_healthy_windows=healthy,
                observed_error_rates=rates,
                final_state=final_state,
                notes="Service recovered below error threshold for 3 consecutive windows.",
            )

        if time.monotonic() >= deadline:
            return VerificationResult(
                state_verified=True,
                recovered=False,
                consecutive_healthy_windows=healthy,
                observed_error_rates=rates,
                final_state=final_state,
                notes="Verify timeout reached without 3 consecutive healthy windows.",
            )
        sleep_fn(poll_interval_s)
