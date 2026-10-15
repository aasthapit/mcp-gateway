"""WP7 telemetry: native FastMCP spans with gateway attributes, log/trace correlation,
OTLP exporter selection, resource attributes."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import fastmcp
import pytest
from fastmcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from mcp_gateway import __version__, telemetry
from mcp_gateway.app import build_gateway
from mcp_gateway.auth.context import bind_principal
from mcp_gateway.auth.principal import Principal
from mcp_gateway.policy.snapshot import AllowAllSnapshot
from mcp_gateway.settings import Settings

EXPORTER = InMemorySpanExporter()


def server_spans(name: str) -> list:
    """The in-memory client emits CLIENT spans with the same names; keep the server's."""
    return [
        s
        for s in EXPORTER.get_finished_spans()
        if s.name == name and s.kind == trace.SpanKind.SERVER
    ]


@pytest.fixture
def native(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    # build_gateway() in native mode would add a real OTLP exporter; keep only the
    # in-memory one (nothing listens on localhost:4318 in unit tests).
    monkeypatch.setattr(telemetry, "_otlp_installed", True)
    s = settings.model_copy(
        update={"telemetry_mode": "native", "static_upstreams": [], "rate_limit_enabled": False}
    )
    if not getattr(EXPORTER, "_installed", False):
        telemetry.configure_tracing(s, __version__, exporter=EXPORTER)
        EXPORTER._installed = True  # type: ignore[attr-defined]
    EXPORTER.clear()
    yield s
    fastmcp.settings.telemetry_mode = "off"


def _gateway(s: Settings):
    gw = build_gateway(s)
    gw.set_snapshot(AllowAllSnapshot(version=42))

    @gw.mcp.tool(name="demo_echo")
    def demo_echo(text: str) -> str:
        logging.getLogger("mcp_gateway.test").warning("inside tool")
        return text

    return gw


async def test_tools_call_span_carries_principal_and_policy_version(native: Settings) -> None:
    gw = _gateway(native)
    async with Client(gw.mcp) as c:
        with bind_principal(Principal("user", sub="u1", client_id="ci-runner")):
            assert (await c.call_tool("demo_echo", {"text": "hi"})).data == "hi"
    spans = server_spans("tools/call demo_echo")
    assert spans, [s.name for s in EXPORTER.get_finished_spans()]
    attrs = dict(spans[-1].attributes or {})
    assert attrs["gen_ai.tool.name"] == "demo_echo"
    assert attrs["gateway.principal.kind"] == "user"
    assert attrs["gateway.principal.client_id"] == "ci-runner"
    assert attrs["gateway.policy.version"] == 42
    assert attrs["gateway.upstream"] == "unknown"  # gateway-local tool, no upstream namespace
    resource = dict(spans[-1].resource.attributes)
    assert resource["service.name"] == "mcp-gateway"
    assert resource["service.version"] == __version__


async def test_logs_carry_trace_and_span_ids(
    native: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    gw = _gateway(native.model_copy(update={"log_json": True}))
    async with Client(gw.mcp) as c:
        await c.call_tool("demo_echo", {"text": "x"})
    span = server_spans("tools/call demo_echo")[-1]
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if "inside tool" in line
    ]
    assert lines, "tool log line not captured"
    assert lines[-1]["trace_id"] == format(span.context.trace_id, "032x")
    assert len(lines[-1]["span_id"]) == 16


def test_http_traceparent_is_continued(native: Settings) -> None:
    gw = _gateway(native)
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    with TestClient(gw.app) as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "demo_echo",
                    "arguments": {"text": "t"},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "demo_echo",
                "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01",
            },
        )
        assert r.status_code == 200, r.text
    span = server_spans("tools/call demo_echo")[-1]
    assert format(span.context.trace_id, "032x") == trace_id
    assert span.attributes["gateway.principal.kind"] == "anonymous"


def test_mode_off_creates_no_spans(settings: Settings) -> None:
    s = settings.model_copy(update={"static_upstreams": []})
    build_gateway(s)
    assert fastmcp.settings.telemetry_mode == "off"


def test_exporter_selection(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpExporter,
    )

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    http = telemetry.build_span_exporter(
        settings.model_copy(update={"otlp_endpoint": "http://c:4318"})
    )
    assert isinstance(http, HttpExporter) and http._endpoint == "http://c:4318/v1/traces"
    assert telemetry.otlp_endpoint(settings) == "http://localhost:4318"
    grpc = settings.model_copy(update={"otlp_protocol": "grpc"})
    assert telemetry.otlp_endpoint(grpc) == "http://localhost:4317"
    assert isinstance(telemetry.build_span_exporter(grpc), GrpcExporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env:4318/")
    assert telemetry.otlp_endpoint(settings) == "http://env:4318"


def test_resource_attributes_from_downward_api(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POD_NAME", "mcp-gateway-abc")
    monkeypatch.setenv("POD_NAMESPACE", "mcp-gateway")
    monkeypatch.delenv("NODE_NAME", raising=False)
    attrs = telemetry.resource_attributes(settings, "1.2.3")
    assert attrs["k8s.pod.name"] == "mcp-gateway-abc"
    assert attrs["k8s.namespace.name"] == "mcp-gateway"
    assert attrs["service.instance.id"] == "mcp-gateway-abc"
    assert attrs["service.version"] == "1.2.3"
    assert "k8s.node.name" not in attrs
