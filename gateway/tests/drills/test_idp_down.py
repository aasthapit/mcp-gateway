"""Drill: the IdP (Keycloak) becomes unreachable.

The gateway fetches the JWKS through a TCP proxy in front of the local Keycloak, which
the drill cuts in two ways: ``refuse`` (connection refused, e.g. pods gone) and
``blackhole`` (accepts, never answers, e.g. a dropped route/firewall). The JWKS cache TTL is
shortened to 1 s so every request exercises the expired-cache path. Expected:

* tokens whose ``kid`` is cached keep verifying, without added latency;
* a token with an unknown ``kid`` (a key the IdP rotated in, or a forgery) is rejected
  with 401, after at most one bounded fetch (``GATEWAY_JWKS_FETCH_TIMEOUT_SEC``), never by
  hanging the request; repeated ones are answered at once (failure backoff);
* when the IdP is back, verification continues without a restart.
"""

from __future__ import annotations

import concurrent.futures
import time

import httpx
import pytest
from joserfc import jwk, jwt

from tests.conftest import port_open
from tests.drills.support import FaultProxy, call, drill_settings, running_gateway

pytestmark = pytest.mark.drill

KC = "http://localhost:8180/realms/mcp"
FETCH_TIMEOUT = 2.0


def keycloak_token() -> str:
    r = httpx.post(
        f"{KC}/protocol/openid-connect/token",
        data={
            "client_id": "mcp-gateway",
            "grant_type": "password",
            "username": "dev1",
            "password": "dev1",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def forged_token(kid: str) -> str:
    """Well-formed, right iss/aud, signed by a key the IdP never published."""
    key = jwk.RSAKey.generate_key(2048, parameters={"kid": kid})
    now = int(time.time())
    claims = {"iss": KC, "aud": "mcp-gateway", "sub": "mallory", "exp": now + 300, "iat": now}
    return jwt.encode({"alg": "RS256", "kid": kid, "typ": "JWT"}, claims, key, algorithms=["RS256"])


def whoami(base: str, token: str) -> tuple[int, float, str | None]:
    t0 = time.monotonic()
    try:
        msg = call(base, "gateway_whoami", {}, token=token, timeout=30)
        kind = msg["result"]["structuredContent"]["kind"] if "result" in msg else None
        return 200, time.monotonic() - t0, kind
    except httpx.HTTPStatusError as exc:
        return exc.response.status_code, time.monotonic() - t0, None


@pytest.mark.parametrize("outage", ["refuse", "blackhole"])
def test_idp_outage_cached_keys_verify_unknown_kid_rejected_fast(outage: str) -> None:
    if not port_open("127.0.0.1", 8180):
        pytest.skip("Keycloak on :8180 not running (make up)")
    token = keycloak_token()
    proxy = FaultProxy("127.0.0.1", 8180)
    settings = drill_settings(
        policy_source="static",
        upstreams_source="static",
        static_upstreams=[],
        ready_requires_upstream=False,
        oidc_issuer=KC,
        jwks_uri=f"http://127.0.0.1:{proxy.port}/realms/mcp/protocol/openid-connect/certs",
        jwks_fetch_timeout_sec=FETCH_TIMEOUT,
        jwks_refresh_floor_sec=0,
    )
    try:
        with running_gateway(settings) as (base, gw):
            assert whoami(base, token)[::2] == (200, "user")
            verifier = gw.jwt_auth.verifier
            assert verifier is not None
            verifier._cache_ttl = 1.0  # every request below sees an expired cache

            proxy.set_mode(outage)
            time.sleep(1.1)
            # Known kid: many concurrent requests, all verified, none waits for the IdP.
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda _: whoami(base, token), range(40)))
            assert all(status == 200 and kind == "user" for status, _, kind in results), results
            slowest = max(sec for _, sec, _ in results)
            assert slowest < 1.0, f"a cached-key request waited {slowest:.2f}s on the IdP"

            # Unknown kid: 401 after at most one bounded fetch, then immediately.
            status, took, _ = whoami(base, forged_token("rotated-in-during-outage"))
            assert status == 401
            assert took < FETCH_TIMEOUT + 1.5, f"unknown kid took {took:.1f}s"
            status, again, _ = whoami(base, forged_token("another-unknown"))
            assert status == 401 and again < 0.5, f"backoff not applied ({again:.2f}s)"
            print(
                f"\n  IdP {outage}: 40 cached-key calls ok (slowest {slowest * 1000:.0f} ms); "
                f"unknown kid 401 in {took * 1000:.0f} ms, then {again * 1000:.0f} ms"
            )

            proxy.set_mode("up")  # IdP back: still verifying, refresh succeeds again
            time.sleep(5.5)  # past the failure backoff
            fetches = verifier.jwks_fetches
            assert whoami(base, token)[::2] == (200, "user")
            deadline = time.monotonic() + 5
            while verifier.jwks_fetches == fetches and time.monotonic() < deadline:
                time.sleep(0.1)
            assert verifier.jwks_fetches > fetches
            assert verifier._last_refresh_ok is not None
    finally:
        proxy.close()
