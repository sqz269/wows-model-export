"""FastAPI app factory + uvicorn launcher for the webview backend.

``create_app(config)`` wires the routers (``/api/library``, ``/api/ships``,
``/api/gameparams/status``, ``/api/extract/*``, ``/repo/*``) onto a fresh
:class:`fastapi.FastAPI` instance. The CLI entry point at
:mod:`wows_model_export.cli.webview_serve` resolves a
:class:`PipelineConfig` from env + ``--workspace`` and hands it in here.

The Svelte client treats this backend identically to the legacy Node
middleware — same routes, same response shapes. The only client-side
change to ship together with this module is the Vite proxy config in
``webview/vite.config.ts``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from ..config import PipelineConfig
from .routes import extract, gameparams, library, repo, ships

logger = logging.getLogger(__name__)


def _app_for_reload() -> FastAPI:
    """Factory used by ``uvicorn --reload``.

    uvicorn's reload mode requires an import string; it pickles
    nothing. We resolve the workspace from the environment (the
    ``wows-webview-serve`` CLI stashes ``WOWS_WORKSPACE`` before
    delegating to uvicorn), build a config, and forward to
    :func:`create_app`.
    """
    return create_app(PipelineConfig.load())


def create_app(config: PipelineConfig) -> FastAPI:
    """Build a FastAPI app bound to the given pipeline config.

    The config carries the workspace + cache_dir paths every route
    handler needs. Routers are built fresh per call so unit tests
    can spin up multiple apps without state bleed.
    """
    app = FastAPI(
        title="wows-model-export webview backend",
        description=(
            "FastAPI port of the Node/Vite dev middleware that fronts "
            "the Svelte webview. Path A Stage 1 — opaque-stdout jobs, "
            "HTTP polling. See `webview/INTEGRATION_PLAN.md`."
        ),
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    logger.info("workspace: %s", config.workspace)

    # /api/* routers
    app.include_router(library.make_router(config), prefix="/api")
    app.include_router(ships.make_router(config), prefix="/api")
    app.include_router(gameparams.make_router(config), prefix="/api")
    app.include_router(extract.make_router(config), prefix="/api")

    # /repo/* static workspace file service. Mounted at /repo so the
    # path parameter captures the remainder.
    app.include_router(repo.make_router(config), prefix="/repo")

    return app


__all__ = ["create_app", "_app_for_reload"]
