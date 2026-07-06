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
            collision_manifest.json   ← optional obstacle/collision sidecar
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
import traceback
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse, Response

from ...config import PipelineConfig
from ...errors import ToolkitError
from ...sky_assets import extract_sky_assets
from ...toolkit import export_decal_textures, export_map, list_spaces

# The manifest/sidecar writers live in ``wows_model_export.maps_export``
# (shared with the ``wows-export-map`` CLI); imported under the historic
# route-local names so the router body below is untouched.
from ...maps_export import (
    SPACE_NAME as _SPACE_NAME,
    classify_space as _classify_space,
    maps_root as _maps_root,
    collision_manifest_path as _collision_manifest_path,
    glb_path as _glb_path,
    meta_path as _meta_path,
    model_instance_manifest_path as _model_instance_manifest_path,
    now_iso as _now_iso,
    particle_manifest_path as _particle_manifest_path,
    point_light_manifest_path as _point_light_manifest_path,
    primary_scene_extras as _primary_scene_extras,
    probe_manifest_path as _probe_manifest_path,
    read_glb_json as _read_glb_json,
    space_cache_dir as _space_cache_dir,
    static_decal_manifest_path as _static_decal_manifest_path,
    user_object_manifest_path as _user_object_manifest_path,
    write_model_instance_manifest_from_glb as _write_model_instance_manifest_from_glb,
    write_particle_manifest_from_glb as _write_particle_manifest_from_glb,
    write_point_light_manifest_from_space_bin as _write_point_light_manifest_from_space_bin,
    write_probe_manifest_from_glb as _write_probe_manifest_from_glb,
    write_static_decal_manifest_from_glb as _write_static_decal_manifest_from_glb,
    write_user_object_manifest_from_glb as _write_user_object_manifest_from_glb,
)

