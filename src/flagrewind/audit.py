"""Append-only redacted audit trail stored at .flagrewind/audit.jsonl."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_SECRET_PATTERNS = (
    re.compile(r"(Api-Key\s+)[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\b(ser\.[A-Za-z0-9._-]{6,})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_-]{12,})\b"),
    re.compile(r"\b(AQ\.[0-9A-Za-z._-]{12,})\b"),
)

_ENV_SECRET_VARS = (
    "FLAGREWIND_ADMIN_KEY",
    "FLAGREWIND_DRILL_KEY",
    "FLAGSMITH_DEMO_SERVER_KEY",
    "FLAGREWIND_WEBHOOK_SECRET",
    "LLM_API_KEY",
)


def redact_secrets(value: str) -> str:
    """Redact API keys, bearer tokens, and active env secrets before logging to disk."""
    redacted = value
    for idx, pattern in enumerate(_SECRET_PATTERNS):
        if idx < 2:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    for var in _ENV_SECRET_VARS:
        secret = os.environ.get(var, "").strip()
        if len(secret) >= 8 and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(item) for item in obj]
    return obj


class AuditEntry(BaseModel):
    """Single immutable audit trail entry."""

    timestamp: datetime
    event_type: str
    incident_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditTrail:
    """Append-only JSONL audit writer under .flagrewind/audit.jsonl."""

    def __init__(self, audit_path: Path) -> None:
        self.audit_path = audit_path

    def log(
        self,
        event_type: str,
        *,
        incident_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            event_type=event_type,
            incident_id=incident_id,
            details=_sanitize(details or {}),
        )
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")
        return entry

    def read_all(self) -> list[AuditEntry]:
        if not self.audit_path.exists():
            return []
        out: list[AuditEntry] = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(AuditEntry.model_validate_json(line))
        return out


def json_sha256(payload: Any) -> str:
    """Stable SHA-256 hex digest for prompt/response auditing."""
    import hashlib

    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
