"""Grant subject matching and glob-based tool filters (docs/PLAN.md section 3.2).

Pure functions over :class:`Principal` and the policy models; no I/O, no FastMCP.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp_gateway.auth.principal import Principal
from mcp_gateway.policy.models import (
    AnonymousSubject,
    AuthenticatedSubject,
    ClaimSubject,
    ClientIdSubject,
)

Subject = AnonymousSubject | AuthenticatedSubject | ClaimSubject | ClientIdSubject


# -- claims ------------------------------------------------------------------------------


def resolve_claim(claims: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Follow a dotted ``path`` into ``claims``.

    Returns ``(found, value)``. At each level the longest key made of the remaining
    segments is tried first, so URL-style claim names that contain dots
    (``https://example.com/roles``) and nested paths (``realm_access.roles``) both work.
    """
    parts = path.split(".")
    node: Any = claims
    i = 0
    while i < len(parts):
        if not isinstance(node, Mapping):
            return False, None
        for j in range(len(parts), i, -1):
            key = ".".join(parts[i:j])
            if key in node:
                node = node[key]
                i = j
                break
        else:
            return False, None
    return True, node


def _scalar_eq(a: Any, b: Any) -> bool:
    """Equality that keeps ``True`` and ``1`` apart (JSON types, not Python's bool-is-int)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return type(a) is type(b) and a == b


def _members(value: Any) -> list[Any]:
    """Elements a ``contains``/``in`` check iterates: list claims as-is, space-separated
    string claims (``scope``) split into words, anything else as a single element."""
    if isinstance(value, list | tuple | set | frozenset):
        return list(value)
    if isinstance(value, str):
        return value.split()
    return [value]


def _client_id_of(p: Principal) -> str | None:
    if p.client_id:
        return p.client_id
    for key in ("azp", "client_id"):
        v = p.claims.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def subject_matches(subject: Subject, p: Principal) -> bool:
    """Does ``subject`` select principal ``p``?"""
    match subject:
        case AnonymousSubject():
            return True
        case AuthenticatedSubject():
            return p.kind == "user"
        case ClientIdSubject(value=value):
            return p.kind == "user" and _client_id_of(p) == value
        case ClaimSubject(path=path, op=op, value=value):
            if p.kind != "user":
                return False
            found, claim = resolve_claim(p.claims, path)
            if op == "exists":
                return found and claim is not None
            if not found or claim is None:
                return False
            if op == "eq":
                return _scalar_eq(claim, value)
            if op == "in":
                accepted = value if isinstance(value, list) else [value]
                return any(_scalar_eq(m, a) for m in _members(claim) for a in accepted)
            if op == "contains":
                return any(_scalar_eq(m, value) for m in _members(claim))
            return False
    return False


# -- tool name globs ---------------------------------------------------------------------


def compile_globs(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """One anchored, case-sensitive regex matching any of the fnmatch ``patterns``."""
    pats = sorted(set(patterns))
    if not pats:
        return None
    return re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in pats))


@dataclass(frozen=True)
class GlobToolFilter:
    """Deny-by-default: visible iff some allow glob matches and no deny glob matches."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    _allow_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False)
    _deny_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_allow_re", compile_globs(self.allow))
        object.__setattr__(self, "_deny_re", compile_globs(self.deny))

    def allows(self, namespaced_name: str) -> bool:
        if self._deny_re is not None and self._deny_re.match(namespaced_name):
            return False
        return self._allow_re is not None and bool(self._allow_re.match(namespaced_name))

    def filter(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if self.allows(n)]

    @property
    def is_empty(self) -> bool:
        return not self.allow
