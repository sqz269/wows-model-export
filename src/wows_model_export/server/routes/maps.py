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
import struct
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
_MAP_EXPORT_BODY = Body(default={})


def _maps_root(config: PipelineConfig) -> Path:
    return config.workspace / "maps"


def _space_cache_dir(config: PipelineConfig, name: str) -> Path:
    return _maps_root(config) / name


def _glb_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / f"{name}.glb"


def _meta_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "export.json"


def _collision_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "collision_manifest.json"


def _particle_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "particle_manifest.json"


def _static_decal_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "static_decal_manifest.json"


def _probe_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "probe_manifest.json"


def _user_object_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "user_object_manifest.json"


def _model_instance_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "model_instance_manifest.json"


def _point_light_manifest_path(config: PipelineConfig, name: str) -> Path:
    return _space_cache_dir(config, name) / "point_light_manifest.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_glb_json(glb_path: Path) -> dict[str, Any]:
    """Read the JSON chunk from a GLB without adding a glTF dependency."""
    with glb_path.open("rb") as fh:
        header = fh.read(12)
        if len(header) != 12:
            raise ValueError("GLB header is truncated")
        magic, version, total_len = struct.unpack("<4sII", header)
        if magic != b"glTF":
            raise ValueError("not a GLB file")
        if version != 2:
            raise ValueError(f"unsupported GLB version {version}")

        pos = 12
        while pos + 8 <= total_len:
            chunk_header = fh.read(8)
            if len(chunk_header) != 8:
                break
            chunk_len, chunk_type = struct.unpack("<II", chunk_header)
            pos += 8
            payload = fh.read(chunk_len)
            pos += chunk_len
            if len(payload) != chunk_len:
                raise ValueError("GLB chunk is truncated")
            if chunk_type == 0x4E4F534A:  # JSON
                return json.loads(payload.decode("utf-8"))

    raise ValueError("GLB JSON chunk not found")


def _primary_scene_extras(gltf: dict[str, Any]) -> dict[str, Any]:
    scenes = gltf.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {}
    scene_index = gltf.get("scene", 0)
    if not isinstance(scene_index, int) or scene_index < 0 or scene_index >= len(scenes):
        scene_index = 0
    scene = scenes[scene_index]
    if not isinstance(scene, dict):
        return {}
    extras = scene.get("extras")
    return extras if isinstance(extras, dict) else {}


