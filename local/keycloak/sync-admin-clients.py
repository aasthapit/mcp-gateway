#!/usr/bin/env python3
"""Bring an already-imported `mcp` realm up to date with the admin clients in realm.json.

Keycloak imports realm.json only on the first start (`--import-realm` skips existing
realms), so a stack created before WP5 lacks the `mcp-admin` / `mcp-admin-ui` clients.
This script adds them through the admin REST API, idempotently and without touching
anything else (no user or role is recreated):

  1. clients `mcp-admin` and `mcp-admin-ui` (created from realm.json when missing)
  2. client role `mcp-admin/policy-admin`
  3. realm role `mcp-admin` made composite with that client role (aud=mcp-admin for admins)

Run by `make up`; standalone: `python3 local/keycloak/sync-admin-clients.py`.
Env: KEYCLOAK_PORT (8180), KC_BOOTSTRAP_ADMIN_USERNAME / _PASSWORD (admin/admin).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REALM = "mcp"
CLIENTS = ("mcp-admin", "mcp-admin-ui")
CLIENT_ROLE = ("mcp-admin", "policy-admin")
REALM_ROLE = "mcp-admin"

base = f"http://localhost:{os.environ.get('KEYCLOAK_PORT', '8180')}"
realm_file = Path(__file__).with_name("realm.json")


def call(
    method: str,
    path: str,
    token: str | None = None,
    body: object | None = None,
    form: dict | None = None,
):
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as res:
        raw = res.read()
        return json.loads(raw) if raw else None


def main() -> int:
    realm = json.loads(realm_file.read_text())
    try:
        token = call(
            "POST",
            "/realms/master/protocol/openid-connect/token",
            form={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin"),
                "password": os.environ.get("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin"),
            },
        )["access_token"]
    except (urllib.error.URLError, KeyError) as exc:
        print(f"keycloak admin login at {base} failed: {exc}", file=sys.stderr)
        return 1
    admin = f"/admin/realms/{REALM}"
    changed: list[str] = []

    for client_id in CLIENTS:
        found = call("GET", f"{admin}/clients?clientId={client_id}", token)
        if not found:
            rep = next(c for c in realm["clients"] if c["clientId"] == client_id)
            call("POST", f"{admin}/clients", token, rep)
            changed.append(f"client {client_id}")

    client_uuid = call("GET", f"{admin}/clients?clientId={CLIENT_ROLE[0]}", token)[0]["id"]
    try:
        role = call("GET", f"{admin}/clients/{client_uuid}/roles/{CLIENT_ROLE[1]}", token)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        rep = next(
            r for r in realm["roles"]["client"][CLIENT_ROLE[0]] if r["name"] == CLIENT_ROLE[1]
        )
        call("POST", f"{admin}/clients/{client_uuid}/roles", token, rep)
        role = call("GET", f"{admin}/clients/{client_uuid}/roles/{CLIENT_ROLE[1]}", token)
        changed.append(f"client role {'/'.join(CLIENT_ROLE)}")

    composites = call("GET", f"{admin}/roles/{REALM_ROLE}/composites", token) or []
    if not any(c["id"] == role["id"] for c in composites):
        call("POST", f"{admin}/roles/{REALM_ROLE}/composites", token, [role])
        changed.append(f"realm role {REALM_ROLE} -> composite {'/'.join(CLIENT_ROLE)}")

    print("keycloak admin clients: " + (", ".join(changed) if changed else "up to date"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
