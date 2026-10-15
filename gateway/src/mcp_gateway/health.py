"""Liveness and readiness.

``/healthz`` answers as soon as the process serves HTTP. ``/readyz`` is 503 until the
lifespan has finished and every registered :class:`ReadinessProbe` passes; later work
packages register probes (Mongo reachable, policy loaded and fresh, change stream up)
without touching the route.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

ProbeResult = tuple[bool, dict[str, Any]]
ProbeFn = Callable[[], ProbeResult | Awaitable[ProbeResult]]


@dataclass
class ReadinessProbe:
    name: str
    check: ProbeFn
    critical: bool = True
    """Non-critical probes are reported but do not flip readiness (e.g. stale policy)."""


@dataclass
class Readiness:
    started: bool = False
    probes: list[ReadinessProbe] = field(default_factory=list)

    def register(self, name: str, check: ProbeFn, *, critical: bool = True) -> None:
        self.probes = [p for p in self.probes if p.name != name]
        self.probes.append(ReadinessProbe(name, check, critical))

    async def evaluate(self) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {"started": self.started, "checks": {}}
        ready = self.started
        for probe in self.probes:
            try:
                res = probe.check()
                if inspect.isawaitable(res):
                    res = await asyncio.wait_for(res, timeout=5)
                ok, info = res
            except Exception as exc:  # a failing probe is a failing check, never a 500
                ok, info = False, {"error": f"{type(exc).__name__}: {exc}"}
            detail["checks"][probe.name] = {"ok": ok, "critical": probe.critical, **info}
            if probe.critical and not ok:
                ready = False
        detail["ready"] = ready
        return ready, detail


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def make_readyz(readiness: Readiness) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def readyz(request: Request) -> JSONResponse:
        ready, detail = await readiness.evaluate()
        return JSONResponse(detail, status_code=200 if ready else 503)

    return readyz
