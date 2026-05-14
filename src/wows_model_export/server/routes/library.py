"""``GET /api/library`` — accessory library index.

Reads ``<workspace>/libraries/accessories/index.json`` on every request
so the SPA stays in sync with library rebuilds without a manual
refresh. File missing → 404 with a hint at how to generate it.

Port of ``webview/src/server/endpoints/library.ts``. The response on
the 200 path is the raw JSON bytes — the client parses; we don't.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the ``/api/library`` router bound to ``config.workspace``."""
    router = APIRouter()
    workspace = config.workspace
    index_path: Path = workspace / "libraries" / "accessories" / "index.json"

    @router.get("/library")
    def get_library() -> Response:
        if not index_path.exists():
            return JSONResponse(
                status_code=404,
                content={
                    "error": "library_index_missing",
                    "path": str(index_path),
                    "hint": (
                        "Run `wows-build-accessory-library` to generate it."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        try:
            raw = index_path.read_bytes()
        except OSError as err:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": str(err)},
            )
        # Pass the bytes through verbatim — the Node version sent the
        # file contents without re-encoding too. Saves a round-trip
        # through Python's json module for a large file.
        return Response(
            content=raw,
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
