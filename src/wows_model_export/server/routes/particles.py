"""``/api/particles`` — single-particle inspector backend.

Two endpoints serve the `#/particles` route (single-particle inspector):

  ``GET /api/particles``                       — list every particle path
  ``GET /api/particles/record?path=<base>``    — parsed Effect record + texture URLs

The :class:`wows_model_export.read.particles.ParticleStore` is opened
lazily on the first call and held for the process lifetime (mmap +
name-index parse is one second on the typical assets.bin; per-record
decode is cached internally so subsequent ``get`` calls are O(1)).

Texture URLs are stamped on the response by walking the already-extracted
``content/effects_textures/`` cache. We do **not** extract on demand here
(toolkit invocations are slow and the inspector is a tight feedback loop
— better to render with the procedural-disc fallback than block the
request for ~5 s). A future ``POST /api/particles/extract`` route can
trigger extraction when a user clicks "load textures".
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ...compose.effects_textures import (
    ATLAS_MANIFEST_VFS_PATH,
    TEXTURE_CACHE_ROOT,
    _strip_content_prefix,
    parse_atlas_manifest,
    stamp_atlas_urls,
)
from ...config import PipelineConfig
from ...read.particles import ParticleStore
from ...toolkit import assets_bin as _assets_bin


def _stamp_existing_texture_urls(
    record: dict[str, Any], workspace: Path,
) -> tuple[int, list[str]]:
    """Stamp ``texture_url_*`` URLs on every system's renderer/animation
    block when the matching DDS exists in the workspace cache.

    Returns ``(stamped_count, missing_paths)``. Does NOT extract — that
    would block the request on a toolkit subprocess.
    """
    cache_root = (workspace / TEXTURE_CACHE_ROOT).resolve()
    stamped = 0
    missing: list[str] = []
    for system in record.get("systems") or []:
        if not isinstance(system, dict):
            continue
        rend = system.get("renderer")
        if isinstance(rend, dict):
            for src_key, url_key in (
                ("textureName0", "textureUrl0"),
                ("textureName1", "textureUrl1"),
            ):
                vfs_path = rend.get(src_key)
                if not isinstance(vfs_path, str) or not vfs_path:
                    continue
                rel = _strip_content_prefix(vfs_path)
                on_disk = (cache_root / rel).resolve()
                rel_url = (TEXTURE_CACHE_ROOT / rel).as_posix()
                if on_disk.is_file():
                    rend[url_key] = rel_url
                    stamped += 1
                else:
                    missing.append(vfs_path)
        anim = system.get("animation")
        if isinstance(anim, dict):
            vfs_path = anim.get("motionVectorsTexture")
            if isinstance(vfs_path, str) and vfs_path:
                rel = _strip_content_prefix(vfs_path)
                on_disk = (cache_root / rel).resolve()
                rel_url = (TEXTURE_CACHE_ROOT / rel).as_posix()
                if on_disk.is_file():
                    anim["motionVectorsTextureUrl"] = rel_url
                    stamped += 1
                else:
                    missing.append(vfs_path)
    return stamped, missing


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the particles router bound to ``config``."""
    router = APIRouter()

    # Lazy-initialised ParticleStore, held for the process lifetime.
    # Opening costs ~1 s of name-index build; per-record decode is
    # cached internally on the store object. A lock guards the first-
    # time open against parallel inbound requests.
    _store_lock = threading.Lock()
    _store_ref: dict[str, ParticleStore | None] = {"v": None}

    # Lazy-initialised atlas manifest. Empty dict when no manifest is on
    # disk (library_particles.build() hasn't run yet — manifest extract
    # happens during the build pass).
    _atlas_lock = threading.Lock()
    _atlas_ref: dict[str, dict[str, Any] | None] = {"v": None}

    def _get_atlas_map() -> dict[str, Any]:
        if _atlas_ref["v"] is not None:
            return _atlas_ref["v"]
        with _atlas_lock:
            if _atlas_ref["v"] is not None:
                return _atlas_ref["v"]
            manifest_path = (
                config.workspace
                / TEXTURE_CACHE_ROOT
                / _strip_content_prefix(ATLAS_MANIFEST_VFS_PATH)
            )
            atlas_map: dict[str, Any] = {}
            if manifest_path.is_file():
                try:
                    atlas_map = parse_atlas_manifest(manifest_path)
                except Exception:  # noqa: BLE001
                    atlas_map = {}
            _atlas_ref["v"] = atlas_map
            return atlas_map

    def _get_store() -> ParticleStore | str:
        """Return the cached ParticleStore, opening it on demand.

        Returns a string error message instead of raising — the
        endpoints surface that as a 503.
        """
        if _store_ref["v"] is not None:
            return _store_ref["v"]
        with _store_lock:
            if _store_ref["v"] is not None:
                return _store_ref["v"]
            try:
                ab = _assets_bin.default_path(config)
            except Exception as err:  # noqa: BLE001
                return f"failed to resolve assets.bin path: {err}"
            if not ab.is_file():
                return (
                    f"assets.bin missing at {ab}. Run an extract first "
                    "(scaffold a ship, or set $WOWS_ASSETS_BIN)."
                )
            try:
                _store_ref["v"] = ParticleStore.open(ab)
            except Exception as err:  # noqa: BLE001
                return f"failed to open ParticleStore: {err}"
            return _store_ref["v"]

    @router.get("/particles")
    def list_particles() -> JSONResponse:
        store = _get_store()
        if isinstance(store, str):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": store},
            )
        names = store.names()
        return JSONResponse(
            content={
                "ok": True,
                "particles": names,
                "count": len(names),
                "record_count": store.record_count(),
            },
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/particles/record")
    def get_particle_record(
        path: str = Query(default=""),
        quality: str = Query(default="high"),
    ) -> JSONResponse:
        if not path:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "path is required"},
            )
        store = _get_store()
        if isinstance(store, str):
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": store},
            )
        qualities = store.qualities_for(path)
        rec = store.get(path, quality=quality)
        if rec is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": f"particle not found in assets.bin: {path}",
                },
            )

        # Deep-copy the decoded record so url-stamping doesn't mutate
        # the store's per-record cache. ParticleStore caches the decoded
        # dict by record_index; mutating it would persist mistakes across
        # requests.
        import copy
        rec = copy.deepcopy(rec)

        stamped, missing = _stamp_existing_texture_urls(rec, config.workspace)
        # Atlas mapping: stamp `textureAtlas0/1` / `motionVectorsTextureAtlas`
        # on every renderer/animation block whose `textureName*` basename
        # appears in the cached manifest. The manifest is built by
        # library_particles.build(); when absent (cold workspace), atlas
        # refs simply fall through to the procedural-disc fallback.
        atlas_map = _get_atlas_map()
        atlas_stamped = (
            stamp_atlas_urls({path: rec}, atlas_map) if atlas_map else 0
        )
        return JSONResponse(
            content={
                "ok": True,
                "path": path,
                "qualities": qualities,
                "quality_used": quality if quality in qualities else (
                    qualities[0] if qualities else None
                ),
                "record": rec,
                "textures_stamped": stamped,
                "textures_missing": missing,
                "atlas_stamped": atlas_stamped,
            },
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["make_router"]
