"""Gateway configuration (environment variables with the ``GATEWAY_`` prefix).

Every knob the gateway reads lives here so later work packages extend one place.
Prod guards are validators on this model: a misconfigured prod gateway refuses to start
instead of silently running in a weaker mode.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Env = Literal["dev", "prod"]
TelemetryMode = Literal["native", "propagation_only", "off"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
UpstreamTransport = Literal["http", "sse", "stdio"]
ProtocolMode = Literal["auto", "legacy", "2026-07-28"]
PolicySource = Literal["mongo", "static"]


class StaticUpstream(BaseModel):
    """One statically configured upstream (WP1 only; WP4 reads upstreams from Mongo).

    ``id`` doubles as the tool namespace: upstream tool ``echo`` on upstream ``public``
    is exposed as ``public_echo``.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    url: str = Field(pattern=r"^https?://")
    transport: UpstreamTransport = "http"
    cache_ttl_sec: int = Field(default=300, ge=0, le=86400)
    protocol_mode: ProtocolMode = "auto"
    """Protocol era the proxy speaks to this upstream. ``auto`` probes ``server/discover``
    per backend connection; pin ``2026-07-28`` for known-modern stateless upstreams."""


#: Ids match ``local/seed/upstreams.json`` so the seeded toolsets (``sample-public_*``)
#: apply to the statically mounted sample upstreams when developing on the host.
DEFAULT_STATIC_UPSTREAMS: tuple[StaticUpstream, ...] = (
    StaticUpstream(id="sample-public", url="http://localhost:8081/mcp"),
    StaticUpstream(id="sample-private", url="http://localhost:8082/mcp"),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=None, extra="ignore", frozen=True
    )

    env: Env = "dev"

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    metrics_port: int = Field(default=9090, ge=0, le=65535)
    """Separate Prometheus listener. ``0`` disables it (``/metrics`` stays on the main port)."""

    public_base_url: str = "http://localhost:8080"
    """Externally visible origin of this gateway; used for RFC 9728 metadata (WP2)."""
    mcp_path: str = "/mcp"

    allowed_hosts: list[str] = Field(default_factory=list)
    """Extra ``Host`` header values accepted by FastMCP's DNS-rebinding guard.
    ``localhost``/``127.0.0.1`` are always accepted in dev."""
    host_origin_protection: bool = True

    allow_anonymous: bool = True
    """``False`` turns the gateway strict: guests get 401 (enforced by WP2's ASGI layer)."""

    mongo_uri: str = "mongodb://root:devpass@localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_db: str = "mcp_gateway"

    policy_source: PolicySource = "mongo"
    """``mongo``: allow-lists from MongoDB with hot reload (WP3). ``static``: allow-all,
    dev/test only (refused in prod)."""
    policy_poll_sec: float = Field(default=30.0, ge=1.0)
    """Fallback poll of ``meta.version`` when the change stream misses or is unavailable."""
    policy_debounce_ms: int = Field(default=250, ge=0, le=10_000)
    """Change-stream events arriving within this window trigger one reload."""

    upstream_secrets_dir: str = "/etc/gateway/upstream-secrets"

    static_upstreams: Annotated[list[StaticUpstream], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_STATIC_UPSTREAMS)
    )
    """WP1 only. JSON list, e.g.
    ``[{"id":"public","url":"http://sample-public:8081/mcp"}]``."""

    tools_list_ttl_sec: int = Field(default=30, ge=1)
    """``ttlMs`` hint attached to cacheable results for modern-era clients (ADR 0002)."""

    log_level: LogLevel = "INFO"
    log_json: bool = True
    telemetry_mode: TelemetryMode = "off"

    @field_validator("static_upstreams", mode="before")
    @classmethod
    def _parse_static_upstreams(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return json.loads(v) if v else []
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("public_base_url", "mongo_uri")
    @classmethod
    def _no_trailing_slash(cls, v: str, info: ValidationInfo) -> str:
        return v.rstrip("/") if info.field_name == "public_base_url" else v

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.env != "prod":
            return self
        problems: list[str] = []
        if any(u.transport == "stdio" for u in self.static_upstreams):
            problems.append("stdio upstreams are not allowed when GATEWAY_ENV=prod")
        if not self.public_base_url.startswith("https://"):
            problems.append("GATEWAY_PUBLIC_BASE_URL must be https:// in prod")
        if not self.host_origin_protection:
            problems.append("GATEWAY_HOST_ORIGIN_PROTECTION cannot be disabled in prod")
        if not self.log_json:
            problems.append("GATEWAY_LOG_JSON must be true in prod")
        if self.policy_source != "mongo":
            problems.append("GATEWAY_POLICY_SOURCE must be mongo in prod")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def effective_allowed_hosts(self) -> list[str]:
        hosts = list(self.allowed_hosts)
        if not self.is_prod:
            for h in ("localhost", "127.0.0.1", "[::1]", "localhost:*", "127.0.0.1:*"):
                if h not in hosts:
                    hosts.append(h)
        return hosts


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, with explicit overrides (tests)."""
    return Settings(**overrides)  # type: ignore[arg-type]
