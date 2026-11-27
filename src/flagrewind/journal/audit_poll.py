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
    """Polls Flagsmith audit log and optional env snapshots to record events & heartbeats."""

    def __init__(
        self,
        client: FlagsmithClient,
        store: JournalStore,
        *,
        project_id: int | None = None,
        organisation_id: int | None = None,
        default_env: str = "production",
        env_keys: dict[str, str] | None = None,
        env_ids: dict[str, int] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.project_id = project_id
        self.organisation_id = organisation_id
        self.default_env = default_env
        self.env_keys = env_keys or {}
        self.env_ids = env_ids or {}
        self._last_feature_states: dict[tuple[str, int], dict[str, Any]] = {}
        self._last_segment_rules: dict[int, str] = {}
        self._feature_names: dict[int, str] = {}
        self._segment_names: dict[int, str] = {}
        self._snapshot_seeded = False

    def _refresh_metadata(self) -> None:
        if self.project_id is None or (self._feature_names and self._segment_names):
            return
        for feat in self.client.list_features(self.project_id):
            if "id" in feat and "name" in feat:
                self._feature_names[int(feat["id"])] = str(feat["name"])
        for seg in self.client.list_segments(self.project_id):
            if "id" in seg and "name" in seg:
                self._segment_names[int(seg["id"])] = str(seg["name"])

    def _collect_state_snapshots(
        self,
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[int, str]]:
        states_out: dict[tuple[str, int], dict[str, Any]] = {}
        seg_rules_out: dict[int, str] = {}
        self._refresh_metadata()

        for env_name, env_key in self.env_keys.items():
            env_id = self.env_ids.get(env_name)
            fseg_to_segname: dict[int, str] = {}
            if env_id is not None:
                all_states = self.client.list_feature_all_states(env_id)
                fids_with_segments = {
                    int(st["feature"])
                    for st in all_states
                    if st.get("feature_segment") is not None and st.get("feature") is not None
                }
                for fid in fids_with_segments:
                    for fs in self.client.list_feature_segments(env_id, fid):
                        if "id" in fs:
                            sname = str(
                                fs.get("segment_name")
                                or self._segment_names.get(int(fs.get("segment", 0)), "segment")
                            )
                            fseg_to_segname[int(fs["id"])] = sname

                for st in all_states:
                    sid = int(st["id"])
                    fid = int(st["feature"])
                    fseg_id = st.get("feature_segment")
                    val = st.get("feature_state_value")
                    if isinstance(val, dict):
                        val = (
                            val.get("string_value")
                            if val.get("string_value") is not None
                            else (
                                val.get("integer_value")
                                if val.get("integer_value") is not None
                                else val.get("boolean_value")
                            )
                        )
                    mv = [
                        {
                            "id": m["id"],
                            "multivariate_feature_option": m["multivariate_feature_option"],
                            "percentage_allocation": float(m["percentage_allocation"]),
                        }
                        for m in (st.get("multivariate_feature_state_values") or [])
                        if isinstance(m, dict) and "multivariate_feature_option" in m
                    ]
                    seg_name = fseg_to_segname.get(int(fseg_id)) if fseg_id is not None else None
                    states_out[(env_name, sid)] = {
                        "state_id": sid,
                        "feature_id": fid,
                        "feature": self._feature_names.get(fid, f"feature:{fid}"),
                        "enabled": bool(st.get("enabled", False)),
                        "value": val,
                        "multivariate_values": mv or None,
                        "segment": seg_name,
                        "kind": (
                            "segment_override"
                            if seg_name is not None
                            else ("multivariate" if mv else "feature_state")
                        ),
                        "updated_at": st.get("updated_at"),
                    }
            else:
                for st in self.client.list_feature_states(env_key):
                    sid = int(st["id"])
                    raw_f = st.get("feature")
                    fid = int(raw_f["id"]) if isinstance(raw_f, dict) else int(raw_f)
                    fname = (
                        str(raw_f["name"])
                        if isinstance(raw_f, dict)
                        else self._feature_names.get(fid, f"feature:{fid}")
                    )
                    mv = [
                        {
                            "id": m["id"],
                            "multivariate_feature_option": m["multivariate_feature_option"],
                            "percentage_allocation": float(m["percentage_allocation"]),
                        }
                        for m in (st.get("multivariate_feature_state_values") or [])
                        if isinstance(m, dict) and "multivariate_feature_option" in m
                    ]
                    states_out[(env_name, sid)] = {
                        "state_id": sid,
                        "feature_id": fid,
                        "feature": fname,
                        "enabled": bool(st.get("enabled", False)),
                        "value": st.get("feature_state_value"),
                        "multivariate_values": mv or None,
                        "segment": None,
                        "kind": "multivariate" if mv else "feature_state",
                        "updated_at": st.get("updated_at"),
                    }

        if self.project_id is not None:
            for seg in self.client.list_segments(self.project_id):
                if "id" in seg:
                    seg_rules_out[int(seg["id"])] = str(seg.get("rules") or "")
        return states_out, seg_rules_out

    def seed_snapshot(self) -> None:
        """Capture baseline Flagsmith Cloud states before injecting a drill."""
        if not self.env_keys:
            return
        states, segs = self._collect_state_snapshots()
        self._last_feature_states = states
        self._last_segment_rules = segs
        self._snapshot_seeded = True

    def poll_state_diffs(self) -> list[JournalEvent]:
        """Detect state diffs when Flagsmith Free Cloud omits /audit/ history."""
        if not self.env_keys:
            return []
        if not self._snapshot_seeded:
            self.seed_snapshot()
            return []

        now_dt = datetime.now(UTC)
        cur_states, cur_segs = self._collect_state_snapshots()
        recorded: list[JournalEvent] = []

        for key, cur in cur_states.items():
            env_name, sid = key
            prev = self._last_feature_states.get(key)
            prev_snap = (
                {
                    "enabled": prev["enabled"],
                    "value": prev["value"],
                    "multivariate_values": prev["multivariate_values"],
                }
                if prev is not None
                else {"enabled": False, "value": None, "multivariate_values": None}
            )
            cur_snap = {
                "enabled": cur["enabled"],
                "value": cur["value"],
                "multivariate_values": cur["multivariate_values"],
            }
            if prev is None or cur_snap != prev_snap:
                occ = parse_iso_dt(cur["updated_at"] or now_dt)
                ev = self.store.append(
                    {
                        "source": "audit",
                        "occurred_at": occ,
                        "received_at": now_dt,
                        "environment": env_name,
                        "feature": cur["feature"],
                        "kind": cur["kind"],
                        "segment": cur["segment"],
                        "previous": prev_snap,
                        "new": cur_snap,
                        "actor": "flagsmith-snapshot-poller",
                        "raw_ref": {
                            "feature_state_id": sid,
                            "feature_id": cur["feature_id"],
                        },
                    },
                    dedupe_key=f"snapshot:{env_name}:{sid}:{cur.get('updated_at') or cur_snap}",
                )
                recorded.append(ev)

        for seg_id, cur_rule in cur_segs.items():
            prev_rule = self._last_segment_rules.get(seg_id)
            if prev_rule is not None and prev_rule != cur_rule:
                sname = self._segment_names.get(seg_id, f"segment:{seg_id}")
                ev = self.store.append(
                    {
                        "source": "audit",
                        "occurred_at": now_dt,
                        "received_at": now_dt,
                        "environment": self.default_env,
                        "feature": sname,
                        "kind": "segment_rule",
                        "segment": sname,
                        "previous": {"enabled": True, "value": prev_rule},
                        "new": {"enabled": True, "value": cur_rule},
                        "actor": "flagsmith-snapshot-poller",
                        "raw_ref": {"segment_id": seg_id},
                    },
                    dedupe_key=f"segrule:{seg_id}:{hash(cur_rule)}",
                )
                recorded.append(ev)

        self._last_feature_states = cur_states
        self._last_segment_rules = cur_segs
        return recorded

    def poll_once(self) -> list[JournalEvent]:
        """Fetch latest audit logs and append unseen records (plus env snapshot diffs)."""
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

        if not recorded and self.env_keys:
            recorded.extend(self.poll_state_diffs())

        self.store.record_heartbeat(source="audit", ok=True)
        return recorded
