"""Headless map-export core: GLB export + every sidecar + export.json.

Extracted from ``server/routes/maps.py`` (2026-07-06) so the manifest
post-pass has a real, supported home outside the webview route: the
FastAPI route and the ``wows-export-map`` CLI both call these writers.
The workspace layout is::

    <workspace>/maps/<name>/
        <name>.glb                    - toolkit output
        particle_manifest.json        - scene.extras.particles[]
        static_decal_manifest.json    - scene.extras.static_decals[]
        probe_manifest.json           - scene.extras.probes[]
        user_object_manifest.json     - scene.extras.user_objects[]
        model_instance_manifest.json  - GLB node extras
        point_light_manifest.json     - re-parsed from space.bin
        decal_textures/               - PNG store + decal_textures.json
        sky_manifest.json + *.hdr     - per-weather equirect skies
        terrain_heightmap.r16 (+ lightmap png) - written by the toolkit
        export.json                   - meta record (sizes, summaries, and
                                        the terrain_heightmap block that
                                        consumers use to register the
                                        heightfield without opening the GLB)

:func:`run_export` is the ``wows-export-map`` CLI backend - one space
end-to-end, soft per-manifest failures recorded as ``*_error`` keys in
the returned meta doc, hard GLB-export failures raised as
:class:`ToolkitError`.
"""

from __future__ import annotations

import json
import re
import struct
import time
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .errors import ToolkitError
from .sky_assets import extract_sky_assets
from .toolkit import export_decal_textures, export_map

# Space names are filesystem-safe: digits, letters, underscore, dash.
# Constrains URL path params, CLI args, and the on-disk cache dir name.
SPACE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def maps_root(config: PipelineConfig) -> Path:
    return config.workspace / "maps"


def space_cache_dir(config: PipelineConfig, name: str) -> Path:
    return maps_root(config) / name


def glb_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / f"{name}.glb"


def meta_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "export.json"


def collision_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "collision_manifest.json"


def particle_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "particle_manifest.json"


def static_decal_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "static_decal_manifest.json"


def probe_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "probe_manifest.json"


def user_object_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "user_object_manifest.json"


def model_instance_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "model_instance_manifest.json"


def point_light_manifest_path(config: PipelineConfig, name: str) -> Path:
    return space_cache_dir(config, name) / "point_light_manifest.json"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_glb_json(glb_path: Path) -> dict[str, Any]:
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


def primary_scene_extras(gltf: dict[str, Any]) -> dict[str, Any]:
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


def write_particle_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.particles[]` into a direct JSON sidecar.

    The toolkit owns the byte-level `space.bin.particles[]` parser. This helper
    consumes only the exported GLB scene extras so the Python server does not
    grow a duplicate map parser.
    """
    gltf = read_glb_json(glb_path)
    extras = primary_scene_extras(gltf)
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
        "generated_at": now_iso(),
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


def write_static_decal_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.static_decals[]` into a direct JSON sidecar."""
    gltf = read_glb_json(glb_path)
    extras = primary_scene_extras(gltf)
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
        "generated_at": now_iso(),
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


def write_probe_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.probes[]` into a direct JSON sidecar."""
    gltf = read_glb_json(glb_path)
    extras = primary_scene_extras(gltf)
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
        "generated_at": now_iso(),
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


