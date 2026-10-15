"""The resolved caller identity, shared by auth (WP2), policy (WP3) and audit.

Interface fixed by docs/PLAN.md section 5. WP2's ASGI layer resolves a ``Principal`` per
request and publishes it on ``request.state.principal`` and :data:`principal_var`;
FastMCP middleware reads it through :func:`current_principal`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

PrincipalKind = Literal["anonymous", "user"]

#: Claims that change on every token issuance and must not affect memoisation.
_VOLATILE_CLAIMS = frozenset(
    {"exp", "iat", "nbf", "jti", "auth_time", "session_state", "sid", "at_hash", "nonce"}
)


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    sub: str | None = None
    client_id: str | None = None
    scopes: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def is_anonymous(self) -> bool:
        return self.kind == "anonymous"

    @property
    def cache_key(self) -> str:
        """Stable key for memoising effective tools.

        Anonymous callers share one key. For users the key covers everything a grant can
        match on (``sub``, ``client_id``, scopes and the non-volatile claims), so two tokens
        for the same identity with different ``exp``/``jti`` hit the same cache entry.
        """
        if self.kind == "anonymous":
            return "anonymous"
        stable_claims = {k: v for k, v in sorted(self.claims.items()) if k not in _VOLATILE_CLAIMS}
        payload = json.dumps(
            [self.sub, self.client_id, sorted(self.scopes), stable_claims],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "user:" + hashlib.sha256(payload.encode()).hexdigest()[:32]

    def audit_fields(self) -> dict[str, Any]:
        """Low-cardinality identity fields for logs and audit events."""
        return {"principal_kind": self.kind, "sub": self.sub, "client_id": self.client_id}


ANONYMOUS = Principal(kind="anonymous")

#: Set by the ASGI auth layer (WP2) for the duration of a request.
principal_var: ContextVar[Principal | None] = ContextVar("mcp_gateway_principal", default=None)


def current_principal() -> Principal:
    """Return the principal of the in-flight request, defaulting to anonymous.

    Looks at :data:`principal_var` first, then at ``request.state.principal`` of the
    current HTTP request (FastMCP's ``get_http_request``). Never raises: outside a request
    (startup, tests, stdio) the caller is anonymous.
    """
    p = principal_var.get()
    if p is not None:
        return p
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception:
        return ANONYMOUS
    state_principal = getattr(request.state, "principal", None)
    return state_principal if isinstance(state_principal, Principal) else ANONYMOUS
