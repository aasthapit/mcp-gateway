"""Dev-only tools registered on the gateway itself when ``GATEWAY_ENV=dev``.

Never registered in prod: ``gateway_whoami`` echoes the caller's verified claims, which is
exactly the kind of thing a production MCP endpoint should not hand out.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from mcp_gateway.auth.principal import current_principal


def register_debug_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="gateway_whoami", description="DEV ONLY: the principal the gateway resolved")
    def gateway_whoami() -> dict[str, Any]:
        p = current_principal()
        claims = dict(p.claims)
        return {
            "kind": p.kind,
            "sub": p.sub,
            "client_id": p.client_id,
            "scopes": sorted(p.scopes),
            "roles": list(claims.get("realm_access", {}).get("roles", []))
            if isinstance(claims.get("realm_access"), dict)
            else [],
            "cache_key": p.cache_key,
            "claims": claims,
        }
