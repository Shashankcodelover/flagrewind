"""Flagsmith environment webhook receiver with HMAC-SHA256 verification."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from flagrewind.journal.store import FlagStateSnapshot, JournalEvent, JournalStore, parse_iso_dt


def verify_flagsmith_signature(
    raw_body: bytes,
    signature_header: str | None,
    secret: str,
) -> bool:
    """Verify Flagsmith X-Flagsmith-Signature hex HMAC-SHA256 over raw body."""
    if not secret:
        return True
    if not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    return hmac.compare_digest(expected.lower(), provided.lower())


def _extract_snapshot(state: dict[str, Any] | None) -> FlagStateSnapshot | None:
    if not isinstance(state, dict) or not state:
        return None
    enabled = bool(state.get("enabled", False))
    value = state.get("feature_state_value")
    mv_values = state.get("multivariate_feature_state_values")
    return FlagStateSnapshot(
        enabled=enabled,
        value=value,
        multivariate_values=mv_values if isinstance(mv_values, list) and mv_values else None,
    )


def _infer_kind(
    new_state: dict[str, Any], prev_state: dict[str, Any] | None
) -> tuple[str, str | None]:
    state = new_state or prev_state or {}
    segment = state.get("feature_segment")
    seg_name: str | None = None
    if isinstance(segment, dict):
        raw_seg = segment.get("segment")
        if isinstance(raw_seg, dict) and raw_seg.get("name"):
            seg_name = str(raw_seg["name"])
        elif segment.get("segment_name"):
            seg_name = str(segment["segment_name"])
        elif segment.get("segment") is not None:
            seg_name = f"segment:{segment['segment']}"
        return "segment_override", seg_name

    mv = state.get("multivariate_feature_state_values")
    if isinstance(mv, list) and mv:
        return "multivariate", None

    return "feature_state", None


def parse_webhook_to_event_dict(
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Normalize a Flagsmith environment webhook JSON body into a journal event dict."""
    event_type = str(payload.get("event_type", "FLAG_UPDATED"))
    data = payload.get("data") or {}
    new_state = data.get("new_state") or {}
    prev_state = data.get("previous_state")
    ref_state = new_state if new_state else (prev_state or {})

    env_info = ref_state.get("environment") or {}
    feat_info = ref_state.get("feature") or {}
    env_name = str(env_info.get("name") or "unknown").lower()
    feature_name = str(feat_info.get("name") or "unknown")
    kind, segment_name = _infer_kind(new_state, prev_state)

    occurred_raw = (
        payload.get("timestamp")
        or new_state.get("updated_at")
        or new_state.get("created_at")
        or received_at
        or datetime.now(UTC)
    )
    occurred_dt = parse_iso_dt(occurred_raw)
    recv_dt = parse_iso_dt(received_at or datetime.now(UTC))

    prev_snap = _extract_snapshot(prev_state)
    new_snap = _extract_snapshot(new_state)
    state_id = ref_state.get("id")
    dedupe_key = (
        f"wh:{event_type}:{env_name}:{feature_name}:{state_id}:{occurred_dt.isoformat()}"
    )

    event_dict: dict[str, Any] = {
        "source": "webhook",
        "occurred_at": occurred_dt,
        "received_at": recv_dt,
        "environment": env_name,
        "feature": feature_name,
        "kind": kind,
        "segment": segment_name,
        "previous": prev_snap.model_dump() if prev_snap else None,
        "new": new_snap.model_dump() if new_snap else None,
        "actor": data.get("changed_by"),
        "raw_ref": {
            "event_type": event_type,
            "feature_state_id": state_id,
            "feature_id": feat_info.get("id"),
            "environment_id": env_info.get("id"),
            "feature_type": feat_info.get("type"),
            "segment_id": (
                ref_state.get("feature_segment", {}).get("segment")
                if isinstance(ref_state.get("feature_segment"), dict)
                else None
            ),
        },
    }
    return event_dict, dedupe_key


def create_webhook_app(store: JournalStore, secret: str) -> FastAPI:
    """Create FastAPI receiver listening for Flagsmith environment webhooks."""
    app = FastAPI(title="FlagRewind Webhook Receiver", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/flagsmith")
    async def receive_flagsmith_webhook(
        request: Request,
        x_flagsmith_signature: str | None = Header(default=None),
    ) -> dict[str, Any]:
        raw_body = await request.body()
        if not verify_flagsmith_signature(raw_body, x_flagsmith_signature, secret):
            raise HTTPException(status_code=401, detail="Invalid X-Flagsmith-Signature")
        payload = await request.json()
        event_dict, dedupe_key = parse_webhook_to_event_dict(payload)
        saved: JournalEvent = store.append(event_dict, dedupe_key=dedupe_key)
        store.record_heartbeat(source="webhook", ok=True)
        return {"status": "accepted", "id": saved.id, "feature": saved.feature}

    return app
