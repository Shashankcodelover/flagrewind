"""Deterministic incident evidence bundle builder."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from flagrewind.codeindex import CodeIndex
from flagrewind.config import FlagRewindConfig
from flagrewind.journal.store import JournalEvent, JournalStore, parse_iso_dt
from flagrewind.symptoms.demo import SymptomSummary


class CandidateFeatures(BaseModel):
    """Deterministic normalized feature scores for a candidate journal change."""

    time: float = Field(ge=0.0, le=1.0)
    env: float = Field(ge=0.0, le=1.0)
    scope: float = Field(ge=0.0, le=1.0)
    code: float = Field(ge=0.0, le=1.0)
    magnitude: float = Field(ge=0.0, le=1.0)


class CandidateChange(BaseModel):
    """Candidate Flagsmith change scored deterministically inside the evidence bundle."""

    change_id: str
    minutes_before_onset: float
    environment: str
    feature: str
    kind: str
    segment_summary: str
    flag_description: str
    features: CandidateFeatures
    rules_score: float = Field(ge=0.0, le=1.0)


class EvidenceBundle(BaseModel):
    """Canonical saved evidence bundle for an incident."""

    incident_id: str
    onset: datetime
    symptom: dict[str, Any]
    error_samples: list[dict[str, Any]]
    candidates: list[CandidateChange]
    journal_complete: bool
    notes: list[str] = Field(default_factory=list)


def _compute_scope_score(event: JournalEvent, symptom: SymptomSummary) -> float:
    countries = symptom.by_dimension.country
    plans = symptom.by_dimension.plan
    total_err = sum(int(v.get("errors", 0)) for v in countries.values())
    if total_err == 0:
        return 0.5

    seg = (event.segment or "").lower()
    if "eu" in seg or event.feature == "tax_engine_v2":
        eu_err = sum(int(countries.get(cc, {}).get("errors", 0)) for cc in ("DE", "FR", "IT"))
        ratio = eu_err / total_err
        return round(min(1.0, max(0.05, ratio)), 3)

    if "free" in seg or event.feature == "profile_v2":
        free_err = int(plans.get("free", {}).get("errors", 0))
        plan_err_total = sum(int(v.get("errors", 0)) for v in plans.values()) or total_err
        ratio = free_err / plan_err_total
        return round(min(1.0, max(0.05, ratio)), 3)

    if event.kind == "segment_rule":
        eu_err = sum(int(countries.get(cc, {}).get("errors", 0)) for cc in ("DE", "FR", "IT"))
        return round(min(1.0, max(0.1, eu_err / total_err)), 3)

    return 0.75


def _compute_magnitude_score(event: JournalEvent) -> float:
    if event.kind == "multivariate":
        return 0.9
    if event.kind == "segment_rule":
        return 0.85
    if event.new is None:
        return 0.4

    prev = event.previous
    if prev is not None and not prev.enabled and event.new.enabled:
        return 1.0
    if prev is not None and prev.enabled and not event.new.enabled:
        return 0.15

    # Remote config numeric drops (e.g. payment_timeout_ms 3000 -> 300)
    if prev is not None and prev.value is not None and event.new.value is not None:
        try:
            old_num = float(prev.value)
            new_num = float(event.new.value)
            if old_num != new_num:
                ratio = abs(new_num - old_num) / max(1.0, abs(old_num))
                return round(min(1.0, max(0.3, ratio)), 3)
        except (TypeError, ValueError):
            if str(prev.value) != str(event.new.value):
                return 0.8
    return 0.7 if event.new.enabled else 0.25


def _compute_code_score(
    event: JournalEvent, symptom: SymptomSummary, code_index: CodeIndex
) -> float:
    samples = symptom.error_samples
    if not samples:
        return 0.0

    # Direct flag name in error messages gives highest deterministic confidence.
    for sample in samples:
        msg = str(sample.get("message", "")).lower()
        if event.feature.lower() in msg:
            return 1.0

    if code_index.matches_error_frames(event.feature, samples):
        return 0.75
    return 0.0


def _score_candidate(
    event: JournalEvent,
    *,
    symptom: SymptomSummary,
    writable_envs: set[str],
    code_index: CodeIndex,
    feature_descriptions: dict[str, str],
) -> CandidateChange:
    onset = parse_iso_dt(symptom.onset)
    occurred = parse_iso_dt(event.occurred_at)
    minutes_before = round((onset - occurred).total_seconds() / 60.0, 2)

    time_score = round(max(0.05, min(1.0, 1.0 - (abs(minutes_before) / 60.0))), 3)
    env_score = 1.0 if event.environment.lower() in writable_envs else 0.0
    scope_score = _compute_scope_score(event, symptom)
    code_score = _compute_code_score(event, symptom, code_index)
    magnitude_score = _compute_magnitude_score(event)

    # Keep non-writable envs (e.g. staging) at 0.0 so they never propose prod writes.
    if env_score == 0.0:
        rules_score = 0.0
    else:
        raw = (
            0.35 * code_score
            + 0.25 * scope_score
            + 0.25 * magnitude_score
            + 0.15 * time_score
        )
        rules_score = round(min(1.0, max(0.0, raw)), 4)

    seg_summary = event.segment or "all users"
    desc = feature_descriptions.get(event.feature, "")[:200]
    return CandidateChange(
        change_id=event.id,
        minutes_before_onset=minutes_before,
        environment=event.environment,
        feature=event.feature,
        kind=event.kind,
        segment_summary=seg_summary,
        flag_description=desc,
        features=CandidateFeatures(
            time=time_score,
            env=env_score,
            scope=scope_score,
            code=code_score,
            magnitude=magnitude_score,
        ),
        rules_score=rules_score,
    )


def build_evidence_bundle(
    *,
    incident_id: str,
    config: FlagRewindConfig,
    store: JournalStore,
    symptom: SymptomSummary,
    code_index: CodeIndex,
    feature_descriptions: dict[str, str] | None = None,
    extra_notes: list[str] | None = None,
) -> EvidenceBundle:
    """Build deterministic EvidenceBundle across [onset - 60m, onset + sdk_refresh_seconds]."""
    onset = parse_iso_dt(symptom.onset)
    win_start = onset - timedelta(minutes=60)
    win_end = onset + timedelta(seconds=config.policy.sdk_refresh_seconds)

    events = store.events_in_window(win_start, win_end)
    journal_ok, gap_notes = store.check_completeness(
        win_start,
        win_end,
        expected_poll_seconds=config.journal.audit_poll_seconds,
    )
    notes = [*gap_notes, *(extra_notes or [])]

    writable_envs = {name.lower() for name in config.writable_environments()}
    descriptions = feature_descriptions or {}
    candidates = [
        _score_candidate(
            ev,
            symptom=symptom,
            writable_envs=writable_envs,
            code_index=code_index,
            feature_descriptions=descriptions,
        )
        for ev in events
    ]
    candidates.sort(key=lambda c: (-c.rules_score, abs(c.minutes_before_onset)))

    return EvidenceBundle(
        incident_id=incident_id,
        onset=onset,
        symptom={
            "metric": symptom.metric,
            "baseline": symptom.baseline,
            "current": symptom.current,
            "by_dimension": symptom.by_dimension.model_dump(),
        },
        error_samples=symptom.error_samples[:20],
        candidates=candidates,
        journal_complete=journal_ok,
        notes=notes,
    )
