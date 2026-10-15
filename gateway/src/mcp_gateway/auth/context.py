"""Request-scoped principal context.

The ASGI auth layer binds the resolved :class:`Principal` for the duration of a request
with :func:`bind_principal`; everything downstream (FastMCP middleware, tools, audit)
reads it through :func:`current_principal`, which also falls back to
``request.state.principal`` so it works from any task that shares the request scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from mcp_gateway.auth.principal import ANONYMOUS, Principal, current_principal, principal_var

__all__ = ["ANONYMOUS", "Principal", "bind_principal", "current_principal", "principal_var"]


@contextmanager
def bind_principal(principal: Principal) -> Iterator[Principal]:
    """Make ``principal`` the current one for the enclosed block (ContextVar scoped)."""
    token = principal_var.set(principal)
    try:
        yield principal
    finally:
        principal_var.reset(token)
