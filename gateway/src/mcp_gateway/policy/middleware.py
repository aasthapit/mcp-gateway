"""Per-request tool visibility and call authorisation.

WP1 behaviour: read the principal (anonymous unless WP2's auth layer set one), ask the
current snapshot for the principal's :class:`ToolFilter`, filter ``tools/list`` and block
``tools/call`` for anything not allowed, and hide resources and prompts entirely (v1
exposes tools only). WP3 swaps the snapshot provider for the live Mongo-backed one; this
file should not need to change for that.

Denied calls answer exactly like unknown tools so existence is not leaked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from mcp_gateway.auth.principal import Principal, current_principal
from mcp_gateway.middleware.audit import audit_event
from mcp_gateway.middleware.metrics import POLICY_DENIALS
from mcp_gateway.policy.snapshot import AllowAllSnapshot, PolicySnapshot

SnapshotProvider = Callable[[], PolicySnapshot]


class PolicyDenied(NotFoundError):
    """Raised for calls the policy hides. Subclasses NotFoundError so the wire error is the
    same as FastMCP's own ``Unknown tool`` response."""


class PolicyMiddleware(Middleware):
    def __init__(
        self,
        snapshot_provider: SnapshotProvider | None = None,
        principal_provider: Callable[[], Principal] | None = None,
    ) -> None:
        self._snapshot: SnapshotProvider = snapshot_provider or _default_snapshot_provider()
        self._principal: Callable[[], Principal] = principal_provider or current_principal

    # -- tools --------------------------------------------------------------------------

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Sequence[Any]]
    ) -> Sequence[Any]:
        tools = await call_next(context)
        allowed = self._snapshot().effective_tools(self._principal())
        return [t for t in tools if allowed.allows(t.name)]

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        name = getattr(context.message, "name", "")
        principal = self._principal()
        if not self._snapshot().effective_tools(principal).allows(name):
            POLICY_DENIALS.labels(name, principal.kind).inc()
            audit_event("tools.call.denied", principal=principal, tool=name, outcome="denied")
            raise PolicyDenied(f"Unknown tool: {name!r}")
        return await call_next(context)

    # -- resources / prompts: hidden in v1 ------------------------------------------------

    async def on_list_resources(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> list[Any]:
        return []

    async def on_list_resource_templates(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> list[Any]:
        return []

    async def on_list_prompts(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> list[Any]:
        return []

    async def on_read_resource(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        uri = getattr(context.message, "uri", "")
        raise NotFoundError(f"Unknown resource: {str(uri)!r}")

    async def on_get_prompt(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        name = getattr(context.message, "name", "")
        raise NotFoundError(f"Unknown prompt: {name!r}")


def _default_snapshot_provider() -> SnapshotProvider:
    snapshot = AllowAllSnapshot()
    return lambda: snapshot
