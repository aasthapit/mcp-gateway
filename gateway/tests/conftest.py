from __future__ import annotations

import socket

import pytest

from mcp_gateway.settings import Settings, load_settings


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Test settings: no separate metrics listener, console logs, static upstreams (no
    Mongo needed), no env leakage."""
    for key in list(__import__("os").environ):
        if key.startswith("GATEWAY_"):
            monkeypatch.delenv(key)
    return load_settings(
        metrics_port=0, log_json=False, log_level="WARNING", upstreams_source="static"
    )
