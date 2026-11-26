"""Unit tests for idempotent Flagsmith bootstrap script using respx mocks."""

from __future__ import annotations

import httpx
import respx

from scripts.bootstrap import (
    DEMO_FEATURES,
    DEMO_KEY_NAME,
    DEMO_SEGMENTS,
    FlagsmithBootstrapClient,
    bootstrap_demo_world,
)


@respx.mock
def test_bootstrap_creates_demo_world_from_empty_org() -> None:
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}organisations/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "FlagRewind Demo"}]})
    )
    respx.get(f"{base}projects/?organisation=1").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.post(f"{base}projects/").mock(
        return_value=httpx.Response(201, json={"id": 10, "name": "Acme Checkout"})
    )
    respx.get(f"{base}environments/?project=10").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 100, "name": "Development", "api_key": "env-dev"},
                    {"id": 101, "name": "Production", "api_key": "env-prod"},
                ]
            },
        )
    )
    respx.patch(f"{base}environments/env-dev/").mock(
        return_value=httpx.Response(200, json={"id": 100, "name": "staging", "api_key": "env-dev"})
    )
    respx.patch(f"{base}environments/env-prod/").mock(
        return_value=httpx.Response(
            200, json={"id": 101, "name": "production", "api_key": "env-prod"}
        )
    )
    respx.get(f"{base}projects/10/features/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    feature_route = respx.post(f"{base}projects/10/features/").mock(
        side_effect=[
            httpx.Response(201, json={"id": 200 + i, "name": spec.name})
            for i, spec in enumerate(DEMO_FEATURES)
        ]
    )
    mv_feature_id = 200 + len(DEMO_FEATURES) - 1
    respx.get(f"{base}projects/10/features/{mv_feature_id}/multivariate-options/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    mv_route = respx.post(
        f"{base}projects/10/features/{mv_feature_id}/multivariate-options/"
    ).mock(return_value=httpx.Response(201, json={"id": 999}))

    respx.get(f"{base}projects/10/segments/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    seg_route = respx.post(f"{base}projects/10/segments/").mock(
        side_effect=[
            httpx.Response(201, json={"id": 300 + i, "name": seg["name"]})
            for i, seg in enumerate(DEMO_SEGMENTS)
        ]
    )
    respx.get(f"{base}environments/env-prod/webhooks/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.post(f"{base}environments/env-prod/webhooks/").mock(
        return_value=httpx.Response(201, json={"id": 500})
    )
    respx.get(f"{base}environments/env-prod/api-keys/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.post(f"{base}environments/env-prod/api-keys/").mock(
        return_value=httpx.Response(201, json={"key": "ser.secret123", "name": DEMO_KEY_NAME})
    )

    with FlagsmithBootstrapClient(base, "org-drill-key") as client:
        summary = bootstrap_demo_world(client, webhook_secret="wh-secret")

    assert summary["project"] == "Acme Checkout"
    assert len(summary["features"]) == len(DEMO_FEATURES)
    assert len(summary["segments"]) == len(DEMO_SEGMENTS)
    assert summary["demo_server_key"] == "ser.secret123"
    assert feature_route.call_count == len(DEMO_FEATURES)
    assert mv_route.call_count == 2
    assert seg_route.call_count == 2


@respx.mock
def test_bootstrap_is_idempotent_on_second_run() -> None:
    base = "http://localhost:8000/api/v1/"
    respx.get(f"{base}organisations/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "FlagRewind Demo"}]})
    )
    respx.get(f"{base}projects/?organisation=1").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 10, "name": "Acme Checkout"}]})
    )
    respx.get(f"{base}environments/?project=10").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 100, "name": "staging", "api_key": "env-stg"},
                    {"id": 101, "name": "production", "api_key": "env-prod"},
                ]
            },
        )
    )
    respx.get(f"{base}projects/10/features/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": i, "name": spec.name} for i, spec in enumerate(DEMO_FEATURES)]
            },
        )
    )
    respx.get(f"{base}projects/10/segments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": i, "name": seg["name"]} for i, seg in enumerate(DEMO_SEGMENTS)]
            },
        )
    )
    respx.get(f"{base}environments/env-prod/webhooks/").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 500, "url": "http://host.docker.internal:8787/webhooks/flagsmith"}]},
        )
    )
    put_hook = respx.put(f"{base}environments/env-prod/webhooks/500/").mock(
        return_value=httpx.Response(200, json={"id": 500})
    )
    respx.get(f"{base}environments/env-prod/api-keys/").mock(
        return_value=httpx.Response(200, json={"results": [{"name": DEMO_KEY_NAME}]})
    )

    with FlagsmithBootstrapClient(base, "org-drill-key") as client:
        summary = bootstrap_demo_world(client, webhook_secret="wh-secret")

    assert put_hook.called
    assert summary["demo_server_key"] is None
