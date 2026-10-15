from __future__ import annotations

import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from prometheus_client import REGISTRY

from mcp_gateway.middleware.audit import AuditMiddleware
from mcp_gateway.middleware.metrics import MetricsMiddleware
from mcp_gateway.settings import load_settings
from mcp_gateway.telemetry import configure_logging


def sample(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def make_server() -> FastMCP:
    mcp = FastMCP("m")

    @mcp.tool
    def ns_ok() -> str:
        return "ok"

    @mcp.tool
    def ns_boom() -> str:
        raise ValueError("boom")

    mcp.add_middleware(AuditMiddleware())
    mcp.add_middleware(MetricsMiddleware())
    mcp.add_middleware(ErrorHandlingMiddleware())
    return mcp


async def test_metrics_count_requests_and_tool_calls() -> None:
    async with Client(make_server()) as c:
        ok0 = counter(
            "gateway_tool_calls_total",
            tool="ns_ok",
            upstream="ns",
            principal_kind="anonymous",
            outcome="ok",
        )
        list0 = counter(
            "gateway_requests_total",
            method="tools/list",
            era="legacy",
            principal_kind="anonymous",
            outcome="ok",
        )
        await c.list_tools()
        await c.call_tool("ns_ok", {})
        with pytest.raises(Exception, match="boom"):
            await c.call_tool("ns_boom", {})
        with pytest.raises(Exception, match="Unknown tool"):
            await c.call_tool("ns_missing", {})
    assert (
        counter(
            "gateway_tool_calls_total",
            tool="ns_ok",
            upstream="ns",
            principal_kind="anonymous",
            outcome="ok",
        )
        == ok0 + 1
    )
    assert (
        counter(
            "gateway_tool_calls_total",
            tool="ns_boom",
            upstream="ns",
            principal_kind="anonymous",
            outcome="error",
        )
        >= 1
    )
    assert (
        counter(
            "gateway_tool_calls_total",
            tool="ns_missing",
            upstream="ns",
            principal_kind="anonymous",
            outcome="not_found",
        )
        >= 1
    )
    listed = sum(
        counter(
            "gateway_requests_total",
            method="tools/list",
            era=era,
            principal_kind="anonymous",
            outcome="ok",
        )
        for era in ("modern", "legacy")
    )
    # The SDK client may re-list tools to refresh output schemas, so count a lower bound.
    assert listed >= list0 + 1
    assert (
        counter(
            "gateway_tool_call_duration_seconds_count",
            tool="ns_ok",
            upstream="ns",
            principal_kind="anonymous",
        )
        >= 1
    )


async def test_audit_logs_one_line_per_call(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(load_settings(metrics_port=0, log_json=False, log_level="INFO"))
    caplog.set_level(logging.INFO, logger="mcp_gateway.audit")
    async with Client(make_server()) as c:
        await c.call_tool("ns_ok", {})
        with pytest.raises(Exception, match="boom"):
            await c.call_tool("ns_boom", {})
    records = [r for r in caplog.records if r.name == "mcp_gateway.audit"]
    assert len(records) == 2
    msgs = [str(r.msg) for r in records]
    assert all("tools.call" in m for m in msgs)
    assert any("ns_boom" in m and "error" in m for m in msgs)
