"""Interactive human approval gate enforcing typed confirmation before any write."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from flagrewind.planner import MitigationPlan, PlanOption, compute_plan_hash


class ApprovalRecord(BaseModel):
    """Recorded human approval or rejection bound to plan hash."""

    plan_id: str
    plan_hash: str
    option_n: int
    approver: str
    approved: bool
    phrase_entered: str
    expected_phrase: str
    decided_at: datetime


def expected_approval_phrase(plan_id: str, option_n: int) -> str:
    """Return exact phrase required to approve a Flagsmith write."""
    return f"apply {plan_id} option {option_n}"


def expected_undo_phrase(plan_id: str) -> str:
    """Return exact phrase required to approve an undo operation."""
    return f"undo {plan_id}"


def resolve_approver_name(
    state_dir: Path,
    *,
    approver: str | None = None,
    input_fn: Callable[[str], str] = input,
) -> str:
    """Ask approver name once per session and cache under .flagrewind/approver.txt."""
    if approver and approver.strip():
        return approver.strip()
    cache_path = state_dir / "approver.txt"
    if cache_path.exists():
        cached = cache_path.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    entered = input_fn("Approver name (recorded in audit log): ").strip() or "operator"
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(entered, encoding="utf-8")
    return entered


def request_approval(
    plan: MitigationPlan,
    option: PlanOption,
    *,
    state_dir: Path,
    expected_phrase: str | None = None,
    approver: str | None = None,
    input_fn: Callable[[str], str] | None = None,
    is_interactive: bool | None = None,
) -> ApprovalRecord:
    """Prompt human for exact confirmation phrase; reject automatically if non-interactive."""
    phrase_target = expected_phrase or expected_approval_phrase(plan.plan_id, option.n)
    plan_hash = compute_plan_hash(plan, option.n)
    interactive = sys.stdin.isatty() if is_interactive is None else is_interactive

    if not interactive:
        return ApprovalRecord(
            plan_id=plan.plan_id,
            plan_hash=plan_hash,
            option_n=option.n,
            approver=approver or "non-interactive",
            approved=False,
            phrase_entered="",
            expected_phrase=phrase_target,
            decided_at=datetime.now(UTC),
        )

    reader = input_fn or input
    operator = resolve_approver_name(state_dir, approver=approver, input_fn=reader)
    entered = reader(f"Type '{phrase_target}' to apply (anything else cancels): ").strip()
    approved = entered == phrase_target
    return ApprovalRecord(
        plan_id=plan.plan_id,
        plan_hash=plan_hash,
        option_n=option.n,
        approver=operator,
        approved=approved,
        phrase_entered=entered,
        expected_phrase=phrase_target,
        decided_at=datetime.now(UTC),
    )
