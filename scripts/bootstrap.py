"""Idempotent bootstrap script to provision FlagRewind's synthetic demo world in Flagsmith.

Uses the Flagsmith Admin API with FLAGREWIND_DRILL_KEY (kept separate from the agent key
so bootstrap/drill mutations remain distinguishable in Flagsmith's audit log).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

TARGET_ORG_NAME = "FlagRewind Demo"
TARGET_PROJECT_NAME = "Acme Checkout"
TARGET_ENVS = ("staging", "production")
DEMO_KEY_NAME = "flagrewind-demo-service"
DEFAULT_WEBHOOK_URL = "http://host.docker.internal:8787/webhooks/flagsmith"


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Definition for a Flagsmith feature or remote config flag."""

    name: str
    description: str
    default_enabled: bool = False
    initial_value: str | int | None = None
    feature_type: str = "STANDARD"
    multivariate_options: tuple[tuple[str, float], ...] = ()


DEMO_FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        name="tax_engine_v2",
        description="SYNTHETIC: VAT calculation v2 engine (triggers fault for DE/FR).",
    ),
    FeatureSpec(
        name="payment_timeout_ms",
        description="SYNTHETIC: Payment gateway timeout in ms (faults when < 500).",
        default_enabled=True,
        initial_value=3000,
    ),
    FeatureSpec(
        name="profile_v2",
        description="SYNTHETIC: New user profile renderer (faults for free plan).",
    ),
    FeatureSpec(
        name="search_ranker_v2",
        description="SYNTHETIC: Adds 400ms search latency without errors.",
    ),
    FeatureSpec(name="checkout_layout", description="SYNTHETIC: Safe UI decoy flag."),
    FeatureSpec(name="maintenance_banner", description="SYNTHETIC: Safe UI decoy flag."),
    FeatureSpec(name="export_csv", description="SYNTHETIC: Safe UI decoy flag."),
    FeatureSpec(name="dark_mode", description="SYNTHETIC: Safe UI decoy flag."),
    FeatureSpec(
        name="payment_provider",
        description="SYNTHETIC: Multivariate payment gateway routing.",
        default_enabled=True,
        initial_value="stripe_v1",
        feature_type="MULTIVARIATE",
        multivariate_options=(("stripe_v1", 100.0), ("broken_gateway", 0.0)),
    ),
)

DEMO_SEGMENTS: tuple[dict[str, Any], ...] = (
    {
        "name": "eu-customers",
        "description": "SYNTHETIC: Customers located in DE or FR.",
        "rules": [
            {
                "type": "ALL",
                "rules": [],
                "conditions": [{"operator": "IN", "property": "country", "value": "DE,FR"}],
            }
        ],
    },
    {
        "name": "free-plan",
        "description": "SYNTHETIC: Accounts on the free tier.",
        "rules": [
            {
                "type": "ALL",
                "rules": [],
                "conditions": [{"operator": "EQUAL", "property": "plan", "value": "free"}],
            }
        ],
    },
)


class BootstrapError(RuntimeError):
    """Raised when Flagsmith Admin API bootstrap cannot proceed."""


