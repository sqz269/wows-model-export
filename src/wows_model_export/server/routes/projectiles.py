"""``GET /api/projectiles`` — projectile geometry index + ammo profiles.

The Projectiles tab needs BOTH artifacts written by
:func:`compose.build_projectile_library` (``libraries/projectiles/
index.json`` — per-asset GLB + textures) AND
:func:`compose.build_ammo_profiles` (``libraries/projectiles/
ammo_profiles.json`` — per-ammo ballistic data). We return both in one
payload so the client only has to round-trip once on page mount.

The join is left to the client: many ammo_profiles share one asset_id
(every artillery shell uses ``CPA001_Shell_Main``), so server-side
join would duplicate the index entry N times instead of letting the
client de-dup at render time.

503 when either file is missing — the empty-state UI directs the user
to the Settings page's "projectiles" bootstrap target which builds
both files in one job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file as a dict. Returns ``None`` for missing/unreadable/
    non-dict roots."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the ``/api/projectiles`` router bound to ``config.workspace``."""
    router = APIRouter()
    workspace = config.workspace
    lib_dir = workspace / "libraries" / "projectiles"
    index_path = lib_dir / "index.json"
    ammo_path = lib_dir / "ammo_profiles.json"

    @router.get("/projectiles")
    def get_projectiles() -> JSONResponse:
        index = _read_json(index_path)
        ammo = _read_json(ammo_path)
        if index is None or ammo is None:
            missing: list[str] = []
            if index is None:
                missing.append(str(index_path))
            if ammo is None:
                missing.append(str(ammo_path))
            return JSONResponse(
                status_code=503,
                content={
                    "ok":      False,
                    "error":   "projectiles_artifacts_missing",
                    "missing": missing,
                    "hint": (
                        "Build the projectile library + ammo profiles "
                        "from Settings -> Workspace artifacts."
                    ),
                },
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse(
            content={
                "ok":            True,
                "index":         index,
                "ammo_profiles": ammo,
            },
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
