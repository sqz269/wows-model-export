"""``GET /api/gameparams/status`` — cache age + size + path.

Used by the Extract page's "GameParams cache stale?" banner. Port of
the matching middleware in
``webview/src/server/endpoints/extract.ts``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    cache_dir = config.cache_dir or (config.workspace / ".cache")
    gp_path: Path = cache_dir / "gameparams.json"

    @router.get("/gameparams/status")
    def status() -> JSONResponse:
        if not gp_path.exists():
            return JSONResponse(
                content={
                    "exists": False,
                    "path": str(gp_path),
                    "hint": (
                        "Run `wows-find-ship-variants --refresh` to "
                        "populate it."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        try:
            st = gp_path.stat()
        except OSError as err:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": str(err)},
            )
        mtime = st.st_mtime
        # JS Date#toISOString uses millisecond precision (e.g.
        # "2026-05-13T19:46:00.123Z"). Match that exactly — Python's
        # default isoformat is microsecond precision, which the client
        # would still parse but renders ugly in the UI.
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        ms = dt.microsecond // 1000
        mtime_iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"
        size_mb = round(st.st_size / (1024 * 1024), 1)
        return JSONResponse(
            content={
                "exists": True,
                "path": str(gp_path),
                "size_mb": size_mb,
                # Match Node's floor(mtimeMs / 1000) — seconds, int.
                "mtime": int(mtime),
                "mtime_iso": mtime_iso,
            },
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
