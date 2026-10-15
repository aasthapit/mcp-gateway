from __future__ import annotations

import pytest

from mcp_gateway.settings import DEFAULT_STATIC_UPSTREAMS, Settings, load_settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = load_settings()
    assert s.env == "dev"
    assert s.port == 8080
    assert s.metrics_port == 9090
    assert s.allow_anonymous is True
    assert s.mcp_path == "/mcp"
    assert [u.id for u in s.static_upstreams] == ["sample-public", "sample-private"]
    assert s.static_upstreams[0].url == "http://localhost:8081/mcp"
    assert list(DEFAULT_STATIC_UPSTREAMS) == s.static_upstreams
    assert "localhost" in s.effective_allowed_hosts


def test_env_prefix_and_json_upstreams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_PORT", "9999")
    monkeypatch.setenv("GATEWAY_ALLOW_ANONYMOUS", "false")
    monkeypatch.setenv("GATEWAY_LOG_LEVEL", "debug")
    monkeypatch.setenv(
        "GATEWAY_STATIC_UPSTREAMS",
        '[{"id":"a","url":"http://a:1/mcp","cache_ttl_sec":5,"protocol_mode":"2026-07-28"}]',
    )
    s = load_settings()
    assert s.port == 9999
    assert s.allow_anonymous is False
    assert s.log_level == "DEBUG"
    assert len(s.static_upstreams) == 1
    assert s.static_upstreams[0].cache_ttl_sec == 5
    assert s.static_upstreams[0].protocol_mode == "2026-07-28"


def test_empty_static_upstreams_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_STATIC_UPSTREAMS", "")
    assert load_settings().static_upstreams == []


def test_invalid_upstream_id_rejected() -> None:
    with pytest.raises(ValueError):
        load_settings(static_upstreams=[{"id": "Bad_Id", "url": "http://x/mcp"}])


def test_prod_rejects_stdio_and_http_base_url() -> None:
    with pytest.raises(ValueError, match="stdio"):
        Settings(
            env="prod",
            public_base_url="https://mcp.example.com",
            static_upstreams=[{"id": "local", "url": "http://x/mcp", "transport": "stdio"}],
        )
    with pytest.raises(ValueError, match="https"):
        Settings(env="prod", public_base_url="http://mcp.example.com", static_upstreams=[])
    with pytest.raises(ValueError, match="HOST_ORIGIN"):
        Settings(
            env="prod",
            public_base_url="https://mcp.example.com",
            host_origin_protection=False,
            static_upstreams=[],
        )


def test_prod_ok_and_no_localhost_hosts() -> None:
    s = Settings(
        env="prod",
        public_base_url="https://mcp.example.com/",
        allowed_hosts=["mcp.example.com"],
        static_upstreams=[],
        oidc_issuer="https://idp.example.com/realms/mcp/",
    )
    assert s.is_prod
    assert s.public_base_url == "https://mcp.example.com"
    assert s.effective_allowed_hosts == ["mcp.example.com"]
    assert s.oidc_issuer == "https://idp.example.com/realms/mcp"
    assert s.resource_url == "https://mcp.example.com/mcp"
    assert s.resource_metadata_url == "https://mcp.example.com/.well-known/oauth-protected-resource"


def test_prod_requires_https_issuer_and_jwks() -> None:
    with pytest.raises(ValueError, match="OIDC_ISSUER is required"):
        Settings(
            env="prod", public_base_url="https://m.example.com", static_upstreams=[], oidc_issuer=""
        )
    with pytest.raises(ValueError, match="OIDC_ISSUER must be https"):
        Settings(
            env="prod",
            public_base_url="https://m.example.com",
            static_upstreams=[],
            oidc_issuer="http://idp.example.com",
        )
    with pytest.raises(ValueError, match="JWKS_URI must be https"):
        Settings(
            env="prod",
            public_base_url="https://m.example.com",
            static_upstreams=[],
            oidc_issuer="https://idp.example.com",
            jwks_uri="http://idp.internal/certs",
        )


def test_jwt_algorithm_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(jwt_algorithms="RS256, PS256,RS256").jwt_algorithms == ["RS256", "PS256"]
    assert Settings(jwt_algorithms='["ES256"]').jwt_algorithms == ["ES256"]
    monkeypatch.setenv("GATEWAY_JWT_ALGORITHMS", "RS512")
    monkeypatch.setenv("GATEWAY_SCOPES_SUPPORTED", "openid,mcp:tools")
    s = Settings()
    assert s.jwt_algorithms == ["RS512"]
    assert s.scopes_supported == ["openid", "mcp:tools"]
    for bad in ("none", "HS256", "RS256,none", "RS256,ES256", ""):
        with pytest.raises(ValueError):
            Settings(jwt_algorithms=bad)
