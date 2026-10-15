"""RFC 9728 OAuth 2.0 Protected Resource Metadata for the gateway.

The document is built from the MCP SDK's ``ProtectedResourceMetadata`` model (same schema
FastMCP's ``RemoteAuthProvider`` would serve) but registered by hand on both well-known
paths, see docs/adr/0007-jwt-verification.md for why the provider is not reused:

* ``/.well-known/oauth-protected-resource`` (what our 401 challenge advertises), and
* ``/.well-known/oauth-protected-resource/mcp`` (RFC 9728 section 3.1 path-suffixed form,
  which spec-following clients try first for a resource with a path).
"""

from __future__ import annotations

from typing import Any

from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_gateway.settings import Settings

WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    """The RFC 9728 document as a JSON-ready dict."""
    if settings.oidc_issuer is None:
        # RFC 9728 requires at least one authorization server; without an issuer the
        # gateway rejects every token, so publish the resource with an empty list.
        return {
            "resource": settings.resource_url,
            "authorization_servers": [],
            "scopes_supported": list(settings.scopes_supported),
            "bearer_methods_supported": ["header"],
            "resource_name": "MCP gateway",
        }
    model = ProtectedResourceMetadata(
        resource=AnyHttpUrl(settings.resource_url),
        authorization_servers=[AnyHttpUrl(settings.oidc_issuer)],
        scopes_supported=list(settings.scopes_supported),
        bearer_methods_supported=["header"],
        resource_name="MCP gateway",
    )
    return model.model_dump(mode="json", exclude_none=True)


def metadata_routes(settings: Settings) -> list[Route]:
    document = protected_resource_metadata(settings)
    headers = {
        "Cache-Control": "public, max-age=300",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "mcp-protocol-version",
    }

    async def handler(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=headers)
        return JSONResponse(document, headers=headers)

    suffixed = WELL_KNOWN_PATH + settings.mcp_path.rstrip("/")
    paths = [WELL_KNOWN_PATH] + ([suffixed] if suffixed != WELL_KNOWN_PATH else [])
    return [Route(p, handler, methods=["GET", "OPTIONS"]) for p in paths]
