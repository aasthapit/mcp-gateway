from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware

from mcp_gateway.auth.principal import ANONYMOUS, Principal
from mcp_gateway.middleware.metrics import POLICY_DENIALS
from mcp_gateway.policy.middleware import PolicyMiddleware
from mcp_gateway.policy.snapshot import (
    AllowAllFilter,
    AllowAllSnapshot,
    PolicySnapshot,
    SetToolFilter,
    ToolFilter,
)


class GuestOnlySnapshot(PolicySnapshot):
    """Anonymous sees pub_echo only; users see everything."""

    def effective_tools(self, p: Principal) -> ToolFilter:
        return AllowAllFilter() if p.kind == "user" else SetToolFilter(frozenset({"pub_echo"}))

    def matching_grants(self, p: Principal) -> list[str]:
        return ["anon"] if p.kind == "anonymous" else ["anon", "dev"]


def make_server(snapshot: PolicySnapshot, principal: Principal) -> FastMCP:
    sub = FastMCP("sub")

    @sub.tool
    def echo(text: str) -> str:
        return text

    @sub.tool
    def secret() -> str:
        return "s3cret"

    @sub.resource("data://info")
    def info() -> str:
        return "info"

    @sub.prompt
    def hello() -> str:
        return "hi"

    mcp = FastMCP("root")
    mcp.add_middleware(PolicyMiddleware(lambda: snapshot, lambda: principal))
    mcp.add_middleware(ErrorHandlingMiddleware())
    mcp.mount(sub, namespace="pub")
    return mcp


async def test_allow_all_snapshot_lists_everything() -> None:
    async with Client(make_server(AllowAllSnapshot(), ANONYMOUS)) as c:
        assert sorted(t.name for t in await c.list_tools()) == ["pub_echo", "pub_secret"]
        assert (await c.call_tool("pub_secret", {})).data == "s3cret"


async def test_guest_filtered_user_not() -> None:
    snap = GuestOnlySnapshot(version=1, loaded_at=datetime.now(UTC))
    async with Client(make_server(snap, ANONYMOUS)) as c:
        assert [t.name for t in await c.list_tools()] == ["pub_echo"]
        assert (await c.call_tool("pub_echo", {"text": "x"})).data == "x"
        before = POLICY_DENIALS.labels("pub_secret", "anonymous")._value.get()
        with pytest.raises(Exception, match="Unknown tool: 'pub_secret'"):
            await c.call_tool("pub_secret", {})
        assert POLICY_DENIALS.labels("pub_secret", "anonymous")._value.get() == before + 1

    async with Client(make_server(snap, Principal("user", sub="dev1"))) as c:
        assert sorted(t.name for t in await c.list_tools()) == ["pub_echo", "pub_secret"]
        assert (await c.call_tool("pub_secret", {})).data == "s3cret"


async def test_denied_matches_unknown_tool_error() -> None:
    """A denied tool and a truly unknown tool must be indistinguishable."""
    snap = GuestOnlySnapshot(version=1, loaded_at=datetime.now(UTC))
    async with Client(make_server(snap, ANONYMOUS)) as c:
        with pytest.raises(Exception) as denied:
            await c.call_tool("pub_secret", {})
        with pytest.raises(Exception) as unknown:
            await c.call_tool("pub_nope", {})
    assert type(denied.value) is type(unknown.value)
    assert str(denied.value).replace("pub_secret", "X") == str(unknown.value).replace(
        "pub_nope", "X"
    )


async def test_resources_and_prompts_hidden() -> None:
    async with Client(make_server(AllowAllSnapshot(), ANONYMOUS)) as c:
        assert await c.list_resources() == []
        assert await c.list_resource_templates() == []
        assert await c.list_prompts() == []
        with pytest.raises(Exception, match=r"(?i)not found"):
            await c.read_resource("data://pub/info")
        with pytest.raises(Exception, match=r"(?i)not found|unknown prompt"):
            await c.get_prompt("pub_hello")


async def test_real_snapshot_through_middleware() -> None:
    """A Mongo-shaped policy (toolsets + grants) drives list and call decisions."""
    from mcp_gateway.policy.snapshot import build_snapshot

    snap, _ = build_snapshot(
        version=9,
        toolsets=[
            {"_id": "guest", "enabled": True, "allow": ["pub_echo"]},
            {"_id": "all", "enabled": True, "allow": ["pub_*"], "deny": ["pub_echo"]},
        ],
        grants=[
            {
                "_id": "anon",
                "enabled": True,
                "subject": {"kind": "anonymous"},
                "toolsets": ["guest"],
            },
            {
                "_id": "admins",
                "enabled": True,
                "subject": {"kind": "claim", "path": "roles", "op": "contains", "value": "admin"},
                "toolsets": ["all"],
            },
        ],
    )
    async with Client(make_server(snap, ANONYMOUS)) as c:
        assert [t.name for t in await c.list_tools()] == ["pub_echo"]
    admin = Principal("user", sub="a", claims={"roles": ["admin"]})
    async with Client(make_server(snap, admin)) as c:
        # deny of pub_echo in the admin toolset wins over the guest floor's allow
        assert [t.name for t in await c.list_tools()] == ["pub_secret"]
        with pytest.raises(Exception, match="Unknown tool: 'pub_echo'"):
            await c.call_tool("pub_echo", {"text": "x"})
