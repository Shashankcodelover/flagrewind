"""Frozen synthetic fault model for Acme Checkout (SYNTHETIC).

Each fault helper explicitly reads named Flagsmith flags so Python traceback frames
map directly back to flag keys when indexed by flagrewind.codeindex.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


class FlagReader(Protocol):
    """Minimal protocol satisfied by Flagsmith SDK Flags and test stubs."""

    def is_feature_enabled(self, feature_name: str) -> bool: ...

    def get_feature_value(self, feature_name: str) -> Any: ...


class VATCalculationError(RuntimeError):
    """Synthetic VAT calculation failure triggered by tax_engine_v2."""


class PaymentTimeoutError(TimeoutError):
    """Synthetic payment gateway timeout triggered by low payment_timeout_ms."""


class PaymentGatewayError(RuntimeError):
    """Synthetic payment failure triggered by broken_gateway multivariate variant."""


class DatabaseConnectionError(RuntimeError):
    """Synthetic non-flag infrastructure failure for Scenario 7."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Synthetic user request attributes evaluated against Flagsmith."""

    user_id: str
    country: str
    plan: str
    seq: int


def is_non_flag_fault_active(override: bool | None = None) -> bool:
    """Return True when the synthetic non-flag host fault switch is active."""
    if override is not None:
        return override
    return os.environ.get("FLAGREWIND_NON_FLAG_FAULT", "0").strip() == "1"


def apply_tax_engine(flags: FlagReader, ctx: RequestContext) -> dict[str, Any]:
    """Evaluate tax_engine_v2 and decoy flags during checkout (SYNTHETIC)."""
    _ = flags.is_feature_enabled("checkout_layout")
    _ = flags.is_feature_enabled("maintenance_banner")
    if (
        flags.is_feature_enabled("tax_engine_v2")
        and ctx.country in {"DE", "FR", "IT"}
        and (ctx.seq % 10) < 6
    ):
        # Scenarios 1 & 10: triggers on 60% of affected EU checkout traffic.
        raise VATCalculationError(
            f"VAT calculation failed under tax_engine_v2 for country={ctx.country}"
        )
    return {"tax_rate": 0.19 if ctx.country in {"DE", "FR", "IT"} else 0.08}


def check_payment_timeout(flags: FlagReader, ctx: RequestContext) -> int:
    """Evaluate payment_timeout_ms remote config (SYNTHETIC)."""
    raw_timeout = flags.get_feature_value("payment_timeout_ms")
    try:
        timeout_ms = int(raw_timeout) if raw_timeout is not None else 3000
    except (TypeError, ValueError):
        timeout_ms = 3000

    if timeout_ms < 500 and (ctx.seq % 10) < 3:
        raise PaymentTimeoutError(
            f"Payment gateway timed out at payment_timeout_ms={timeout_ms}ms"
        )
    return timeout_ms


def charge_payment_provider(
    flags: FlagReader,
    ctx: RequestContext,
    *,
    non_flag_fault: bool | None = None,
) -> str:
    """Evaluate payment_provider multivariate variant and non-flag fault switch (SYNTHETIC)."""
    if is_non_flag_fault_active(non_flag_fault) and (ctx.seq % 4) == 0:
        raise DatabaseConnectionError("Primary checkout database connection pool exhausted")

    provider = str(flags.get_feature_value("payment_provider") or "stripe_v1")
    if provider == "broken_gateway" and (ctx.seq % 10) < 8:
        raise PaymentGatewayError("Multivariate variant payment_provider=broken_gateway rejected")
    return provider


def render_profile(flags: FlagReader, ctx: RequestContext) -> dict[str, Any]:
    """Evaluate profile_v2 and export_csv/dark_mode decoys (SYNTHETIC)."""
    _ = flags.is_feature_enabled("export_csv")
    _ = flags.is_feature_enabled("dark_mode")
    if flags.is_feature_enabled("profile_v2") and ctx.plan == "free":
        profile_payload: dict[str, str] = {"plan": ctx.plan}
        # Deliberate KeyError on missing pro_badge field in profile_v2 for free users.
        return {"badge": profile_payload["pro_badge"], "plan": ctx.plan}
    return {"badge": "standard", "plan": ctx.plan}


def run_search_ranker(flags: FlagReader) -> float:
    """Evaluate search_ranker_v2 and return synthetic extra delay in seconds."""
    if flags.is_feature_enabled("search_ranker_v2"):
        return 0.4
    return 0.0
