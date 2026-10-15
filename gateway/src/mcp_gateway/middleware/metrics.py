"""Prometheus metrics for the gateway.

Metric names follow docs/PLAN.md section 3.6. The registry is the prometheus-client default
so ``/metrics`` on the main app and the separate metrics listener serve the same data.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from prometheus_client import Counter, Gauge, Histogram

from mcp_gateway import __version__
from mcp_gateway.auth.principal import current_principal

Era = str  # "modern" | "legacy" | "unknown"

REQUESTS = Counter(
    "gateway_requests_total",
    "MCP requests and notifications handled, by method and outcome.",
    ["method", "era", "principal_kind", "outcome"],
)
REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "Latency of MCP request handling inside the gateway.",
    ["method", "era", "principal_kind"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
TOOL_CALLS = Counter(
    "gateway_tool_calls_total",
    "tools/call requests by tool, upstream and outcome (ok|error|denied|upstream_error).",
    ["tool", "upstream", "principal_kind", "outcome"],
)
TOOL_CALL_DURATION = Histogram(
    "gateway_tool_call_duration_seconds",
    "Latency of tools/call including the upstream round trip.",
    ["tool", "upstream", "principal_kind"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
POLICY_DENIALS = Counter(
    "gateway_policy_denials_total",
    "tools/call attempts hidden by policy (answered as unknown tool).",
    ["tool", "principal_kind"],
)
POLICY_VERSION = Gauge("gateway_policy_version", "Version of the in-memory policy snapshot.")
POLICY_AGE = Gauge(
    "gateway_policy_age_seconds", "Seconds since the in-memory policy snapshot was loaded."
)
UPSTREAM_UP = Gauge(
    "gateway_upstream_up",
    "1 when the last probe of the upstream succeeded, 0 when it failed or the upstream "
    "is degraded (configured but not mounted).",
    ["upstream"],
)
UPSTREAM_PROBE_SECONDS = Histogram(
    "gateway_upstream_probe_seconds",
    "Duration of one health probe (connect + tools/list) per upstream.",
    ["upstream"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
UPSTREAM_ERRORS = Counter(
    "gateway_upstream_errors_total",
    "Upstream problems seen by the registry, by kind (probe_failed, secret_missing, "
    "stdio_forbidden, build_failed, invalid_config, mount_failed, load_failed).",
    ["upstream", "kind"],
)
UPSTREAMS_MOUNTED = Gauge(
    "gateway_upstreams_mounted", "Number of upstreams currently mounted on the gateway."
)
BUILD_INFO = Gauge("gateway_build_info", "Gateway build information.", ["version"])
BUILD_INFO.labels(version=__version__).set(1)


def _era(context: MiddlewareContext[Any]) -> Era:
    ctx = context.fastmcp_context
    if ctx is None:
        return "unknown"
    try:
        rc = ctx.request_context
        if rc is None:
            return "unknown"
        pv = rc.protocol_version
    except Exception:
        return "unknown"
    if not pv:
        return "unknown"
    return "modern" if str(pv) >= "2026-07-28" else "legacy"


def _outcome_of(exc: BaseException) -> str:
    """Map an exception to a low-cardinality outcome label.

    ErrorHandlingMiddleware (innermost) has already turned FastMCP exceptions into
    MCPError by the time they reach us, so MCP errors are classified by message.
    """
    from fastmcp.exceptions import NotFoundError

    from mcp_gateway.policy.middleware import PolicyDenied

    if isinstance(exc, PolicyDenied):
        return "denied"
    if isinstance(exc, NotFoundError):
        return "not_found"
    msg = str(exc)
    if "Client failed to connect" in msg:
        return "upstream_error"
    if "Not found" in msg or "Unknown tool" in msg or "Method not found" in msg:
        return "not_found"
    return "error"


class MetricsMiddleware(Middleware):
    """Counts every inbound message and times tool calls."""

    def __init__(self, upstream_of: Callable[[str], str] | None = None) -> None:
        self._upstream_of = upstream_of or (
            lambda name: name.split("_", 1)[0] if "_" in name else "unknown"
        )

    async def on_message(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        method = context.method or "unknown"
        era = _era(context)
        kind = current_principal().kind
        start = time.perf_counter()
        outcome = "ok"
        try:
            return await call_next(context)
        except BaseException as exc:
            outcome = _outcome_of(exc)
            raise
        finally:
            REQUESTS.labels(method, era, kind, outcome).inc()
            if context.type == "request":
                REQUEST_DURATION.labels(method, era, kind).observe(time.perf_counter() - start)

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tool = getattr(context.message, "name", "unknown")
        upstream = self._upstream_of(tool)
        kind = current_principal().kind
        start = time.perf_counter()
        outcome = "ok"
        try:
            result = await call_next(context)
            if getattr(result, "is_error", False):
                outcome = "error"
            return result
        except BaseException as exc:
            outcome = _outcome_of(exc)
            raise
        finally:
            TOOL_CALLS.labels(tool, upstream, kind, outcome).inc()
            TOOL_CALL_DURATION.labels(tool, upstream, kind).observe(time.perf_counter() - start)
