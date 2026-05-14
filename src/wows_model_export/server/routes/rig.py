"""Rig override + rebuild endpoints for the accessory library.

Three routes:

  ``GET    /api/rig-overrides?assetId=X`` — read the asset's sidecar
  ``POST   /api/rig-overrides?assetId=X`` — write the sidecar
  ``DELETE /api/rig-overrides?assetId=X`` — clear the sidecar
  ``POST   /api/rig-rebuild``             — spawn ``wows-turret-autorig``

The override sidecar (``<asset_id>.rig_overrides.json``) lives next to
the asset's GLB. Shape (mirrors the rigger's :class:`RigOverrides`)::

    {
      "schema":   "wows_rig_overrides/v1",
      "asset_id": "<id>",
      "authored_at": "<iso8601>",         # stamped server-side on write
      "category_overrides": [
        {"fingerprint": {"center": [x, y, z], "verts": int},
         "category":    "body" | "elev" | "skin",
         "note":        "..."}            # optional
      ],
      "face_plate": {
        "fingerprint": {"center": [x, y, z], "verts": int},
        "note": "..."                     # optional
      }
    }

The rebuild endpoint spawns ``wows-turret-autorig <asset_id>`` and waits
for completion. The current ``wows-turret-autorig`` doesn't emit the
``.rig.debug.glb`` debug scene that the rig editor's picker consumes —
that's a known gap (see the legacy ``tools/ship/turret_rig.py
--debug-scene`` path on the I:-side repo). The endpoint still works for
the pivot-extraction half: a successful rebuild refreshes the
``.rig_pivots.json`` that the viewer overlay reads.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from ...config import PipelineConfig


# Asset id: alnum + underscore. Same shape ``wows-turret-autorig``
# validates internally; we mirror it here so a bad assetId fails fast
# at the HTTP layer rather than after a spawn.
_ASSET_ID = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _accessories_root(config: PipelineConfig) -> Path:
    return config.workspace / "libraries" / "accessories"


def _library_index_path(config: PipelineConfig) -> Path:
    return _accessories_root(config) / "index.json"


def _resolve_asset_dir(
    config: PipelineConfig, asset_id: str
) -> tuple[Path, Path] | None:
    """Return ``(asset_dir, override_path)`` from the library index.

    Returns ``None`` when the asset isn't registered in ``index.json`` —
    the caller surfaces 404. Resolving via the index (rather than a
    filesystem walk) keeps us correct as the library tree grows new
    scopes / categories.
    """
    idx_path = _library_index_path(config)
    if not idx_path.is_file():
        return None
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = (idx.get("assets") or {}).get(asset_id)
    if not entry:
        return None
    glb_rel = entry.get("glb")
    if not isinstance(glb_rel, str) or not glb_rel:
        return None
    accessories_root = _accessories_root(config)
    asset_dir = (accessories_root / glb_rel).parent
    override_path = asset_dir / f"{asset_id}.rig_overrides.json"
    return (asset_dir, override_path)


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the rig router bound to ``config.workspace``."""
    router = APIRouter()
    workspace = config.workspace

    # ── /api/rig-overrides ──────────────────────────────────────────────
    @router.get("/rig-overrides")
    def get_rig_overrides(assetId: str = Query(default="")) -> JSONResponse:
        if not _ASSET_ID.match(assetId):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid assetId"},
            )
        resolved = _resolve_asset_dir(config, assetId)
        if resolved is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "asset not in library index",
                },
            )
        _, override_path = resolved
        if not override_path.exists():
            return JSONResponse(
                content={"ok": True, "exists": False, "doc": None}
            )
        try:
            doc = json.loads(override_path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": f"parse failed: {err}",
                },
            )
        return JSONResponse(
            content={"ok": True, "exists": True, "doc": doc}
        )

    @router.post("/rig-overrides")
    def post_rig_overrides(
        assetId: str = Query(default=""),
        body: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        if not _ASSET_ID.match(assetId):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid assetId"},
            )
        resolved = _resolve_asset_dir(config, assetId)
        if resolved is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "asset not in library index",
                },
            )
        asset_dir, override_path = resolved

        schema = str(body.get("schema") or "")
        if not schema.startswith("wows_rig_overrides/"):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "schema must start with wows_rig_overrides/",
                },
            )
        if body.get("asset_id") != assetId:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": (
                        f"asset_id mismatch: body says "
                        f"{body.get('asset_id')!r}, URL says {assetId!r}"
                    ),
                },
            )
        try:
            stamped = {**body, "authored_at": _now_iso()}
            asset_dir.mkdir(parents=True, exist_ok=True)
            override_path.write_text(
                json.dumps(stamped, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return JSONResponse(
                content={"ok": True, "path": str(override_path)}
            )
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": str(err)},
            )

    @router.delete("/rig-overrides")
    def delete_rig_overrides(assetId: str = Query(default="")) -> JSONResponse:
        if not _ASSET_ID.match(assetId):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid assetId"},
            )
        resolved = _resolve_asset_dir(config, assetId)
        if resolved is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "asset not in library index",
                },
            )
        _, override_path = resolved
        if override_path.exists():
            try:
                override_path.unlink()
            except Exception as err:  # noqa: BLE001
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": str(err)},
                )
        return JSONResponse(content={"ok": True, "deleted": True})

    # ── /api/rig-rebuild ────────────────────────────────────────────────
    # Spawns ``wows-turret-autorig <asset_id>``. Sync subprocess — the
    # rigger is fast for a single asset (~1 s) and surfacing structured
    # job state isn't worth the complexity at the typical click rate.
    @router.post("/rig-rebuild")
    async def post_rig_rebuild(body: dict[str, Any] = Body(default={})) -> JSONResponse:
        asset_id = str(body.get("assetId") or "")
        if not _ASSET_ID.match(asset_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid assetId"},
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                "wows-turret-autorig",
                asset_id,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await proc.communicate()
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": f"wows-turret-autorig exit={proc.returncode}",
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                )
            return JSONResponse(
                content={"ok": True, "stdout": stdout, "stderr": stderr}
            )
        except FileNotFoundError as err:
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": (
                        f"wows-turret-autorig not on PATH: {err}. "
                        "Install the package or run the rigger CLI from a "
                        "shell where it's available."
                    ),
                },
            )
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": f"{type(err).__name__}: {err}",
                    "traceback": traceback.format_exc(),
                },
            )

    return router


__all__ = ["make_router"]