class FlagsmithBootstrapClient:
    """Thin Admin API client dedicated to idempotent demo provisioning."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        normalized = base_url.rstrip("/") + "/"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=normalized,
            headers={"Authorization": f"Api-Key {api_key}"},
            timeout=15.0,
        )

    def __enter__(self) -> FlagsmithBootstrapClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """Issue an Admin API request and return decoded JSON."""
        resp = self._client.request(method, path.lstrip("/"), json=json)
        if resp.status_code >= 400:
            raise BootstrapError(f"{method} {path} failed ({resp.status_code}): {resp.text[:300]}")
        if not resp.content:
            return {}
        return resp.json()  # type: ignore[no-any-return]

    def resolve_organisation(self) -> dict[str, Any]:
        """Locate FlagRewind Demo organisation or sole available organisation."""
        orgs = _extract_results(self.request("GET", "organisations/"))
        if not orgs:
            raise BootstrapError(
                "No Flagsmith organisations found. Complete 'Get Started' at "
                "http://localhost:8000 and create an organisation API key first."
            )
        for org in orgs:
            if org.get("name") == TARGET_ORG_NAME:
                return org
        return orgs[0]

    def ensure_project(self, org_id: int) -> dict[str, Any]:
        """Get or create the Acme Checkout project."""
        projects = _extract_results(self.request("GET", f"projects/?organisation={org_id}"))
        for project in projects:
            if project.get("name") == TARGET_PROJECT_NAME:
                return project
        created = self.request(
            "POST",
            "projects/",
            json={"name": TARGET_PROJECT_NAME, "organisation": org_id},
        )
        assert isinstance(created, dict)
        return created

    def ensure_environments(self, project_id: int) -> dict[str, dict[str, Any]]:
        """Ensure staging and production environments exist with v2 versioning off."""
        envs = _extract_results(self.request("GET", f"environments/?project={project_id}"))
        by_name = {env["name"].lower(): env for env in envs if env.get("name")}
        renames = {"development": "staging", "production": "production"}

        for default_name, target_name in renames.items():
            if target_name not in by_name and default_name in by_name:
                env = by_name[default_name]
                updated = self.request(
                    "PATCH",
                    f"environments/{env['api_key']}/",
                    json={"name": target_name, "project": project_id},
                )
                assert isinstance(updated, dict)
                by_name[target_name] = updated

        for target_name in TARGET_ENVS:
            if target_name not in by_name:
                created = self.request(
                    "POST",
                    "environments/",
                    json={"name": target_name, "project": project_id},
                )
                assert isinstance(created, dict)
                by_name[target_name] = created
        return {name: by_name[name] for name in TARGET_ENVS}

    def ensure_features(self, project_id: int) -> dict[str, dict[str, Any]]:
        """Create any missing synthetic features and multivariate options."""
        existing = _extract_results(self.request("GET", f"projects/{project_id}/features/"))
        by_name = {f["name"]: f for f in existing if f.get("name")}

        for spec in DEMO_FEATURES:
            if spec.name not in by_name:
                payload: dict[str, Any] = {
                    "name": spec.name,
                    "description": spec.description,
                    "default_enabled": spec.default_enabled,
                    "initial_value": spec.initial_value,
                    "type": spec.feature_type,
                }
                created = self.request("POST", f"projects/{project_id}/features/", json=payload)
                assert isinstance(created, dict)
                by_name[spec.name] = created
                if spec.multivariate_options:
                    self._ensure_mv_options(project_id, int(created["id"]), spec)
        return by_name

    def _ensure_mv_options(self, project_id: int, feature_id: int, spec: FeatureSpec) -> None:
        existing = _extract_results(
            self.request(
                "GET", f"projects/{project_id}/features/{feature_id}/multivariate-options/"
            )
        )
        if existing:
            return
        for value, weight in spec.multivariate_options:
            self.request(
                "POST",
                f"projects/{project_id}/features/{feature_id}/multivariate-options/",
                json={
                    "type": "unicode",
                    "string_value": value,
                    "default_percentage_allocation": weight,
                },
            )

    def ensure_segments(self, project_id: int) -> dict[str, dict[str, Any]]:
        """Create synthetic user segments if missing."""
        existing = _extract_results(self.request("GET", f"projects/{project_id}/segments/"))
        by_name = {s["name"]: s for s in existing if s.get("name")}
        for seg in DEMO_SEGMENTS:
            if seg["name"] not in by_name:
                created = self.request(
                    "POST",
                    f"projects/{project_id}/segments/",
                    json={**seg, "project": project_id},
                )
                assert isinstance(created, dict)
                by_name[seg["name"]] = created
        return by_name

    def ensure_webhook(self, env_api_key: str, url: str, secret: str) -> dict[str, Any]:
        """Create or update the Flagsmith environment webhook targeting FlagRewind journal."""
        hooks = _extract_results(self.request("GET", f"environments/{env_api_key}/webhooks/"))
        payload = {"url": url, "enabled": True, "secret": secret}
        for hook in hooks:
            if hook.get("url") == url:
                updated = self.request(
                    "PUT",
                    f"environments/{env_api_key}/webhooks/{hook['id']}/",
                    json=payload,
                )
                assert isinstance(updated, dict)
                return updated
        created = self.request("POST", f"environments/{env_api_key}/webhooks/", json=payload)
        assert isinstance(created, dict)
        return created

    def ensure_server_side_key(self, env_api_key: str) -> str | None:
        """Create server-side SDK key for demo_service if not already created."""
        keys = _extract_results(self.request("GET", f"environments/{env_api_key}/api-keys/"))
        for item in keys:
            if item.get("name") == DEMO_KEY_NAME:
                return None  # Flagsmith never returns raw key after initial creation
        created = self.request(
            "POST",
            f"environments/{env_api_key}/api-keys/",
            json={"name": DEMO_KEY_NAME},
        )
        assert isinstance(created, dict)
        key = created.get("key")
        return str(key) if key else None


def _extract_results(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    results = payload.get("results", [])
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def bootstrap_demo_world(
    client: FlagsmithBootstrapClient,
    *,
    webhook_url: str = DEFAULT_WEBHOOK_URL,
    webhook_secret: str,
) -> dict[str, Any]:
    """Provision FlagRewind Demo project, environments, features, segments, and webhook."""
    org = client.resolve_organisation()
    project = client.ensure_project(int(org["id"]))
    envs = client.ensure_environments(int(project["id"]))
    features = client.ensure_features(int(project["id"]))
    segments = client.ensure_segments(int(project["id"]))
    prod_key = str(envs["production"]["api_key"])
    webhook = client.ensure_webhook(prod_key, webhook_url, webhook_secret)
    server_key = client.ensure_server_side_key(prod_key)
    return {
        "organisation": org.get("name"),
        "project": project.get("name"),
        "environments": {k: v["api_key"] for k, v in envs.items()},
        "features": sorted(features),
        "segments": sorted(segments),
        "webhook_id": webhook.get("id"),
        "demo_server_key": server_key,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap FlagRewind demo world in Flagsmith.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("FLAGSMITH_API_URL", "http://localhost:8000/api/v1/"),
    )
    parser.add_argument(
        "--key-env",
        default="FLAGREWIND_DRILL_KEY",
        help="Env var holding Flagsmith Organisation API Key (default: FLAGREWIND_DRILL_KEY).",
    )
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api_key = os.environ.get(args.key_env) or os.environ.get("FLAGREWIND_ADMIN_KEY")
    secret = os.environ.get("FLAGREWIND_WEBHOOK_SECRET", "flagrewind-local-webhook-secret")
    if not api_key:
        print(
            f"Error: set {args.key_env} (or FLAGREWIND_ADMIN_KEY) before running bootstrap.",
            file=sys.stderr,
        )
        return 2
    try:
        with FlagsmithBootstrapClient(args.api_url, api_key) as client:
            summary = bootstrap_demo_world(
                client,
                webhook_url=args.webhook_url,
                webhook_secret=secret,
            )
    except (BootstrapError, httpx.HTTPError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 3

    print(f"Bootstrapped organisation={summary['organisation']} project={summary['project']}")
    print(f"Features ({len(summary['features'])}): {', '.join(summary['features'])}")
    print(f"Segments ({len(summary['segments'])}): {', '.join(summary['segments'])}")
    if summary["demo_server_key"]:
        print(f"New FLAGSMITH_DEMO_SERVER_KEY={summary['demo_server_key']}")
    else:
        print("Existing server-side key 'flagrewind-demo-service' preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
