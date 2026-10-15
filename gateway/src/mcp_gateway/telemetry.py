"""Logging setup: JSON lines to stdout via structlog, stdlib loggers routed through it."""

from __future__ import annotations

import logging
import os
import sys

import structlog

from mcp_gateway.settings import Settings

_HANDLER_NAME = "mcp_gateway"


def configure_logging(settings: Settings) -> None:
    level = logging.getLevelName(settings.log_level)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
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

    # FastMCP reads its own settings from the environment at import time; make the
    # telemetry mode follow ours when the operator did not set it explicitly.
    os.environ.setdefault("FASTMCP_TELEMETRY_MODE", settings.telemetry_mode)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
