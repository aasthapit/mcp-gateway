"""Pydantic models for upstreams, mirroring ``schemas/upstreams.schema.json``.

WP4 loads these from MongoDB (``upstreams/source.py``); WP1's ``GATEWAY_STATIC_UPSTREAMS``
path still builds them from settings. Field aliases are the Mongo/JSON names, attribute
names are snake_case; ``populate_by_name`` lets code use either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Transport = Literal["http", "sse", "stdio"]
AuthType = Literal["none", "static_bearer", "static_header", "client_credentials"]
SessionMode = Literal["shared", "per_request"]
ProtocolMode = Literal["auto", "legacy", "2026-07-28"]
HealthStatus = Literal["unknown", "up", "down", "degraded"]


class UpstreamAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    type: AuthType = "none"
    secret_ref: str | None = Field(
        default=None, alias="secretRef", pattern=r"^[A-Za-z0-9._-]{1,253}$"
    )
    header_name: str | None = Field(
        default=None, alias="headerName", pattern=r"^[A-Za-z0-9-]{1,100}$"
    )

    @model_validator(mode="after")
    def _consistent(self) -> UpstreamAuth:
        if self.type in ("static_bearer", "static_header") and not self.secret_ref:
            raise ValueError(f"auth.type={self.type} requires auth.secretRef")
        if self.type == "static_header" and not self.header_name:
            raise ValueError("auth.type=static_header requires auth.headerName")
        return self


class UpstreamTimeouts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connect: int = Field(default=2000, ge=1, le=600_000)
    """Milliseconds to establish the connection (TCP/TLS) and negotiate the protocol."""
    call: int = Field(default=60_000, ge=1, le=3_600_000)
    """Milliseconds to wait for one upstream response (``tools/list``, ``tools/call``)."""

    @property
    def connect_s(self) -> float:
        return self.connect / 1000

    @property
    def call_s(self) -> float:
        return self.call / 1000


class UpstreamConfig(BaseModel):
    """One proxied upstream. ``id`` is also the tool namespace prefix."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    id: str = Field(alias="_id", pattern=r"^[a-z][a-z0-9-]{0,62}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=200)
    enabled: bool = True
    transport: Transport = "http"
    url: str | None = Field(default=None, pattern=r"^https?://")
    command: str | None = Field(default=None, min_length=1)
    args: tuple[str, ...] = ()
    auth: UpstreamAuth = Field(default_factory=UpstreamAuth)
    session_mode: SessionMode = Field(default="per_request", alias="sessionMode")
    timeouts_ms: UpstreamTimeouts = Field(default_factory=UpstreamTimeouts, alias="timeoutsMs")
    cache_ttl_sec: int = Field(default=300, alias="cacheTtlSec", ge=0, le=86_400)
    tags: tuple[str, ...] = ()
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy")
    protocol_mode: ProtocolMode = Field(default="auto", alias="protocolMode")
    """Protocol era the proxy speaks upstream (ADR 0001). Not in the Mongo schema yet, so
    Mongo-sourced upstreams default to ``auto``; ``sessionMode: shared`` needs ``2026-07-28``."""

    @model_validator(mode="after")
    def _transport_fields(self) -> UpstreamConfig:
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"transport={self.transport} requires url")
        if self.transport == "stdio" and not self.command:
            raise ValueError("transport=stdio requires command")
        return self

    def namespaced(self, tool_name: str) -> str:
        return f"{self.id}_{tool_name}"

    def fingerprint(self) -> dict[str, Any]:
        """Everything that affects how the upstream is mounted (audit fields excluded)."""
        return self.model_dump(exclude={"updated_at", "updated_by"}, by_alias=True)

    def same_mount_as(self, other: UpstreamConfig) -> bool:
        return self.fingerprint() == other.fingerprint()


@dataclass(frozen=True)
class CatalogTool:
    """A tool as the admin UI's picker sees it (``/internal/catalog``)."""

    namespaced_name: str
    """Wire name on the gateway, e.g. ``sample-public_echo``."""
    upstream: str
    name: str
    """The tool's name on the upstream."""
    description: str | None = None
    input_schema: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespaced_name": self.namespaced_name,
            "upstream": self.upstream,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class UpstreamHealth:
    """Last known state of one upstream.

    ``status`` is ``degraded`` when the upstream is configured but not mounted
    (``reason`` says why: ``secret_missing``, ``stdio_forbidden_in_prod``, ...),
    ``unknown`` between mount and the first probe, else ``up``/``down`` from the probe.
    """

    upstream: str
    status: HealthStatus
    mounted: bool
    checked_at: datetime | None = None
    error: str | None = None
    reason: str | None = None
    tool_count: int | None = None
    latency_ms: float | None = None
    session_mode: SessionMode | None = None
    """Effective mode after the registry applied its rules (may differ from the config)."""

    @property
    def up(self) -> bool:
        return self.status == "up"

    def as_dict(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "status": self.status,
            "up": self.up,
            "mounted": self.mounted,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "error": self.error,
            "reason": self.reason,
            "tool_count": self.tool_count,
            "latency_ms": self.latency_ms,
            "session_mode": self.session_mode,
        }
