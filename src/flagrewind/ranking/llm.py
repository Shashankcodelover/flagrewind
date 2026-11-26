"""Validated LLM suspect ranker with single repair retry and labelled rules fallback."""

from __future__ import annotations

from typing import Any, Protocol

from flagrewind.audit import AuditTrail
from flagrewind.config import FlagRewindConfig
from flagrewind.evidence import EvidenceBundle
from flagrewind.llm_client import LLMCallResult, LLMClientError
from flagrewind.ranking.prompts import SYSTEM_PROMPT, build_repair_prompt, build_user_prompt
from flagrewind.ranking.rules import rank_with_rules
from flagrewind.ranking.schema import (
    RankingValidationError,
    RankingVerdict,
    validate_ranking_output,
)


class LLMCompleter(Protocol):
    """Protocol satisfied by OpenAICompatibleLLMClient and unit test doubles."""

    model: str

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMCallResult: ...


def rank_with_llm(
    bundle: EvidenceBundle,
    *,
    config: FlagRewindConfig,
    llm_client: LLMCompleter | None,
    audit_trail: AuditTrail | None = None,
) -> tuple[RankingVerdict, str, dict[str, Any]]:
    """Rank candidates via LLM with 1 repair retry, falling back to rules-only if invalid/down."""
    confidence_floor = config.policy.confidence_floor
    if llm_client is None:
        verdict = rank_with_rules(bundle, confidence_floor=confidence_floor)
        return verdict, "rules-only", {"calls": 0, "tokens": None, "model": None}

    candidate_ids = {c.change_id for c in bundle.candidates}
    user_prompt = build_user_prompt(bundle, confidence_floor=confidence_floor)
    schema = RankingVerdict.model_json_schema()
    max_calls = min(2, config.llm.max_calls_per_incident)

    calls = 0
    total_tokens = 0
    has_tokens = False
    last_raw = ""
    last_error = ""
    rejected = False

    while calls < max_calls:
        calls += 1
        prompt_to_send = (
            user_prompt
            if calls == 1
            else build_repair_prompt(user_prompt, last_raw, last_error)
        )
        try:
            res = llm_client.complete_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt_to_send,
                json_schema=schema,
            )
        except LLMClientError as exc:
            last_error = str(exc)
            if audit_trail is not None:
                audit_trail.log(
                    "llm_call",
                    incident_id=bundle.incident_id,
                    details={
                        "model": llm_client.model,
                        "status": "error",
                        "error": last_error[:200],
                    },
                )
            if "invalid JSON" in last_error:
                rejected = True
                last_raw = last_error
                continue
            break

        last_raw = res.raw_text
        if res.total_tokens is not None:
            total_tokens += res.total_tokens
            has_tokens = True

        try:
            verdict = validate_ranking_output(
                res.parsed_json,
                candidate_ids,
                confidence_floor=confidence_floor,
            )
            if audit_trail is not None:
                audit_trail.log(
                    "llm_call",
                    incident_id=bundle.incident_id,
                    details={
                        "model": res.model,
                        "status": "valid",
                        "attempt": calls,
                        "prompt_sha256": res.prompt_sha256,
                        "response_sha256": res.response_sha256,
                        "tokens": res.total_tokens,
                    },
                )
            return (
                verdict,
                f"llm ({res.model})",
                {
                    "model": res.model,
                    "calls": calls,
                    "tokens": total_tokens if has_tokens else None,
                },
            )
        except RankingValidationError as exc:
            rejected = True
            last_error = str(exc)
            if audit_trail is not None:
                audit_trail.log(
                    "llm_call",
                    incident_id=bundle.incident_id,
                    details={
                        "model": res.model,
                        "status": "rejected",
                        "attempt": calls,
                        "prompt_sha256": res.prompt_sha256,
                        "response_sha256": res.response_sha256,
                        "error": last_error[:200],
                    },
                )

    fallback = rank_with_rules(bundle, confidence_floor=confidence_floor)
    label = "rules-only (LLM output rejected)" if rejected else "rules-only (LLM unavailable)"
    return (
        fallback,
        label,
        {
            "model": getattr(llm_client, "model", None),
            "calls": calls,
            "tokens": total_tokens if has_tokens else None,
            "fallback_reason": last_error[:200],
        },
    )
