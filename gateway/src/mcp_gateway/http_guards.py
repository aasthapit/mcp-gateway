"""Pure-ASGI guards around the whole Starlette app (outermost layer).

* :class:`HttpGuardMiddleware`

  - resolves the client IP once per request (``X-Forwarded-For`` only from trusted
    proxies, see :func:`client_ip_from_scope`) and publishes it on
    ``scope["state"]["client_ip"]`` for the rate limiter and audit;
  - enforces ``GATEWAY_MAX_REQUEST_BODY_BYTES``: ``Content-Length`` above the limit → 413
    before the app runs; bodies without a length are buffered up to the limit (MCP
    requests are small JSON-RPC messages) and replayed, anything longer → 413;
  - counts responses by route group and status class (``gateway_http_responses_total``),
    the input of the 5xx-ratio alert;
  - in ``propagation_only``/``native`` telemetry mode, attaches the W3C ``traceparent``
    of the HTTP request so FastMCP's SERVER span continues the caller's trace.

Limits are per pod (docs/adr/0012-limits-and-telemetry.md).
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_gateway.middleware.metrics import HTTP_RESPONSES, REQUEST_BODY_REJECTED

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_PROBE_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def parse_networks(entries: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip())


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:  # [v6]:port
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, nets: tuple[IPNetwork, ...]
) -> bool:
    return any(ip in net for net in nets)


def client_ip_from_scope(scope: Scope, trusted: tuple[IPNetwork, ...]) -> str:
    """The caller's IP.

    The TCP peer is the client unless it is a trusted proxy; then ``X-Forwarded-For`` is
    walked right to left and the first address that is not a trusted proxy wins (entries
    further left were written by the client and cannot be believed). A malformed entry
    stops the walk at the last trusted hop. Returns ``"unknown"`` without a peer.
    """
    client = scope.get("client")
    peer = client[0] if client else None
    if peer is None:
        return "unknown"
    peer_ip = _parse_ip(peer)
    if peer_ip is None or not trusted or not _trusted(peer_ip, trusted):
        return peer
    forwarded: list[str] = []
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-for":
            forwarded.extend(part for part in value.decode("latin-1").split(","))
    result = str(peer_ip)
    for raw in reversed(forwarded):
        ip = _parse_ip(raw)
        if ip is None:
            break
        result = str(ip)
        if not _trusted(ip, trusted):
            break
    return result


def route_group(path: str) -> str:
    if path.startswith("/internal"):
        return "internal"
    if path in _PROBE_PATHS:
        return "probe"
    if path.startswith("/mcp"):
        return "mcp"
    return "other"


class _BodyTooLarge(Exception):
    pass


class HttpGuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = 1_048_576,
        trusted_proxies: Iterable[str] = (),
        mcp_path: str = "/mcp",
        propagate_trace: bool = False,
    ) -> None:
        self.app = app
        self.max_body = max_body_bytes
        self.trusted = parse_networks(trusted_proxies)
        self.mcp_path = mcp_path
        self.propagate_trace = propagate_trace

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["client_ip"] = client_ip_from_scope(scope, self.trusted)
        group = "mcp" if scope["path"].startswith(self.mcp_path) else route_group(scope["path"])
        status: list[int] = []

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        try:
            if self.max_body and scope["method"] in _BODY_METHODS:
                length = _content_length(scope)
                if length is not None and length > self.max_body:
                    await self._reject_too_large(send_wrapper)
                    return
                if length is None:
                    try:
                        receive = await self._buffered(receive)
                    except _BodyTooLarge:
                        await self._reject_too_large(send_wrapper)
                        return
                else:
                    receive = self._capped(receive)
            token = self._attach_trace(scope) if self.propagate_trace else None
            try:
                await self.app(scope, receive, send_wrapper)
            except _BodyTooLarge:
                # Content-Length lied; nothing was sent yet in practice (the MCP transport
                # reads the whole body before answering).
                if not status:
                    await self._reject_too_large(send_wrapper)
                    return
                raise
            finally:
                if token is not None:
                    from opentelemetry import context as otel_context

                    otel_context.detach(token)
        finally:
            code = f"{status[0] // 100}xx" if status else "5xx"
            HTTP_RESPONSES.labels(group, code).inc()

    async def _reject_too_large(self, send: Send) -> None:
        REQUEST_BODY_REJECTED.inc()
        body = json.dumps(
            {
                "error": "request_too_large",
                "error_description": f"request body exceeds {self.max_body} bytes",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _buffered(self, receive: Receive) -> Receive:
        """Read a body without Content-Length (chunked) up to the limit, then replay it."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break  # disconnect: let the app see an empty body and the disconnect
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                raise _BodyTooLarge
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()  # later calls: disconnect detection for streams

        return replay

    def _capped(self, receive: Receive) -> Receive:
        size = 0

        async def capped() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.max_body:
                    raise _BodyTooLarge
            return message

        return capped

    @staticmethod
    def _attach_trace(scope: Scope) -> Any:
        carrier = {
            k: v.decode("latin-1")
            for k, v in ((n.decode("latin-1"), v) for n, v in scope.get("headers") or ())
            if k in ("traceparent", "tracestate")
        }
        if "traceparent" not in carrier:
            return None
        from opentelemetry import context as otel_context
        from opentelemetry import propagate

        return otel_context.attach(propagate.extract(carrier))


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