_MAP_EXPORT_BODY = Body(default={})

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
            collision_manifest = _collision_manifest_path(config, name)
            particle_manifest = _particle_manifest_path(config, name)
            static_decal_manifest = _static_decal_manifest_path(config, name)
            probe_manifest = _probe_manifest_path(config, name)
            user_object_manifest = _user_object_manifest_path(config, name)
            model_instance_manifest = _model_instance_manifest_path(config, name)
            point_light_manifest = _point_light_manifest_path(config, name)
            entry: dict[str, Any] = {
                "name": name,
                "vfs_path": vfs_path,
                "category": _classify_space(name),
                "exported": glb.is_file(),
                "collision_manifest_exported": collision_manifest.is_file(),
                "particle_manifest_exported": particle_manifest.is_file(),
                "static_decal_manifest_exported": static_decal_manifest.is_file(),
                "probe_manifest_exported": probe_manifest.is_file(),
                "user_object_manifest_exported": user_object_manifest.is_file(),
                "model_instance_manifest_exported": model_instance_manifest.is_file(),
                "point_light_manifest_exported": point_light_manifest.is_file(),
            }
            if glb.is_file():
                try:
                    entry["glb_size"] = glb.stat().st_size
                except OSError:
                    pass
            if collision_manifest.is_file():
                try:
                    entry["collision_manifest_size"] = collision_manifest.stat().st_size
                except OSError:
                    pass
            if particle_manifest.is_file():
                try:
                    entry["particle_manifest_size"] = particle_manifest.stat().st_size
                except OSError:
                    pass
            if static_decal_manifest.is_file():
                try:
                    entry["static_decal_manifest_size"] = static_decal_manifest.stat().st_size
                except OSError:
                    pass
            if probe_manifest.is_file():
                try:
                    entry["probe_manifest_size"] = probe_manifest.stat().st_size
                except OSError:
                    pass
            if user_object_manifest.is_file():
                try:
                    entry["user_object_manifest_size"] = user_object_manifest.stat().st_size
                except OSError:
                    pass
            if model_instance_manifest.is_file():
                try:
                    entry["model_instance_manifest_size"] = model_instance_manifest.stat().st_size
                except OSError:
                    pass
            if point_light_manifest.is_file():
                try:
                    entry["point_light_manifest_size"] = point_light_manifest.stat().st_size
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
    #    "no_terrain": bool, "lod": int, "vegetation_density": float,
    #    "collision_manifest": bool}
    # All optional; defaults match the toolkit's defaults.
    @router.post("/maps/{name}/export")
    def post_export_map(
        name: str, body: dict[str, Any] = _MAP_EXPORT_BODY
    ) -> JSONResponse:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )

        cache_dir = _space_cache_dir(config, name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        glb_out = _glb_path(config, name)
        collision_manifest_out = _collision_manifest_path(config, name)
        particle_manifest_out = _particle_manifest_path(config, name)
        static_decal_manifest_out = _static_decal_manifest_path(config, name)
        probe_manifest_out = _probe_manifest_path(config, name)
        user_object_manifest_out = _user_object_manifest_path(config, name)
        model_instance_manifest_out = _model_instance_manifest_path(config, name)
        point_light_manifest_out = _point_light_manifest_path(config, name)
        want_collision_manifest = bool(body.get("collision_manifest"))
        if not want_collision_manifest and collision_manifest_out.is_file():
            try:
                collision_manifest_out.unlink()
            except OSError:
                pass

        # Pull out only the kwargs export_map accepts; ignore unknowns.
        # This keeps the wire format permissive (the client can include
        # forward-compat fields) without surprising the toolkit wrapper.
        kwargs: dict[str, Any] = {}
        for key in (
            "lod", "terrain_step", "no_terrain", "no_water",
            "no_vegetation", "no_textures", "vegetation_density",
            "max_texture_size", "lightmap_density",
        ):
            if key in body and body[key] is not None:
                kwargs[key] = body[key]

        try:
            result = export_map(
                f"spaces/{name}",
                glb_out,
                config=config,
                collision_manifest_json=collision_manifest_out if want_collision_manifest else None,
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

        particle_manifest_doc: dict[str, Any] | None = None
        particle_manifest_error: str | None = None
        try:
            particle_manifest_doc = _write_particle_manifest_from_glb(
                name,
                glb_out,
                particle_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            particle_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if particle_manifest_out.is_file():
                    particle_manifest_out.unlink()
            except OSError:
                pass

        static_decal_manifest_doc: dict[str, Any] | None = None
        static_decal_manifest_error: str | None = None
        try:
            static_decal_manifest_doc = _write_static_decal_manifest_from_glb(
                name,
                glb_out,
                static_decal_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            static_decal_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if static_decal_manifest_out.is_file():
                    static_decal_manifest_out.unlink()
            except OSError:
                pass

        # Decal texture payload (PNG store + decal_textures.json mapping).
        # Separate toolkit invocation (`wowsunpack export-decals`) because
        # the map GLB embeds only model/terrain textures — decal textures
        # were never part of the GLB. Best-effort like the manifests.
        decal_textures_doc: dict[str, Any] | None = None
        decal_textures_error: str | None = None
        decal_textures_dir = cache_dir / "decal_textures"
        try:
            export_decal_textures(
                f"spaces/{name}",
                decal_textures_dir,
                config=config,
            )
            decal_textures_doc = json.loads(
                (decal_textures_dir / "decal_textures.json").read_text(encoding="utf-8")
            )
        except Exception as err:  # noqa: BLE001
            decal_textures_error = f"{type(err).__name__}: {err}"

        probe_manifest_doc: dict[str, Any] | None = None
        probe_manifest_error: str | None = None
        try:
            probe_manifest_doc = _write_probe_manifest_from_glb(
                name,
                glb_out,
                probe_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            probe_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if probe_manifest_out.is_file():
                    probe_manifest_out.unlink()
            except OSError:
                pass

        user_object_manifest_doc: dict[str, Any] | None = None
        user_object_manifest_error: str | None = None
        try:
            user_object_manifest_doc = _write_user_object_manifest_from_glb(
                name,
                glb_out,
                user_object_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            user_object_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if user_object_manifest_out.is_file():
                    user_object_manifest_out.unlink()
            except OSError:
                pass

        model_instance_manifest_doc: dict[str, Any] | None = None
        model_instance_manifest_error: str | None = None
        try:
            model_instance_manifest_doc = _write_model_instance_manifest_from_glb(
                name,
                glb_out,
                model_instance_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            model_instance_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if model_instance_manifest_out.is_file():
                    model_instance_manifest_out.unlink()
            except OSError:
                pass

        point_light_manifest_doc: dict[str, Any] | None = None
        point_light_manifest_error: str | None = None
        try:
            point_light_manifest_doc = _write_point_light_manifest_from_space_bin(
                config,
                name,
                point_light_manifest_out,
            )
        except Exception as err:  # noqa: BLE001
            point_light_manifest_error = f"{type(err).__name__}: {err}"
            try:
                if point_light_manifest_out.is_file():
                    point_light_manifest_out.unlink()
            except OSError:
                pass

        # Per-weather sky/IBL assets: join the GLB's `weathers[]` extras to
        # the on-disk lightcube probes and emit equirect .hdr environment
        # maps + sky_manifest.json. Best-effort per weather.
        sky_manifest_doc: dict[str, Any] | None = None
        sky_manifest_error: str | None = None
        scene_extras_doc: dict[str, Any] | None = None
        try:
            gltf_doc = _read_glb_json(glb_out)
            sky_extras = _primary_scene_extras(gltf_doc)
            if isinstance(sky_extras, dict):
                scene_extras_doc = sky_extras
            res_unpack = config.require_game_dir() / "res_unpack"
            sky_manifest_doc = extract_sky_assets(sky_extras, res_unpack, glb_out.parent)
            # Same byte format as maps_export.run_export (ensure_ascii off,
            # trailing newline) so route/CLI runs produce identical files.
            (glb_out.parent / "sky_manifest.json").write_text(
                json.dumps(sky_manifest_doc, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as err:  # noqa: BLE001
            sky_manifest_error = f"{type(err).__name__}: {err}"

        # Raw terrain heightfield sidecar: echo the GLB's `terrain_heightmap`
        # extras (+ on-disk sidecar size) so consumers can discover it from
        # export.json without opening the GLB.
        terrain_heightmap_doc: dict[str, Any] | None = None
        thm = (scene_extras_doc or {}).get("terrain_heightmap")
        if isinstance(thm, dict) and thm.get("file"):
            sidecar = glb_out.parent / str(thm["file"])
            terrain_heightmap_doc = dict(thm)
            terrain_heightmap_doc["size"] = (
                sidecar.stat().st_size if sidecar.is_file() else None
            )

        # Persist an export record so the list endpoint can show
        # "exported at <time> with <flags>". Best-effort — a failed
        # write doesn't fail the export.
        meta_doc = {
            "schema": "wows_map_export/v1",
            "generated_at": _now_iso(),
            "flags": kwargs,
            "glb_size": glb_out.stat().st_size if glb_out.is_file() else None,
            "collision_manifest_size": (
                collision_manifest_out.stat().st_size
                if collision_manifest_out.is_file()
                else None
            ),
            "particle_manifest_size": (
                particle_manifest_out.stat().st_size
                if particle_manifest_out.is_file()
                else None
            ),
            "static_decal_manifest_size": (
                static_decal_manifest_out.stat().st_size
                if static_decal_manifest_out.is_file()
                else None
            ),
            "probe_manifest_size": (
                probe_manifest_out.stat().st_size
                if probe_manifest_out.is_file()
                else None
            ),
            "user_object_manifest_size": (
                user_object_manifest_out.stat().st_size
                if user_object_manifest_out.is_file()
                else None
            ),
            "model_instance_manifest_size": (
                model_instance_manifest_out.stat().st_size
                if model_instance_manifest_out.is_file()
                else None
            ),
            "point_light_manifest_size": (
                point_light_manifest_out.stat().st_size
                if point_light_manifest_out.is_file()
                else None
            ),
            "sky_manifest": (
                {
                    "schema": sky_manifest_doc.get("schema"),
                    "weather_count": sky_manifest_doc.get("weather_count"),
                    "extracted_count": sky_manifest_doc.get("extracted_count"),
                }
                if sky_manifest_doc
                else None
            ),
            "sky_manifest_error": sky_manifest_error,
            "particle_manifest": (
                {
                    "schema": particle_manifest_doc.get("schema"),
                    "anchor_count": particle_manifest_doc.get("anchor_count"),
                    "resolved_anchor_count": particle_manifest_doc.get("resolved_anchor_count"),
                    "unique_resource_path_count": particle_manifest_doc.get(
                        "unique_resource_path_count"
                    ),
                }
                if particle_manifest_doc
                else None
            ),
            "static_decal_manifest": (
                {
                    "schema": static_decal_manifest_doc.get("schema"),
                    "decal_count": static_decal_manifest_doc.get("decal_count"),
                    "valid_transform_count": static_decal_manifest_doc.get(
                        "valid_transform_count"
                    ),
                    "unique_texture_path_count": static_decal_manifest_doc.get(
                        "unique_texture_path_count"
                    ),
                    "unique_texture_triple_count": static_decal_manifest_doc.get(
                        "unique_texture_triple_count"
                    ),
                }
                if static_decal_manifest_doc
                else None
            ),
            "decal_textures": (
                {
                    "schema": decal_textures_doc.get("schema"),
                    "texture_count": decal_textures_doc.get("texture_count"),
                    "error_count": len(decal_textures_doc.get("errors") or {}),
                    "dir": str(decal_textures_dir),
                }
                if decal_textures_doc
                else None
            ),
            "probe_manifest": (
                {
                    "schema": probe_manifest_doc.get("schema"),
                    "probe_count": probe_manifest_doc.get("probe_count"),
                    "valid_transform_count": probe_manifest_doc.get(
                        "valid_transform_count"
                    ),
                    "main_probe_count": probe_manifest_doc.get("main_probe_count"),
                    "draw_full_scene_count": probe_manifest_doc.get(
                        "draw_full_scene_count"
                    ),
                    "unique_guid_count": probe_manifest_doc.get("unique_guid_count"),
                    "unique_name_count": probe_manifest_doc.get("unique_name_count"),
                    "resolution_counts": probe_manifest_doc.get("resolution_counts"),
                }
                if probe_manifest_doc
                else None
            ),
            "user_object_manifest": (
                {
                    "schema": user_object_manifest_doc.get("schema"),
                    "object_count": user_object_manifest_doc.get("object_count"),
                    "valid_transform_count": user_object_manifest_doc.get(
                        "valid_transform_count"
                    ),
                    "well_formed_properties_count": user_object_manifest_doc.get(
                        "well_formed_properties_count"
                    ),
                    "visible_model_reference_count": user_object_manifest_doc.get(
                        "visible_model_reference_count"
                    ),
                    "waypoint_edge_reference_count": user_object_manifest_doc.get(
                        "waypoint_edge_reference_count"
                    ),
                    "unique_type_count": user_object_manifest_doc.get("unique_type_count"),
                    "type_counts": user_object_manifest_doc.get("type_counts"),
                }
                if user_object_manifest_doc
                else None
            ),
            "model_instance_manifest": (
                {
                    "schema": model_instance_manifest_doc.get("schema"),
                    "instance_count": model_instance_manifest_doc.get("instance_count"),
                    "valid_transform_count": model_instance_manifest_doc.get(
                        "valid_transform_count"
                    ),
                    "landscape_count": model_instance_manifest_doc.get("landscape_count"),
                    "stable_guid_count": model_instance_manifest_doc.get(
                        "stable_guid_count"
                    ),
                    "dyed_instance_count": model_instance_manifest_doc.get(
                        "dyed_instance_count"
                    ),
                    "dye_pair_count": model_instance_manifest_doc.get("dye_pair_count"),
                    "unique_dye_pair_count": model_instance_manifest_doc.get(
                        "unique_dye_pair_count"
                    ),
                    "material_override_instance_count": model_instance_manifest_doc.get(
                        "material_override_instance_count"
                    ),
                    "material_instance_record_count": model_instance_manifest_doc.get(
                        "material_instance_record_count"
                    ),
                    "min_quality_counts": model_instance_manifest_doc.get(
                        "min_quality_counts"
                    ),
                }
                if model_instance_manifest_doc
                else None
            ),
            "point_light_manifest": (
                {
                    "schema": point_light_manifest_doc.get("schema"),
                    "light_count": point_light_manifest_doc.get("light_count"),
                    "valid_record_count": point_light_manifest_doc.get(
                        "valid_record_count"
                    ),
                    "animated_color_flag_count": point_light_manifest_doc.get(
                        "animated_color_flag_count"
                    ),
                    "animated_radius_flag_count": point_light_manifest_doc.get(
                        "animated_radius_flag_count"
                    ),
                    "color_animation_descriptor_count": point_light_manifest_doc.get(
                        "color_animation_descriptor_count"
                    ),
                    "radius_animation_descriptor_count": point_light_manifest_doc.get(
                        "radius_animation_descriptor_count"
                    ),
                    "color_animation_payload_nonzero_count": point_light_manifest_doc.get(
                        "color_animation_payload_nonzero_count"
                    ),
                    "radius_animation_payload_nonzero_count": point_light_manifest_doc.get(
                        "radius_animation_payload_nonzero_count"
                    ),
                    "color_animation_oob_count": point_light_manifest_doc.get(
                        "color_animation_oob_count"
                    ),
                    "radius_animation_oob_count": point_light_manifest_doc.get(
                        "radius_animation_oob_count"
                    ),
                    "min_quality_counts": point_light_manifest_doc.get(
                        "min_quality_counts"
                    ),
                    "radius_min": point_light_manifest_doc.get("radius_min"),
                    "radius_max": point_light_manifest_doc.get("radius_max"),
                }
                if point_light_manifest_doc
                else None
            ),
            "terrain_heightmap": terrain_heightmap_doc,
            "elapsed_ms": int(result.elapsed_ms),
            "stderr": result.stderr,
        }
        if particle_manifest_error:
            meta_doc["particle_manifest_error"] = particle_manifest_error
        if static_decal_manifest_error:
            meta_doc["static_decal_manifest_error"] = static_decal_manifest_error
        if decal_textures_error:
            meta_doc["decal_textures_error"] = decal_textures_error
        if probe_manifest_error:
            meta_doc["probe_manifest_error"] = probe_manifest_error
        if user_object_manifest_error:
            meta_doc["user_object_manifest_error"] = user_object_manifest_error
        if model_instance_manifest_error:
            meta_doc["model_instance_manifest_error"] = model_instance_manifest_error
        if point_light_manifest_error:
            meta_doc["point_light_manifest_error"] = point_light_manifest_error
        if want_collision_manifest:
            meta_doc["flags"]["collision_manifest"] = True
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
                "collision_manifest_path": (
                    str(collision_manifest_out)
                    if collision_manifest_out.is_file()
                    else None
                ),
                "collision_manifest_size": meta_doc["collision_manifest_size"],
                "particle_manifest_path": (
                    str(particle_manifest_out)
                    if particle_manifest_out.is_file()
                    else None
                ),
                "particle_manifest_size": meta_doc["particle_manifest_size"],
                "particle_manifest": meta_doc["particle_manifest"],
                "particle_manifest_error": meta_doc.get("particle_manifest_error"),
                "static_decal_manifest_path": (
                    str(static_decal_manifest_out)
                    if static_decal_manifest_out.is_file()
                    else None
                ),
                "static_decal_manifest_size": meta_doc["static_decal_manifest_size"],
                "static_decal_manifest": meta_doc["static_decal_manifest"],
                "static_decal_manifest_error": meta_doc.get("static_decal_manifest_error"),
                # Gated on THIS run's doc (not is_file()) so a failed
                # re-export can't advertise a stale store from a prior run.
                "decal_textures_dir": (
                    str(decal_textures_dir) if decal_textures_doc is not None else None
                ),
                "decal_textures": meta_doc["decal_textures"],
                "decal_textures_error": meta_doc.get("decal_textures_error"),
                "probe_manifest_path": (
                    str(probe_manifest_out)
                    if probe_manifest_out.is_file()
                    else None
                ),
                "probe_manifest_size": meta_doc["probe_manifest_size"],
                "probe_manifest": meta_doc["probe_manifest"],
                "probe_manifest_error": meta_doc.get("probe_manifest_error"),
                "user_object_manifest_path": (
                    str(user_object_manifest_out)
                    if user_object_manifest_out.is_file()
                    else None
                ),
                "user_object_manifest_size": meta_doc["user_object_manifest_size"],
                "user_object_manifest": meta_doc["user_object_manifest"],
                "user_object_manifest_error": meta_doc.get("user_object_manifest_error"),
                "model_instance_manifest_path": (
                    str(model_instance_manifest_out)
                    if model_instance_manifest_out.is_file()
                    else None
                ),
                "model_instance_manifest_size": meta_doc["model_instance_manifest_size"],
                "model_instance_manifest": meta_doc["model_instance_manifest"],
                "model_instance_manifest_error": meta_doc.get("model_instance_manifest_error"),
                "point_light_manifest_path": (
                    str(point_light_manifest_out)
                    if point_light_manifest_out.is_file()
                    else None
                ),
                "point_light_manifest_size": meta_doc["point_light_manifest_size"],
                "point_light_manifest": meta_doc["point_light_manifest"],
                "point_light_manifest_error": meta_doc.get("point_light_manifest_error"),
                "sky_manifest": meta_doc.get("sky_manifest"),
                "sky_manifest_error": meta_doc.get("sky_manifest_error"),
                "elapsed_ms": meta_doc["elapsed_ms"],
                "flags": meta_doc["flags"],
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

    # ── GET /api/maps/{name}/sky-manifest ─────────────────────────────
    # Per-weather sky/IBL manifest: which weather presets have an
    # extracted equirect environment .hdr (from the engine's per-weather
    # lightcube probes).
    @router.get("/maps/{name}/sky-manifest")
    def get_sky_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _space_cache_dir(config, name) / "sky_manifest.json"
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "sky_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="sky_manifest.json",
        )

    # ── GET /api/maps/{name}/sky/{weather}/env_cube.hdr ───────────────
    # Serves an extracted per-weather equirect environment map (Radiance
    # RGBE). Weather names are sanitized at write time; reject anything
    # that isn't a plain path component here.
    @router.get("/maps/{name}/sky/{weather}/env_cube.hdr")
    def get_sky_env(name: str, weather: str) -> Response:
        if not _SPACE_NAME.match(name) or not re.fullmatch(r"[A-Za-z0-9_-]+", weather):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid name"},
            )
        hdr = _space_cache_dir(config, name) / "sky" / weather / "env_cube.hdr"
        if not hdr.is_file():
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "sky_env_not_exported"},
            )
        return FileResponse(
            path=hdr,
            media_type="image/vnd.radiance",
            filename="env_cube.hdr",
        )

    # ── GET /api/maps/{name}/collision-manifest ───────────────────────
    # Serves the optional collision manifest sidecar emitted by
    # `export-map --collision-manifest-json`. The webview uses this for
    # debug/proxy overlays; absence means re-export with
    # {"collision_manifest": true}.
    @router.get("/maps/{name}/collision-manifest")
    def get_collision_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _collision_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "collision_manifest_not_exported",
                    "hint": (
                        f"POST /api/maps/{name}/export with "
                        '{"collision_manifest": true} first.'
                    ),
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="collision_manifest.json",
        )

    # ── GET /api/maps/{name}/particle-manifest ───────────────────────
    # Serves the map-authored particle anchor sidecar extracted from the
    # exported GLB scene extras. This is a direct JSON contract for non-web
    # consumers such as Unity debug tooling; Effect prototype data still comes
    # from the shared particle library.
    @router.get("/maps/{name}/particle-manifest")
    def get_particle_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _particle_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "particle_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="particle_manifest.json",
        )

    # ── GET /api/maps/{name}/static-decal-manifest ───────────────────
    # Serves authored map static decals extracted from the exported GLB scene
    # extras. These are fixed map/projector records, not dynamic combat decal
    # events from the dyndecals library.
    @router.get("/maps/{name}/static-decal-manifest")
    def get_static_decal_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _static_decal_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "static_decal_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="static_decal_manifest.json",
        )

    # ── GET /api/maps/{name}/probe-manifest ──────────────────────────
    # Serves authored map probe records extracted from exported GLB scene
    # extras. This preserves the `space.bin.probes[]` layer separately from
    # the weather/environment PMREM manifest.
    @router.get("/maps/{name}/probe-manifest")
    def get_probe_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _probe_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "probe_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="probe_manifest.json",
        )

    # ── GET /api/maps/{name}/user-object-manifest ────────────────────
    # Serves authored map user objects extracted from exported GLB scene
    # extras. These are static authoring records, not live BattleLogic or
    # replay entity state.
    @router.get("/maps/{name}/user-object-manifest")
    def get_user_object_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _user_object_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "user_object_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="user_object_manifest.json",
        )

    # ── GET /api/maps/{name}/model-instance-manifest ─────────────────
    # Serves map model-instance placement/adjunct metadata extracted from GLB
    # node extras: stable GUIDs, LOD/min-quality metadata, dyes, and shallow or
    # decoded material-instance override records where the toolkit emitted them.
    @router.get("/maps/{name}/model-instance-manifest")
    def get_model_instance_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _model_instance_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "model_instance_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="model_instance_manifest.json",
        )

    # ── GET /api/maps/{name}/point-light-manifest ────────────────────
    # Serves direct `space.bin.pointLights[]` data, including the authored
    # color/radius animation prototype descriptors that the GLB extras do not
    # currently preserve.
    @router.get("/maps/{name}/point-light-manifest")
    def get_point_light_manifest(name: str) -> Response:
        if not _SPACE_NAME.match(name):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid space name"},
            )
        manifest = _point_light_manifest_path(config, name)
        if not manifest.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": "point_light_manifest_not_exported",
                    "hint": f"POST /api/maps/{name}/export first.",
                },
            )
        return FileResponse(
            path=manifest,
            media_type="application/json",
            filename="point_light_manifest.json",
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
        for p in (
            _glb_path(config, name),
            _collision_manifest_path(config, name),
            _particle_manifest_path(config, name),
            _static_decal_manifest_path(config, name),
            _probe_manifest_path(config, name),
            _user_object_manifest_path(config, name),
            _model_instance_manifest_path(config, name),
            _point_light_manifest_path(config, name),
            _meta_path(config, name),
        ):
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
