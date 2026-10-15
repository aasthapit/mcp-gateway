"""Table tests for subject matching and globs; property tests for the set semantics of
docs/PLAN.md section 3.2 (deny wins, anonymous is the floor, deny by default)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_gateway.auth.principal import ANONYMOUS, Principal
from mcp_gateway.policy.matcher import GlobToolFilter, resolve_claim, subject_matches
from mcp_gateway.policy.models import (
    AnonymousSubject,
    AuthenticatedSubject,
    ClaimSubject,
    ClientIdSubject,
    GrantDoc,
    ToolsetDoc,
)
from mcp_gateway.policy.snapshot import PolicySnapshot, build_snapshot

DEV = Principal(
    "user",
    sub="dev1",
    client_id="mcp-gateway",
    claims={
        "realm_access": {"roles": ["mcp-developer", "offline_access"]},
        "resource_access": {"mcp-gateway": {"roles": ["viewer"]}},
        "scope": "openid profile email",
        "https://example.com/tenant": "acme",
        "level": 3,
        "flag": True,
        "nothing": None,
    },
)
CI = Principal("user", sub="svc", client_id="ci-runner", claims={"azp": "ci-runner"})
AZP_ONLY = Principal("user", sub="x", claims={"azp": "ci-runner"})


# -- claim paths -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("realm_access.roles", (True, ["mcp-developer", "offline_access"])),
        ("resource_access.mcp-gateway.roles", (True, ["viewer"])),
        ("https://example.com/tenant", (True, "acme")),
        ("scope", (True, "openid profile email")),
        ("nothing", (True, None)),
        ("missing", (False, None)),
        ("realm_access.missing", (False, None)),
        ("scope.deeper", (False, None)),
    ],
)
def test_resolve_claim(path: str, expected: tuple[bool, Any]) -> None:
    assert resolve_claim(DEV.claims, path) == expected


# -- subjects ----------------------------------------------------------------------------


def claim(path: str, op: str, value: Any = None) -> ClaimSubject:
    raw: dict[str, Any] = {"kind": "claim", "path": path, "op": op}
    if value is not None:
        raw["value"] = value
    return ClaimSubject.model_validate(raw)


@pytest.mark.parametrize(
    ("subject", "principal", "expected"),
    [
        (AnonymousSubject(kind="anonymous"), ANONYMOUS, True),
        (AnonymousSubject(kind="anonymous"), DEV, True),
        (AuthenticatedSubject(kind="authenticated"), ANONYMOUS, False),
        (AuthenticatedSubject(kind="authenticated"), DEV, True),
        (ClientIdSubject(kind="client_id", value="ci-runner"), CI, True),
        (ClientIdSubject(kind="client_id", value="ci-runner"), AZP_ONLY, True),
        (ClientIdSubject(kind="client_id", value="ci-runner"), DEV, False),
        (ClientIdSubject(kind="client_id", value="ci-runner"), ANONYMOUS, False),
        # contains: list claims, and space-separated string claims (scope)
        (claim("realm_access.roles", "contains", "mcp-developer"), DEV, True),
        (claim("realm_access.roles", "contains", "mcp-admin"), DEV, False),
        (claim("scope", "contains", "email"), DEV, True),
        (claim("scope", "contains", "mail"), DEV, False),
        (claim("realm_access.roles", "contains", "mcp-developer"), ANONYMOUS, False),
        # eq: scalars, JSON-typed (True != 1)
        (claim("https://example.com/tenant", "eq", "acme"), DEV, True),
        (claim("level", "eq", 3), DEV, True),
        (claim("level", "eq", 3.0), DEV, True),
        (claim("level", "eq", "3"), DEV, False),
        (claim("flag", "eq", True), DEV, True),
        (claim("flag", "eq", 1), DEV, False),
        (claim("level", "eq", True), DEV, False),
        (claim("realm_access.roles", "eq", "mcp-developer"), DEV, False),
        # in: claim scalar (or any element of a list claim) among accepted values
        (claim("https://example.com/tenant", "in", ["acme", "globex"]), DEV, True),
        (claim("https://example.com/tenant", "in", ["globex"]), DEV, False),
        (claim("realm_access.roles", "in", ["offline_access"]), DEV, True),
        (claim("level", "in", [1, 2, 3]), DEV, True),
        # exists: present and non-null
        (claim("realm_access", "exists"), DEV, True),
        (claim("nothing", "exists"), DEV, False),
        (claim("missing", "exists"), DEV, False),
        (claim("missing", "eq", "x"), DEV, False),
    ],
)
def test_subject_matches(subject: Any, principal: Principal, expected: bool) -> None:
    assert subject_matches(subject, principal) is expected


# -- globs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allow", "deny", "name", "expected"),
    [
        ((), (), "a_b", False),
        (("a_b",), (), "a_b", True),
        (("a_b",), (), "a_bc", False),
        (("a_*",), (), "a_bc", True),
        (("a_*",), (), "A_bc", False),  # case-sensitive
        (("a_*",), (), "b_a_x", False),  # anchored
        (("a_?",), (), "a_bc", False),
        (("a_[xy]",), (), "a_x", True),
        (("a_[!xy]",), (), "a_x", False),
        (("*",), ("a_secret",), "a_secret", False),
        (("*",), ("a_secret",), "a_other", True),
        (("a_secret",), ("a_*",), "a_secret", False),  # deny glob beats exact allow
        (("a_*", "b_*"), ("*_secret",), "b_secret", False),
        (("a_*", "b_*"), ("*_secret",), "b_public", True),
        (("x.y-z",), (), "x.y-z", True),  # '.' and '-' are literal
        (("x.y-z",), (), "xAy-z", False),
    ],
)
def test_glob_filter(allow: tuple[str, ...], deny: tuple[str, ...], name: str, expected: bool):
    assert GlobToolFilter(allow, deny).allows(name) is expected


# -- snapshot semantics (table) ------------------------------------------------------------


def ts(id: str, allow: list[str], deny: list[str] | None = None, enabled: bool = True) -> dict:
    return {"_id": id, "enabled": enabled, "allow": allow, "deny": deny or []}


def grant(id: str, subject: dict[str, Any], toolsets: list[str], **kw: Any) -> dict:
    return {"_id": id, "enabled": True, "subject": subject, "toolsets": toolsets, **kw}


SEED_LIKE = dict(
    toolsets=[
        ts("public-basics", ["sample-public_time_now", "sample-public_echo"]),
        ts("engineering", ["sample-private_*"]),
        ts("ci-tools", ["sample-public_*"], deny=["sample-public_echo"]),
        ts("disabled-set", ["*"], enabled=False),
    ],
    grants=[
        grant("anon-default", {"kind": "anonymous"}, ["public-basics"], priority=100),
        grant(
            "developers",
            {
                "kind": "claim",
                "path": "realm_access.roles",
                "op": "contains",
                "value": "mcp-developer",
            },
            ["public-basics", "engineering"],
            priority=50,
        ),
        grant("ci-bot", {"kind": "client_id", "value": "ci-runner"}, ["ci-tools"], priority=60),
        grant("everyone-all", {"kind": "anonymous"}, ["disabled-set"]),
        grant("nobody", {"kind": "authenticated"}, ["does-not-exist"]),
        {**grant("off", {"kind": "anonymous"}, ["engineering"]), "enabled": False},
    ],
)
TOOLS = [
    "sample-public_time_now",
    "sample-public_echo",
    "sample-private_secret_lookup",
    "other_tool",
]


def seed_snapshot() -> PolicySnapshot:
    snap, report = build_snapshot(version=1, **SEED_LIKE)
    assert report.total_invalid == 0
    assert report.unknown_toolsets == ("does-not-exist",)
    return snap


def test_seed_semantics_guest_dev_ci() -> None:
    snap = seed_snapshot()
    assert snap.matching_grants(ANONYMOUS) == ["anon-default", "everyone-all"]
    assert snap.effective_names(ANONYMOUS, TOOLS) == [
        "sample-public_time_now",
        "sample-public_echo",
    ]
    assert snap.matching_grants(DEV) == ["anon-default", "developers", "everyone-all", "nobody"]
    assert snap.effective_names(DEV, TOOLS) == [
        "sample-public_time_now",
        "sample-public_echo",
        "sample-private_secret_lookup",
    ]
    # ci-runner: guest floor + ci-tools, whose deny of echo wins over the floor's allow.
    assert snap.effective_names(CI, TOOLS) == ["sample-public_time_now"]
    # An authenticated caller matching no role grant still has the guest floor.
    plain = Principal("user", sub="nobody", claims={})
    assert snap.effective_names(plain, TOOLS) == snap.effective_names(ANONYMOUS, TOOLS)


def test_deny_by_default_without_grants() -> None:
    snap, _ = build_snapshot(version=1, toolsets=SEED_LIKE["toolsets"], grants=[])
    assert snap.matching_grants(ANONYMOUS) == []
    assert snap.effective_names(ANONYMOUS, TOOLS) == []
    assert snap.effective_names(DEV, TOOLS) == []


def test_memoised_per_cache_key() -> None:
    snap = seed_snapshot()
    f1 = snap.effective_tools(DEV)
    same_identity_new_token = Principal(
        "user", sub="dev1", client_id="mcp-gateway", claims={**DEV.claims, "exp": 999, "jti": "new"}
    )
    assert snap.effective_tools(same_identity_new_token) is f1
    assert snap.effective_tools(ANONYMOUS) is snap.effective_tools(ANONYMOUS)
    assert snap.effective_tools(ANONYMOUS) is not f1


def test_build_snapshot_skips_invalid_documents() -> None:
    snap, report = build_snapshot(
        version=3,
        toolsets=[ts("ok", ["a_*"]), {"_id": "bad", "allow": "not-a-list"}],
        grants=[grant("g", {"kind": "anonymous"}, ["ok"]), {"_id": "bad"}],
        upstreams=[{"_id": "nope"}],
    )
    assert report.invalid == {"upstreams": 1, "toolsets": 1, "grants": 1}
    assert set(snap.toolsets) == {"ok"} and set(snap.grants) == {"g"} and not snap.upstreams
    assert snap.effective_tools(ANONYMOUS).allows("a_x")
    assert snap.version == 3 and snap.age_seconds < 5


# -- property tests ----------------------------------------------------------------------

names = st.from_regex(r"\A[a-z]{1,3}_[a-z]{1,3}\Z")
globs = st.one_of(names, st.from_regex(r"\A[a-z]{1,2}\*\Z"), st.just("*"))
subjects = st.sampled_from(
    [
        {"kind": "anonymous"},
        {"kind": "authenticated"},
        {"kind": "client_id", "value": "ci-runner"},
        {"kind": "claim", "path": "realm_access.roles", "op": "contains", "value": "mcp-developer"},
        {"kind": "claim", "path": "level", "op": "eq", "value": 3},
        {"kind": "claim", "path": "missing", "op": "exists"},
    ]
)
principals = st.sampled_from([ANONYMOUS, DEV, CI, Principal("user", sub="p", claims={})])


@st.composite
def policies(draw: st.DrawFn) -> tuple[PolicySnapshot, list[dict], list[dict]]:
    n_ts = draw(st.integers(1, 4))
    toolsets = [
        {
            "_id": f"t{i}",
            "enabled": draw(st.booleans()),
            "allow": draw(st.lists(globs, max_size=4, unique=True)),
            "deny": draw(st.lists(globs, max_size=2, unique=True)),
        }
        for i in range(n_ts)
    ]
    grants = [
        {
            "_id": f"g{i}",
            "enabled": draw(st.booleans()),
            "priority": draw(st.integers(0, 100)),
            "subject": draw(subjects),
            "toolsets": draw(
                st.lists(
                    st.sampled_from([t["_id"] for t in toolsets]),
                    min_size=1,
                    max_size=3,
                    unique=True,
                )
            ),
        }
        for i in range(draw(st.integers(0, 5)))
    ]
    snap, report = build_snapshot(version=1, toolsets=toolsets, grants=grants)
    assert report.total_invalid == 0
    return snap, toolsets, grants


def oracle(snap: PolicySnapshot, toolsets: list[dict], grants: list[dict], p: Principal):
    """Independent re-statement of PLAN 3.2 for the property tests."""
    by_id = {t["_id"]: t for t in toolsets}
    allow, deny = set(), set()
    for g in grants:
        if not g["enabled"]:
            continue
        subject = next(x for x in snap.grants.values() if x.id == g["_id"]).subject
        if not subject_matches(subject, p):
            continue
        for tid in g["toolsets"]:
            t = by_id[tid]
            if t["enabled"]:
                allow |= set(t["allow"])
                deny |= set(t["deny"])
    return allow, deny


@settings(max_examples=200, deadline=None)
@given(policies(), principals, st.lists(names, max_size=8, unique=True))
def test_effective_tools_matches_oracle(policy, p, tool_names) -> None:
    snap, toolsets, grants = policy
    allow, deny = oracle(snap, toolsets, grants, p)
    f = GlobToolFilter(tuple(allow), tuple(deny))
    assert snap.effective_names(p, tool_names) == [n for n in tool_names if f.allows(n)]
    # deny wins: a name matched by any deny glob is never visible
    denied = GlobToolFilter(tuple(deny), ())
    for n in tool_names:
        if denied.allows(n):
            assert not snap.effective_tools(p).allows(n)
    # deny by default: nothing matched by an allow glob is never visible
    allowed = GlobToolFilter(tuple(allow), ())
    for n in tool_names:
        if not allowed.allows(n):
            assert not snap.effective_tools(p).allows(n)


@settings(max_examples=200, deadline=None)
@given(policies(), principals)
def test_anonymous_grants_are_the_floor(policy, p) -> None:
    """Every grant that matches a guest matches every principal, so without extra denies
    an authenticated caller sees at least the guest set."""
    snap, toolsets, grants = policy
    guest_grants = set(snap.matching_grants(ANONYMOUS))
    assert guest_grants <= set(snap.matching_grants(p))
    _, guest_deny = oracle(snap, toolsets, grants, ANONYMOUS)
    _, p_deny = oracle(snap, toolsets, grants, p)
    if p_deny == guest_deny:
        for n in ["ab_cd", "x_y", "foo_bar", "a_b"]:
            if snap.effective_tools(ANONYMOUS).allows(n):
                assert snap.effective_tools(p).allows(n)


def test_snapshot_is_immutable() -> None:
    snap = seed_snapshot()
    with pytest.raises(AttributeError):
        snap.version = 2  # type: ignore[misc]
    assert isinstance(snap.loaded_at, datetime) and snap.loaded_at.tzinfo is UTC


def test_toolset_and_grant_models_roundtrip() -> None:
    t = ToolsetDoc.model_validate(ts("x", ["a_*"], ["a_secret"]))
    g = GrantDoc.model_validate(grant("g", {"kind": "anonymous"}, ["x"]))
    assert t.allow == ("a_*",) and t.deny == ("a_secret",) and g.toolsets == ("x",)
