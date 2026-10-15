"""apply/diff logic against a fake FastMCP, plus the ADR 0005 live-unmount guard on the
real one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from prometheus_client import REGISTRY

from mcp_gateway.settings import Settings
from mcp_gateway.upstreams.models import UpstreamConfig
from mcp_gateway.upstreams.registry import UpstreamRegistry


class FakeFastMCP:
    """Just enough of FastMCP for the registry: a public ``providers`` list that ``mount``
    appends one wrapper to."""

    _mask_error_details = False

    def __init__(self) -> None:
        self.providers: list[Any] = []
        self.mount_calls: list[tuple[str, Any]] = []

    def mount(self, server: Any, namespace: str | None = None, **_: Any) -> None:
        self.mount_calls.append((namespace or "", server))
        self.providers.append(("wrapped", namespace, server))


def cfg(upstream_id: str = "a", **over: object) -> UpstreamConfig:
    base: dict = {
        "_id": upstream_id,
        "displayName": upstream_id,
        "enabled": True,
        "transport": "http",
        "url": f"http://127.0.0.1:1/{upstream_id}",
        "auth": {"type": "none"},
        "sessionMode": "per_request",
        "updatedAt": "2026-10-15T10:00:00Z",
    }
    base.update(over)
    return UpstreamConfig.model_validate(base)


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def registry(settings: Settings, tmp_path: Path) -> tuple[UpstreamRegistry, FakeFastMCP]:
    mcp = FakeFastMCP()
    s = settings.model_copy(update={"upstream_secrets_dir": str(tmp_path)})
    return UpstreamRegistry(mcp, settings=s), mcp  # type: ignore[arg-type]


async def test_apply_mounts_new_skips_unchanged_remounts_changed_unmounts_removed(
    registry: tuple[UpstreamRegistry, FakeFastMCP],
) -> None:
    reg, mcp = registry
    await reg.apply_configs([cfg("a"), cfg("b"), cfg("off", enabled=False)])
    assert reg.namespaces == ("a", "b") and len(mcp.providers) == 2
    assert metric("gateway_upstreams_mounted") == 2
    first_a = reg.mounted["a"].provider

    # Same mount fingerprint with a new updatedAt: nothing happens.
    await reg.apply_configs([cfg("a", updatedAt="2026-10-16T00:00:00Z"), cfg("b")])
    assert reg.mounted["a"].provider is first_a and len(mcp.mount_calls) == 2

    # Changed URL: unmount + mount, old provider gone from the list.
    await reg.apply_configs([cfg("a", url="http://127.0.0.1:1/a2"), cfg("b")])
    assert reg.mounted["a"].provider is not first_a
    assert all(p is not first_a for p in mcp.providers) and len(mcp.providers) == 2
    assert reg.mounted["a"].generation == 2

    # b disabled, a removed -> both unmounted.
    await reg.apply_configs([cfg("b", enabled=False)])
    assert reg.namespaces == () and mcp.providers == []
    assert metric("gateway_upstreams_mounted") == 0
    assert reg.health() == {}


async def test_prod_rejects_stdio_without_crashing(settings: Settings) -> None:
    prod = settings.model_copy(update={"env": "prod"})  # bypasses the prod validators on purpose
    mcp = FakeFastMCP()
    reg = UpstreamRegistry(mcp, settings=prod)  # type: ignore[arg-type]
    before = metric("gateway_upstream_errors_total", upstream="s", kind="stdio_forbidden")
    await reg.apply_configs([cfg("s", transport="stdio", url=None, command="uvx"), cfg("ok")])
    assert reg.namespaces == ("ok",)
    h = reg.health()["s"]
    assert h.status == "degraded" and not h.mounted and h.reason == "stdio_forbidden_in_prod"
    assert (
        metric("gateway_upstream_errors_total", upstream="s", kind="stdio_forbidden") == before + 1
    )
    assert metric("gateway_upstream_up", upstream="s") == 0
    ok, info = reg.readiness_probe()
    assert ok and info["degraded"] == {"s": "stdio_forbidden_in_prod"}


async def test_dev_allows_stdio(registry: tuple[UpstreamRegistry, FakeFastMCP]) -> None:
    reg, _ = registry
    await reg.apply_configs(
        [cfg("s", transport="stdio", url=None, command="uvx", sessionMode="shared")]
    )
    assert reg.namespaces == ("s",)
    assert reg.health()["s"].session_mode == "per_request"  # shared forced off for stdio


async def test_missing_secret_degrades_then_recovers(
    registry: tuple[UpstreamRegistry, FakeFastMCP], tmp_path: Path
) -> None:
    reg, mcp = registry
    secured = cfg("gh", auth={"type": "static_bearer", "secretRef": "upstream-gh"})
    await reg.apply_configs([secured])
    h = reg.health()["gh"]
    assert h.status == "degraded" and h.reason == "secret_missing" and mcp.providers == []
    assert "upstream-gh" in (h.error or "")
    assert reg.readiness_probe()[0] is False  # nothing mounted

    (tmp_path / "upstream-gh").write_text("tok\n")
    await reg.apply_configs([secured])  # retried on the next apply
    assert reg.namespaces == ("gh",)
    entry = reg.mounted["gh"]
    assert entry.built is not None
    assert entry.built.base_client.transport.headers == {"Authorization": "Bearer tok"}


async def test_unsupported_auth_is_degraded(registry: tuple[UpstreamRegistry, FakeFastMCP]) -> None:
    reg, _ = registry
    await reg.apply_configs([cfg("cc", auth={"type": "client_credentials"})])
    assert reg.health()["cc"].reason == "auth_unsupported"


def test_namespace_of_prefers_longest_mounted_namespace(settings: Settings) -> None:
    reg = UpstreamRegistry(FakeFastMCP(), settings=settings)  # type: ignore[arg-type]
    reg.mount(cfg("sample"))
    reg.mount(cfg("sample-public"))
    assert reg.namespace_of("sample-public_echo") == "sample-public"
    assert reg.namespace_of("sample_x") == "sample"
    assert reg.namespace_of("other_x") == "unknown"


# -- ADR 0005 guard: real FastMCP ------------------------------------------------------------


async def test_live_unmount_via_providers_list_on_real_fastmcp(settings: Settings) -> None:
    """Fails loudly if FastMCP stops exposing ``providers`` as a list that ``mount`` appends
    exactly one entry to (ADR 0005 is a list mutation, not an API)."""
    mcp = FastMCP("parent")
    child = FastMCP("child")

    @child.tool
    def hello() -> str:
        return "hi"

    assert isinstance(mcp.providers, list), "FastMCP.providers is no longer a list (ADR 0005)"
    reg = UpstreamRegistry(mcp, settings=settings)
    before = len(mcp.providers)
    # Go through the registry's bookkeeping with a config, then swap the proxy for the
    # in-memory child so the assertion needs no network.
    entry = reg.mount(cfg("c"))
    assert len(mcp.providers) == before + 1, "mount() must append exactly one provider"
    mcp.providers.remove(entry.provider)
    mcp.mount(child, namespace="c")
    entry.provider = mcp.providers[-1]

    async with Client(mcp) as c:
        assert {t.name for t in await c.list_tools()} == {"c_hello"}
        assert (await c.call_tool("c_hello", {})).data == "hi"

    await reg.unmount("c")
    assert len(mcp.providers) == before
    async with Client(mcp) as c:
        assert await c.list_tools() == []
        with pytest.raises(Exception, match="Unknown tool"):
            await c.call_tool("c_hello", {})


# -- probe ------------------------------------------------------------------------------------


async def test_probe_marks_up_and_down(settings: Settings) -> None:
    s = settings.model_copy(update={"upstream_probe_timeout_sec": 1.0})
    reg = UpstreamRegistry(FakeFastMCP(), settings=s)  # type: ignore[arg-type]
    good = FastMCP("good")

    @good.tool
    def t1() -> str:
        return "x"

    reg.mount(cfg("good"))
    reg.mount(cfg("dead"))  # 127.0.0.1:1 -> connection refused
    assert all(h.status == "unknown" for h in reg.health().values())
    reg.mounted["good"].built.base_client = Client(good)  # type: ignore[union-attr]

    health = await reg.probe_once()
    assert health["good"].status == "up" and health["good"].tool_count == 1
    assert health["good"].latency_ms is not None
    assert health["dead"].status == "down" and health["dead"].error
    assert metric("gateway_upstream_up", upstream="good") == 1
    assert metric("gateway_upstream_up", upstream="dead") == 0
    assert metric("gateway_upstream_errors_total", upstream="dead", kind="probe_failed") >= 1
    assert (
        REGISTRY.get_sample_value("gateway_upstream_probe_seconds_count", {"upstream": "good"}) >= 1
    )
    ok, info = reg.readiness_probe()
    assert ok and info["down"] == ["dead"]  # a dead upstream does not flip readiness
    assert reg.probe_runs == 1
