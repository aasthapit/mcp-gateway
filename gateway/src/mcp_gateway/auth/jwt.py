"""JWT verification built on FastMCP 4.1's ``JWTVerifier``.

What FastMCP gives us (``fastmcp.server.auth.providers.jwt``): JWKS fetching with a
1 h whole-set cache keyed by ``kid``, refresh on an unknown ``kid`` throttled by
``jwks_refresh_interval`` (30 s default, applied to failed attempts too), a single
``algorithm``, ``exp`` without leeway, no ``nbf`` check. What this module adds, see
docs/adr/0007-jwt-verification.md:

* an explicit ``alg`` allow-list (one key family; never ``none`` or ``HS*``),
* ``exp``/``nbf`` with a small clock skew, required ``exp``,
* ``iss``/``aud`` checks with a typed rejection reason for logs and metrics,
* unknown-``kid`` refresh with a 5 min floor on *successful* refreshes, a short backoff
  after failed fetches, and a stale-cache grace so an IdP outage never logs callers out
  whose key we already hold,
* JWKS URI discovery from ``<issuer>/.well-known/openid-configuration`` at startup with
  an explicit ``GATEWAY_JWKS_URI`` override.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import (
    JWTVerifier,
    _has_unsupported_critical_headers,
    _import_key_for_algorithm,
)
from fastmcp.utilities.auth import decode_jwt_header
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jws import JWSRegistry

from mcp_gateway.auth.principal import Principal
from mcp_gateway.settings import Settings
from mcp_gateway.telemetry import get_logger

log = get_logger(__name__)

#: Seconds a failed JWKS fetch blocks further fetch attempts (keeps a down IdP from being
#: hammered once per request without turning an outage into a five minute lock-out).
JWKS_FAILURE_BACKOFF_SEC = 5.0


class TokenInvalid(Exception):
    """A bearer token was presented but must not be accepted. ``reason`` is a stable,
    low-cardinality code for logs; never shown to clients beyond ``error_description``."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class GatewayJWTVerifier(JWTVerifier):
    """``JWTVerifier`` with an algorithm allow-list, skew-aware time checks and a
    rotation-friendly JWKS refresh policy."""

    def __init__(
        self,
        *,
        jwks_uri: str,
        issuer: str,
        audience: str,
        algorithms: list[str],
        clock_skew_sec: int = 30,
        jwks_refresh_floor_sec: float = 300.0,
        jwks_fetch_timeout_sec: float = 3.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not algorithms:
            raise ValueError("algorithms must not be empty")
        for alg in algorithms:
            if alg == "none" or alg.startswith("HS"):
                raise ValueError(f"algorithm {alg!r} is not allowed for bearer tokens")
        super().__init__(
            jwks_uri=jwks_uri,
            issuer=issuer,
            audience=audience,
            algorithm=algorithms[0],  # selects the JWK key family for the JWKS cache
            http_client=http_client,
            jwks_refresh_interval=0,  # we implement our own throttle below
        )
        self.algorithms = list(algorithms)
        self.clock_skew = float(clock_skew_sec)
        self.jwks_refresh_floor = float(jwks_refresh_floor_sec)
        self.jwks_fetch_timeout = float(jwks_fetch_timeout_sec)
        self._registry = JWSRegistry(algorithms=self.algorithms, strict_check_header=False)
        self._last_refresh_ok: float | None = None
        self._last_refresh_failed: float | None = None
        self.jwks_fetches = 0  # observability / tests

    # -- JWKS cache policy ---------------------------------------------------------------

    def _cached_key(self, kid: str | None, *, allow_stale: bool) -> str | None:
        fresh = time.monotonic() - self._jwks_cache_time < self._cache_ttl
        if not fresh and not allow_stale:
            return None
        if kid:
            return self._jwks_cache.get(kid)
        if len(self._jwks_cache) == 1:
            return next(iter(self._jwks_cache.values()))
        return None

    async def _get_jwks_key(self, kid: str | None) -> str:
        """Resolve the verification key for ``kid``.

        Fresh cache hit → key. Otherwise refresh the set, but at most once per
        ``jwks_refresh_floor`` seconds after a successful refresh (an attacker sending
        random ``kid`` values cannot turn the gateway into a JWKS flood) and not within
        :data:`JWKS_FAILURE_BACKOFF_SEC` of a failed fetch. When the refresh is throttled
        or fails, a stale cached key for the same ``kid`` is still accepted.
        """
        if (key := self._cached_key(kid, allow_stale=False)) is not None:
            return key
        if (
            self._jwks_refresh_lock.locked()
            and (stale := self._cached_key(kid, allow_stale=True)) is not None
        ):
            # Another request is refreshing (maybe against an unreachable IdP): a known
            # key keeps verifying instead of queueing behind that fetch.
            return stale
        async with self._jwks_refresh_lock:
            if (key := self._cached_key(kid, allow_stale=False)) is not None:
                return key
            now = time.monotonic()
            since_ok = (
                now - self._last_refresh_ok if self._last_refresh_ok is not None else math.inf
            )
            since_failed = (
                now - self._last_refresh_failed
                if self._last_refresh_failed is not None
                else math.inf
            )
            throttled = (
                since_ok < self.jwks_refresh_floor or since_failed < JWKS_FAILURE_BACKOFF_SEC
            )
            if throttled:
                if (key := self._cached_key(kid, allow_stale=True)) is not None:
                    return key
                raise TokenInvalid(
                    "unknown_kid",
                    f"key id {kid!r} not in the cached JWKS and refresh is throttled",
                )
            cache_time_before = self._jwks_cache_time
            try:
                self.jwks_fetches += 1
                return await self._refresh_jwks_key(kid)
            except ValueError as exc:
                fetched = self._jwks_cache_time != cache_time_before
                if fetched:
                    self._last_refresh_ok = time.monotonic()
                    raise TokenInvalid("unknown_kid", str(exc)) from exc
                self._last_refresh_failed = time.monotonic()
                log.warning("jwks fetch failed", jwks_uri=self.jwks_uri, error=str(exc))
                if (key := self._cached_key(kid, allow_stale=True)) is not None:
                    return key
                raise TokenInvalid("jwks_unavailable", str(exc)) from exc
            finally:
                if self._jwks_cache_time != cache_time_before:
                    self._last_refresh_ok = time.monotonic()

    async def _fetch_jwks(self) -> dict[str, Any]:
        """FastMCP's fetch, bounded by ``jwks_fetch_timeout`` (theirs is 10 s per phase).

        A timeout surfaces as ``httpx2.TimeoutException`` so the refresh path treats it like
        any other failed fetch (backoff, stale key for known ``kid``, 401 otherwise)."""
        try:
            with anyio.fail_after(self.jwks_fetch_timeout):
                if self._http_client is not None:
                    return await super()._fetch_jwks()
                timeout = httpx2.Timeout(self.jwks_fetch_timeout)
                async with httpx2.AsyncClient(timeout=timeout) as client:
                    response = await client.get(self.jwks_uri)
                    response.raise_for_status()
                    return response.json()
        except TimeoutError as exc:
            raise httpx2.ReadTimeout(f"JWKS fetch exceeded {self.jwks_fetch_timeout:g}s") from exc

    # -- verification ----------------------------------------------------------------------

    async def verify(self, token: str) -> AccessToken:
        """Verify ``token`` or raise :class:`TokenInvalid` with a reason code."""
        try:
            header = decode_jwt_header(token)
        except (ValueError, TypeError) as exc:
            raise TokenInvalid("malformed", "not a JWS compact serialization") from exc
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise TokenInvalid("alg_not_allowed", f"alg {alg!r} is not accepted")
        if _has_unsupported_critical_headers(header):
            raise TokenInvalid("unsupported_crit", "unsupported critical header")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise TokenInvalid("malformed", "kid must be a string")

        pem = await self._get_jwks_key(kid)
        try:
            key = _import_key_for_algorithm(pem, self.algorithm)
            claims = jwt.decode(
                token, key, algorithms=self.algorithms, registry=self._registry
            ).claims
        except JoseError as exc:
            raise TokenInvalid("bad_signature", "signature or format invalid") from exc
        except (ValueError, TypeError) as exc:
            raise TokenInvalid("malformed", str(exc)) from exc

        now = time.time()
        exp = claims.get("exp")
        if not isinstance(exp, int | float):
            raise TokenInvalid("missing_exp", "exp claim is required")
        if exp + self.clock_skew < now:
            raise TokenInvalid("expired", "token expired")
        nbf = claims.get("nbf")
        if nbf is not None:
            if not isinstance(nbf, int | float):
                raise TokenInvalid("malformed", "nbf must be numeric")
            if nbf - self.clock_skew > now:
                raise TokenInvalid("not_yet_valid", "token not yet valid")
        if not self._validate_issuer(claims.get("iss")):
            raise TokenInvalid("wrong_issuer", "issuer mismatch")
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        if self.audience not in aud_list:
            raise TokenInvalid("wrong_audience", "audience mismatch")

        client_id = claims.get("client_id") or claims.get("azp")
        return AccessToken(
            token=token,
            client_id=str(client_id) if client_id else "",
            scopes=self._extract_scopes(claims),
            expires_at=int(exp),
            subject=claims.get("sub"),
            claims=claims,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """FastMCP ``TokenVerifier`` protocol: ``None`` instead of an exception."""
        try:
            return await self.verify(token)
        except TokenInvalid as exc:
            log.debug("token rejected", reason=exc.reason, detail=exc.detail)
            return None


def principal_from_token(access: AccessToken) -> Principal:
    claims = dict(access.claims or {})
    client_id = access.client_id or None
    return Principal(
        kind="user",
        sub=access.subject,
        client_id=client_id,
        scopes=frozenset(access.scopes),
        claims=claims,
    )


# -- discovery and lifecycle ------------------------------------------------------------------


async def discover_jwks_uri(
    issuer: str, *, timeout: float = 5.0, http_client: httpx2.AsyncClient | None = None
) -> str:
    """Fetch ``<issuer>/.well-known/openid-configuration`` and return its ``jwks_uri``.

    The document's ``issuer`` must equal the configured one (OpenID Connect Discovery 1.0
    section 4.3); a mismatch is treated as a misconfiguration, not silently accepted.
    """
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"

    async def fetch(client: httpx2.AsyncClient) -> dict[str, Any]:
        response = await client.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()

    try:
        if http_client is not None:
            doc = await fetch(http_client)
        else:
            async with httpx2.AsyncClient() as client:
                doc = await fetch(client)
    except httpx2.HTTPError as exc:
        raise RuntimeError(f"OIDC discovery failed at {url}: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"OIDC discovery at {url} returned invalid JSON: {exc}") from exc
    if doc.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
        raise RuntimeError(
            f"OIDC discovery issuer mismatch: configured {issuer!r}, document says "
            f"{doc.get('issuer')!r}"
        )
    jwks_uri = doc.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri.startswith(("http://", "https://")):
        raise RuntimeError(f"OIDC discovery at {url} has no usable jwks_uri")
    return jwks_uri


#: Seconds between attempts to (re)configure the verifier after a failed discovery.
DISCOVERY_RETRY_SEC = 30.0


@dataclass
class JwtAuth:
    """Owns the verifier for the process. Configured once at startup (OIDC discovery);
    when discovery fails in dev the next bearer token retries it, so a late-starting
    local Keycloak does not require a gateway restart."""

    settings: Settings
    http_client: httpx2.AsyncClient | None = None
    verifier: GatewayJWTVerifier | None = None
    last_error: str | None = None
    jwks_uri: str | None = None
    _lock: anyio.Lock | None = None
    _last_attempt: float | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.oidc_issuer is not None

    async def configure(self) -> GatewayJWTVerifier:
        """Resolve the JWKS URI and build the verifier. Raises on failure."""
        s = self.settings
        if s.oidc_issuer is None:
            raise RuntimeError("GATEWAY_OIDC_ISSUER is not set; bearer tokens cannot be verified")
        self._last_attempt = time.monotonic()
        try:
            jwks_uri = s.jwks_uri or await discover_jwks_uri(
                s.oidc_issuer, timeout=s.oidc_discovery_timeout_sec, http_client=self.http_client
            )
        except RuntimeError as exc:
            self.last_error = str(exc)
            raise
        verifier = GatewayJWTVerifier(
            jwks_uri=jwks_uri,
            issuer=s.oidc_issuer,
            audience=s.jwt_audience,
            algorithms=s.jwt_algorithms,
            clock_skew_sec=s.jwt_clock_skew_sec,
            jwks_refresh_floor_sec=s.jwks_refresh_floor_sec,
            jwks_fetch_timeout_sec=s.jwks_fetch_timeout_sec,
            http_client=self.http_client,
        )
        self.verifier, self.jwks_uri, self.last_error = verifier, jwks_uri, None
        log.info(
            "jwt verification configured",
            issuer=s.oidc_issuer,
            jwks_uri=jwks_uri,
            audience=s.jwt_audience,
            algorithms=s.jwt_algorithms,
            discovered=s.jwks_uri is None,
        )
        return verifier

    async def startup(self) -> None:
        """Called from the app lifespan. Fatal in prod, a warning in dev."""
        if not self.enabled:
            log.warning("GATEWAY_OIDC_ISSUER unset: every bearer token will be rejected")
            return
        try:
            await self.configure()
        except RuntimeError as exc:
            if self.settings.is_prod:
                raise
            log.warning("jwt verification not configured yet; will retry on demand", error=str(exc))

    async def verify(self, token: str) -> AccessToken:
        verifier = self.verifier
        if verifier is None:
            if not self.enabled:
                raise TokenInvalid("not_configured", "token verification is not configured")
            if self._lock is None:
                self._lock = anyio.Lock()
            async with self._lock:
                verifier = self.verifier
                if verifier is None:
                    recently = (
                        self._last_attempt is not None
                        and time.monotonic() - self._last_attempt < DISCOVERY_RETRY_SEC
                    )
                    if recently:
                        raise TokenInvalid("not_configured", self.last_error or "discovery pending")
                    try:
                        verifier = await self.configure()
                    except RuntimeError as exc:
                        raise TokenInvalid("not_configured", str(exc)) from exc
        return await verifier.verify(token)

    def probe(self) -> tuple[bool, dict[str, Any]]:
        """Readiness probe: configured verifier (or auth deliberately disabled)."""
        info: dict[str, Any] = {
            "issuer": self.settings.oidc_issuer,
            "jwks_uri": self.jwks_uri,
            "configured": self.verifier is not None,
        }
        if self.last_error:
            info["error"] = self.last_error
        return (self.verifier is not None or not self.enabled), info
