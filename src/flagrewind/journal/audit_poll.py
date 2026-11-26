"""Flagsmith Admin audit-log poller (catches segment rules and missed webhooks)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from flagrewind.flagsmith_client import FlagsmithClient, FlagsmithError
from flagrewind.journal.store import JournalEvent, JournalStore, parse_iso_dt

_SEGMENT_PATTERN = re.compile(r"Segment(?:\s+rules)?\s+(?:updated|created).*?:\s*([A-Za-z0-9_-]+)")
_FEATURE_PATTERN = re.compile(r":\s*([A-Za-z0-9_.-]+)")


def _classify_audit_entry(entry: dict[str, Any]) -> tuple[str, str, str | None]:
    obj_type = str(entry.get("related_object_type") or "").upper()
    log_text = str(entry.get("log") or "")
    if obj_type == "SEGMENT":
        m = _SEGMENT_PATTERN.search(log_text) or _FEATURE_PATTERN.search(log_text)
        seg = m.group(1) if m else "unknown-segment"
        return "segment_rule", seg, seg
    if obj_type in {"FEATURE", "FEATURE_STATE"}:
        m = _FEATURE_PATTERN.search(log_text)
        feat = m.group(1) if m else f"object:{entry.get('related_object_id', 'unknown')}"
        return "feature_state", feat, None
    return "other", f"audit:{obj_type.lower() or 'event'}", None


def parse_audit_entry_to_event_dict(
    entry: dict[str, Any],
    *,
    default_env: str = "production",
    received_at: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Convert one Flagsmith /api/v1/audit/ record into a normalized journal dict."""
    audit_id = entry.get("id")
    occurred_dt = parse_iso_dt(entry.get("created_date") or datetime.now(UTC))
    recv_dt = parse_iso_dt(received_at or datetime.now(UTC))

    env_obj = entry.get("environment")
    env_name = (
        str(env_obj.get("name")).lower()
        if isinstance(env_obj, dict) and env_obj.get("name")
        else default_env
    )
    author = entry.get("author")
    actor: str | None = None
    if isinstance(author, dict):
        actor = author.get("email") or author.get("first_name")

    kind, feature_name, segment_name = _classify_audit_entry(entry)
    dedupe_key = (
        f"audit:{audit_id}"
        if audit_id is not None
        else f"audit:{occurred_dt.isoformat()}:{feature_name}"
    )

    event_dict: dict[str, Any] = {
        "source": "audit",
        "occurred_at": occurred_dt,
        "received_at": recv_dt,
        "environment": env_name,
        "feature": feature_name,
        "kind": kind,
        "segment": segment_name,
        "previous": None,
        "new": None,
        "actor": actor,
        "raw_ref": {
            "audit_id": audit_id,
            "related_object_type": entry.get("related_object_type"),
            "related_object_id": entry.get("related_object_id"),
            "log": str(entry.get("log") or "")[:300],
        },
    }
    return event_dict, dedupe_key


class AuditPoller:
    """Polls Flagsmith audit log and records new events + completeness heartbeats."""

    def __init__(
        self,
        client: FlagsmithClient,
        store: JournalStore,
        *,
        project_id: int | None = None,
        organisation_id: int | None = None,
        default_env: str = "production",
    ) -> None:
        self.client = client
        self.store = store
        self.project_id = project_id
        self.organisation_id = organisation_id
        self.default_env = default_env

    def poll_once(self) -> list[JournalEvent]:
        """Fetch latest audit logs and append unseen records."""
        try:
            entries = self.client.list_audit_logs(
                project_id=self.project_id,
                organisation_id=self.organisation_id,
            )
        except FlagsmithError:
            self.store.record_heartbeat(source="audit", ok=False)
            raise

        recorded: list[JournalEvent] = []
        for entry in entries:
            obj_type = str(entry.get("related_object_type") or "").upper()
            if obj_type not in {"SEGMENT", "FEATURE", "FEATURE_STATE", "ENVIRONMENT"}:
                continue
            event_dict, dedupe_key = parse_audit_entry_to_event_dict(
                entry, default_env=self.default_env
            )
            recorded.append(self.store.append(event_dict, dedupe_key=dedupe_key))

        self.store.record_heartbeat(source="audit", ok=True)
        return recorded
