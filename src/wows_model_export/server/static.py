"""Resolve + serve the bundled Svelte webview build.

The webview's production output (`webview/dist`) is mirrored into the
package at install time as `wows_model_export/_static/webview/` (see
`src/wows_model_export/_static/README.md` for why a committed mirror
beats a build hook for this project). This module finds that directory
across the three install modes the project ships in and exposes a
single :func:`mount_webview` helper that wires it onto a FastAPI app.

Install modes the resolver handles:

  1. **Editable install** (`pip install -e .`) — package files live at
     ``<repo>/src/wows_model_export/`` so the bundled mirror is at
     ``<repo>/src/wows_model_export/_static/webview/``. ``importlib.resources``
     returns a real on-disk path here.
  2. **Wheel install** (`pip install ./*.whl`) — files land under
     ``<site-packages>/wows_model_export/_static/webview/`` and again
     ``importlib.resources`` returns a real path.
  3. **PyInstaller frozen** — the spec file (or `--collect-data
     wows_model_export`) drops the package data under ``sys._MEIPASS``,
     and ``importlib.resources`` continues to work because PyInstaller
     installs an importer that proxies it transparently.

The dev-tree fallback (`<repo>/webview/dist`) covers a fourth case the
task spec calls out: someone running `wows-webview-serve` from a fresh
clone who hasn't built the webview yet AND somehow has no mirror under
`_static/`. In practice the mirror is committed, so the fallback only
fires in deliberately broken configurations — but having it costs
nothing and gives a useful "did you forget to npm run build?" log line.
"""

from __future__ import annotations

import logging
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


# Subpath inside the package where the build mirror lives. Kept as a
# module constant so tests / debug tools can discover the same location
# without re-deriving it.
_PACKAGE_STATIC_SUBPATH = ("_static", "webview")


def _resolve_bundled_dir() -> Path | None:
    """Return the on-disk path of the bundled webview build, or None.

    Tries the package-resource path first (covers editable, wheel, and
    PyInstaller installs). Falls back to a sibling `webview/dist`
    directory if the package doesn't carry the mirror — that branch is
    only useful in a working tree where the user runs the server before
    syncing the mirror, and it deliberately reaches outside the package
    so it's never the production path.
    """
    # importlib.resources is the right tool here: it abstracts over
    # zipped vs unpacked package layouts (PyInstaller bundles can be
    # either) without us having to test for `__file__` shape.
    try:
        root: Traversable = resources.files("wows_model_export")
        candidate = root.joinpath(*_PACKAGE_STATIC_SUBPATH)
        # `as_file` would copy a zipped resource to a temp dir; we want
        # the real path so StaticFiles can serve files lazily. For the
        # three modes we ship in, the package resources are always
        # unpacked on disk, so a direct `Path(str(...))` is fine.
        bundled = Path(str(candidate))
        if (bundled / "index.html").is_file():
            return bundled
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        pass

    # Dev-tree fallback. Walk up from this file until we find a sibling
    # `webview/dist/index.html`. Capped at 6 levels to fail fast on a
    # truly weird layout instead of marching to the filesystem root.
    here = Path(__file__).resolve()
    for parent in (here, *here.parents)[:6]:
        candidate = parent / "webview" / "dist" / "index.html"
        if candidate.is_file():
            return candidate.parent

    return None


def mount_webview(app: FastAPI) -> Path | None:
    """Mount the Svelte SPA at ``/`` on ``app``. Returns the dist path
    used (or ``None`` if no bundle was found, in which case the function
    is a no-op except for a warning log).

    Order matters: this must run **after** the `/api/*` and `/repo/*`
    routers are added so those routes win over the catch-all SPA mount.
    The Svelte client uses HTML5 client-side routing, so any unknown GET
    that doesn't start with `/api` or `/repo` should resolve to
    `index.html` rather than 404 — that's the SPA fallback dance below.
    """
    bundled = _resolve_bundled_dir()
    if bundled is None:
        logger.warning(
            "webview UI not bundled; serving API only. "
            "Run `cd webview && npm run build` (then resync the mirror) "
            "or use Vite via `cd webview && npm run dev`."
        )
        return None

    logger.info("serving Svelte UI from %s", bundled)
    index_html = bundled / "index.html"

    # Mount Starlette's StaticFiles at /assets/ for the fingerprinted
    # JS/CSS bundles (and any future static folders Vite emits). We do
    # NOT mount it at "/" because that would let it answer any GET with
    # a 404 for unknown paths — we want the SPA fallback instead. So we
    # serve `/assets` directly and synthesise `/` + arbitrary paths via
    # a catch-all route below. `html=False` because we don't want
    # StaticFiles to do its own index lookups; the SPA handler owns that.
    assets_dir = bundled / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=assets_dir, html=False),
            name="webview-assets",
        )

    # Catch-all GET for the SPA. Registered last so explicit routes
    # (anything under /api or /repo, plus the /assets mount) take
    # precedence by virtue of FastAPI's first-match-wins routing.
    #
    # Why a path-converter route instead of `app.mount("/", StaticFiles(...))`?
    # StaticFiles' built-in `html=True` index lookup only kicks in for
    # directories, not arbitrary unknown paths — visiting
    # `/ships/Halland` directly would 404 instead of falling back to the
    # SPA's index.html. We also want explicit control over the "don't
    # swallow /api/* 404s" behaviour, which a Mount can't express.
    @app.get("/{spa_path:path}", include_in_schema=False)
    def serve_spa(spa_path: str) -> FileResponse:
        # Belt-and-braces: an unknown path under /api or /repo must
        # still 404 even though the corresponding routers are registered
        # first. This catches the case where the API router exists but
        # has no route for the given subpath — without this guard, the
        # SPA fallback would shadow API 404s and confuse callers.
        first_segment = spa_path.split("/", 1)[0] if spa_path else ""
        if first_segment in ("api", "repo"):
            raise HTTPException(status_code=404)

        # If the URL points at a real top-level file in dist (e.g.
        # `/favicon.ico`, `/robots.txt`), serve it directly. Anything
        # else is a Svelte client-side route → return index.html so the
        # SPA router can render it.
        if spa_path:
            candidate = (bundled / spa_path).resolve()
            try:
                candidate.relative_to(bundled.resolve())
            except ValueError:
                # Path traversal attempt — fall through to the SPA index
                # rather than leaking the resolution failure.
                candidate = None  # type: ignore[assignment]
            if candidate is not None and candidate.is_file():
                return FileResponse(str(candidate))

        return FileResponse(str(index_html))

    return bundled


__all__ = ["mount_webview"]
