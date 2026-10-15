"""Per-pod request rate limiting and tool-result size limiting on FastMCP's middleware.

* :class:`PrincipalRateLimitingMiddleware` is FastMCP's ``RateLimitingMiddleware``
  (token bucket per key) keyed by principal: ``user:<sub>`` (``client_id`` when the
  token has no ``sub``) or ``ip:<client ip>`` for anonymous callers, with separate rates
  for the two kinds. The bucket table is an LRU bounded by ``GATEWAY_RATE_LIMIT_MAX_KEYS``
  so random source IPs cannot grow memory without limit. Rejections are JSON-RPC errors
  (``-32000 Rate limit exceeded``) and count ``gateway_rate_limited_total{principal_kind}``.
* :class:`CountingResponseLimitingMiddleware` is FastMCP's ``ResponseLimitingMiddleware``
  (oversized results become one truncated text block) plus
  ``gateway_response_truncated_total{upstream}``.

Both are per pod: N replicas admit N times the rate. Global limits need shared state
(Redis) and are v2 (docs/adr/0012-limits-and-telemetry.md).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    RateLimitingMiddleware,
    TokenBucketRateLimiter,
)
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.tools.base import ToolResult

from mcp_gateway.auth.principal import Principal, current_principal
from mcp_gateway.middleware.metrics import RATE_LIMITED, RESPONSE_TRUNCATED
from mcp_gateway.telemetry import get_logger

log = get_logger(__name__)


def request_client_ip() -> str:
    """Client IP resolved by :class:`~mcp_gateway.http_guards.HttpGuardMiddleware`."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception:
        return "unknown"
    ip = getattr(request.state, "client_ip", None)
    if isinstance(ip, str):
        return ip
    return request.client.host if request.client else "unknown"


def rate_limit_key(principal: Principal, client_ip: Callable[[], str] = request_client_ip) -> str:
    if principal.kind == "user":
        ident = principal.sub or principal.client_id
        if ident:
            return f"user:{ident}"
    return f"ip:{client_ip()}"


class _BucketTable(OrderedDict[str, TokenBucketRateLimiter]):
    """LRU of token buckets; the bucket shape depends on the key's principal kind."""

    def __init__(self, factory: Callable[[str], TokenBucketRateLimiter], max_keys: int) -> None:
        super().__init__()
        self._factory = factory
        self._max = max_keys

    def __getitem__(self, key: str) -> TokenBucketRateLimiter:
        if key in self:
            self.move_to_end(key)
            return super().__getitem__(key)
        bucket = self._factory(key)
        self[key] = bucket
        while len(self) > self._max:
            self.popitem(last=False)
        return bucket


class PrincipalRateLimitingMiddleware(RateLimitingMiddleware):
    def __init__(
        self,
        *,
        anonymous_rps: float,
        anonymous_burst: int,
        user_rps: float,
        user_burst: int,
        max_keys: int = 10_000,
        client_ip: Callable[[], str] = request_client_ip,
    ) -> None:
        self._client_ip = client_ip
        super().__init__(
            max_requests_per_second=user_rps,
            burst_capacity=user_burst,
            get_client_id=self._key,
        )
        shapes = {
            "user": (user_burst, user_rps),
            "ip": (anonymous_burst, anonymous_rps),
        }

        def factory(key: str) -> TokenBucketRateLimiter:
            capacity, rate = shapes["user" if key.startswith("user:") else "ip"]
            return TokenBucketRateLimiter(capacity, rate)

        self.limiters = _BucketTable(factory, max_keys)  # type: ignore[assignment]

    def _key(self, context: MiddlewareContext[Any]) -> str:
        return rate_limit_key(current_principal(), self._client_ip)

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        try:
            return await super().on_request(context, call_next)
        except RateLimitError:
            kind = current_principal().kind
            RATE_LIMITED.labels(kind).inc()
            log.debug("rate limited", principal_kind=kind, method=context.method)
            from opentelemetry import trace

            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute("gateway.rate_limited", True)
            raise RateLimitError("Rate limit exceeded; retry later") from None


class CountingResponseLimitingMiddleware(ResponseLimitingMiddleware):
    def __init__(self, *, max_size: int, upstream_of: Callable[[str], str]) -> None:
        super().__init__(max_size=max_size)
        self._upstream_of = upstream_of

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        token = _current_tool.set(getattr(context.message, "name", "unknown"))
        try:
            return await super().on_call_tool(context, call_next)
        finally:
            _current_tool.reset(token)

    def _truncate_to_result(
        self, text: str, meta: dict[str, Any] | None = None, is_error: bool = False
    ) -> ToolResult:
        tool = _current_tool.get()
        upstream = self._upstream_of(tool)
        RESPONSE_TRUNCATED.labels(upstream).inc()
        log.warning("tool result truncated", tool=tool, upstream=upstream, max_bytes=self.max_size)
        return super()._truncate_to_result(text, meta=meta, is_error=is_error)


#: Tool name of the call being limited (task-local; read by ``_truncate_to_result``).
_current_tool: ContextVar[str] = ContextVar("mcp_gateway_limited_tool", default="unknown")
