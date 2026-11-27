"""Allowlisted Flagsmith Admin API executor with precondition drift checks."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from flagrewind.approval import ApprovalRecord
from flagrewind.config import FlagRewindConfig
from flagrewind.flagsmith_client import FlagsmithClient
from flagrewind.planner import ApiOperation, MitigationPlan, PlanOption, compute_plan_hash

_ALLOWED_PATH_RE = re.compile(r"^environments/[^/]+/featurestates/(?:\d+/)?$")


class ExecutorError(RuntimeError):
    """Raised when an execution violates allowlists or lacks approval."""


class PreconditionDriftError(ExecutorError):
    """Raised when Flagsmith state changed between planning and execution (exit 5)."""


class ExecutionResult(BaseModel):
    """Summary of executed Flagsmith Admin API operations."""

    plan_id: str
    option_n: int
    executed_at: datetime
    approver: str
    operations_applied: int
    resulting_state: dict[str, Any] = {}


def _validate_operation(op: ApiOperation, option: PlanOption, writable_envs: set[str]) -> None:
    if option.environment.lower() not in writable_envs:
        raise ExecutorError(
            f"Blocked write to non-writable environment '{option.environment}'."
        )
    if op.method not in {"PATCH", "POST", "PUT"}:
        raise ExecutorError(f"Blocked disallowed HTTP method '{op.method}'.")
    if not _ALLOWED_PATH_RE.match(op.path.lstrip("/")):
        raise ExecutorError(f"Blocked write outside featurestates allowlist: {op.path}")


def _check_precondition(
    client: FlagsmithClient,
    option: PlanOption,
) -> dict[str, Any]:
    if not option.environment_api_key or option.feature_state_id is None:
        raise ExecutorError("Cannot verify precondition: missing environment_api_key or state id.")
    current = client.get_feature_state(option.environment_api_key, option.feature_state_id)
    for key, expected in option.precondition.items():
        actual = current.get(key)
        if actual != expected:
            raise PreconditionDriftError(
                f"State drift detected for {option.feature} ({key}: expected {expected!r}, "
                f"got {actual!r}). Re-plan before applying."
            )
    return current


def execute_option(
    *,
    client: FlagsmithClient,
    config: FlagRewindConfig,
    plan: MitigationPlan,
    option: PlanOption,
    approval: ApprovalRecord,
    skip_precondition: bool = False,
) -> ExecutionResult:
    """Execute a planned Flagsmith change only after verifying approval, allowlist, and drift."""
    if not approval.approved:
        raise ExecutorError("Execution blocked: human approval was not granted.")
    if approval.plan_id != plan.plan_id or approval.option_n != option.n:
        raise ExecutorError("Execution blocked: approval does not match selected plan option.")
    if approval.plan_hash != compute_plan_hash(plan, option.n):
        raise ExecutorError("Execution blocked: plan hash mismatch (plan altered after approval).")
    if not option.executable or not option.operations:
        raise ExecutorError(option.unexecutable_reason or "Selected plan option is not executable.")

    writable_envs = {name.lower() for name in config.writable_environments()}
    for op in option.operations:
        _validate_operation(op, option, writable_envs)

    if not skip_precondition and option.precondition:
        _check_precondition(client, option)

    last_state: dict[str, Any] = {}
    for op in option.operations:
        if op.method == "PATCH" and option.environment_api_key and option.feature_state_id:
            last_state = client.patch_feature_state(
                option.environment_api_key,
                option.feature_state_id,
                op.body,
            )
        elif op.method == "PUT" and option.environment_api_key and option.feature_state_id:
            last_state = client.put_feature_state(
                option.environment_api_key,
                option.feature_state_id,
                op.body,
            )
        elif op.method == "POST" and option.environment_api_key:
            last_state = client.create_segment_override(option.environment_api_key, op.body)
        else:
            raise ExecutorError(f"Unsupported operation dispatch: {op.method} {op.path}")

    return ExecutionResult(
        plan_id=plan.plan_id,
        option_n=option.n,
        executed_at=datetime.now(UTC),
        approver=approval.approver,
        operations_applied=len(option.operations),
        resulting_state=last_state,
    )
