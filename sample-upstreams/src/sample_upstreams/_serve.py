"""Shared entrypoint: serve a FastMCP server over stateless streamable HTTP."""

from __future__ import annotations

import os

from fastmcp import FastMCP


def serve(mcp: FastMCP, default_port: int) -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", default_port))
    # stateless_http: no server-side sessions, so the gateway can use a shared client
    # (upstream `sessionMode: shared`) and the container can be restarted freely.
    mcp.run(transport="http", host=host, port=port, path="/mcp", stateless_http=True)
