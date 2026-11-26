"""Deterministic inverse planner producing exact Flagsmith Admin API operations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from flagrewind.config import FlagRewindConfig
from flagrewind.evidence import EvidenceBundle
from flagrewind.journal.store import FlagStateSnapshot, JournalEvent
from flagrewind.ranking.schema import RankingVerdict


class ApiOperation(BaseModel):
    """Single allowlisted Admin API mutation (never DELETE)."""

    method: Literal["PATCH", "POST"]
    path: str
    body: dict[str, Any]


class PlanOption(BaseModel):
    """Concrete mitigation option derived deterministically from journal history."""

    n: int
    action: Literal["revert_change", "disable_for_segment"]
    change_id: str
    environment: str
    environment_api_key: str | None = None
    feature: str
    feature_state_id: int | None = None
    operations: list[ApiOperation]
    precondition: dict[str, Any]
    inverse_operations: list[ApiOperation]
    blast_radius: str
    preview: str
    executable: bool = True
    unexecutable_reason: str | None = None


class MitigationPlan(BaseModel):
    """Complete dry-run plan for an incident."""

    plan_id: str
    incident_id: str
    created_at: datetime
    options: list[PlanOption] = Field(default_factory=list)


def compute_plan_hash(plan: MitigationPlan, option_n: int) -> str:
    """Compute stable SHA-256 hash binding approval to exact operations and preconditions."""
    option = next((o for o in plan.options if o.n == option_n), None)
    if option is None:
        raise ValueError(f"Option {option_n} not found in plan {plan.plan_id}.")
    canonical = json.dumps(
        {
            "plan_id": plan.plan_id,
            "incident_id": plan.incident_id,
            "option": option.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_to_patch_body(snapshot: FlagStateSnapshot) -> dict[str, Any]:
    body: dict[str, Any] = {"enabled": snapshot.enabled}
    if snapshot.value is not None:
        body["feature_state_value"] = snapshot.value
    if snapshot.multivariate_values is not None:
        body["multivariate_feature_state_values"] = snapshot.multivariate_values
    return body


def _precondition_from_snapshot(snapshot: FlagStateSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    pre: dict[str, Any] = {"enabled": snapshot.enabled}
    if snapshot.value is not None:
        pre["feature_state_value"] = snapshot.value
    return pre


def _build_option_for_candidate(
    *,
    option_n: int,
    action: Literal["revert_change", "disable_for_segment"],
    event: JournalEvent,
    env_key: str | None,
    state_id: int | None,
) -> PlanOption:
    target_path = (
        f"environments/{env_key}/featurestates/{state_id}/"
        if env_key and state_id is not None
        else ""
    )
    precondition = _precondition_from_snapshot(event.new)
    can_exec = bool(target_path) and event.previous is not None

    if action == "revert_change":
        if event.previous is None:
            return PlanOption(
                n=option_n,
                action="revert_change",
                change_id=event.id,
                environment=event.environment,
                environment_api_key=env_key,
                feature=event.feature,
                feature_state_id=state_id,
                operations=[],
                precondition=precondition,
                inverse_operations=[],
                blast_radius=f"{event.environment} / {event.feature} ({event.segment or 'all'})",
                preview="Cannot auto-revert: journal lacks previous state (pick target value).",
                executable=False,
                unexecutable_reason="Missing previous state in journal.",
            )
        target_body = _snapshot_to_patch_body(event.previous)
        inverse_body = _snapshot_to_patch_body(event.new) if event.new else {}
        new_enabled = event.new.enabled if event.new else "?"
        new_value = event.new.value if event.new else "?"
        preview = (
            f"{event.feature} ({event.segment or 'all users'}) in {event.environment}: "
            f"enabled={new_enabled} value={new_value} "
            f"-> enabled={event.previous.enabled} value={event.previous.value}"
        )
    else:
        # disable_for_segment explicitly disables the feature in the segment override.
        target_body = {"enabled": False}
        inverse_body = _snapshot_to_patch_body(event.new) if event.new else {"enabled": True}
        preview = (
            f"Disable {event.feature} strictly for segment "
            f"'{event.segment or 'unknown'}' in {event.environment}"
        )
        can_exec = bool(target_path) and event.segment is not None

    ops = [ApiOperation(method="PATCH", path=target_path, body=target_body)] if can_exec else []
    inv = (
        [ApiOperation(method="PATCH", path=target_path, body=inverse_body)]
        if can_exec and inverse_body
        else []
    )
    reason = None if can_exec else "Missing environment_api_key or feature_state_id in raw_ref."
    return PlanOption(
        n=option_n,
        action=action,
        change_id=event.id,
        environment=event.environment,
        environment_api_key=env_key,
        feature=event.feature,
        feature_state_id=state_id,
        operations=ops,
        precondition=precondition,
        inverse_operations=inv,
        blast_radius=(
            f"env={event.environment}, feature={event.feature}, "
            f"scope={event.segment or 'all users'}"
        ),
        preview=preview,
        executable=can_exec,
        unexecutable_reason=reason,
    )


def build_mitigation_plan(
    *,
    incident_id: str,
    bundle: EvidenceBundle,
    verdict: RankingVerdict,
    events: list[JournalEvent],
    config: FlagRewindConfig,
    env_key_lookup: dict[str, str] | None = None,
) -> MitigationPlan:
    """Build deterministic PlanOptions for ranked writable suspects."""
    event_by_id = {ev.id: ev for ev in events}
    writable = {name.lower() for name in config.writable_environments()}
    allowed = set(config.policy.allowed_actions)
    plan_id = f"PLN-{incident_id.replace('INC-', '')}"
    options: list[PlanOption] = []
    next_n = 1

    # Always order recommended change first if present, followed by remaining writable suspects.
    ordered_ids: list[str] = []
    if verdict.recommended.change_id:
        ordered_ids.append(verdict.recommended.change_id)
    for item in verdict.ranking:
        if item.change_id not in ordered_ids:
            ordered_ids.append(item.change_id)

    for chg_id in ordered_ids:
        ev = event_by_id.get(chg_id)
        if ev is None or ev.environment.lower() not in writable:
            continue
        env_key = ev.raw_ref.get("environment_api_key") or (
            (env_key_lookup or {}).get(ev.environment.lower())
        )
        raw_sid = ev.raw_ref.get("feature_state_id")
        state_id = int(raw_sid) if raw_sid is not None else None

        if "revert_change" in allowed:
            options.append(
                _build_option_for_candidate(
                    option_n=next_n,
                    action="revert_change",
                    event=ev,
                    env_key=str(env_key) if env_key else None,
                    state_id=state_id,
                )
            )
            next_n += 1

        if "disable_for_segment" in allowed and ev.segment is not None:
            options.append(
                _build_option_for_candidate(
                    option_n=next_n,
                    action="disable_for_segment",
                    event=ev,
                    env_key=str(env_key) if env_key else None,
                    state_id=state_id,
                )
            )
            next_n += 1

    _ = bundle
    return MitigationPlan(
        plan_id=plan_id,
        incident_id=incident_id,
        created_at=datetime.now(UTC),
        options=options,
    )