def _write_particle_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.particles[]` into a direct JSON sidecar.

    The toolkit owns the byte-level `space.bin.particles[]` parser. This helper
    consumes only the exported GLB scene extras so the Python server does not
    grow a duplicate map parser.
    """
    gltf = _read_glb_json(glb_path)
    extras = _primary_scene_extras(gltf)
    particles = extras.get("particles")
    if not isinstance(particles, list):
        particles = []

    def _is_resolved(anchor: Any) -> bool:
        if not isinstance(anchor, dict):
            return False
        resource_path = anchor.get("resource_path")
        transform = anchor.get("transform")
        return (
            isinstance(resource_path, str)
            and bool(resource_path)
            and isinstance(transform, list)
            and len(transform) >= 16
        )

    paths = sorted(
        {
            str(anchor.get("resource_path"))
            for anchor in particles
            if isinstance(anchor, dict)
            and isinstance(anchor.get("resource_path"), str)
            and anchor.get("resource_path")
        }
    )
    doc: dict[str, Any] = {
        "schema": "wows.map.particle_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_glb": glb_path.name,
        "anchor_count": len(particles),
        "resolved_anchor_count": sum(1 for anchor in particles if _is_resolved(anchor)),
        "unique_resource_path_count": len(paths),
        "resource_paths": paths,
        "particles": particles,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def _write_static_decal_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.static_decals[]` into a direct JSON sidecar."""
    gltf = _read_glb_json(glb_path)
    extras = _primary_scene_extras(gltf)
    decals = extras.get("static_decals")
    if not isinstance(decals, list):
        decals = []

    def _has_valid_transform(decal: Any) -> bool:
        if not isinstance(decal, dict):
            return False
        transform = decal.get("transform")
        return isinstance(transform, list) and len(transform) >= 16

    texture_paths: set[str] = set()
    texture_triples: set[tuple[str, str, str]] = set()
    for decal in decals:
        if not isinstance(decal, dict):
            continue
        paths = decal.get("texture_paths")
        if not isinstance(paths, list):
            continue
        normalized = [str(path) for path in paths[:3]]
        while len(normalized) < 3:
            normalized.append("")
        texture_triples.add((normalized[0], normalized[1], normalized[2]))
        for path in normalized:
            if path:
                texture_paths.add(path)

    doc: dict[str, Any] = {
        "schema": "wows.map.static_decal_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_glb": glb_path.name,
        "decal_count": len(decals),
        "valid_transform_count": sum(1 for decal in decals if _has_valid_transform(decal)),
        "unique_texture_path_count": len(texture_paths),
        "unique_texture_triple_count": len(texture_triples),
        "texture_paths": sorted(texture_paths),
        "static_decals": decals,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def _write_probe_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.probes[]` into a direct JSON sidecar."""
    gltf = _read_glb_json(glb_path)
    extras = _primary_scene_extras(gltf)
    probes = extras.get("probes")
    if not isinstance(probes, list):
        probes = []

    def _has_valid_transform(probe: Any) -> bool:
        if not isinstance(probe, dict):
            return False
        transform = probe.get("transform")
        return isinstance(transform, list) and len(transform) >= 16

    guids: set[str] = set()
    names: set[str] = set()
    resolution_counts: dict[str, int] = {}
    main_probe_count = 0
    draw_full_scene_count = 0
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        guid = probe.get("guid")
        if isinstance(guid, str) and guid:
            guids.add(guid)
        probe_name = probe.get("name")
        if isinstance(probe_name, str) and probe_name:
            names.add(probe_name)
        resolution = probe.get("resolution")
        if isinstance(resolution, int):
            key = str(resolution)
            resolution_counts[key] = resolution_counts.get(key, 0) + 1
        if bool(probe.get("is_main_probe")):
            main_probe_count += 1
        if bool(probe.get("draw_full_scene")):
            draw_full_scene_count += 1

    doc: dict[str, Any] = {
        "schema": "wows.map.probe_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_glb": glb_path.name,
        "probe_count": len(probes),
        "valid_transform_count": sum(1 for probe in probes if _has_valid_transform(probe)),
        "main_probe_count": main_probe_count,
        "draw_full_scene_count": draw_full_scene_count,
        "unique_guid_count": len(guids),
        "unique_name_count": len(names),
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "guids": sorted(guids),
        "names": sorted(names),
        "probes": probes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def _write_user_object_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.user_objects[]` into a direct JSON sidecar."""
    gltf = _read_glb_json(glb_path)
    extras = _primary_scene_extras(gltf)
    objects = extras.get("user_objects")
    if not isinstance(objects, list):
        objects = []

    def _has_valid_transform(obj: Any) -> bool:
        if not isinstance(obj, dict):
            return False
        transform = obj.get("transform")
        return isinstance(transform, list) and len(transform) >= 16

    type_counts: dict[str, int] = {}
    property_tag_counts: dict[str, int] = {}
    property_path_counts: dict[str, int] = {}
    visible_model_reference_count = 0
    waypoint_edge_reference_count = 0
    well_formed_count = 0
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type")
        type_key = obj_type if isinstance(obj_type, str) and obj_type else "(unknown)"
        type_counts[type_key] = type_counts.get(type_key, 0) + 1
        if bool(obj.get("properties_well_formed")):
            well_formed_count += 1

        tags = obj.get("property_tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag:
                    property_tag_counts[tag] = property_tag_counts.get(tag, 0) + 1

        values = obj.get("property_values")
        if not isinstance(values, list):
            continue
        has_visible_model = False
        has_waypoint_edge = False
        for value in values:
            if not isinstance(value, dict):
                continue
            path = value.get("path")
            if not isinstance(path, str) or not path:
                continue
            property_path_counts[path] = property_path_counts.get(path, 0) + 1
            if path in {"model", "modelPath", "bargeModelPath", "sailorModelPath"}:
                has_visible_model = True
            if path.endswith(".guid") or path == "next.item.guid":
                has_waypoint_edge = True
        if has_visible_model:
            visible_model_reference_count += 1
        if has_waypoint_edge:
            waypoint_edge_reference_count += 1

    doc: dict[str, Any] = {
        "schema": "wows.map.user_object_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_glb": glb_path.name,
        "object_count": len(objects),
        "valid_transform_count": sum(1 for obj in objects if _has_valid_transform(obj)),
        "well_formed_properties_count": well_formed_count,
        "visible_model_reference_count": visible_model_reference_count,
        "waypoint_edge_reference_count": waypoint_edge_reference_count,
        "unique_type_count": len(type_counts),
        "type_counts": dict(sorted(type_counts.items())),
        "property_tag_counts": dict(sorted(property_tag_counts.items())),
        "property_path_counts": dict(sorted(property_path_counts.items())),
        "user_objects": objects,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def _write_model_instance_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract map model-instance node extras into a direct JSON sidecar."""
    gltf = _read_glb_json(glb_path)
    nodes = gltf.get("nodes")
    if not isinstance(nodes, list):
        nodes = []

    instances: list[dict[str, Any]] = []
    min_quality_counts: dict[str, int] = {}
    lod_extent_count_counts: dict[str, int] = {}
    stable_guids: set[str] = set()
    dye_pair_counts: dict[tuple[int, int], int] = {}
    landscape_count = 0
    valid_transform_count = 0
    dyed_instance_count = 0
    material_override_instance_count = 0
    material_instance_record_count = 0

    def _node_is_instance(extras: Any) -> bool:
        if not isinstance(extras, dict):
            return False
        return any(
            key in extras
            for key in (
                "is_landscape",
                "min_quality_level",
                "lod_extents",
                "stable_guid",
                "dyes",
                "material_instance_count",
                "material_instances",
            )
        )

    def _position_from_node(node: dict[str, Any]) -> list[float] | None:
        matrix = node.get("matrix")
        if isinstance(matrix, list) and len(matrix) >= 16:
            return [float(matrix[12]), float(matrix[13]), float(matrix[14])]
        translation = node.get("translation")
        if isinstance(translation, list) and len(translation) >= 3:
            return [float(translation[0]), float(translation[1]), float(translation[2])]
        return None

    def _material_record_count(extras: dict[str, Any]) -> int:
        records = extras.get("material_instances")
        if isinstance(records, list):
            return len(records)
        count = extras.get("material_instance_count")
        return count if isinstance(count, int) and count > 0 else 0

    for node_index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        extras = node.get("extras")
        if not _node_is_instance(extras):
            continue
        assert isinstance(extras, dict)

        is_landscape = bool(extras.get("is_landscape"))
        if is_landscape:
            landscape_count += 1
        if _position_from_node(node) is not None:
            valid_transform_count += 1
        min_quality = extras.get("min_quality_level")
        if isinstance(min_quality, int):
            key = str(min_quality)
            min_quality_counts[key] = min_quality_counts.get(key, 0) + 1
        lod_extents = extras.get("lod_extents")
        if isinstance(lod_extents, list):
            key = str(len(lod_extents))
            lod_extent_count_counts[key] = lod_extent_count_counts.get(key, 0) + 1
        stable_guid = extras.get("stable_guid")
        if isinstance(stable_guid, str) and stable_guid:
            stable_guids.add(stable_guid)

        dyes = extras.get("dyes")
        normalized_dyes: list[list[int]] = []
        if isinstance(dyes, list):
            for dye in dyes:
                if (
                    isinstance(dye, list)
                    and len(dye) >= 2
                    and isinstance(dye[0], int)
                    and isinstance(dye[1], int)
                ):
                    pair = (int(dye[0]), int(dye[1]))
                    normalized_dyes.append([pair[0], pair[1]])
                    dye_pair_counts[pair] = dye_pair_counts.get(pair, 0) + 1
        if normalized_dyes:
            dyed_instance_count += 1

        material_count = _material_record_count(extras)
        if material_count > 0:
            material_override_instance_count += 1
            material_instance_record_count += material_count

        record: dict[str, Any] = {
            "node_index": node_index,
            "name": node.get("name"),
            "mesh": node.get("mesh"),
            "matrix": node.get("matrix"),
            "translation": node.get("translation"),
            "rotation": node.get("rotation"),
            "scale": node.get("scale"),
            "position": _position_from_node(node),
            "is_landscape": is_landscape,
            "min_quality_level": min_quality,
            "lod_extents": lod_extents if isinstance(lod_extents, list) else None,
            "stable_guid": stable_guid if isinstance(stable_guid, str) else None,
            "dyes": normalized_dyes,
            "material_instance_count": material_count,
            "material_instances": extras.get("material_instances"),
            "extras": extras,
        }
        instances.append(record)

    dye_pairs = [
        {
            "matter_id": matter,
            "replaces_id": replaces,
            "matter_id_hex": f"0x{matter:08X}",
            "replaces_id_hex": f"0x{replaces:08X}",
            "count": count,
        }
        for (matter, replaces), count in sorted(
            dye_pair_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]

    doc: dict[str, Any] = {
        "schema": "wows.map.model_instance_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_glb": glb_path.name,
        "node_count": len(nodes),
        "instance_count": len(instances),
        "valid_transform_count": valid_transform_count,
        "landscape_count": landscape_count,
        "non_landscape_count": len(instances) - landscape_count,
        "stable_guid_count": sum(1 for instance in instances if instance.get("stable_guid")),
        "unique_stable_guid_count": len(stable_guids),
        "dyed_instance_count": dyed_instance_count,
        "dye_pair_count": sum(pair["count"] for pair in dye_pairs),
        "unique_dye_pair_count": len(dye_pairs),
        "material_override_instance_count": material_override_instance_count,
        "material_instance_record_count": material_instance_record_count,
        "min_quality_counts": dict(sorted(min_quality_counts.items())),
        "lod_extent_count_counts": dict(sorted(lod_extent_count_counts.items())),
        "dye_pairs": dye_pairs,
        "instances": instances,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def _write_point_light_manifest_from_space_bin(
    config: PipelineConfig,
    name: str,
    out_path: Path,
) -> dict[str, Any]:
    """Extract `space.bin.pointLights[]` into a direct JSON sidecar.

    The current toolkit exports static point-light extras into the GLB, but it
    skips the two authored animation prototype blocks. This narrow parser keeps
    those descriptors and their resolved point payloads visible for downstream
    validation while matching the native reader's relative-pointer base.
    """
    game_dir = config.require_game_dir()
    space_bin = game_dir / "res_unpack" / "spaces" / name / "space.bin"
    data = space_bin.read_bytes()
    if len(data) < 0x60:
        raise ValueError(f"{space_bin} is too small for a space.bin header")

    def _u32(offset: int) -> int:
        return struct.unpack_from("<I", data, offset)[0]

    def _i64(offset: int) -> int:
        return struct.unpack_from("<q", data, offset)[0]

    def _f32(offset: int) -> float:
        return struct.unpack_from("<f", data, offset)[0]

    def _f32s(offset: int, count: int) -> list[float]:
        return [float(v) for v in struct.unpack_from(f"<{count}f", data, offset)]

    def _hex(offset: int, size: int) -> str:
        return data[offset:offset + size].hex()

    def _world_position(matrix: list[float], local: list[float]) -> list[float]:
        x, y, z = local
        return [
            matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
            matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
            matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
        ]

    point_light_count = _u32(0x0C)
    point_light_offset = struct.unpack_from("<Q", data, 0x38)[0]
    record_stride = 0xC0
    table_end = point_light_offset + point_light_count * record_stride
    valid_record_count = 0
    if point_light_offset <= len(data):
        valid_record_count = max(0, min(point_light_count, (len(data) - point_light_offset) // record_stride))

    color_descriptor_count = 0
    radius_descriptor_count = 0
    color_point_count = 0
    radius_point_count = 0
    color_nonzero_count = 0
    radius_nonzero_count = 0
    color_oob_count = 0
    radius_oob_count = 0
    animated_color_flag_count = 0
    animated_radius_flag_count = 0
    opaque_nonzero_count = 0
    min_quality_counts: dict[str, int] = {}
    radii: list[float] = []
    lights: list[dict[str, Any]] = []

    def _read_animation(record_offset: int, block_offset: int, value_count: int) -> dict[str, Any]:
        descriptor_base = record_offset + block_offset + 0x10
        point_count = _u32(descriptor_base)
        relptr = _i64(descriptor_base + 0x08)
        point_stride = 0x04 + value_count * 0x04
        target = descriptor_base + relptr
        payload_size = point_count * point_stride
        target_in_range = (
            point_count == 0
            or (0 <= target <= len(data) and target + payload_size <= len(data))
        )
        points: list[dict[str, Any]] = []
        nonzero_payload = False
        if point_count > 0 and target_in_range:
            for point_index in range(point_count):
                point_offset = target + point_index * point_stride
                values = _f32s(point_offset + 0x04, value_count)
                time_value = _f32(point_offset)
                if time_value != 0.0 or any(value != 0.0 for value in values):
                    nonzero_payload = True
                point_doc: dict[str, Any] = {
                    "index": point_index,
                    "offset": point_offset,
                    "offset_hex": f"0x{point_offset:X}",
                    "time": time_value,
                    "value": values if value_count > 1 else values[0],
                }
                points.append(point_doc)

        return {
            "block_offset": block_offset,
            "block_offset_hex": f"0x{record_offset + block_offset:X}",
            "prefix_hex": _hex(record_offset + block_offset, 0x10),
            "descriptor_base": descriptor_base,
            "descriptor_base_hex": f"0x{descriptor_base:X}",
            "descriptor_raw_hex": _hex(descriptor_base, 0x10),
            "point_count": point_count,
            "relptr": relptr,
            "relptr_hex": f"0x{relptr & 0xFFFFFFFFFFFFFFFF:016X}",
            "target": target,
            "target_hex": f"0x{target:X}",
            "point_stride": point_stride,
            "target_in_range": target_in_range,
            "payload_size": payload_size,
            "nonzero_payload": nonzero_payload,
            "points": points,
        }

    for index in range(valid_record_count):
        record_offset = point_light_offset + index * record_stride
        matrix = _f32s(record_offset, 16)
        local_position = _f32s(record_offset + 0xA0, 3)
        color = _f32s(record_offset + 0x90, 4)
        radius = _f32(record_offset + 0xB0)
        min_quality = _u32(record_offset + 0xB4)
        flags_tail = data[record_offset + 0xB8:record_offset + 0xC0]
        animated_color_flag = bool(flags_tail[0]) if len(flags_tail) >= 1 else False
        animated_radius_flag = bool(flags_tail[1]) if len(flags_tail) >= 2 else False
        opaque_hex = _hex(record_offset + 0x40, 0x10)
        color_animation = _read_animation(record_offset, 0x50, 4)
        radius_animation = _read_animation(record_offset, 0x70, 1)

        if opaque_hex != "00" * 0x10:
            opaque_nonzero_count += 1
        if color_animation["point_count"] > 0:
            color_descriptor_count += 1
            color_point_count += int(color_animation["point_count"])
        if radius_animation["point_count"] > 0:
            radius_descriptor_count += 1
            radius_point_count += int(radius_animation["point_count"])
        if bool(color_animation["nonzero_payload"]):
            color_nonzero_count += 1
        if bool(radius_animation["nonzero_payload"]):
            radius_nonzero_count += 1
        if not bool(color_animation["target_in_range"]):
            color_oob_count += 1
        if not bool(radius_animation["target_in_range"]):
            radius_oob_count += 1
        if animated_color_flag:
            animated_color_flag_count += 1
        if animated_radius_flag:
            animated_radius_flag_count += 1
        min_quality_key = str(min_quality)
        min_quality_counts[min_quality_key] = min_quality_counts.get(min_quality_key, 0) + 1
        radii.append(radius)

        lights.append(
            {
                "index": index,
                "record_offset": record_offset,
                "record_offset_hex": f"0x{record_offset:X}",
                "transform": matrix,
                "local_position": local_position,
                "world_position": _world_position(matrix, local_position),
                "color": color,
                "radius": radius,
                "min_quality": min_quality,
                "opaque_0x40_hex": opaque_hex,
                "animated_color_flag": animated_color_flag,
                "animated_radius_flag": animated_radius_flag,
                "flags_tail_hex": flags_tail.hex(),
                "color_animation": color_animation,
                "radius_animation": radius_animation,
            }
        )

    doc: dict[str, Any] = {
        "schema": "wows.map.point_light_manifest.v1",
        "space": name,
        "generated_at": _now_iso(),
        "source_space_bin": f"spaces/{name}/space.bin",
        "source_space_bin_path": str(space_bin),
        "record_stride": record_stride,
        "point_light_table_offset": point_light_offset,
        "point_light_table_offset_hex": f"0x{point_light_offset:X}",
        "point_light_table_end": table_end,
        "point_light_table_end_hex": f"0x{table_end:X}",
        "light_count": point_light_count,
        "valid_record_count": valid_record_count,
        "truncated_record_count": point_light_count - valid_record_count,
        "opaque_descriptor_nonzero_count": opaque_nonzero_count,
        "animated_color_flag_count": animated_color_flag_count,
        "animated_radius_flag_count": animated_radius_flag_count,
        "color_animation_descriptor_count": color_descriptor_count,
        "radius_animation_descriptor_count": radius_descriptor_count,
        "color_animation_point_count": color_point_count,
        "radius_animation_point_count": radius_point_count,
        "color_animation_payload_nonzero_count": color_nonzero_count,
        "radius_animation_payload_nonzero_count": radius_nonzero_count,
        "color_animation_oob_count": color_oob_count,
        "radius_animation_oob_count": radius_oob_count,
        "min_quality_counts": dict(sorted(min_quality_counts.items())),
        "radius_min": min(radii) if radii else None,
        "radius_max": max(radii) if radii else None,
        "lights": lights,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


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
            "max_texture_size",
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
            "elapsed_ms": int(result.elapsed_ms),
            "stderr": result.stderr,
        }
        if particle_manifest_error:
            meta_doc["particle_manifest_error"] = particle_manifest_error
        if static_decal_manifest_error:
            meta_doc["static_decal_manifest_error"] = static_decal_manifest_error
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
