"""``GET /api/aircraft`` — the aircraft library index.

Returns ``libraries/aircraft/index.json`` (written by
:func:`compose.build_aircraft_library`). The GLBs and DDS files it names
are served by the ``/repo`` static route like every other library.

503 when the index is missing: the hint names the CLI that builds it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the ``/api/aircraft`` router bound to ``config.workspace``."""
    router = APIRouter()
    index_path = config.workspace / "libraries" / "aircraft" / "index.json"

    @router.get("/aircraft")
    def get_aircraft() -> JSONResponse:
        index = _read_json(index_path)
        if index is None:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "aircraft_library_missing",
                    "missing": [str(index_path)],
                    "hint": (
                        "Build the aircraft library: wows-build-aircraft-library "
                        "<model or GameParams key> ... (or --all)."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse(
            content={"ok": True, "index": index},
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
