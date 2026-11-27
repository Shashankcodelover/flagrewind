"""Demo service ('Acme Checkout') symptom adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from flagrewind.config import SymptomsConfig
from flagrewind.journal.store import parse_iso_dt


class SymptomError(RuntimeError):
    """Raised when the symptom endpoint is unreachable or invalid."""


class DimensionBreakdown(BaseModel):
    """Aggregated traffic and error metrics across key dimensions."""

    country: dict[str, dict[str, float | int]] = Field(default_factory=dict)
    plan: dict[str, dict[str, float | int]] = Field(default_factory=dict)
    endpoint: dict[str, dict[str, float | int]] = Field(default_factory=dict)


class SymptomSummary(BaseModel):
    """Normalized symptom snapshot extracted from /metrics and /errors."""

    onset: datetime
    breached: bool
    metric: str = "error_rate"
    baseline: float
    current: float
    by_dimension: DimensionBreakdown
    error_samples: list[dict[str, Any]] = Field(default_factory=list)


def _merge_dim(
    target: dict[str, dict[str, float | int]],
    source: dict[str, dict[str, int]],
) -> None:
    for key, counts in source.items():
        bucket = target.setdefault(key, {"total": 0, "errors": 0, "error_rate": 0.0})
        bucket["total"] = int(bucket["total"]) + int(counts.get("total", 0))
        bucket["errors"] = int(bucket["errors"]) + int(counts.get("errors", 0))
        tot = int(bucket["total"])
        bucket["error_rate"] = round(int(bucket["errors"]) / tot, 4) if tot else 0.0


def analyze_metrics_and_errors(
    metrics_payload: dict[str, Any],
    errors_payload: dict[str, Any],
    *,
    threshold: float,
    baseline_window_s: int,
    detect_window_s: int,
    now: datetime | None = None,
) -> SymptomSummary:
    """Compute onset, baseline/current error rates, breakdowns, and <=20 error samples."""
    windows = sorted(metrics_payload.get("windows", []), key=lambda w: w.get("epoch_s", 0))
    now_dt = parse_iso_dt(now or datetime.now(UTC))

    if not windows:
        return SymptomSummary(
            onset=now_dt,
            breached=False,
            baseline=0.0,
            current=0.0,
            by_dimension=DimensionBreakdown(),
            error_samples=[],
        )

    detect_count = max(1, detect_window_s // 10)
    detect_windows = windows[-detect_count:]
    cur_total = sum(int(w.get("total", 0)) for w in detect_windows)
    cur_err = sum(int(w.get("errors", 0)) for w in detect_windows)
    current_rate = round(cur_err / cur_total, 4) if cur_total else 0.0

    # Find start of the most recent continuous breach above threshold.
    breach_start_idx: int | None = None
    for idx in range(len(windows) - 1, -1, -1):
        w = windows[idx]
        if int(w.get("total", 0)) > 0 and float(w.get("error_rate", 0.0)) >= threshold:
            breach_start_idx = idx
        elif breach_start_idx is not None:
            break

    breached = breach_start_idx is not None and current_rate >= threshold
    if breach_start_idx is not None:
        onset_dt = parse_iso_dt(windows[breach_start_idx]["window_start"])
        pre_windows = windows[:breach_start_idx]
        dim_windows = windows[breach_start_idx:]
    else:
        onset_dt = parse_iso_dt(windows[-1]["window_start"])
        pre_windows = windows[:-detect_count]
        dim_windows = detect_windows

    baseline_count = max(1, baseline_window_s // 10)
    base_slice = pre_windows[-baseline_count:] if pre_windows else []
    base_total = sum(int(w.get("total", 0)) for w in base_slice)
    base_err = sum(int(w.get("errors", 0)) for w in base_slice)
    baseline_rate = round(base_err / base_total, 4) if base_total else 0.0

    by_country: dict[str, dict[str, float | int]] = {}
    by_plan: dict[str, dict[str, float | int]] = {}
    by_endpoint: dict[str, dict[str, float | int]] = {}
    for w in dim_windows:
        _merge_dim(by_country, w.get("by_country", {}))
        _merge_dim(by_plan, w.get("by_plan", {}))
        _merge_dim(by_endpoint, w.get("by_endpoint", {}))

    raw_errors = errors_payload.get("errors", [])
    trimmed_errors = [
        {
            "type": str(err.get("type", "Error"))[:100],
            "message": str(err.get("message", ""))[:300],
            "frames": list(err.get("frames", []))[:6],
        }
        for err in raw_errors[-20:]
        if isinstance(err, dict)
    ]

    return SymptomSummary(
        onset=onset_dt,
        breached=breached,
        baseline=baseline_rate,
        current=current_rate,
        by_dimension=DimensionBreakdown(
            country=by_country,
            plan=by_plan,
            endpoint=by_endpoint,
        ),
        error_samples=trimmed_errors,
    )


class DemoSymptomReader:
    """HTTP reader for demo_service /metrics and /errors endpoints."""

    def __init__(self, cfg: SymptomsConfig, *, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.base_url = cfg.demo_url.rstrip("/")
        self._owns_client = client is None
        is_local = "localhost" in self.base_url or "127.0.0.1" in self.base_url
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=10.0,
            trust_env=not is_local,
        )

    def __enter__(self) -> DemoSymptomReader:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self) -> SymptomSummary:
        """Fetch /metrics and /errors and return normalized SymptomSummary."""
        try:
            m_resp = self._client.get("/metrics")
            e_resp = self._client.get("/errors")
            m_resp.raise_for_status()
            e_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SymptomError(f"Symptom source unreachable at {self.base_url}: {exc}") from exc

        return analyze_metrics_and_errors(
            m_resp.json(),
            e_resp.json(),
            threshold=self.cfg.error_rate_threshold,
            baseline_window_s=self.cfg.baseline_window_s,
            detect_window_s=self.cfg.detect_window_s,
        )
