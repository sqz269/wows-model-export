"""Map / dock / operations-space endpoints for the webview.

Three routes:

  ``GET  /api/maps``                 — list all available spaces (battle
                                        maps, docks, ops scenarios)
  ``POST /api/maps/{name}/export``   — wowsunpack export-map → GLB cached
                                        under <workspace>/maps/<name>/
  ``GET  /api/maps/{name}/glb``      — serve the cached GLB (404 if not
                                        yet exported)

This is Phase 1 of the maps webview — sync export, no job system. A
modest battle map exports in 3-8 seconds on the release-build toolkit
(see audit `map_extraction_audit_2026_05_21.md`), short enough to
block the request without needing polling. If/when texture caps or
LOD-0 forest fixes push the time past ~30s we'll graduate to the
:mod:`jobs` async pattern the long-running endpoints use.

The workspace layout is:

    <workspace>/maps/
        14_Atlantic/
            14_Atlantic.glb           ← cached toolkit output
            export.json               ← {generated_at, flags, glb_size, ...}
        dock_Dunkirk/
            dock_Dunkirk.glb
            export.json
        ...

Exports are idempotent: re-POSTing overwrites in place. The webview
viewer (Maps.svelte) reads `/api/maps/{name}/glb` directly.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse, Response

from ...config import PipelineConfig
from ...errors import ToolkitError
from ...toolkit import export_map, list_spaces


# Space names are filesystem-safe: digits, letters, underscore, dash.
# Constrains URL path params + the on-disk cache dir name. The toolkit's
# own naming (e.g. `s02_Naval_Defense`, `dock_BDAY2024`) all match.
_SPACE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _maps_root(config: PipelineConfig) -> Path:
    return config.workspace / "maps"


def _space_cache_dir(config: PipelineConfig, name: str) -> Path:
    return _maps_root(config) / name


def _glb_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / f"{name}.glb"


def _meta_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "export.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _classify_space(name: str) -> str:
    """Bucket a space name into ``battle`` / ``dock`` / ``ops`` / ``other``.

    Useful for the webview to group the picker. Heuristic matches the
    audit doc's categorisation:

    - ``NN_<name>`` → battle map (e.g. ``14_Atlantic``)
    - ``Dock`` or ``dock_<name>`` → dock environment
    - ``sNN_<name>`` → operations scenario
    """
    if re.match(r"^\d{2}_", name):
        return "battle"
    if name == "Dock" or name.startswith("Dock_") or name.startswith("dock_"):
        return "dock"
    if re.match(r"^s\d{2}_", name):
        return "ops"
    return "other"


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the maps router bound to ``config.workspace`` + game_dir."""
    router = APIRouter()
    maps_root = _maps_root(config)

    # ── GET /api/maps ──────────────────────────────────────────────────
    # Lists every space visible via list_spaces() (res_unpack scan or
    # VFS-manifest fallback). Tags each entry with its on-disk cache
    # state so the webview can show "exported / not exported" without a
    # second round-trip per row.
    @router.get("/maps")
    def get_maps() -> JSONResponse:
        try:
            vfs_paths = list_spaces(config)
        except Exception as err:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": f"{type(err).__name__}: {err}",
                },
            )

        if not vfs_paths:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": "no_spaces_found",
                    "hint": (
                        "Couldn't find spaces/ under <game_dir>/res_unpack/ "
                        "and no cached VFS manifest exists. Launch WoWS once "
                        "(populates res_unpack/) or build the manifest via "
                        "the VFS endpoint."
                    ),
                },
            )

        items: list[dict[str, Any]] = []
        for vfs_path in vfs_paths:
            name = vfs_path.split("/", 1)[1]  # strip "spaces/"
            glb = _glb_path(config, name)
            meta = _meta_path(config, name)
            entry: dict[str, Any] = {
                "name": name,
                "vfs_path": vfs_path,
                "category": _classify_space(name),
                "exported": glb.is_file(),
            }
            if glb.is_file():
                try:
                    entry["glb_size"] = glb.stat().st_size
                except OSError:
                    pass
            if meta.is_file():
                try:
                    entry["export"] = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            items.append(entry)

        return JSONResponse(content={"ok": True, "items": items})

    # ── POST /api/maps/{name}/export ───────────────────────────────────
    # Synchronously runs `wowsunpack export-map`. Cached under
    # workspace/maps/<name>/. Flags are mirrored from the toolkit CLI
    # so the client can pass through what it needs:
    #   {"max_texture_size": int|null, "terrain_step": int,
    #    "no_textures": bool, "no_vegetation": bool, "no_water": bool,
    #    "no_terrain": bool, "lod": int, "vegetation_density": float}
    # All optional; defaults match the toolkit's defaults.
    @router.post("/maps/{name}/export")
    def post_export_map(
        name: str, body: dict[str, Any] = Body(default={})
    ) -> JSONResponse:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )

        cache_dir = _space_cache_dir(config, name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        glb_out = _glb_path(config, name)

        # Pull out only the kwargs export_map accepts; ignore unknowns.
        # This keeps the wire format permissive (the client can include
        # forward-compat fields) without surprising the toolkit wrapper.
        kwargs: dict[str, Any] = {}
        for key in (
            "lod", "terrain_step", "no_terrain", "no_water",
            "no_vegetation", "no_textures", "vegetation_density",
            "max_texture_size",
        ):
            if key in body and body[key] is not None:
                kwargs[key] = body[key]

        try:
            result = export_map(
                f"spaces/{name}",
                glb_out,
                config=config,
                **kwargs,
            )
        except ToolkitError as err:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "error": str(err),
                    "stderr": err.stderr or "",
                    "exit_code": err.exit_code,
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

        # Persist an export record so the list endpoint can show
        # "exported at <time> with <flags>". Best-effort — a failed
        # write doesn't fail the export.
        meta_doc = {
            "schema": "wows_map_export/v1",
            "generated_at": _now_iso(),
            "flags": kwargs,
            "glb_size": glb_out.stat().st_size if glb_out.is_file() else None,
            "elapsed_ms": int(result.elapsed_ms),
            "stderr": result.stderr,
        }
        try:
            _meta_path(config, name).write_text(
                json.dumps(meta_doc, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

        return JSONResponse(
            content={
                "ok": True,
                "name": name,
                "glb_path": str(glb_out),
                "glb_size": meta_doc["glb_size"],
                "elapsed_ms": meta_doc["elapsed_ms"],
                "flags": kwargs,
            }
        )

    # ── GET /api/maps/{name}/glb ───────────────────────────────────────
    # Serves the exported GLB directly. Content-Type model/gltf-binary so
    # the browser doesn't try to decode it as text. The webview's
    # three.js loader fetches via this route.
    @router.get("/maps/{name}/glb")
    def get_map_glb(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        glb = _glb_path(config, name)
        if not glb.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "glb_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=glb,
            media_type="model/gltf-binary",
            filename=f"{name}.glb",
        )

    # ── DELETE /api/maps/{name} ────────────────────────────────────────
    # Wipes the on-disk cache for one map (GLB + export.json). Useful
    # when re-exporting with different flags shouldn't keep stale
    # artefacts around between runs.
    @router.delete("/maps/{name}")
    def delete_map_cache(name: str) -> JSONResponse:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        cache_dir = _space_cache_dir(config, name)
        removed: list[str] = []
        for p in (_glb_path(config, name), _meta_path(config, name)):
            try:
                if p.is_file():
                    p.unlink()
                    removed.append(p.name)
            except OSError:
                pass
        # Best-effort rmdir; leaves the parent if other files (sidecar
        # JSONs, raw_dds_dir/, future per-instance data) live there.
        try:
            if cache_dir.is_dir() and not any(cache_dir.iterdir()):
                cache_dir.rmdir()
        except OSError:
            pass
        return JSONResponse(content={"ok": True, "removed": removed})

    # Silence unused-binding lint for `maps_root` — kept as a hook for
    # follow-up endpoints (e.g. cache-clear-all, bulk export).
    _ = maps_root

    return router


__all__ = ["make_router"]
