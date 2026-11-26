"""P1 read-only client for the official Flagsmith MCP server."""

from __future__ import annotations

import os
from typing import Any

import httpx

_ALLOWED_READ_PREFIXES = ("list_", "get_")


class MCPReadOnlyError(RuntimeError):
    """Raised when MCP server is unreachable or a non-read tool is requested."""


class FlagsmithMCPReader:
    """Minimal JSON-RPC HTTP client for read-only Flagsmith MCP inspection."""

    def __init__(
        self,
        mcp_url: str,
        api_token: str,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.mcp_url = mcp_url
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers={
                "Authorization": f"Api-Key {api_token}",
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
        )

    @classmethod
    def from_env(cls, admin_key_env: str) -> FlagsmithMCPReader | None:
        url = os.environ.get("FLAGSMITH_MCP_URL", "").strip()
        token = os.environ.get(admin_key_env, "").strip()
        if not url or not token:
            return None
        return cls(url, token)

    def __enter__(self) -> FlagsmithMCPReader:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def list_tools(self) -> list[str]:
        """List read-only MCP tool names advertised by Flagsmith MCP server."""
        try:
            resp = self._client.post(
                self.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MCPReadOnlyError(f"MCP server unreachable at {self.mcp_url}: {exc}") from exc

        tools = data.get("result", {}).get("tools", [])
        return [
            str(t["name"])
            for t in tools
            if isinstance(t, dict) and str(t.get("name", "")).startswith(_ALLOWED_READ_PREFIXES)
        ]

    def call_read_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a read-only MCP tool; mutation tools are strictly rejected."""
        if not name.startswith(_ALLOWED_READ_PREFIXES):
            raise MCPReadOnlyError(f"Blocked non-read-only MCP tool: {name}")
        try:
            resp = self._client.post(
                self.mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
            )
            resp.raise_for_status()
            return resp.json().get("result")
        except (httpx.HTTPError, ValueError) as exc:
            raise MCPReadOnlyError(f"MCP tool '{name}' failed: {exc}") from exc
