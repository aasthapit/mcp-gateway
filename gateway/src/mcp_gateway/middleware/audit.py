"""Audit trail: one structured log line per ``tools/call`` (and per denied call).

WP1 ships the shape of the event; WP7 decides retention/export. Fields are stable:
``event, tool, upstream, principal_kind, sub, client_id, outcome, duration_ms, request_id``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from mcp_gateway.auth.principal import Principal, current_principal
from mcp_gateway.telemetry import get_logger

log = get_logger("mcp_gateway.audit")


def _request_id(context: MiddlewareContext[Any]) -> str | None:
    ctx = context.fastmcp_context
    if ctx is None:
        return None
    try:
        return str(ctx.request_id)
    except Exception:
        return None


def audit_event(event: str, *, principal: Principal, **fields: Any) -> None:
    """Emit one audit line. Used by this middleware and by policy denials."""
    log.info(event, audit=True, **principal.audit_fields(), **fields)


class AuditMiddleware(Middleware):
    def __init__(self, upstream_of: Callable[[str], str] | None = None) -> None:
        self._upstream_of = upstream_of or (
            lambda name: name.split("_", 1)[0] if "_" in name else "unknown"
        )

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tool = getattr(context.message, "name", "unknown")
        principal = current_principal()
        start = time.perf_counter()
        outcome = "ok"
        error: str | None = None
        try:
            result = await call_next(context)
            if getattr(result, "is_error", False):
                outcome = "error"
            return result
        except BaseException as exc:
            outcome = "error"
            error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            audit_event(
                "tools.call",
                principal=principal,
                tool=tool,
                upstream=self._upstream_of(tool),
                outcome=outcome,
                error=error,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                request_id=_request_id(context),
            )
