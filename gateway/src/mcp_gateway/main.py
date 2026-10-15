"""Process entry point: ``uv run mcp-gateway`` or ``python -m mcp_gateway.main``.

One uvicorn worker per process by design (PLAN section 3.6): scale with replicas so each
pod runs exactly one policy watcher and one set of upstream clients.

Graceful shutdown (SIGTERM from the kubelet, after the preStop sleep):

1. :class:`GatewayServer` calls :meth:`~mcp_gateway.app.Gateway.begin_drain` first:
   ``/readyz`` turns 503 and every ``subscriptions/listen`` stream is closed with its final
   ``SubscriptionsListenResult`` (graceful completion, so clients re-listen on another
   pod instead of seeing a dropped stream);
2. uvicorn stops accepting connections, closes idle keep-alive connections and waits up
   to ``GATEWAY_SHUTDOWN_GRACE_SEC`` for in-flight requests (slow tool calls) to finish,
   then cancels what is left;
3. the lifespan shutdown stops the policy watcher, upstream probes and clients and
   flushes spans.

Budget: preStop sleep + shutdown grace + lifespan (~1 s) < terminationGracePeriodSeconds
(chart: 5 + 45 + margin < 60).
"""

from __future__ import annotations

import socket

import uvicorn

from mcp_gateway.app import Gateway, build_gateway
from mcp_gateway.settings import Settings, load_settings


class GatewayServer(uvicorn.Server):
    """uvicorn server that drains the gateway before its own graceful shutdown."""

    def __init__(self, config: uvicorn.Config, gateway: Gateway) -> None:
        super().__init__(config)
        self.gateway = gateway

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.gateway.begin_drain()
        await super().shutdown(sockets)


def server_config(gateway: Gateway, settings: Settings, **overrides: object) -> uvicorn.Config:
    assert gateway.app is not None
    options: dict[str, object] = {
        "host": settings.host,
        "port": settings.port,
        "workers": 1,
        "log_config": None,  # logging is configured by mcp_gateway.telemetry
        "access_log": False,
        "lifespan": "on",
        # Client IPs come from GATEWAY_TRUSTED_PROXIES (http_guards.py), not from
        # uvicorn's own X-Forwarded-For handling.
        "proxy_headers": False,
        "timeout_graceful_shutdown": settings.shutdown_grace_sec,
        "timeout_keep_alive": 75,
    }
    options.update(overrides)
    return uvicorn.Config(gateway.app, **options)  # type: ignore[arg-type]


def run() -> None:
    settings = load_settings()
    gateway = build_gateway(settings)
    GatewayServer(server_config(gateway, settings), gateway).run()


if __name__ == "__main__":
    run()
