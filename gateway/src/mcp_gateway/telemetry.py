"""Logging and tracing setup.

Logging: JSON lines to stdout via structlog, stdlib loggers routed through it. Every line
written inside a traced request carries ``trace_id``/``span_id`` (W3C hex) so logs and
traces join in the backend.

Tracing (``GATEWAY_TELEMETRY_MODE``, docs/adr/0012-limits-and-telemetry.md):

* ``off`` (default): FastMCP creates no spans and propagates nothing.
* ``propagation_only``: incoming trace context (``traceparent`` header or MCP ``_meta``)
  is carried to upstreams, no spans of our own.
* ``native``: an OpenTelemetry SDK ``TracerProvider`` with an OTLP exporter
  (http/protobuf or grpc) is installed, so FastMCP's native spans (one SERVER span per
  MCP request, ``delegate`` spans per mounted proxy, client spans to upstreams) are
  exported. Resource: ``service.name``, ``service.version``, ``deployment.environment``,
  ``k8s.pod.name``/``k8s.namespace.name``/``k8s.node.name`` from the Downward API env
  (``POD_NAME``, ``POD_NAMESPACE``, ``NODE_NAME``) plus ``OTEL_RESOURCE_ATTRIBUTES``.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from typing import Any

import structlog
from opentelemetry import trace

from mcp_gateway.settings import Settings

_HANDLER_NAME = "mcp_gateway"

_provider: Any = None  # the SDK TracerProvider we installed (native mode), for shutdown
_otlp_installed = False


def add_trace_context(
    logger: Any, method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: attach the current span's ids (no-op outside a span)."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = logging.getLevelName(settings.log_level)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Replace only our own previous handler so test harness handlers (caplog) survive.
    root.handlers[:] = [h for h in root.handlers if h.get_name() != _HANDLER_NAME] + [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "httpx2"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# -- tracing ------------------------------------------------------------------------------


def resource_attributes(settings: Settings, version: str) -> dict[str, str]:
    """Resource attributes we set explicitly; ``OTEL_RESOURCE_ATTRIBUTES`` is merged in by
    the SDK (``Resource.create``) and wins for keys it sets."""
    attrs = {
        "service.name": settings.otel_service_name,
        "service.version": version,
        "deployment.environment.name": settings.env,
        "service.instance.id": os.environ.get("POD_NAME") or socket.gethostname(),
    }
    for env, key in (
        ("POD_NAME", "k8s.pod.name"),
        ("POD_NAMESPACE", "k8s.namespace.name"),
        ("NODE_NAME", "k8s.node.name"),
    ):
        if value := os.environ.get(env):
            attrs[key] = value
    return attrs


def otlp_endpoint(settings: Settings) -> str:
    if settings.otlp_endpoint:
        return settings.otlp_endpoint
    if env := os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/"):
        return env
    return "http://localhost:4317" if settings.otlp_protocol == "grpc" else "http://localhost:4318"


def build_span_exporter(settings: Settings) -> Any:
    endpoint = otlp_endpoint(settings)
    if settings.otlp_protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(
            endpoint=endpoint,
            insecure=endpoint.startswith("http://"),
            timeout=settings.otlp_timeout_sec,
        )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpOTLPSpanExporter,
    )

    # The http exporter takes the full signal URL (the env-var form appends it itself).
    url = endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"
    return HttpOTLPSpanExporter(endpoint=url, timeout=settings.otlp_timeout_sec)


def configure_tracing(settings: Settings, version: str, *, exporter: Any = None) -> None:
    """Apply ``GATEWAY_TELEMETRY_MODE`` to FastMCP and, for ``native``, install the SDK.

    ``exporter`` replaces the OTLP exporter (tests pass an in-memory one). Idempotent per
    process: a second call in native mode reuses the installed provider.
    """
    global _provider, _otlp_installed
    import fastmcp

    fastmcp.settings.telemetry_mode = settings.telemetry_mode
    if settings.telemetry_mode != "native":
        return
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    if _provider is None:
        _provider = TracerProvider(resource=Resource.create(resource_attributes(settings, version)))
        trace.set_tracer_provider(_provider)
    if exporter is not None:
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
        return
    if _otlp_installed:
        return
    _provider.add_span_processor(BatchSpanProcessor(build_span_exporter(settings)))
    _otlp_installed = True
    get_logger(__name__).info(
        "otlp trace export configured",
        endpoint=otlp_endpoint(settings),
        protocol=settings.otlp_protocol,
        service_name=settings.otel_service_name,
    )


def shutdown_tracing() -> None:
    """Flush buffered spans (lifespan shutdown). Safe to call when tracing is off."""
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=5_000)
        except Exception as exc:  # export failures must not block shutdown
            get_logger(__name__).warning("span flush failed", error=str(exc))
