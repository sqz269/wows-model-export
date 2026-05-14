"""``GET /api/ships`` — per-ship summary list.

Walks ``<workspace>/ships/`` for entries that have BOTH a hull GLB
(``<name>_hull.glb``) AND a placements JSON
(``<name>_accessories.json``), returning enough metadata for the ship
picker sidebar to render without a second fetch (section counts,
sidecar-derived nation / class / tier, hull mtime for cache-busting).

Port of ``webview/src/server/endpoints/ships.ts``. Response shape and
field names match byte-for-byte — see the client at
``webview/src/lib/api/extract.ts::fetchExtractedShips``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


def _count_sections(placements_path: Path) -> dict[str, int]:
    """Read the placements JSON and tally the five mount section lengths.

    Matches the Node implementation: any parse error yields all-zeros so
    the SPA keeps rendering — the picker can show the ship name even
    without counts.
    """
    empty = {
        "turrets": 0,
        "secondaries": 0,
        "antiair": 0,
        "torpedoes": 0,
        "accessories": 0,
    }
    try:
        with placements_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return empty
    out = dict(empty)
    for key in empty:
        val = doc.get(key)
        if isinstance(val, list):
            out[key] = len(val)
    return out


def _read_sidecar(sidecar_path: Path) -> dict[str, Any] | None:
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _workspace_rel(p: Path, workspace: Path) -> str:
    """Forward-slash relative path. Matches the Node version which
    explicitly normalises ``path.sep`` to ``'/'`` before sending."""
    try:
        rel = p.relative_to(workspace)
    except ValueError:
        return str(p)
    return rel.as_posix()


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    workspace = config.workspace
    ships_root = workspace / "ships"

    @router.get("/ships")
    def get_ships() -> JSONResponse:
        if not ships_root.exists() or not ships_root.is_dir():
            return JSONResponse(
                content={"ships": []},
                headers={"Cache-Control": "no-cache"},
            )
        try:
            entries = sorted(p for p in ships_root.iterdir() if p.is_dir())
        except OSError as err:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": str(err)},
            )

        ships: list[dict[str, Any]] = []
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            hull = entry / "models" / f"{name}_hull.glb"
            placements = entry / "models" / f"{name}_accessories.json"
            if not hull.is_file() or not placements.is_file():
                continue
            try:
                hull_stat = hull.stat()
            except OSError:
                continue
            sidecar_path = entry / f"{name}.meta.json"
            sidecar = (
                _read_sidecar(sidecar_path) if sidecar_path.is_file() else None
            )
            ship_meta: dict[str, Any] = (
                sidecar.get("ship") if isinstance(sidecar, dict) else None
            ) or {}

            display_name = ship_meta.get("display_name") or name
            nation = ship_meta.get("nation")
            ship_class = ship_meta.get("class")
            tier_raw = ship_meta.get("tier")
            tier: int | None = tier_raw if isinstance(tier_raw, int) else None

            ships.append(
                {
                    "name": name,
                    "display_name": display_name,
                    "nation": nation,
                    "ship_class": ship_class,
                    "tier": tier,
                    "hull_glb": _workspace_rel(hull, workspace),
                    "accessories_json": _workspace_rel(placements, workspace),
                    "sidecar_json": (
                        _workspace_rel(sidecar_path, workspace)
                        if sidecar is not None
                        else None
                    ),
                    "hull_bytes": hull_stat.st_size,
                    # Match Node's floor(mtimeMs / 1000) — seconds, int.
                    "hull_mtime": int(hull_stat.st_mtime),
                    "section_counts": _count_sections(placements),
                }
            )

        ships.sort(key=lambda s: s["name"])
        return JSONResponse(
            content={"ships": ships},
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
