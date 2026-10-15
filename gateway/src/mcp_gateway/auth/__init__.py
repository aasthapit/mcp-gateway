"""Caller identity. WP1 ships the :class:`Principal` contract; WP2 adds JWT verification."""

from mcp_gateway.auth.principal import ANONYMOUS, Principal, current_principal, principal_var

__all__ = ["ANONYMOUS", "Principal", "current_principal", "principal_var"]
