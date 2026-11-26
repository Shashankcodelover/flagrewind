"""FastAPI synthetic demo service ('Acme Checkout') on port 8001."""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from flagsmith import Flagsmith
from flagsmith.models import DefaultFlag

from demo_service.faults import (
    FlagReader,
    RequestContext,
    apply_tax_engine,
    charge_payment_provider,
    check_payment_timeout,
    render_profile,
    run_search_ranker,
)


class StaticDefaultFlags:
    """Fallback flag evaluator when demo_service starts without a live Flagsmith key."""

    def is_feature_enabled(self, feature_name: str) -> bool:
        return feature_name in {"payment_timeout_ms", "payment_provider"}

    def get_feature_value(self, feature_name: str) -> Any:
        defaults: dict[str, Any] = {
            "payment_timeout_ms": 3000,
            "payment_provider": "stripe_v1",
        }
        return defaults.get(feature_name)


@dataclass(slots=True)
class WindowBucket:
    """10-second rolling metric bucket broken down by endpoint, country, and plan."""

    bucket_ts: int
    total: int = 0
    errors: int = 0
    by_endpoint: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"total": 0, "errors": 0})
    )
    by_country: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"total": 0, "errors": 0})
    )
    by_plan: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"total": 0, "errors": 0})
    )


class MetricsStore:
    """Thread-safe in-memory 10-second bucket store + 50-sample error ring buffer."""

    def __init__(self, *, max_buckets: int = 120, max_errors: int = 50) -> None:
        self._buckets: dict[int, WindowBucket] = {}
        self._max_buckets = max_buckets
        self._errors: deque[dict[str, Any]] = deque(maxlen=max_errors)
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(
        self,
        *,
        endpoint: str,
        country: str,
        plan: str,
        exc: BaseException | None = None,
        now: float | None = None,
    ) -> None:
        ts = int(now if now is not None else time.time())
        bucket_ts = ts - (ts % 10)
        bucket = self._buckets.get(bucket_ts)
        if bucket is None:
            bucket = WindowBucket(bucket_ts=bucket_ts)
            self._buckets[bucket_ts] = bucket
            self._prune_old_buckets()

        is_err = 1 if exc is not None else 0
        bucket.total += 1
        bucket.errors += is_err
        for group, key in (
            (bucket.by_endpoint, endpoint),
            (bucket.by_country, country),
            (bucket.by_plan, plan),
        ):
            group[key]["total"] += 1
            group[key]["errors"] += is_err

        if exc is not None:
            tb = traceback.extract_tb(exc.__traceback__)
            frames = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in tb[-6:]
            ]
            self._errors.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": type(exc).__name__,
                    "message": str(exc)[:300],
                    "endpoint": endpoint,
                    "country": country,
                    "plan": plan,
                    "frames": frames,
                }
            )

    def _prune_old_buckets(self) -> None:
        if len(self._buckets) <= self._max_buckets:
            return
        for key in sorted(self._buckets)[: len(self._buckets) - self._max_buckets]:
            del self._buckets[key]

    def metrics_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        ts = int(now if now is not None else time.time())
        cutoff = ts - 300
        windows = []
        for bucket_ts in sorted(k for k in self._buckets if k >= cutoff):
            b = self._buckets[bucket_ts]
            windows.append(
                {
                    "window_start": datetime.fromtimestamp(b.bucket_ts, tz=UTC).isoformat(),
                    "epoch_s": b.bucket_ts,
                    "total": b.total,
                    "errors": b.errors,
                    "error_rate": round(b.errors / b.total, 4) if b.total else 0.0,
                    "by_endpoint": dict(b.by_endpoint),
                    "by_country": dict(b.by_country),
                    "by_plan": dict(b.by_plan),
                }
            )
        return {
            "synthetic": True,
            "service": "acme-checkout",
            "window_seconds": 10,
            "windows": windows,
        }

    def errors_snapshot(self) -> dict[str, Any]:
        return {"synthetic": True, "service": "acme-checkout", "errors": list(self._errors)}


def _build_flagsmith_client() -> Flagsmith | None:
    server_key = os.environ.get("FLAGSMITH_DEMO_SERVER_KEY", "").strip()
    if not server_key or server_key.startswith("replace-"):
        return None
    api_url = os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/").strip()
    return Flagsmith(
        environment_key=server_key,
        api_url=api_url,
        enable_local_evaluation=True,
        environment_refresh_interval_seconds=5,
        default_flag_handler=lambda _: DefaultFlag(enabled=False, value=None),
    )


def create_app(
    *,
    flagsmith_client: Flagsmith | None = None,
    fallback_flags: FlagReader | None = None,
    latency_scale: float = 1.0,
    non_flag_fault: bool | None = None,
) -> FastAPI:
    """Build the FastAPI Acme Checkout demo application."""
    app = FastAPI(title="Acme Checkout (SYNTHETIC)", version="0.1.0")
    fs_client = flagsmith_client if flagsmith_client is not None else _build_flagsmith_client()
    store = MetricsStore()
    default_reader = fallback_flags or StaticDefaultFlags()

    def _eval_flags(user_id: str, country: str, plan: str) -> FlagReader:
        if fs_client is None:
            return default_reader
        return fs_client.get_identity_flags(
            identifier=user_id,
            traits={"country": country, "plan": plan},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "synthetic": True,
            "flagsmith_local_eval": fs_client is not None,
        }

    @app.get("/checkout")
    def checkout(
        country: str = Query(default="US"),
        plan: str = Query(default="free"),
        user_id: str = Query(default="user_1"),
    ) -> dict[str, Any]:
        ctx = RequestContext(user_id=user_id, country=country, plan=plan, seq=store.next_seq())
        flags = _eval_flags(user_id, country, plan)
        try:
            tax = apply_tax_engine(flags, ctx)
            timeout_ms = check_payment_timeout(flags, ctx)
            provider = charge_payment_provider(flags, ctx, non_flag_fault=non_flag_fault)
        except Exception as exc:
            store.record(endpoint="/checkout", country=country, plan=plan, exc=exc)
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        store.record(endpoint="/checkout", country=country, plan=plan)
        return {
            "status": "ok",
            "synthetic": True,
            "provider": provider,
            "timeout_ms": timeout_ms,
            **tax,
        }

    @app.get("/search")
    async def search(
        q: str = Query(default="shoes"),
        country: str = Query(default="US"),
        plan: str = Query(default="free"),
        user_id: str = Query(default="user_1"),
    ) -> dict[str, Any]:
        flags = _eval_flags(user_id, country, plan)
        delay_s = run_search_ranker(flags) * latency_scale
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        store.record(endpoint="/search", country=country, plan=plan)
        return {"status": "ok", "synthetic": True, "query": q, "extra_delay_s": delay_s}

    @app.get("/profile")
    def profile(
        country: str = Query(default="US"),
        plan: str = Query(default="free"),
        user_id: str = Query(default="user_1"),
    ) -> dict[str, Any]:
        ctx = RequestContext(user_id=user_id, country=country, plan=plan, seq=store.next_seq())
        flags = _eval_flags(user_id, country, plan)
        try:
            rendered = render_profile(flags, ctx)
        except Exception as exc:
            store.record(endpoint="/profile", country=country, plan=plan, exc=exc)
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        store.record(endpoint="/profile", country=country, plan=plan)
        return {"status": "ok", "synthetic": True, **rendered}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return store.metrics_snapshot()

    @app.get("/errors")
    def errors() -> dict[str, Any]:
        return store.errors_snapshot()

    return app


app = create_app()
