"""Gateway attributes on FastMCP's per-request SERVER span.

FastMCP opens the span at its middleware seam, so it is the current span while our
middleware runs (``trace.get_current_span()``). Outermost in the chain so rate-limited
and denied calls carry the attributes too. Attributes (low cardinality, no PII):

* ``gateway.principal.kind`` (``anonymous``/``user``) and ``gateway.principal.client_id``
* ``gateway.policy.version``
* on ``tools/call``: ``gateway.upstream`` (namespace of the tool)

A no-op unless ``GATEWAY_TELEMETRY_MODE=native`` and an SDK is installed (the span is
then non-recording).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from opentelemetry import trace

from mcp_gateway.auth.principal import current_principal
from mcp_gateway.policy.snapshot import PolicySnapshot


class SpanAttributesMiddleware(Middleware):
    def __init__(
        self,
        snapshot_provider: Callable[[], PolicySnapshot],
        upstream_of: Callable[[str], str],
    ) -> None:
        self._snapshot = snapshot_provider
        self._upstream_of = upstream_of

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        span = trace.get_current_span()
        if span.is_recording():
            principal = current_principal()
            attrs: dict[str, Any] = {
                "gateway.principal.kind": principal.kind,
                "gateway.policy.version": self._snapshot().version,
            }
            if principal.client_id:
                attrs["gateway.principal.client_id"] = principal.client_id
            if context.method == "tools/call":
                tool = getattr(context.message, "name", None)
                if tool:
                    attrs["gateway.upstream"] = self._upstream_of(tool)
            span.set_attributes(attrs)
        return await call_next(context)
