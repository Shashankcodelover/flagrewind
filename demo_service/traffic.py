"""Seedable synthetic traffic generator for Acme Checkout (SYNTHETIC)."""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass

import httpx

ENDPOINT_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("/checkout", 0.50),
    ("/search", 0.30),
    ("/profile", 0.20),
)
COUNTRY_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("US", 0.45),
    ("GB", 0.20),
    ("DE", 0.15),
    ("FR", 0.15),
    ("IT", 0.05),
)
PLAN_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("free", 0.50),
    ("pro", 0.35),
    ("enterprise", 0.15),
)


@dataclass(frozen=True, slots=True)
class SyntheticRequest:
    """Single deterministic synthetic traffic sample."""

    endpoint: str
    country: str
    plan: str
    user_id: str


def _weighted_pick(rng: random.Random, choices: tuple[tuple[str, float], ...]) -> str:
    names = [item[0] for item in choices]
    weights = [item[1] for item in choices]
    return rng.choices(names, weights=weights, k=1)[0]


def generate_requests(seed: int, count: int) -> list[SyntheticRequest]:
    """Generate a reproducible batch of synthetic user requests."""
    rng = random.Random(seed)
    items: list[SyntheticRequest] = []
    for idx in range(count):
        items.append(
            SyntheticRequest(
                endpoint=_weighted_pick(rng, ENDPOINT_WEIGHTS),
                country=_weighted_pick(rng, COUNTRY_WEIGHTS),
                plan=_weighted_pick(rng, PLAN_WEIGHTS),
                user_id=f"syn_user_{(idx % 500) + 1}",
            )
        )
    return items


def run_traffic(
    base_url: str,
    *,
    rate_rps: float = 20.0,
    duration_s: float = 60.0,
    seed: int = 42,
    client: httpx.Client | None = None,
) -> dict[str, int]:
    """Send synthetic requests at rate_rps for duration_s seconds."""
    total_count = max(1, int(round(rate_rps * duration_s)))
    requests = generate_requests(seed, total_count)
    interval = 1.0 / max(0.1, rate_rps)
    owns_client = client is None
    http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=5.0)
    ok = 0
    errors = 0

    try:
        start = time.monotonic()
        for idx, req in enumerate(requests):
            try:
                resp = http.get(
                    req.endpoint,
                    params={
                        "country": req.country,
                        "plan": req.plan,
                        "user_id": req.user_id,
                    },
                )
                if resp.status_code < 500:
                    ok += 1
                else:
                    errors += 1
            except httpx.HTTPError:
                errors += 1

            target_next = start + (idx + 1) * interval
            sleep_for = target_next - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        if owns_client:
            http.close()

    return {"sent": len(requests), "ok": ok, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic Acme Checkout traffic.")
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    summary = run_traffic(args.url, rate_rps=args.rate, duration_s=args.duration, seed=args.seed)
    print(f"SYNTHETIC traffic complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
