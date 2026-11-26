"""Generic non-fatal JSON webhook notifier for incident briefs and mitigations."""

from __future__ import annotations

from typing import Any

import httpx


def send_notify_webhook(
    notify_url: str | None,
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """POST generic JSON status payload to notify_url without raising on failure."""
    if not notify_url or not notify_url.strip():
        return False
    owns_client = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        resp = http.post(notify_url.strip(), json=payload)
        return resp.status_code < 400
    except httpx.HTTPError:
        return False
    finally:
        if owns_client:
            http.close()
