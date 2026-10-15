"""Pure-ASGI bearer authentication around the ``/mcp`` mount (PLAN section 3.1 step 1).

Rules:

* no ``Authorization`` header → anonymous principal (or 401 when
  ``GATEWAY_ALLOW_ANONYMOUS=false``);
* ``Authorization: Bearer <jwt>`` that verifies → user principal;
* anything else that is present (non-Bearer scheme, empty token, invalid/expired JWT) →
  401 with ``WWW-Authenticate: Bearer resource_metadata="...", error="invalid_token"`` and a
  small JSON body. Never downgraded to guest (PLAN section 7.4);
* tokens in the query string or cookies are ignored entirely (they count as absent).

The principal is published on ``scope["state"]["principal"]`` (= ``request.state.principal``
for every Starlette ``Request`` built on this scope, including FastMCP's) and on the
``principal_var`` ContextVar via :func:`bind_principal`.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.auth.context import ANONYMOUS, Principal, bind_principal
from mcp_gateway.auth.jwt import JwtAuth, TokenInvalid, principal_from_token
from mcp_gateway.middleware.audit import audit_event
from mcp_gateway.middleware.metrics import AUTH_RESULTS
from mcp_gateway.telemetry import get_logger

log = get_logger(__name__)


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: JwtAuth,
        resource_metadata_url: str,
        allow_anonymous: bool = True,
    ) -> None:
        self.app = app
        self.auth = auth
        self.resource_metadata_url = resource_metadata_url
        self.allow_anonymous = allow_anonymous

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization")
        if header is None:
            if not self.allow_anonymous:
                AUTH_RESULTS.labels("disabled_anonymous").inc()
                audit_event("auth.rejected", principal=ANONYMOUS, reason="anonymous_disabled")
                await self._reject(scope, receive, send, error=None, detail="bearer token required")
                return
            AUTH_RESULTS.labels("anonymous").inc()
            principal: Principal = ANONYMOUS
        else:
            scheme, _, token = header.partition(" ")
            token = token.strip()
            try:
                if scheme.lower() != "bearer":
                    raise TokenInvalid("unsupported_scheme", "only the Bearer scheme is accepted")
                if not token or " " in token:
                    raise TokenInvalid("malformed", "empty or malformed bearer credential")
                access = await self.auth.verify(token)
            except TokenInvalid as exc:
                AUTH_RESULTS.labels("invalid").inc()
                audit_event("auth.rejected", principal=ANONYMOUS, reason=exc.reason)
                await self._reject(scope, receive, send, error="invalid_token", detail=exc.detail)
                return
            principal = principal_from_token(access)
            AUTH_RESULTS.labels("valid").inc()

        scope.setdefault("state", {})["principal"] = principal
        with bind_principal(principal):
            await self.app(scope, receive, send)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, *, error: str | None, detail: str
    ) -> None:
        challenge = f'Bearer resource_metadata="{self.resource_metadata_url}"'
        body: dict[str, Any] = {"error_description": detail}
        if error:
            challenge += f', error="{error}"'
            body["error"] = error
        else:
            body["error"] = "unauthorized"
        response = JSONResponse(
            body,
            status_code=401,
            headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
