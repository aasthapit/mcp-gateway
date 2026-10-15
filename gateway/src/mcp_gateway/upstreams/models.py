"""Pydantic models for upstreams, mirroring ``schemas/upstreams.schema.json``.

WP4 loads these from MongoDB; WP1 builds them from ``GATEWAY_STATIC_UPSTREAMS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UpstreamAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "static_bearer", "static_header", "client_credentials"] = "none"
    secret_ref: str | None = Field(default=None, alias="secretRef")
    header_name: str | None = Field(default=None, alias="headerName")


class UpstreamTimeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect: int = Field(default=2000, ge=1, le=600_000)
    call: int = Field(default=60_000, ge=1, le=3_600_000)


class UpstreamConfig(BaseModel):
    """One proxied upstream. ``id`` is also the tool namespace prefix."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)

    id: str = Field(alias="_id", pattern=r"^[a-z][a-z0-9-]{0,62}$")
    display_name: str = Field(alias="displayName", min_length=1, max_length=200)
    enabled: bool = True
    transport: Literal["http", "sse", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    auth: UpstreamAuth = Field(default_factory=UpstreamAuth)
    session_mode: Literal["shared", "per_request"] = Field(
        default="per_request", alias="sessionMode"
    )
    timeouts_ms: UpstreamTimeouts = Field(default_factory=UpstreamTimeouts, alias="timeoutsMs")
    cache_ttl_sec: int = Field(default=300, alias="cacheTtlSec", ge=0, le=86_400)
    tags: tuple[str, ...] = ()
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy")
    protocol_mode: Literal["auto", "legacy", "2026-07-28"] = "auto"
    """Not in the Mongo schema yet; the era the proxy speaks upstream (see ADR 0001)."""

    def namespaced(self, tool_name: str) -> str:
        return f"{self.id}_{tool_name}"


@dataclass(frozen=True)
class CatalogTool:
    """A tool as the admin UI's picker sees it (``/internal/catalog``, WP4/WP5)."""

    name: str
    """Namespaced name, e.g. ``public_echo``."""
    upstream: str
    original_name: str
    description: str | None = None


@dataclass(frozen=True)
class UpstreamHealth:
    upstream: str
    up: bool
    checked_at: datetime | None = None
    error: str | None = None
    tool_count: int | None = None
