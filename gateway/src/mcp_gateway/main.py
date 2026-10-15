"""Process entry point: ``uv run mcp-gateway`` or ``python -m mcp_gateway.main``.

One uvicorn worker per process by design (PLAN section 3.6): scale with replicas so each
pod runs exactly one policy watcher and one set of upstream clients.
"""

from __future__ import annotations

import uvicorn

from mcp_gateway.app import build_gateway
from mcp_gateway.settings import load_settings


def run() -> None:
    settings = load_settings()
    gateway = build_gateway(settings)
    assert gateway.app is not None
    uvicorn.run(
        gateway.app,
        host=settings.host,
        port=settings.port,
        workers=1,
        log_config=None,  # logging is configured by mcp_gateway.telemetry
        access_log=False,
        lifespan="on",
        proxy_headers=True,
        timeout_graceful_shutdown=30,
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    run()
