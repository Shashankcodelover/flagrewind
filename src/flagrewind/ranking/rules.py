"""Deterministic rules-only suspect ranker (also serves as fallback and baseline B2)."""

from __future__ import annotations

from flagrewind.evidence import CandidateChange, EvidenceBundle
from flagrewind.ranking.schema import RankedSuspect, RankingVerdict, RecommendedAction


def _build_evidence_bullets(
    c: CandidateChange, bundle: EvidenceBundle
) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    counter: list[str] = []

    evidence.append(
        f"{c.feature} ({c.kind}) changed in {c.environment} "
        f"{c.minutes_before_onset:.1f}m before onset (rules_score={c.rules_score:.2f})."
    )
    if c.features.code >= 0.7:
        evidence.append(f"Flag key '{c.feature}' correlates directly with failing code/errors.")
    else:
        counter.append(f"No direct stack-frame or error-message match for '{c.feature}'.")

    if c.features.scope >= 0.75:
        evidence.append(
            f"Affected traffic dimensions align with target scope ({c.segment_summary})."
        )
    else:
        counter.append(
            f"Target scope ({c.segment_summary}) only partially matches error dimensions."
        )

    if c.features.env == 0.0:
        counter.append(f"Change occurred in non-writable environment '{c.environment}'.")
    if not bundle.journal_complete:
        counter.append("Change journal has gaps during the incident window.")

    return [b[:300] for b in evidence[:3]], [b[:300] for b in counter[:3]]


def rank_with_rules(
    bundle: EvidenceBundle,
    *,
    confidence_floor: float = 0.6,
) -> RankingVerdict:
    """Rank candidates deterministically from evidence bundle without calling any LLM."""
    if not bundle.candidates:
        return RankingVerdict(
            verdict="not_flag_related",
            ranking=[],
            recommended=RecommendedAction(
                action="none",
                change_id=None,
                why="No Flagsmith changes recorded inside the incident window.",
            ),
            questions=["Check recent app deploys, DB health, or host fault switches."],
        )

    ranked: list[RankedSuspect] = []
    for cand in bundle.candidates[:5]:
        ev_bullets, ctr_bullets = _build_evidence_bullets(cand, bundle)
        ranked.append(
            RankedSuspect(
                change_id=cand.change_id,
                confidence=round(min(1.0, max(0.0, cand.rules_score)), 4),
                evidence=ev_bullets,
                counter_evidence=ctr_bullets,
            )
        )

    writable_candidates = [c for c in bundle.candidates if c.features.env > 0.0]
    if not writable_candidates:
        return RankingVerdict(
            verdict="not_flag_related",
            ranking=ranked,
            recommended=RecommendedAction(
                action="none",
                change_id=None,
                why="Changes occurred only in non-writable environments (e.g. staging).",
            ),
            questions=["Verify whether production flags drifted outside the journal."],
        )

    top = writable_candidates[0]
    if not bundle.journal_complete:
        return RankingVerdict(
            verdict="insufficient_evidence",
            ranking=ranked,
            recommended=RecommendedAction(
                action="none",
                change_id=None,
                why="Journal gaps detected during incident window; human review required.",
            ),
            questions=[
                "Confirm missing audit/webhook events before approving any Flagsmith change."
            ],
        )

    if top.rules_score < 0.35:
        return RankingVerdict(
            verdict="not_flag_related",
            ranking=ranked,
            recommended=RecommendedAction(
                action="none",
                change_id=None,
                why=(
                    f"Top change '{top.feature}' scored {top.rules_score:.2f}; "
                    "fault looks non-flag."
                ),
            ),
            questions=["Inspect /errors and infrastructure health outside Flagsmith."],
        )

    if top.rules_score < confidence_floor:
        return RankingVerdict(
            verdict="insufficient_evidence",
            ranking=ranked,
            recommended=RecommendedAction(
                action="none",
                change_id=None,
                why=(
                    f"Top confidence {top.rules_score:.2f} is below floor "
                    f"{confidence_floor:.2f}; asking before recommending."
                ),
            ),
            questions=[
                f"Should we inspect {top.feature} ({top.change_id}) or check non-flag faults?"
            ],
        )

    return RankingVerdict(
        verdict="flag_change",
        ranking=ranked,
        recommended=RecommendedAction(
            action="revert_change",
            change_id=top.change_id,
            why=(
                f"Revert {top.feature} ({top.change_id}) in {top.environment}; "
                f"rules confidence={top.rules_score:.2f}."
            )[:300],
        ),
        questions=[],
    )
