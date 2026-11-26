"""Unit tests for Acme Checkout synthetic demo_service and deterministic faults."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from demo_service.app import create_app
from demo_service.traffic import generate_requests


class FakeFlags:
    """In-memory deterministic FlagReader stub for unit tests."""

    def __init__(self, enabled: set[str] | None = None, values: dict[str, Any] | None = None):
        self.enabled = enabled or set()
        self.values = values or {}

    def is_feature_enabled(self, feature_name: str) -> bool:
        return feature_name in self.enabled

    def get_feature_value(self, feature_name: str) -> Any:
        return self.values.get(feature_name)


def test_healthy_baseline_and_decoys_produce_zero_errors() -> None:
    flags = FakeFlags(
        enabled={"checkout_layout", "maintenance_banner", "export_csv", "dark_mode"},
        values={"payment_timeout_ms": 3000, "payment_provider": "stripe_v1"},
    )
    client = TestClient(create_app(fallback_flags=flags, latency_scale=0.0))
    for cc in ("US", "GB", "DE", "FR", "IT"):
        assert client.get(f"/checkout?country={cc}&plan=free").status_code == 200
        assert client.get(f"/profile?country={cc}&plan=free").status_code == 200
        assert client.get(f"/search?country={cc}&plan=free").status_code == 200

    metrics = client.get("/metrics").json()
    assert metrics["synthetic"] is True
    assert metrics["windows"][0]["errors"] == 0
    assert client.get("/errors").json()["errors"] == []


def test_tax_engine_v2_breaks_de_and_fr_only() -> None:
    flags = FakeFlags(enabled={"tax_engine_v2"})
    client = TestClient(create_app(fallback_flags=flags, latency_scale=0.0))
    de_statuses = [client.get("/checkout?country=DE&plan=pro").status_code for _ in range(10)]
    us_statuses = [client.get("/checkout?country=US&plan=pro").status_code for _ in range(10)]

    assert sum(1 for code in de_statuses if code == 500) == 6
    assert all(code == 200 for code in us_statuses)
    errors = client.get("/errors").json()["errors"]
    assert errors[0]["type"] == "VATCalculationError"
    assert any("faults.py" in f["file"] for f in errors[0]["frames"])


def test_payment_timeout_ms_under_500_breaks_checkout() -> None:
    flags = FakeFlags(enabled={"payment_timeout_ms"}, values={"payment_timeout_ms": 300})
    client = TestClient(create_app(fallback_flags=flags, latency_scale=0.0))
    statuses = [client.get("/checkout?country=US&plan=pro").status_code for _ in range(10)]
    assert sum(1 for code in statuses if code == 500) == 3
    assert client.get("/errors").json()["errors"][0]["type"] == "PaymentTimeoutError"


def test_profile_v2_breaks_free_plan_and_error_cap_is_50() -> None:
    flags = FakeFlags(enabled={"profile_v2"})
    client = TestClient(create_app(fallback_flags=flags, latency_scale=0.0))
    assert client.get("/profile?plan=pro").status_code == 200
    for _ in range(60):
        assert client.get("/profile?plan=free").status_code == 500
    errors = client.get("/errors").json()["errors"]
    assert len(errors) == 50
    assert errors[0]["type"] == "KeyError"


def test_multivariate_and_non_flag_fault_and_search_latency() -> None:
    mv_flags = FakeFlags(
        enabled={"payment_provider", "search_ranker_v2"},
        values={"payment_provider": "broken_gateway"},
    )
    mv_client = TestClient(create_app(fallback_flags=mv_flags, latency_scale=0.0))
    mv_statuses = [mv_client.get("/checkout?country=US").status_code for _ in range(10)]
    assert sum(1 for code in mv_statuses if code == 500) == 8
    search_resp = mv_client.get("/search").json()
    assert search_resp["status"] == "ok"

    non_flag_client = TestClient(
        create_app(fallback_flags=FakeFlags(), latency_scale=0.0, non_flag_fault=True)
    )
    nf_statuses = [non_flag_client.get("/checkout").status_code for _ in range(8)]
    assert sum(1 for code in nf_statuses if code == 500) == 2
    assert non_flag_client.get("/errors").json()["errors"][0]["type"] == "DatabaseConnectionError"


def test_traffic_generator_is_deterministic_by_seed() -> None:
    batch_a = generate_requests(seed=123, count=25)
    batch_b = generate_requests(seed=123, count=25)
    assert batch_a == batch_b
