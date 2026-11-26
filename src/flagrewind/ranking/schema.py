"""Strict Pydantic v2 schemas and anti-injection validators for suspect ranking."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"bypass\s+(?:the\s+)?approval", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
)


class RankingValidationError(ValueError):
    """Raised when ranking output violates schema, candidate IDs, or safety checks."""


class RankedSuspect(BaseModel):
    """Single ranked Flagsmith change suspect."""

    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    counter_evidence: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("evidence", "counter_evidence")
    @classmethod
    def _cap_bullets(cls, items: list[str]) -> list[str]:
        for item in items:
            if len(item) > 300:
                raise ValueError("Evidence bullet exceeds 300 characters.")
        return items


class RecommendedAction(BaseModel):
    """Proposed mitigation action (always 'none' unless verdict == 'flag_change')."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["revert_change", "disable_for_segment", "none"]
    change_id: str | None = Field(default=None, max_length=64)
    why: str = Field(min_length=1, max_length=300)


class RankingVerdict(BaseModel):
    """Validated output of either rules-only or LLM suspect ranking."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["flag_change", "not_flag_related", "insufficient_evidence"]
    ranking: list[RankedSuspect] = Field(default_factory=list, max_length=5)
    recommended: RecommendedAction
    questions: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("questions")
    @classmethod
    def _cap_questions(cls, items: list[str]) -> list[str]:
        for item in items:
            if len(item) > 300:
                raise ValueError("Question exceeds 300 characters.")
        return items


def validate_ranking_output(
    raw: dict[str, Any] | RankingVerdict,
    candidate_ids: set[str],
    *,
    confidence_floor: float = 0.0,
) -> RankingVerdict:
    """Validate ranking JSON/model against candidate_ids and safety invariants."""
    try:
        parsed = (
            raw
            if isinstance(raw, RankingVerdict)
            else RankingVerdict.model_validate(raw)
        )
    except ValidationError as exc:
        raise RankingValidationError(f"Schema validation failed: {exc}") from exc

    for item in parsed.ranking:
        if item.change_id not in candidate_ids:
            raise RankingValidationError(
                f"Hallucinated change_id '{item.change_id}' not in candidate list."
            )
        for text in [*item.evidence, *item.counter_evidence]:
            _reject_injection(text)

    _reject_injection(parsed.recommended.why)
    for q in parsed.questions:
        _reject_injection(q)

    if parsed.verdict != "flag_change":
        if parsed.recommended.action != "none" or parsed.recommended.change_id is not None:
            raise RankingValidationError(
                f"Verdict '{parsed.verdict}' requires recommended.action='none' and change_id=null."
            )
    else:
        rec_id = parsed.recommended.change_id
        if parsed.recommended.action == "none" or rec_id is None:
            raise RankingValidationError(
                "Verdict 'flag_change' requires non-none action and target change_id."
            )
        if rec_id not in candidate_ids:
            raise RankingValidationError(
                f"Recommended change_id '{rec_id}' not in candidate list."
            )
        if parsed.ranking and parsed.ranking[0].confidence < confidence_floor:
            raise RankingValidationError(
                "Recommended flag_change while top confidence is below confidence_floor."
            )

    return parsed


def _reject_injection(text: str) -> None:
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            raise RankingValidationError("Potential prompt-injection text rejected in output.")
