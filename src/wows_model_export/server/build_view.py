"""Serve an archived game build's workspace beside the current one.

The webview's build picker stores the chosen build in the cookie
``wows_build``. :class:`BuildViewMiddleware` hands every ``/api/*`` and
``/repo/*`` request that carries it to an app built for
``config.for_build(<build>)``: the same routers, bound to that build's
workspace (``<workspace>/builds/<build>/``) and reading that build's game
data (the archive, else the install while it still has the build). Those
apps are made on first use and kept.

A request goes to the main app (the install's current build) when it has no
cookie, when the cookie names the current build or a build nothing can
read, and for the routes that concern the whole installation rather than
one build: the picker's own ``/api/builds/sources``, the archive status
(``/api/builds/archive``) and ``/api/settings``.

While a request for an archived build runs, its pinned config is the
active one (``config.activated``), so a ``PipelineConfig.load()`` fallback
inside a route reads that build too.

Every ``/api/*`` and ``/repo/*`` response carries ``X-Wows-Build`` (the
build that served it) and ``Vary: Cookie``, so the browser never answers a
request from a file it cached for another build. The selection is per
browser: every tab of one browser views the same build.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import PipelineConfig, activated
from ..toolkit import build as _build

COOKIE = "wows_build"
HEADER = "X-Wows-Build"

# Paths always served by the main app.
_EXEMPT_PREFIXES = ("/api/builds/sources", "/api/builds/archive", "/api/settings")
_ROUTED_PREFIXES = ("/api/", "/repo/")
# How long a "this build is readable" answer is reused.
_SOURCE_TTL_S = 10.0


def cookie_build(headers: list[tuple[bytes, bytes]] | Any) -> int | None:
    """The build named by the ``wows_build`` cookie in raw ASGI headers."""
    for k, v in headers or ():
        if k.lower() != b"cookie":
            continue
        try:
            jar = SimpleCookie()
            jar.load(v.decode("latin-1"))
        except Exception:  # noqa: BLE001 - malformed cookie header
            continue
        morsel = jar.get(COOKIE)
        if morsel is not None and morsel.value.strip().isdigit():
            return int(morsel.value.strip())
    return None


def resolve_view(base: PipelineConfig, build: int | None) -> PipelineConfig:
    """The config a request naming ``build`` is served with: pinned when
    ``build`` is readable and not the current build, else ``base``."""
    if build is None:
        return base
    cfg = base.for_build(build)
    if cfg.build is None or _build.find_source(cfg.build, cfg) is None:
        return base
    return cfg


class BuildViewMiddleware:
    """Pure ASGI middleware: route a request to its build's app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        base_config: PipelineConfig,
        factory: Callable[[PipelineConfig], ASGIApp],
    ) -> None:
        self.app = app
        self.base = base_config
        self.factory = factory
        self._apps: dict[int, tuple[ASGIApp, PipelineConfig]] = {}
        self._checked: dict[int, tuple[float, PipelineConfig | None]] = {}
        self._lock = threading.Lock()

    def _view(self, build: int | None) -> tuple[ASGIApp, PipelineConfig | None]:
        if build is None:
            return self.app, None
        now = time.monotonic()
        with self._lock:
            hit = self._checked.get(build)
        if hit is None or now - hit[0] > _SOURCE_TTL_S:
            cfg = resolve_view(self.base, build)
            pinned = cfg if cfg.build is not None else None
            with self._lock:
                self._checked[build] = (now, pinned)
        else:
            pinned = hit[1]
        if pinned is None:
            return self.app, None
        with self._lock:
            hit_app = self._apps.get(build)
            if hit_app is None:
                hit_app = (self.factory(pinned), pinned)
                self._apps[build] = hit_app
        return hit_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith(_ROUTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        build = None if path.startswith(_EXEMPT_PREFIXES) else cookie_build(scope.get("headers"))
        target, pinned = self._view(build)
        label = str(pinned.build if pinned is not None
                    else (_build.install_current_build(self.base) or "current"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("Vary", "Cookie")
                headers[HEADER] = label
            await send(message)

        if pinned is None:
            await target(scope, receive, send_with_headers)
            return
        # The pinned config is what PipelineConfig.load() returns while
        # this request runs (sync routes run in the thread pool with a
        # copy of this context), like a job for that build.
        with activated(pinned):
            await target(scope, receive, send_with_headers)


__all__ = ["BuildViewMiddleware", "COOKIE", "HEADER", "cookie_build", "resolve_view"]
