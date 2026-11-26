"""Append-only JSONL change journal store and gap detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class FlagStateSnapshot(BaseModel):
    """Normalized representation of a Flagsmith state before or after a change."""

    enabled: bool
    value: Any = None
    multivariate_values: list[dict[str, Any]] | None = None


class JournalEvent(BaseModel):
    """Canonical FlagRewind journal event stored per line in journal.jsonl."""

    id: str = Field(pattern=r"^CHG-\d{6}$")
    source: Literal["webhook", "audit"]
    occurred_at: datetime
    received_at: datetime
    environment: str
    feature: str
    kind: Literal["feature_state", "segment_override", "segment_rule", "multivariate", "other"]
    segment: str | None = None
    previous: FlagStateSnapshot | None = None
    new: FlagStateSnapshot | None = None
    actor: str | None = None
    raw_ref: dict[str, Any] = Field(default_factory=dict)


def parse_iso_dt(value: str | datetime) -> datetime:
    """Parse ISO-8601 timestamp into timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class JournalStore:
    """JSONL-backed append-only change journal with deduplication and gap checks."""

    def __init__(self, journal_path: Path, heartbeat_path: Path | None = None) -> None:
        self.journal_path = journal_path
        self.heartbeat_path = heartbeat_path or journal_path.with_name("journal_heartbeat.json")

    def read_all(self) -> list[JournalEvent]:
        """Load all recorded journal events in chronological write order."""
        if not self.journal_path.exists():
            return []
        events: list[JournalEvent] = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(JournalEvent.model_validate_json(line))
        return events

    def next_id(self, existing: list[JournalEvent] | None = None) -> str:
        """Compute next monotonic CHG-XXXXXX identifier."""
        items = existing if existing is not None else self.read_all()
        if not items:
            return "CHG-000001"
        max_num = max(int(item.id.split("-")[1]) for item in items)
        return f"CHG-{max_num + 1:06d}"

    def append(
        self, event_without_id: dict[str, Any], *, dedupe_key: str | None = None
    ) -> JournalEvent:
        """Append a new journal event unless an event with dedupe_key already exists."""
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read_all()
        if dedupe_key is not None:
            for item in existing:
                if item.raw_ref.get("dedupe_key") == dedupe_key:
                    return item

        payload = dict(event_without_id)
        payload["id"] = self.next_id(existing)
        if dedupe_key is not None:
            raw_ref = dict(payload.get("raw_ref") or {})
            raw_ref["dedupe_key"] = dedupe_key
            payload["raw_ref"] = raw_ref

        event = JournalEvent.model_validate(payload)
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        return event

    def events_in_window(self, start: datetime, end: datetime) -> list[JournalEvent]:
        """Return events whose occurred_at falls inside [start, end]."""
        start_utc = parse_iso_dt(start)
        end_utc = parse_iso_dt(end)
        return [
            ev
            for ev in self.read_all()
            if start_utc <= parse_iso_dt(ev.occurred_at) <= end_utc
        ]

    def record_heartbeat(
        self, *, source: str, ok: bool = True, now: datetime | None = None
    ) -> None:
        """Record a successful journal/poller tick for completeness checks."""
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        ts = parse_iso_dt(now or datetime.now(UTC)).isoformat()
        state: dict[str, Any] = {}
        if self.heartbeat_path.exists():
            try:
                state = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
        ticks = list(state.get("ticks", []))
        ticks.append({"ts": ts, "source": source, "ok": ok})
        # Keep trailing 500 ticks to bound file growth.
        state["ticks"] = ticks[-500:]
        state["last_ok_ts"] = ts if ok else state.get("last_ok_ts")
        self.heartbeat_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def check_completeness(
        self,
        start: datetime,
        end: datetime,
        *,
        expected_poll_seconds: int = 15,
    ) -> tuple[bool, list[str]]:
        """Verify whether journal coverage exists across [start, end] without long gaps."""
        notes: list[str] = []
        if not self.journal_path.exists() and not self.heartbeat_path.exists():
            return False, ["Journal has no recorded events or heartbeats."]

        if not self.heartbeat_path.exists():
            # Events exist without explicit heartbeat; mark complete unless explicitly broken.
            return True, notes

        try:
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False, ["Journal heartbeat file is corrupted."]

        ticks = [
            parse_iso_dt(t["ts"])
            for t in data.get("ticks", [])
            if t.get("ok") and "ts" in t
        ]
        if not ticks:
            return False, ["No healthy journal heartbeats recorded."]

        start_utc = parse_iso_dt(start)
        end_utc = parse_iso_dt(end)
        max_gap = timedelta(seconds=max(60, expected_poll_seconds * 4))
        window_ticks = sorted(t for t in ticks if start_utc - max_gap <= t <= end_utc + max_gap)
        if not window_ticks:
            return False, ["No journal heartbeats overlap the incident investigation window."]

        for prev, curr in zip(window_ticks, window_ticks[1:], strict=False):
            if curr - prev > max_gap:
                gap_s = int((curr - prev).total_seconds())
                notes.append(
                    f"Journal gap of {gap_s}s between {prev.isoformat()} and {curr.isoformat()}."
                )

        if window_ticks[-1] + max_gap < end_utc:
            notes.append("Journal heartbeat ended before incident onset window closed.")

        return len(notes) == 0, notes