def write_user_object_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract `scene.extras.user_objects[]` into a direct JSON sidecar."""
    gltf = read_glb_json(glb_path)
    extras = primary_scene_extras(gltf)
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
        "generated_at": now_iso(),
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


def write_model_instance_manifest_from_glb(name: str, glb_path: Path, out_path: Path) -> dict[str, Any]:
    """Extract map model-instance node extras into a direct JSON sidecar."""
    gltf = read_glb_json(glb_path)
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
            "local_mesh": extras.get("local_mesh"),
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
        "generated_at": now_iso(),
        "source_glb": glb_path.name,
        "node_count": len(nodes),
        "instance_count": len(instances),
        "valid_transform_count": valid_transform_count,
        "landscape_count": landscape_count,
        "non_landscape_count": len(instances) - landscape_count,
        "local_mesh_instance_count": sum(1 for instance in instances if instance.get("local_mesh")),
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


def write_point_light_manifest_from_space_bin(
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
        "generated_at": now_iso(),
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


def classify_space(name: str) -> str:
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


# ---- headless one-space export (CLI backend) -------------------------


# Small count-dicts the route's hand-picked summaries carry; kept so
# CLI-written export.json summaries match route-written ones.
_SUMMARY_DICT_KEYS = frozenset(
    {"resolution_counts", "type_counts", "min_quality_counts"}
)


def _slim_summary(doc: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """Scalar-only view of a manifest doc for the export.json record:
    drops the record arrays, keeps schema/counts plus the small
    count-dicts the route summaries surface."""
    if not isinstance(doc, dict):
        return None
    return {
        k: v
        for k, v in doc.items()
        if not isinstance(v, (list, dict)) or k in _SUMMARY_DICT_KEYS
    }


def run_export(
    name: str,
    *,
    config: "PipelineConfig | None" = None,
    flags: "dict[str, Any] | None" = None,
    collision_manifest: bool = False,
    skip_glb: bool = False,
    skip_decal_textures: bool = False,
) -> "dict[str, Any]":
    """Export one space end-to-end and write ``export.json``.

    ``flags`` are the :func:`wows_model_export.toolkit.export_map`
    keyword args (``lod``, ``terrain_step``, ``lightmap_density``, ...).
    ``skip_glb`` reuses an already-exported GLB and re-runs only the
    sidecar post-pass. Soft failures (individual manifests, decal
    textures, sky) are recorded as ``*_error`` keys in the returned
    meta doc; a hard GLB-export failure raises ``ToolkitError``.
    """
    if not SPACE_NAME.match(name):
        raise ValueError(f"invalid space name: {name!r}")
    cfg = config or PipelineConfig.load()
    flags = dict(flags or {})

    cache_dir = space_cache_dir(cfg, name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    glb_out = glb_path(cfg, name)
    collision_out = collision_manifest_path(cfg, name)

    elapsed_ms: "int | None" = None
    stderr = ""
    # Route semantics regardless of skip_glb: a run that didn't request
    # the collision manifest must not leave a stale one advertised.
    if not collision_manifest and collision_out.is_file():
        try:
            collision_out.unlink()
        except OSError:
            pass
    if skip_glb:
        if not glb_out.is_file():
            raise FileNotFoundError(
                f"{glb_out} missing - run without skip_glb to export it"
            )
    else:
        result = export_map(
            f"spaces/{name}",
            glb_out,
            config=cfg,
            collision_manifest_json=collision_out if collision_manifest else None,
            **flags,
        )
        elapsed_ms = int(result.elapsed_ms)
        stderr = result.stderr

    # GLB-derived manifests, best-effort each (a failure deletes its own
    # partial output and records an error, matching the route).
    glb_writers = {
        "particle_manifest": (
            particle_manifest_path(cfg, name),
            write_particle_manifest_from_glb,
        ),
        "static_decal_manifest": (
            static_decal_manifest_path(cfg, name),
            write_static_decal_manifest_from_glb,
        ),
        "probe_manifest": (
            probe_manifest_path(cfg, name),
            write_probe_manifest_from_glb,
        ),
        "user_object_manifest": (
            user_object_manifest_path(cfg, name),
            write_user_object_manifest_from_glb,
        ),
        "model_instance_manifest": (
            model_instance_manifest_path(cfg, name),
            write_model_instance_manifest_from_glb,
        ),
    }
    docs: "dict[str, dict[str, Any] | None]" = {}
    errors: "dict[str, str]" = {}
    for key, (out_path, writer) in glb_writers.items():
        try:
            docs[key] = writer(name, glb_out, out_path)
        except Exception as err:  # noqa: BLE001
            errors[key] = f"{type(err).__name__}: {err}"
            docs[key] = None
            try:
                if out_path.is_file():
                    out_path.unlink()
            except OSError:
                pass

    # Point lights re-parse space.bin directly (animation prototype
    # blocks the GLB omits).
    point_light_out = point_light_manifest_path(cfg, name)
    try:
        docs["point_light_manifest"] = write_point_light_manifest_from_space_bin(
            cfg, name, point_light_out
        )
    except Exception as err:  # noqa: BLE001
        errors["point_light_manifest"] = f"{type(err).__name__}: {err}"
        docs["point_light_manifest"] = None
        try:
            if point_light_out.is_file():
                point_light_out.unlink()
        except OSError:
            pass

    # Decal texture payload (PNG store + mapping) via the toolkit.
    decal_textures_dir = cache_dir / "decal_textures"
    decal_textures_doc: "dict[str, Any] | None" = None
    if not skip_decal_textures:
        try:
            export_decal_textures(
                f"spaces/{name}", decal_textures_dir, config=cfg
            )
            decal_textures_doc = json.loads(
                (decal_textures_dir / "decal_textures.json").read_text(
                    encoding="utf-8"
                )
            )
        except Exception as err:  # noqa: BLE001
            errors["decal_textures"] = f"{type(err).__name__}: {err}"

    # Sky assets (per-weather equirect .hdr + sky_manifest.json).
    sky_doc: "dict[str, Any] | None" = None
    scene_extras: "dict[str, Any] | None" = None
    try:
        gltf_doc = read_glb_json(glb_out)
        scene_extras = primary_scene_extras(gltf_doc)
        sky_doc = extract_sky_assets(
            scene_extras, cfg.require_game_dir() / "res_unpack", cache_dir
        )
        (cache_dir / "sky_manifest.json").write_text(
            json.dumps(sky_doc, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as err:  # noqa: BLE001
        errors["sky_manifest"] = f"{type(err).__name__}: {err}"

    # Terrain heightfield echo: consumers (e.g. the Unity terrain loader)
    # read export.json's terrain_heightmap block - bounds included - to
    # register the .r16 without opening the GLB.
    terrain_heightmap_doc: "dict[str, Any] | None" = None
    thm = (scene_extras or {}).get("terrain_heightmap")
    if isinstance(thm, dict) and thm.get("file"):
        sidecar = cache_dir / str(thm["file"])
        terrain_heightmap_doc = dict(thm)
        terrain_heightmap_doc["size"] = (
            sidecar.stat().st_size if sidecar.is_file() else None
        )

    def _size(p: Path) -> "int | None":
        return p.stat().st_size if p.is_file() else None

    meta_doc: "dict[str, Any]" = {
        "schema": "wows_map_export/v1",
        "name": name,
        "generated_at": now_iso(),
        "flags": flags,
        "glb_size": _size(glb_out),
        "collision_manifest_size": _size(collision_out),
        "terrain_heightmap": terrain_heightmap_doc,
        "elapsed_ms": elapsed_ms,
        "stderr": stderr,
    }
    for key, (out_path, _writer) in glb_writers.items():
        meta_doc[f"{key}_size"] = _size(out_path)
        meta_doc[key] = _slim_summary(docs.get(key))
    meta_doc["point_light_manifest_size"] = _size(point_light_out)
    meta_doc["point_light_manifest"] = _slim_summary(
        docs.get("point_light_manifest")
    )
    meta_doc["decal_textures"] = (
        {
            "schema": decal_textures_doc.get("schema"),
            "texture_count": decal_textures_doc.get("texture_count"),
            "file_count": decal_textures_doc.get("file_count"),
            "error_count": len(decal_textures_doc.get("errors") or {}),
            "dir": str(decal_textures_dir),
        }
        if decal_textures_doc
        else None
    )
    meta_doc["sky_manifest"] = (
        {
            "schema": sky_doc.get("schema"),
            "weather_count": sky_doc.get("weather_count"),
            "extracted_count": sky_doc.get("extracted_count"),
        }
        if sky_doc
        else None
    )
    if collision_manifest:
        meta_doc["flags"]["collision_manifest"] = True
    for key, msg in errors.items():
        meta_doc[f"{key}_error"] = msg

    meta_path(cfg, name).write_text(
        json.dumps(meta_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return meta_doc


# Defensive: surface ToolkitError for callers that catch it by identity.
_ = ToolkitError
