"""Verifier-level tests: GatewayJWTVerifier + discovery against the in-process FakeIdp."""

from __future__ import annotations

import time

import pytest
from joserfc import jwk

from mcp_gateway.auth.jwt import (
    JWKS_FAILURE_BACKOFF_SEC,
    GatewayJWTVerifier,
    JwtAuth,
    TokenInvalid,
    discover_jwks_uri,
    principal_from_token,
)
from mcp_gateway.settings import Settings
from tests.jwt_support import AUDIENCE, FakeIdp


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def make_verifier(idp: FakeIdp, **kw: object) -> GatewayJWTVerifier:
    params: dict[str, object] = {
        "jwks_uri": idp.jwks_uri,
        "issuer": idp.issuer,
        "audience": AUDIENCE,
        "algorithms": ["RS256"],
        "clock_skew_sec": 30,
        "jwks_refresh_floor_sec": 300,
        "http_client": idp.client(),
    }
    params.update(kw)
    return GatewayJWTVerifier(**params)  # type: ignore[arg-type]


async def reason(verifier: GatewayJWTVerifier, token: str) -> str:
    with pytest.raises(TokenInvalid) as exc:
        await verifier.verify(token)
    return exc.value.reason


async def test_valid_token_yields_principal(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    access = await v.verify(idp.token())
    p = principal_from_token(access)
    assert p.kind == "user"
    assert p.sub == "user-1"
    assert p.client_id == "mcp-gateway"
    assert p.scopes == frozenset({"openid", "profile"})
    assert p.claims["realm_access"]["roles"] == ["mcp-developer"]
    assert idp.jwks_fetches == 1
    await v.verify(idp.token())
    assert idp.jwks_fetches == 1  # cached
    assert await v.verify_token(idp.token()) is not None  # FastMCP protocol still works
    assert await v.verify_token("nope") is None


async def test_client_id_claim_beats_azp(idp: FakeIdp) -> None:
    access = await make_verifier(idp).verify(idp.token(client_id="svc", azp="other"))
    assert principal_from_token(access).client_id == "svc"


async def test_expired_within_skew_ok_beyond_rejected(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    now = int(time.time())
    await v.verify(idp.token(exp=now - 10))  # inside the 30 s skew
    assert await reason(v, idp.token(exp=now - 60)) == "expired"
    assert await reason(v, idp.token(exp=None)) == "missing_exp"


async def test_nbf_in_future(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    now = int(time.time())
    await v.verify(idp.token(nbf=now + 10))
    assert await reason(v, idp.token(nbf=now + 120)) == "not_yet_valid"


async def test_wrong_audience_and_issuer(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    assert await reason(v, idp.token(aud="other-api")) == "wrong_audience"
    assert await reason(v, idp.token(aud=["other-api", "account"])) == "wrong_audience"
    await v.verify(idp.token(aud=["other-api", AUDIENCE]))  # list containing us is fine
    assert await reason(v, idp.token(aud=None)) == "wrong_audience"
    assert await reason(v, idp.token(iss="https://evil.test")) == "wrong_issuer"
    assert await reason(v, idp.token(iss=None)) == "wrong_issuer"


async def test_alg_none_rejected(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    assert await reason(v, idp.token(alg="none")) == "alg_not_allowed"
    assert idp.jwks_fetches == 0  # rejected before any key lookup


async def test_hs256_with_public_key_as_secret_rejected(idp: FakeIdp) -> None:
    v = make_verifier(idp)
    assert await reason(v, idp.hs256_with_public_key()) == "alg_not_allowed"
    # Even a verifier that (hypothetically) listed RS256 and PS256 never accepts HS*.
    with pytest.raises(ValueError):
        make_verifier(idp, algorithms=["RS256", "HS256"])
    with pytest.raises(ValueError):
        make_verifier(idp, algorithms=["none"])


async def test_allowed_alg_but_wrong_family_in_list_is_rejected_by_signature(idp: FakeIdp) -> None:
    """A token signed by a key we do not hold must fail even with the right alg."""
    v = make_verifier(idp)
    rogue = jwk.RSAKey.generate_key(2048, parameters={"kid": "k1"})
    assert await reason(v, idp.token("k1", key=rogue)) == "bad_signature"
    assert await reason(v, "not.a.jwt") == "malformed"
    assert await reason(v, "") == "malformed"


async def test_unknown_kid_triggers_refresh_then_succeeds(idp: FakeIdp) -> None:
    v = make_verifier(idp, jwks_refresh_floor_sec=0)
    await v.verify(idp.token("k1"))
    assert idp.jwks_fetches == 1
    idp.add_key("k2")  # key rotation at the IdP
    await v.verify(idp.token("k2"))
    assert idp.jwks_fetches == 2
    await v.verify(idp.token("k1"))  # old key still cached from the refreshed set
    assert idp.jwks_fetches == 2


async def test_unknown_kid_refresh_is_floored(idp: FakeIdp) -> None:
    v = make_verifier(idp, jwks_refresh_floor_sec=300)
    await v.verify(idp.token("k1"))
    assert idp.jwks_fetches == 1
    idp.add_key("k2")
    # The first unknown kid after startup is allowed to refresh (no successful refresh
    # happened within the floor yet besides the initial fetch -> it counts). So k2 works
    # only if the floor has elapsed; here it has not: the initial fetch was just now.
    assert await reason(v, idp.token("k2")) == "unknown_kid"
    assert idp.jwks_fetches == 1
    # Random kids cannot make the gateway hammer the JWKS endpoint.
    for i in range(20):
        assert await reason(v, idp.token("k1", header={"kid": f"random-{i}"})) == "unknown_kid"
    assert idp.jwks_fetches == 1
    # After the floor passes, the refresh happens and k2 verifies.
    v._last_refresh_ok = time.monotonic() - 301
    await v.verify(idp.token("k2"))
    assert idp.jwks_fetches == 2


async def test_jwks_outage_keeps_known_keys_and_backs_off(idp: FakeIdp) -> None:
    v = make_verifier(idp, jwks_refresh_floor_sec=0)
    await v.verify(idp.token("k1"))
    idp.jwks_status = 503
    v._jwks_cache_time -= v._cache_ttl + 1  # make the cache stale
    await v.verify(idp.token("k1"))  # stale key still accepted while the IdP is down
    assert idp.jwks_fetches == 2
    idp.add_key("k2")
    assert await reason(v, idp.token("k2")) == "unknown_kid"  # backed off, no fetch
    assert idp.jwks_fetches == 2
    v._last_refresh_failed = time.monotonic() - JWKS_FAILURE_BACKOFF_SEC - 1
    assert await reason(v, idp.token("k2")) == "jwks_unavailable"
    assert idp.jwks_fetches == 3


async def test_unknown_kid_after_fresh_refresh_is_rejected(idp: FakeIdp) -> None:
    v = make_verifier(idp, jwks_refresh_floor_sec=0)
    assert await reason(v, idp.token("k1", header={"kid": "ghost"})) == "unknown_kid"
    assert idp.jwks_fetches == 1


async def test_discovery(idp: FakeIdp) -> None:
    assert await discover_jwks_uri(idp.issuer, http_client=idp.client()) == idp.jwks_uri
    # a trailing slash is not a mismatch
    assert await discover_jwks_uri(idp.issuer + "/", http_client=idp.client()) == idp.jwks_uri
    idp.discovery_issuer = "https://idp.test/realms/other"
    with pytest.raises(RuntimeError, match="issuer mismatch"):
        await discover_jwks_uri(idp.issuer, http_client=idp.client())
    idp.discovery_issuer = idp.issuer
    with pytest.raises(RuntimeError, match="discovery failed"):
        await discover_jwks_uri("https://idp.test/realms/missing", http_client=idp.client())


async def test_jwt_auth_lifecycle(idp: FakeIdp) -> None:
    s = Settings(oidc_issuer=idp.issuer, metrics_port=0)
    auth = JwtAuth(s, http_client=idp.client())
    assert auth.probe()[0] is False
    await auth.startup()
    assert auth.jwks_uri == idp.jwks_uri
    assert auth.probe() == (
        True,
        {"issuer": idp.issuer, "jwks_uri": idp.jwks_uri, "configured": True},
    )
    assert (await auth.verify(idp.token())).subject == "user-1"

    explicit = JwtAuth(Settings(oidc_issuer=idp.issuer, jwks_uri=idp.jwks_uri), idp.client())
    await explicit.startup()
    assert idp.discovery_fetches == 1  # explicit JWKS URI skips discovery

    disabled = JwtAuth(Settings(oidc_issuer=""), idp.client())
    await disabled.startup()
    assert disabled.probe()[0] is True  # deliberately disabled is "ready"
    with pytest.raises(TokenInvalid, match="not configured"):
        await disabled.verify(idp.token())


async def test_jwt_auth_dev_retries_discovery_lazily(idp: FakeIdp) -> None:
    s = Settings(oidc_issuer="https://idp.test/realms/missing", metrics_port=0)
    auth = JwtAuth(s, http_client=idp.client())
    await auth.startup()  # dev: warning, not fatal
    assert auth.verifier is None and auth.last_error
    with pytest.raises(TokenInvalid) as exc:
        await auth.verify(idp.token())
    assert exc.value.reason == "not_configured"
    auth._last_attempt = None  # pretend the retry interval elapsed
    auth.settings = Settings(oidc_issuer=idp.issuer, metrics_port=0)  # IdP "came up"
    assert (await auth.verify(idp.token())).subject == "user-1"


async def test_jwt_auth_prod_startup_is_fatal(idp: FakeIdp) -> None:
    s = Settings(
        env="prod",
        public_base_url="https://mcp.example.com",
        static_upstreams=[],
        oidc_issuer="https://idp.test/realms/missing",
    )
    with pytest.raises(RuntimeError, match="discovery failed"):
        await JwtAuth(s, http_client=idp.client()).startup()
