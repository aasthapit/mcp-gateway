"""An in-process OpenID provider for auth tests: RSA keys, JWKS + discovery endpoints
served by a tiny Starlette app, and token minting with arbitrary headers/claims."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx2
from joserfc import jwk, jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ISSUER = "https://idp.test/realms/unit"
AUDIENCE = "mcp-gateway"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeIdp:
    def __init__(self) -> None:
        self.issuer = ISSUER
        self.jwks_uri = self.issuer + "/protocol/openid-connect/certs"
        self.keys: dict[str, jwk.RSAKey] = {}
        self.jwks_fetches = 0
        self.discovery_fetches = 0
        self.jwks_status = 200
        self.jwks_hang = False  # True: accept the JWKS request and never answer (blackhole)
        self.discovery_issuer = self.issuer  # what the discovery document claims
        self.add_key("k1")
        self.app = Starlette(
            routes=[
                Route("/realms/unit/.well-known/openid-configuration", self._discovery),
                Route("/realms/unit/protocol/openid-connect/certs", self._jwks),
            ]
        )

    # -- keys ---------------------------------------------------------------------------

    def add_key(self, kid: str) -> jwk.RSAKey:
        key = jwk.RSAKey.generate_key(2048, parameters={"kid": kid})
        self.keys[kid] = key
        return key

    def remove_key(self, kid: str) -> None:
        del self.keys[kid]

    def jwks(self) -> dict[str, Any]:
        return {"keys": [k.as_dict(private=False) for k in self.keys.values()]}

    # -- endpoints ----------------------------------------------------------------------

    async def _discovery(self, request: Request) -> JSONResponse:
        self.discovery_fetches += 1
        return JSONResponse({"issuer": self.discovery_issuer, "jwks_uri": self.jwks_uri})

    async def _jwks(self, request: Request) -> JSONResponse:
        self.jwks_fetches += 1
        if self.jwks_hang:
            import anyio

            await anyio.sleep(3600)
        if self.jwks_status != 200:
            return JSONResponse({"error": "down"}, status_code=self.jwks_status)
        return JSONResponse(self.jwks())

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app))

    # -- tokens ---------------------------------------------------------------------------

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base: dict[str, Any] = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": "user-1",
            "azp": "mcp-gateway",
            "iat": now,
            "exp": now + 300,
            "scope": "openid profile",
            "realm_access": {"roles": ["mcp-developer"]},
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def token(
        self,
        kid: str = "k1",
        alg: str = "RS256",
        *,
        key: Any = None,
        header: dict[str, Any] | None = None,
        **claims: Any,
    ) -> str:
        h = {"alg": alg, "typ": "JWT"}
        if kid is not None:
            h["kid"] = kid
        if header:
            h.update(header)
        payload = self.claims(**claims)
        if alg == "none":
            return (
                _b64url(json.dumps(h).encode()) + "." + _b64url(json.dumps(payload).encode()) + "."
            )
        signing_key = key if key is not None else self.keys[kid]
        return jwt.encode(h, payload, signing_key, algorithms=[alg])

    def hs256_with_public_key(self, kid: str = "k1", **claims: Any) -> str:
        """The classic key-confusion attack: HS256 signed with the RSA public key PEM."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            oct_key = jwk.OctKey.import_key(self.keys[kid].as_pem(private=False))
        return self.token(kid, "HS256", key=oct_key, **claims)
