from __future__ import annotations

from mcp_gateway.auth.principal import ANONYMOUS, Principal, current_principal, principal_var


def test_anonymous_cache_key() -> None:
    assert ANONYMOUS.cache_key == "anonymous"
    assert ANONYMOUS.is_anonymous


def test_user_cache_key_ignores_volatile_claims() -> None:
    a = Principal(
        "user",
        sub="u1",
        client_id="c",
        scopes=frozenset({"x"}),
        claims={"exp": 1, "jti": "a", "roles": ["r"]},
    )
    b = Principal(
        "user",
        sub="u1",
        client_id="c",
        scopes=frozenset({"x"}),
        claims={"exp": 2, "jti": "b", "roles": ["r"]},
    )
    c = Principal(
        "user", sub="u1", client_id="c", scopes=frozenset({"x"}), claims={"roles": ["admin"]}
    )
    assert a.cache_key == b.cache_key
    assert a.cache_key != c.cache_key
    assert a.cache_key.startswith("user:")


def test_principal_is_frozen_and_hashable() -> None:
    p = Principal("user", sub="u1")
    assert hash(p)
    assert p.audit_fields() == {"principal_kind": "user", "sub": "u1", "client_id": None}


def test_current_principal_defaults_and_contextvar() -> None:
    assert current_principal() is ANONYMOUS
    token = principal_var.set(Principal("user", sub="me"))
    try:
        assert current_principal().sub == "me"
    finally:
        principal_var.reset(token)
    assert current_principal() is ANONYMOUS
