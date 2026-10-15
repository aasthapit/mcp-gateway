"""Caller identity: the :class:`Principal` contract (WP1) and JWT bearer auth (WP2).

* ``principal.py``  frozen ``Principal``, ``current_principal()``
* ``context.py``    ``bind_principal()`` (ContextVar scope used by the ASGI layer)
* ``jwt.py``        ``GatewayJWTVerifier`` on FastMCP's ``JWTVerifier``; ``JwtAuth`` lifecycle
* ``asgi.py``       ``BearerAuthMiddleware`` around the ``/mcp`` mount
* ``metadata.py``   RFC 9728 protected-resource metadata routes
"""

from mcp_gateway.auth.principal import ANONYMOUS, Principal, current_principal, principal_var

__all__ = ["ANONYMOUS", "Principal", "current_principal", "principal_var"]
