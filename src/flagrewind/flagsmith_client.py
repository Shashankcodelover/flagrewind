"""Flagsmith Admin API client for read-only inspection and allowlisted writes."""

from __future__ import annotations

from typing import Any

import httpx


class FlagsmithError(RuntimeError):
    """Base error for Flagsmith API interactions."""


class FlagsmithAuthOrReachabilityError(FlagsmithError):
    """Raised when Flagsmith is unreachable or rejects credentials (exit code 3)."""


def _extract_results(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    results = payload.get("results", [])
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


class FlagsmithClient:
    """Typed HTTP wrapper over Flagsmith /api/v1/ Admin API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Api-Key {api_key}"},
            timeout=timeout_s,
        )

    def __enter__(self) -> FlagsmithClient:
        return self

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        try:
            resp = self._client.request(method, path.lstrip("/"), params=params, json=json)
        except httpx.HTTPError as exc:
            raise FlagsmithAuthOrReachabilityError(
                f"Flagsmith API unreachable at {self.base_url}: {exc}"
            ) from exc

        if resp.status_code in {401, 403}:
            raise FlagsmithAuthOrReachabilityError(
                f"Flagsmith rejected API key on {method} {path} ({resp.status_code})."
            )
        if resp.status_code >= 400:
            raise FlagsmithError(
                f"Flagsmith {method} {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        if not resp.content:
            return {}
        return resp.json()  # type: ignore[no-any-return]

    def list_organisations(self) -> list[dict[str, Any]]:
        return _extract_results(self._request("GET", "organisations/"))

    def list_projects(self, organisation_id: int | None = None) -> list[dict[str, Any]]:
        params = {"organisation": organisation_id} if organisation_id is not None else None
        return _extract_results(self._request("GET", "projects/", params=params))

    def get_project_by_name(self, name: str) -> dict[str, Any]:
        for project in self.list_projects():
            if project.get("name") == name:
                return project
        raise FlagsmithError(f"Project '{name}' not found in Flagsmith.")

    def list_environments(self, project_id: int) -> list[dict[str, Any]]:
        return _extract_results(
            self._request("GET", "environments/", params={"project": project_id})
        )

    def list_features(self, project_id: int) -> list[dict[str, Any]]:
        return _extract_results(self._request("GET", f"projects/{project_id}/features/"))

    def list_segments(self, project_id: int) -> list[dict[str, Any]]:
        return _extract_results(self._request("GET", f"projects/{project_id}/segments/"))

    def list_feature_states(self, env_api_key: str) -> list[dict[str, Any]]:
        return _extract_results(self._request("GET", f"environments/{env_api_key}/featurestates/"))

    def get_feature_state(self, env_api_key: str, feature_state_id: int) -> dict[str, Any]:
        res = self._request("GET", f"environments/{env_api_key}/featurestates/{feature_state_id}/")
        assert isinstance(res, dict)
        return res

    def patch_feature_state(
        self,
        env_api_key: str,
        feature_state_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an existing feature state (should be invoked only via executor.py)."""
        res = self._request(
            "PATCH",
            f"environments/{env_api_key}/featurestates/{feature_state_id}/",
            json=payload,
        )
        assert isinstance(res, dict)
        return res

    def create_segment_override(
        self,
        env_api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a segment-scoped feature state override in an environment."""
        res = self._request("POST", f"environments/{env_api_key}/featurestates/", json=payload)
        assert isinstance(res, dict)
        return res

    def list_webhooks(self, env_api_key: str) -> list[dict[str, Any]]:
        return _extract_results(self._request("GET", f"environments/{env_api_key}/webhooks/"))

    def list_audit_logs(
        self,
        *,
        project_id: int | None = None,
        organisation_id: int | None = None,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page_size": page_size}
        if project_id is not None:
            params["project"] = project_id
        try:
            return _extract_results(self._request("GET", "audit/", params=params))
        except FlagsmithError:
            if organisation_id is None:
                raise
            return _extract_results(
                self._request("GET", f"organisations/{organisation_id}/audit/", params=params)
            )
