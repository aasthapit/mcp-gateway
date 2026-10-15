"""Where the desired upstream list comes from.

WP4 interim: a minimal loader over the ``upstreams`` collection (``readConcern:
majority``), called at startup and on ``POST /internal/upstreams/reload``. WP3's policy
watcher loads the same collection into ``PolicySnapshot.upstreams`` and calls
``UpstreamRegistry.apply(snapshot)`` after every reload; once that lands, ``app.py`` swaps
``await gateway.upstream_source.load()`` for ``snapshot.upstreams`` and this module can go.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from mcp_gateway.middleware.metrics import UPSTREAM_ERRORS
from mcp_gateway.telemetry import get_logger
from mcp_gateway.upstreams.models import UpstreamConfig

if TYPE_CHECKING:
    from mcp_gateway.settings import Settings, StaticUpstream

log = get_logger(__name__)

COLLECTION = "upstreams"


@runtime_checkable
class UpstreamSource(Protocol):
    async def load(self) -> Mapping[str, UpstreamConfig]: ...

    async def close(self) -> None: ...


def static_to_config(static: StaticUpstream) -> UpstreamConfig:
    return UpstreamConfig(
        _id=static.id,
        displayName=static.id,
        enabled=True,
        transport=static.transport,
        url=static.url,
        cacheTtlSec=static.cache_ttl_sec,
        sessionMode=static.session_mode,
        protocolMode=static.protocol_mode,
    )


def parse_upstream_docs(docs: Iterable[Mapping[str, Any]]) -> dict[str, UpstreamConfig]:
    """Validate raw documents; an invalid one is logged, counted and skipped so a single
    bad admin write cannot take the other upstreams down."""
    out: dict[str, UpstreamConfig] = {}
    for doc in docs:
        upstream_id = str(doc.get("_id", "?"))
        try:
            cfg = UpstreamConfig.model_validate(dict(doc))
        except ValidationError as exc:
            UPSTREAM_ERRORS.labels(upstream_id, "invalid_config").inc()
            log.error(
                "upstream document rejected",
                upstream=upstream_id,
                errors=[f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()],
            )
            continue
        out[cfg.id] = cfg
    return out


class StaticUpstreamSource:
    """WP1 behaviour: ``GATEWAY_STATIC_UPSTREAMS``."""

    def __init__(self, settings: Settings) -> None:
        self._configs = {s.id: static_to_config(s) for s in settings.static_upstreams}

    async def load(self) -> Mapping[str, UpstreamConfig]:
        return dict(self._configs)

    async def close(self) -> None:
        return None


class MongoUpstreamSource:
    """Reads the ``upstreams`` collection with ``readConcern: majority``."""

    def __init__(self, uri: str, database: str, *, server_selection_timeout_ms: int) -> None:
        self._uri = uri
        self._db = database
        self._timeout_ms = server_selection_timeout_ms
        self._client: Any = None

    def _collection(self) -> Any:
        if self._client is None:
            from pymongo import AsyncMongoClient

            # Created lazily so it binds to the running event loop.
            self._client = AsyncMongoClient(
                self._uri,
                serverSelectionTimeoutMS=self._timeout_ms,
                connectTimeoutMS=self._timeout_ms,
                appname="mcp-gateway",
            )
        from pymongo.read_concern import ReadConcern

        return self._client[self._db].get_collection(
            COLLECTION, read_concern=ReadConcern("majority")
        )

    async def load(self) -> Mapping[str, UpstreamConfig]:
        docs = await self._collection().find({}).to_list()
        configs = parse_upstream_docs(docs)
        log.info("upstreams loaded from mongo", count=len(configs), database=self._db)
        return configs

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def make_upstream_source(settings: Settings) -> UpstreamSource:
    if settings.upstreams_source == "static":
        return StaticUpstreamSource(settings)
    return MongoUpstreamSource(
        settings.mongo_uri,
        settings.mongo_db,
        server_selection_timeout_ms=settings.mongo_server_selection_timeout_ms,
    )
