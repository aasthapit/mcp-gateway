from __future__ import annotations

from prometheus_client import REGISTRY

from mcp_gateway.settings import Settings, StaticUpstream
from mcp_gateway.upstreams.source import (
    MongoUpstreamSource,
    StaticUpstreamSource,
    make_upstream_source,
    parse_upstream_docs,
)


def test_parse_skips_invalid_documents_and_counts_them() -> None:
    before = (
        REGISTRY.get_sample_value(
            "gateway_upstream_errors_total", {"upstream": "broken", "kind": "invalid_config"}
        )
        or 0
    )
    docs = [
        {
            "_id": "ok",
            "displayName": "Ok",
            "enabled": True,
            "transport": "http",
            "url": "http://x/mcp",
            "auth": {"type": "none"},
            "sessionMode": "shared",
        },
        {
            "_id": "broken",
            "displayName": "B",
            "enabled": True,
            "transport": "http",
            "auth": {"type": "none"},
            "sessionMode": "shared",
        },  # no url
        {"_id": "ok2", "displayName": "Ok2", "transport": "stdio", "command": "uvx", "args": ["x"]},
    ]
    parsed = parse_upstream_docs(docs)
    assert set(parsed) == {"ok", "ok2"}
    assert parsed["ok"].session_mode == "shared"
    after = REGISTRY.get_sample_value(
        "gateway_upstream_errors_total", {"upstream": "broken", "kind": "invalid_config"}
    )
    assert after == before + 1


async def test_static_source_mirrors_settings(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "static_upstreams": [
                StaticUpstream(
                    id="x", url="http://h/mcp", protocol_mode="2026-07-28", session_mode="shared"
                )
            ]
        }
    )
    src = make_upstream_source(s)
    assert isinstance(src, StaticUpstreamSource)
    cfgs = await src.load()
    assert cfgs["x"].protocol_mode == "2026-07-28" and cfgs["x"].session_mode == "shared"
    assert cfgs["x"].display_name == "x"
    await src.close()


def test_default_source_is_mongo(settings: Settings) -> None:
    s = settings.model_copy(update={"upstreams_source": "mongo"})
    assert isinstance(make_upstream_source(s), MongoUpstreamSource)
