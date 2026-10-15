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

#: JWS algorithms the verifier may be configured with, mapped to their JWK key family.
#: Symmetric ``HS*`` and ``none`` are deliberately absent (RFC 8725 section 3.1).
ASYMMETRIC_JWS_ALGORITHMS: dict[str, str] = {
    "RS256": "RSA",
    "RS384": "RSA",
    "RS512": "RSA",
    "PS256": "RSA",
    "PS384": "RSA",
    "PS512": "RSA",
    "ES256": "EC",
    "ES384": "EC",
    "ES512": "EC",
    "EdDSA": "OKP",
    "Ed25519": "OKP",
    "Ed448": "OKP",
}
UpstreamsSource = Literal["mongo", "static"]


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
    session_mode: Literal["shared", "per_request"] = "per_request"
    """``shared`` reuses one connected client (only honoured with ``protocol_mode``
    ``2026-07-28`` on http; see upstreams/proxy.py)."""


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
    """``False`` turns the gateway strict: requests without a bearer token get 401."""

    # -- JWT verification (WP2; see auth/jwt.py and docs/adr/0007-jwt-verification.md) --
    oidc_issuer: str | None = "http://localhost:8180/realms/mcp"
    """Expected ``iss`` claim. The JWKS URI is discovered from
    ``<issuer>/.well-known/openid-configuration`` at startup unless ``jwks_uri`` is set.
    ``None``/empty disables token acceptance: every bearer token is rejected with 401."""
    jwks_uri: str | None = None
    """Explicit JWKS endpoint. Skips OIDC discovery; needed when the issuer URL baked into
    tokens is not reachable from the gateway (e.g. containers: issuer is the host-visible
    Keycloak URL, JWKS is fetched from the service DNS name)."""
    jwt_audience: str = "mcp-gateway"
    """``aud`` value tokens must carry (RFC 8707 resource indicator)."""
    jwt_algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["RS256"])
    """Explicit JWS ``alg`` allow-list (CSV or JSON list). Asymmetric only, one key family
    (all RSA, all EC or all OKP); ``none`` and ``HS*`` are refused."""
    jwt_clock_skew_sec: int = Field(default=30, ge=0, le=300)
    """Leeway applied to ``exp`` and ``nbf``."""
    jwks_refresh_floor_sec: int = Field(default=300, ge=0)
    """Minimum seconds between successful JWKS refreshes triggered by an unknown ``kid``."""
    oidc_discovery_timeout_sec: float = Field(default=5.0, gt=0)
    scopes_supported: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Advertised in RFC 9728 metadata (CSV or JSON list). Informational only."""

    mongo_uri: str = "mongodb://root:devpass@localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_db: str = "mcp_gateway"

    policy_source: PolicySource = "mongo"
    """``mongo``: allow-lists from MongoDB with hot reload (WP3). ``static``: allow-all,
    dev/test only (refused in prod)."""
    policy_poll_sec: float = Field(default=30.0, ge=1.0)
    """Fallback poll of ``meta.version`` when the change stream misses or is unavailable."""
    policy_debounce_ms: int = Field(default=250, ge=0, le=10_000)
    """Change-stream events arriving within this window trigger one reload."""
    # -- upstreams (WP4; see upstreams/ and docs/adr/0009-upstream-timeouts.md) ----------
    upstreams_source: UpstreamsSource = "mongo"
    """Where the upstream list comes from: the ``upstreams`` collection (``mongo``) or
    ``GATEWAY_STATIC_UPSTREAMS`` (``static``; no Mongo needed, reloads leave the static
    mounts alone). With ``mongo`` and ``GATEWAY_POLICY_SOURCE=mongo`` the upstreams come
    from the policy snapshot, so the watcher mounts/unmounts them live; with a static
    policy the standalone Mongo loader (``upstreams/source.py``) is used at startup and
    on ``POST /internal/upstreams/reload``."""
    mongo_server_selection_timeout_ms: int = Field(default=5000, ge=100, le=120_000)
    """How long the Mongo loader waits for a reachable server before giving up."""

    upstream_secrets_dir: str = "/etc/gateway/upstream-secrets"
    """``auth.secretRef`` resolves to the contents of ``<dir>/<secretRef>`` (a Kubernetes
    Secret volume); ``GATEWAY_UPSTREAM_SECRET_<REF>`` is the local-dev fallback."""

    upstream_probe_sec: float = Field(default=30.0, ge=0.2, le=3600)
    """Interval of the background health probe (``tools/list`` per upstream)."""
    upstream_probe_timeout_sec: float = Field(default=5.0, gt=0, le=600)
    """Per-upstream deadline of one probe (connect + negotiate + ``tools/list``)."""

    ready_requires_upstream: bool = True
    """``/readyz`` fails until at least one enabled upstream is mounted. A dead upstream
    that is mounted does not flip readiness."""

    internal_token: str | None = None
    """Shared secret for ``/internal/*`` (``X-Internal-Token``). Unset = routes answer 404."""

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

    @field_validator("jwt_algorithms", "scopes_supported", mode="before")
    @classmethod
    def _parse_str_list(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("jwt_algorithms")
    @classmethod
    def _check_algorithms(cls, algs: list[str]) -> list[str]:
        if not algs:
            raise ValueError("GATEWAY_JWT_ALGORITHMS must list at least one algorithm")
        families: set[str] = set()
        for alg in algs:
            if alg not in ASYMMETRIC_JWS_ALGORITHMS:
                raise ValueError(
                    f"GATEWAY_JWT_ALGORITHMS: {alg!r} is not an allowed asymmetric JWS "
                    f"algorithm (choose from {sorted(ASYMMETRIC_JWS_ALGORITHMS)})"
                )
            families.add(ASYMMETRIC_JWS_ALGORITHMS[alg])
        if len(families) > 1:
            raise ValueError(
                "GATEWAY_JWT_ALGORITHMS must stay within one key family "
                f"(got {sorted(families)}); the JWKS cache is keyed per family"
            )
        return list(dict.fromkeys(algs))

    @field_validator("oidc_issuer", "jwks_uri", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().rstrip("/")
            return v or None
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("public_base_url", "mongo_uri")
    @classmethod
    def _no_trailing_slash(cls, v: str, info: ValidationInfo) -> str:
        return v.rstrip("/") if info.field_name == "public_base_url" else v

    @field_validator("internal_token", mode="before")
    @classmethod
    def _empty_token_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.env != "prod":
            return self
        problems: list[str] = []
        if any(u.transport == "stdio" for u in self.static_upstreams):
            problems.append("stdio upstreams are not allowed when GATEWAY_ENV=prod")
        # Mongo-sourced stdio upstreams cannot be known here; UpstreamRegistry.apply()
        # refuses them at mount time (logged error + metric, never a crash).
        if not self.public_base_url.startswith("https://"):
            problems.append("GATEWAY_PUBLIC_BASE_URL must be https:// in prod")
        if not self.host_origin_protection:
            problems.append("GATEWAY_HOST_ORIGIN_PROTECTION cannot be disabled in prod")
        if not self.log_json:
            problems.append("GATEWAY_LOG_JSON must be true in prod")
        if self.oidc_issuer is None:
            problems.append("GATEWAY_OIDC_ISSUER is required in prod")
        elif not self.oidc_issuer.startswith("https://"):
            problems.append("GATEWAY_OIDC_ISSUER must be https:// in prod")
        if self.jwks_uri is not None and not self.jwks_uri.startswith("https://"):
            problems.append("GATEWAY_JWKS_URI must be https:// in prod")
        if self.policy_source != "mongo":
            problems.append("GATEWAY_POLICY_SOURCE must be mongo in prod")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def resource_url(self) -> str:
        """RFC 9728 ``resource`` identifier of the MCP endpoint."""
        return f"{self.public_base_url}{self.mcp_path}"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.public_base_url}/.well-known/oauth-protected-resource"

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
