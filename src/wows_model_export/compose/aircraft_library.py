"""Aircraft library builder.

Layer 4 (composer): exports WoWS airframes (the ``aircraft`` model
folders of the VFS) into one shared library, the way
:mod:`.projectile_library` exports projectiles. One airframe is one
``.geometry`` model folder; the GameParams ``Aircraft`` entries that fly it
(often several per model: stock / top / consumable variants) are listed
on its index entry.

Output layout::

    <library_root>/
      ├── index.json                                   — the library index
      └── {nation}/{category}/{asset_id}/
            ├── {asset_id}.glb                         — every render set
            │                                            (all LODs, gear up
            │                                            and down, racks,
            │                                            propellers) + the
            │                                            node tree (HP_*
            │                                            hardpoints, moving
            │                                            parts, skin bones)
            ├── {asset_id}.visual.json                 — toolkit visual JSON
            │                                            (LOD table, render
            │                                            set → node binding)
            ├── {asset_id}_material_mappings.json      — toolkit material →
            │                                            texture-stem table
            └── textures_dds/                          — raw WG DDS mips +
                                                         glTF-conformant
                                                         siblings

The index describes each airframe for a consumer that builds an aircraft
from it: the authored LOD table, the render sets grouped into the parts the
client toggles (gear down / up, propeller blades / blur disc / hub cap,
antenna wires, ordnance racks), the propellers, the moving-part pivots
(ailerons, elevators, rudders, flaps, air brakes, slats), the hardpoints by
kind (ordnance mounts, engine lights, guns, effect anchors), the authored
bounding box with the box and ground height measured from the LOD-0
vertices, the render sets whose names only resemble a toggled family, the
material manifest with its DDS mip chains, and the GameParams aircraft
entries that use the model. The schema is documented in
``reference/schemas/AIRCRAFT_LIBRARY.md``.

Selection: an explicit list of model folder names and/or GameParams
aircraft keys (resolved to their model), or every aircraft model in the
VFS. Airframes already on disk outside the selection keep their entries.

With the GameParams join, the build also writes the per-plane stats
sidecar ``stats.json`` beside the index (:mod:`.aircraft_stats`, schema
in ``reference/schemas/AIRCRAFT_STATS.md``): HP, speeds, heights,
squadron and hangar sizes, attack timing and the ordnance of every
GameParams entry that flies an indexed airframe.

The composer emits these canonical :class:`StepEvent` names::

    "discover_aircraft"  "plan_batch"  "batch_export"
    "populate_materials"  "describe_airframes"  "write_index"
    "write_stats"
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .. import shared_textures, toolkit
from .._glb import parse_glb, write_glb
from ..config import PipelineConfig
from ..errors import StepError, ToolkitError
from ..resolve import sidecar as resolve_sidecar
from ..toolkit._subprocess import toolkit_capabilities, toolkit_supports
from ..types import AircraftLibraryResult, OnEvent
from . import aircraft_stats
from ._step_runner import StepRunner

#: Index schema identifier (bump on a breaking change).
INDEX_SCHEMA = "wows_aircraft_library/v1"

#: Toolkit features the build needs: the per-model visual JSON with the
#: authored LOD table (render-set names do not encode their LOD), and the
#: Z-mirror skin bake that keeps the skin (about half the skinned airframes
#: author their skin in a mirrored bind frame; without the bake their
#: airframe draws Z-mirrored against its own propellers, gear and nodes).
REQUIRED_TOOLKIT_FEATURES: tuple[str, ...] = (
    "export_model.visual_json",
    "export_model.bake_skin_mirror",
)

# /content/gameplay/{nation}/aircraft/{category}/{ID}/{ID}.geometry
_AIRCRAFT_PATH_RE = re.compile(
    r"^/?content/gameplay/(?P<nation>[^/]+)/aircraft/(?P<category>[^/]+)"
    r"/(?P<asset_id>[^/]+)/(?P=asset_id)\.geometry$"
)

# GameParams `model` path of an aircraft entry → its model folder name.
_MODEL_DIR_RE = re.compile(r"/aircraft/[^/]+/(?P<asset_id>[^/]+)/[^/]+\.model$")

# ── Client render-set families ──────────────────────────────────────────
# The client toggles render sets by the name of the node they hang on
# (`PlaneRenderSetNames`): single sets, numbered sets (`disc`, `disc_1`,
# `disc_2`, ...) and the ordnance racks. Every other render set (the
# airframe itself, floats, ...) is always drawn.
_SINGLE_PARTS: dict[str, str] = {
    "chassis_on_BlendBone": "gear_down",
    "chassis_off_BlendBone": "gear_up",
    "hull_wire": "wire",
    "Cradle": "cradle",
    # Older-format airframe (the J7W1): gear sets on `chassis_on` /
    # `chassis_off`, which the client's lists do not name.
    "chassis_on": "gear_down",
    "chassis_off": "gear_up",
}
_NUMBERED_PARTS: tuple[tuple[str, str], ...] = (
    # Longest prefix first: `disc_cap_1` must not read as `disc` + `_cap_1`.
    ("disc_cap", "propeller_cap"),
    ("disc", "propeller_disc"),
    ("disk", "propeller_disc"),
    ("mod", "propeller_blades"),
    # Older format: the blades are a render set on the pivot node itself.
    ("propeller_mod", "propeller_blades"),
)
_PYLON_RE = re.compile(
    r"^pylon_(?P<ordnance>[a-z]+)_(?P<variant>default|custom)(?:_(?P<index>\d+))?_BlendBone$"
)
# Render sets whose node name looks like a toggled family but is not one of
# the client's names (`chassis_off_hull_lod2`, `chassis_on_BlendBone_2`,
# `pylon_rocket_default`, `disc_1_cap`). The client's lists are matched by
# exact name, so these sets read as always drawn; the index lists them in
# `near_miss_render_sets`, because nobody has checked that the client draws
# them in every state.
_NEAR_MISS_FAMILIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^chassis_on"), "gear_down"),
    (re.compile(r"^chassis_off"), "gear_up"),
    (re.compile(r"^pylon_"), "rack"),
    (re.compile(r"^disc_\d+_cap$"), "propeller_cap"),
)
# The parts the STAND preset draws (`RENDER_PRESETS`): the lowest of their
# LOD-0 vertices is the airframe's `ground_y_m`.
_STAND_PARTS = frozenset({"airframe", "gear_down", "propeller_blades", "wire"})

# ── Moving parts (`PlaneNode` prefixes, numbered from 1) ────────────────
_MOVING_PART_PREFIXES: tuple[tuple[str, str], ...] = (
    ("elevator", "elevator_"),
    ("rudder", "rudder_"),
    ("aileron_left", "l_ailerone_"),
    ("aileron_right", "r_ailerone_"),
    ("airbrake_left", "l_airbrake_"),
    ("airbrake_right", "r_airbrake_"),
    ("flaps_left", "l_flaps_"),
    ("flaps_right", "r_flaps_"),
    ("slats_left", "l_slats_"),
    ("slats_right", "r_slats_"),
    ("bomb_bay_left", "l_bombbay_"),
    ("bomb_bay_right", "r_bombbay_"),
)

# Propeller pivots: `propeller_mod[_<n>]` and `propeller_mod_r[_<n>]` spin
# one way, `propeller_mod_l[_<n>]` the other.
_PROPELLER_RE = re.compile(r"^propeller_(?:mod|disk)(?:_(?P<side>[lr]))?(?:_(?P<index>\d+))?$")

# Hardpoints: `HP_Air_<kind>_<n>`, `HP_<kind>_<n>` (`HP_jato_1`,
# `HP_smoke_generator_1`) and the older scouts' `HP_gunFire*` muzzles.
_HP_AIR_RE = re.compile(r"^HP_Air_(?P<kind>[A-Za-z]+(?:_[A-Za-z]+)*)_(?P<n>\d+)$")
_HP_OTHER_RE = re.compile(r"^HP_(?P<kind>[A-Za-z]+(?:_[A-Za-z]+)*)_(?P<n>\d+)$")
_HP_LEGACY_GUN_RE = re.compile(r"^HP_gunFire(?P<back>Back)?(?P<n>\d*)$", re.IGNORECASE)


def _natural_key(name: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AircraftKey:
    """One aircraft model folder in the VFS."""

    nation:   str
    category: str
    asset_id: str


@dataclass
class AircraftRecord:
    key:            AircraftKey
    geometry_vfs:   str
    glb_rel_path:   str | None = None
    glb_bytes:      int | None = None
    built_at:       int | None = None
    built_now:      bool = False
    toolkit:        dict | None = None
    materials:      list[dict] = field(default_factory=list)
    texture_sets:   dict = field(default_factory=dict)
    description:    dict = field(default_factory=dict)
    gameparams:     list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery + selection
# ---------------------------------------------------------------------------


def discover_aircraft(manifest_path: Path) -> dict[str, AircraftRecord]:
    """Every aircraft model folder with a ``.geometry`` of its own, keyed by
    folder name. Visual-only folders (legacy models whose geometry is
    gone) are skipped: there is nothing to export."""
    with open(manifest_path, encoding="utf-8") as f:
        entries = json.load(f)
    out: dict[str, AircraftRecord] = {}
    for e in entries:
        m = _AIRCRAFT_PATH_RE.match(e.get("path") or "")
        if not m:
            continue
        if e.get("is_directory") or e.get("unpacked_size", 0) == 0:
            continue
        key = AircraftKey(m.group("nation"), m.group("category"), m.group("asset_id"))
        out[key.asset_id] = AircraftRecord(key=key, geometry_vfs=(e["path"]).lstrip("/"))
    return out


def gameparams_aircraft_by_model(gp: dict[str, Any]) -> dict[str, list[dict]]:
    """``{model folder: [aircraft entry summary, ...]}`` over a GameParams
    dict (unwrapped). The summary is identity only; per-plane stats and
    loadouts belong to the plane sidecar, not to the model library."""
    out: dict[str, list[dict]] = {}
    for key, v in gp.items():
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo") or {}
        if ti.get("type") != "Aircraft":
            continue
        m = _MODEL_DIR_RE.search(v.get("model") or "")
        if not m:
            continue
        subtypes = v.get("planeSubtype")
        out.setdefault(m.group("asset_id"), []).append({
            "key": key,
            "species": ti.get("species"),
            "nation": ti.get("nation"),
            "level": v.get("level"),
            "plane_subtype": list(subtypes) if isinstance(subtypes, list) else [],
        })
    for rows in out.values():
        rows.sort(key=lambda r: r["key"])
    return out


def resolve_selection(
    names: Iterable[str],
    records: dict[str, AircraftRecord],
    by_model: dict[str, list[dict]],
) -> tuple[list[str], list[str]]:
    """Map model folder names and/or GameParams aircraft keys to model
    folder names. Returns ``(asset_ids, unresolved)``."""
    key_to_model = {row["key"]: aid for aid, rows in by_model.items() for row in rows}
    picked: list[str] = []
    missing: list[str] = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        aid = n if n in records else key_to_model.get(n)
        if aid is None or aid not in records:
            missing.append(n)
        elif aid not in picked:
            picked.append(aid)
    return picked, missing


def output_dir_for(library_root: Path, key: AircraftKey) -> Path:
    return library_root / key.nation / key.category / key.asset_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _paths(library_root: Path, key: AircraftKey) -> dict[str, Path]:
    d = output_dir_for(library_root, key)
    aid = key.asset_id
    return {
        "dir": d,
        "glb": d / f"{aid}.glb",
        "dds": d / "textures_dds",
        "mm": d / f"{aid}_material_mappings.json",
        "visual": d / f"{aid}.visual.json",
    }


def build_aircraft_batch(
    records: dict[str, AircraftRecord],
    targets: list[str],
    library_root: Path,
    *,
    rebuild: bool,
    config: PipelineConfig,
) -> tuple[int, int, list[str]]:
    """Export the targets that are missing (or all of them with
    ``rebuild``) in one ``batch-export-model`` call. Returns
    ``(built, failed, failures)``."""
    items: list[dict] = []
    for aid in targets:
        rec = records[aid]
        p = _paths(library_root, rec.key)
        complete = p["glb"].is_file() and p["visual"].is_file()
        if complete and not rebuild:
            continue
        p["dir"].mkdir(parents=True, exist_ok=True)
        items.append({
            "geometry": rec.geometry_vfs,
            "output": str(p["glb"]),
            "raw_dds_dir": str(p["dds"]),
            "material_mappings_json": str(p["mm"]),
            "visual_json": str(p["visual"]),
        })
    if not items:
        return 0, 0, []
    # Every render set (all LODs, gear, racks, propellers) as its own mesh,
    # and the static node tree even on the unskinned airframes (scouts,
    # floatplane fighters), whose hardpoints the default gate would drop.
    # Z-mirror skins baked into model space with the skin kept, so the
    # airframe, its static parts and its node tree agree at rest.
    shared = {
        "all_render_sets": True,
        "emit_hardpoints": True,
        "bake_skin_mirror": True,
        "no_textures": True,
    }
    try:
        toolkit.batch_export_model(
            items, shared=shared, keep_going=True, config=config, adopt_shared=False,
        )
    except ToolkitError as e:
        return 0, len(items), [f"batch aborted: {str(e).splitlines()[0][:160]}"]
    tool = (toolkit_capabilities(config.require_toolkit_bin()).get("tool") or {})
    provenance = {
        "package_version": tool.get("package_version"),
        "git_commit": tool.get("git_commit"),
        "git_dirty": tool.get("git_dirty"),
    }
    built = failed = 0
    failures: list[str] = []
    for it in items:
        aid = Path(it["output"]).stem
        if Path(it["output"]).is_file() and Path(it["visual_json"]).is_file():
            built += 1
            records[aid].built_now = True
            records[aid].toolkit = dict(provenance)
        else:
            failed += 1
            failures.append(f"{aid}: no GLB / visual JSON written (see the toolkit output)")
    return built, failed, failures


# ---------------------------------------------------------------------------
# Description (index entry body)
# ---------------------------------------------------------------------------


def _render_set_part(carrier: str, skinned: bool) -> tuple[str, dict | None]:
    """Classify one render set by the node it hangs on. Returns
    ``(part, rack_info)``."""
    if carrier in _SINGLE_PARTS:
        return _SINGLE_PARTS[carrier], None
    for stem, part in _NUMBERED_PARTS:
        if carrier == stem or re.fullmatch(re.escape(stem) + r"_\d+", carrier):
            return part, None
    m = _PYLON_RE.match(carrier)
    if m:
        return "rack", {
            "ordnance": m.group("ordnance"),
            "variant": m.group("variant"),
            "index": int(m.group("index")) if m.group("index") else 0,
        }
    return "airframe", None


def _hardpoint_kind(name: str) -> str | None:
    m = _HP_AIR_RE.match(name)
    if m:
        return m.group("kind").lower()
    m = _HP_LEGACY_GUN_RE.match(name)
    if m:
        return "gunfire_back" if m.group("back") else "gunfire"
    m = _HP_OTHER_RE.match(name)
    if m:
        return m.group("kind").lower()
    return None


_ACCESSOR_DTYPES = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}


def _local_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    x, y, z, w = node.get("rotation") or (0.0, 0.0, 0.0, 1.0)
    s = np.array(node.get("scale") or (1.0, 1.0, 1.0), dtype=np.float64)
    r = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m = np.eye(4)
    m[:3, :3] = r * s[None, :]
    m[:3, 3] = node.get("translation") or (0.0, 0.0, 0.0)
    return m


def _positions(gltf: dict, bin_data: bytes, accessor_index: int) -> np.ndarray:
    acc = gltf["accessors"][accessor_index]
    view = gltf["bufferViews"][acc["bufferView"]]
    dtype = np.dtype(_ACCESSOR_DTYPES[acc["componentType"]])
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or 3 * dtype.itemsize
    count = acc["count"]
    if count == 0:
        return np.zeros((0, 3))
    raw = np.frombuffer(bin_data, dtype=np.uint8, count=stride * (count - 1) + 3 * dtype.itemsize, offset=start)
    rows = np.lib.stride_tricks.as_strided(raw, shape=(count, 3 * dtype.itemsize), strides=(stride, 1))
    return np.ascontiguousarray(rows).view(dtype).reshape(count, 3).astype(np.float64)


def mesh_extents(gltf: dict, bin_data: bytes) -> dict[str, tuple[list[float], list[float]]]:
    """``{mesh name: (min, max)}`` of every GLB mesh's vertices in the model
    frame: a static mesh through its node's world transform, a skinned mesh
    as stored (its rest pose is the identity, see ``bake_skin_mirror``)."""
    nodes = gltf.get("nodes") or []
    scenes = gltf.get("scenes") or []
    out: dict[str, tuple[list[float], list[float]]] = {}
    if not scenes or not bin_data:
        return out

    def walk(i: int, parent: np.ndarray) -> None:
        n = nodes[i]
        world = parent @ _local_matrix(n)
        if "mesh" in n:
            mesh = gltf["meshes"][n["mesh"]]
            m = np.eye(4) if "skin" in n else world
            lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
            for prim in mesh.get("primitives") or []:
                pos_index = (prim.get("attributes") or {}).get("POSITION")
                if pos_index is None:
                    continue
                p = _positions(gltf, bin_data, pos_index) @ m[:3, :3].T + m[:3, 3]
                if len(p):
                    lo, hi = np.minimum(lo, p.min(0)), np.maximum(hi, p.max(0))
            name = mesh.get("name") or ""
            if np.isfinite(lo).all():
                if name in out:
                    lo = np.minimum(lo, out[name][0])
                    hi = np.maximum(hi, out[name][1])
                out[name] = ([float(v) for v in lo], [float(v) for v in hi])
        for c in n.get("children") or []:
            walk(c, world)

    for root in scenes[gltf.get("scene", 0)].get("nodes") or []:
        walk(root, np.eye(4))
    return out


def describe_airframe(visual: dict, gltf: dict, bin_data: bytes | None = None) -> tuple[dict, list[str]]:
    """Build the index description of one airframe from its toolkit visual
    JSON and its GLB's JSON chunk. With the GLB's binary chunk it also
    measures ``mesh_bounds_m`` and ``ground_y_m`` from the LOD-0 vertices.
    Returns ``(description, problems)``."""
    problems: list[str] = []
    glb_meshes = {m.get("name") for m in gltf.get("meshes", []) if m.get("name")}
    glb_nodes = [n.get("name") or "" for n in gltf.get("nodes", [])]
    glb_node_set = set(glb_nodes)

    nodes = visual.get("nodes") or []
    names = [n.get("name") or "" for n in nodes]
    parent = [n.get("parent") for n in nodes]
    index_of = {n: i for i, n in enumerate(names)}

    def ancestors(name: str) -> list[str]:
        out: list[str] = []
        i = index_of.get(name)
        seen = set()
        while i is not None and parent[i] is not None and i not in seen:
            seen.add(i)
            i = parent[i]
            out.append(names[i])
        return out

    # LOD membership by render-set name.
    lods_out: list[dict] = []
    lod_of: dict[str, list[int]] = {}
    for li, lod in enumerate(visual.get("lods") or []):
        members = [rs for rs in (lod.get("render_sets") or [])]
        for rs in members:
            lod_of.setdefault(rs, []).append(li)
        missing = [rs for rs in members if rs not in glb_meshes]
        if missing:
            problems.append(f"LOD {li} names render sets the GLB lacks: {missing}")
        lods_out.append({
            "extent": lod.get("extent"),
            "casts_shadow": bool(lod.get("casts_shadow")),
            "meshes": [rs for rs in members if rs in glb_meshes],
        })

    meshes: dict[str, dict] = {}
    render_sets: dict[str, dict] = {}
    for rs in visual.get("render_sets") or []:
        name = rs.get("name") or ""
        if name not in glb_meshes:
            continue  # excluded from the export (e.g. `_hide` sets)
        bound = rs.get("nodes") or []
        skinned = bool(rs.get("skinned"))
        # The client names a render set by the node it hangs on: a static
        # set's one node; for a skinned set the airframe skin
        # (`Root_BlendBone` in its palette) or the single bone it is bound
        # to (the SB2C's skinned gear sets ride `chassis_on_BlendBone`).
        if skinned and "Root_BlendBone" in bound:
            carrier = "Root_BlendBone"
        else:
            carrier = bound[0] if bound else name
        part, rack = _render_set_part(carrier, skinned)
        meshes[name] = {
            "render_set": carrier,
            "part": part,
            "lods": lod_of.get(name, []),
            "material": rs.get("material"),
            "skinned": skinned,
        }
        entry = render_sets.setdefault(carrier, {"part": part, "meshes": []})
        entry["meshes"].append(name)
        if rack:
            entry["rack"] = rack
    for entry in render_sets.values():
        entry["meshes"].sort(key=lambda n: (min(meshes[n]["lods"] or [99]), _natural_key(n)))
    unlisted = sorted(glb_meshes - set(meshes))
    if unlisted:
        problems.append(f"GLB meshes with no render set in the visual: {unlisted}")
    no_lod = sorted(n for n, m in meshes.items() if not m["lods"])
    if no_lod:
        problems.append(f"meshes in no LOD: {no_lod}")

    parts: dict[str, list[str]] = {}
    for carrier, entry in sorted(render_sets.items(), key=lambda kv: _natural_key(kv[0])):
        parts.setdefault(entry["part"], []).append(carrier)

    near_miss = []
    for carrier in parts.get("airframe", []):
        for pattern, family in _NEAR_MISS_FAMILIES:
            if pattern.search(carrier):
                near_miss.append({"render_set": carrier, "family": family})
                break
    if near_miss:
        problems.append(
            "render sets drawn always although their node names resemble a toggled family "
            "(the client toggles exact names): "
            + ", ".join(f"{n['render_set']} ({n['family']})" for n in near_miss)
        )

    racks = []
    for carrier in parts.get("rack", []):
        r = dict(render_sets[carrier]["rack"])
        r["render_set"] = carrier
        racks.append(r)
    racks.sort(key=lambda r: (r["ordnance"], r["variant"], r["index"]))

    # Propellers: the pivot node and the render sets under it.
    propellers = []
    for nm in names:
        m = _PROPELLER_RE.match(nm)
        if not m:
            continue
        under = sorted(
            (c for c, e in render_sets.items()
             if e["part"].startswith("propeller_") and (c == nm or nm in ancestors(c))),
            key=_natural_key,
        )
        propellers.append({
            "node": nm,
            "spin": "opposite" if m.group("side") == "l" else "direct",
            "render_sets": under,
        })
    propellers.sort(key=lambda p: _natural_key(p["node"]))

    # Moving parts: pivot nodes the client drives (numbered from 1), plus
    # any other articulated pivot (a node with a `<name>_BlendBone` child).
    moving: dict[str, list[str]] = {}
    claimed: set[str] = set()
    for group, prefix in _MOVING_PART_PREFIXES:
        found = sorted((n for n in names if re.fullmatch(re.escape(prefix) + r"\d+", n)), key=_natural_key)
        if found:
            moving[group] = found
            claimed.update(found)
    articulated = {
        names[parent[i]] for i, n in enumerate(names)
        if n.endswith("_BlendBone") and parent[i] is not None
        and names[parent[i]] + "_BlendBone" == n
    }
    other = sorted(articulated - claimed - {"Root", "Scene Root"}, key=_natural_key)
    if other:
        moving["other"] = other

    hardpoints: dict[str, list[str]] = {}
    for nm in names:
        if not nm.startswith("HP_"):
            continue
        kind = _hardpoint_kind(nm)
        if kind is None:
            kind = "other"
        hardpoints.setdefault(kind, []).append(nm)
    for k in hardpoints:
        hardpoints[k].sort(key=_natural_key)
    lost = sorted(n for v in hardpoints.values() for n in v if n not in glb_node_set)
    if lost:
        problems.append(f"hardpoints missing from the GLB node tree: {lost}")

    bb = visual.get("bounding_box") or {}
    bmin, bmax = bb.get("min"), bb.get("max")
    dims = None
    if bmin and bmax:
        dims = {
            "span": round(bmax[0] - bmin[0], 3),
            "height": round(bmax[1] - bmin[1], 3),
            "length": round(bmax[2] - bmin[2], 3),
        }

    # Measured from the LOD-0 vertices: the box of every part (an authored
    # box can leave parts out: the N1K2's omits its gear and propeller), and
    # the lowest point of the parts a parked plane draws.
    mesh_bounds = None
    ground_y = None
    if bin_data is not None:
        ext = mesh_extents(gltf, bin_data)
        lod0 = [n for n in (lods_out[0]["meshes"] if lods_out else []) if n in ext]
        if lod0:
            lo = [min(ext[n][0][k] for n in lod0) for k in range(3)]
            hi = [max(ext[n][1][k] for n in lod0) for k in range(3)]
            mesh_bounds = {"min": [round(v, 4) for v in lo], "max": [round(v, 4) for v in hi]}
        stand = [n for n in lod0 if meshes.get(n, {}).get("part") in _STAND_PARTS]
        if stand:
            ground_y = round(min(ext[n][0][1] for n in stand), 4)
        else:
            problems.append("no LOD-0 mesh of a standing part: ground_y_m not measured")

    desc = {
        "skinned": any(m["skinned"] for m in meshes.values()),
        "bounds_m": {"min": bmin, "max": bmax} if bmin and bmax else None,
        "mesh_bounds_m": mesh_bounds,
        "ground_y_m": ground_y,
        "dimensions_m": dims,
        "lods": lods_out,
        "meshes": dict(sorted(meshes.items(), key=lambda kv: _natural_key(kv[0]))),
        "render_sets": dict(sorted(render_sets.items(), key=lambda kv: _natural_key(kv[0]))),
        "parts": parts,
        "racks": racks,
        "propellers": propellers,
        "moving_parts": moving,
        "hardpoints": dict(sorted(hardpoints.items())),
        "near_miss_render_sets": near_miss,
    }
    return desc, problems


def parent_static_render_sets(glb_path: Path, visual: dict) -> int:
    """Hang each static render-set mesh under the node it is bound to.

    A static (unskinned) render set's vertices are in its node's local
    frame: the engine draws them with that node's world transform (the
    toolkit's map export bakes the same matrix,
    ``static_render_set_world_matrix``). ``export-model --all-render-sets``
    puts every mesh node at the scene root with no transform, which is
    right only for sets whose node sits at the model origin. On aircraft
    the propeller discs, blades and hub caps hang on offset pivots, so at
    the root they would draw at the origin. Parenting the mesh node under
    its bound node (present in the GLB's node tree) puts it where the
    engine draws it and keeps it free to spin about its pivot.

    Idempotent: only meshes still at the scene root move. Rewrites the GLB
    only when something moved. Returns the number of meshes moved.
    """
    gltf, bin_data = parse_glb(glb_path.read_bytes())
    nodes = gltf.get("nodes") or []
    scenes = gltf.get("scenes") or []
    if not scenes:
        return 0
    scene = scenes[gltf.get("scene", 0)]
    roots: list[int] = list(scene.get("nodes") or [])
    by_name: dict[str, int] = {}
    for i, n in enumerate(nodes):
        if "mesh" not in n and n.get("name"):
            by_name.setdefault(n["name"], i)
    bound: dict[str, str] = {}
    for rs in visual.get("render_sets") or []:
        if not rs.get("skinned") and len(rs.get("nodes") or []) == 1:
            bound[rs.get("name") or ""] = rs["nodes"][0]
    moved = 0
    keep: list[int] = []
    for i in roots:
        n = nodes[i]
        target = by_name.get(bound.get(n.get("name") or "", ""))
        if "mesh" in n and "skin" not in n and target is not None and target != i:
            nodes[target].setdefault("children", []).append(i)
            moved += 1
        else:
            keep.append(i)
    if moved:
        scene["nodes"] = keep
        write_glb(gltf, bin_data, glb_path)
    return moved


def _classify_plane_materials(materials: list[dict]) -> None:
    """The propeller blur disc (``PROPELLERMAT_*``) samples a DXT5
    ``*_alpha_a`` disc texture and blends over the airframe; the toolkit's
    glTF flags every material OPAQUE, so mark it transparent and double
    sided (the disc is seen from the front and the back)."""
    for m in materials:
        if "PROPELLERMAT" in (m.get("material_id") or "").upper():
            m["shader_intent"] = "transparent"
            m["render_queue"] = "transparent"
            m["double_sided"] = True


def _read_glb_json(path: Path) -> dict:
    gltf, _bin = parse_glb(path.read_bytes())
    return gltf


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def _rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def write_index(
    records: dict[str, AircraftRecord],
    library_root: Path,
    *,
    keep_prior_gameparams: bool = False,
) -> Path:
    """Write ``index.json``. ``built_at`` carries forward across rebuilds of
    an unchanged entry, as in the other libraries. With
    ``keep_prior_gameparams`` (a run without the GameParams join) each
    entry keeps the ``gameparams`` list the previous index had."""
    out = library_root / "index.json"
    prior: dict[str, Any] = {}
    if out.is_file():
        try:
            prior = (json.loads(out.read_text(encoding="utf-8")).get("assets") or {})
        except (OSError, ValueError):
            prior = {}
    assets: dict[str, dict] = {}
    for aid in sorted(records):
        rec = records[aid]
        if rec.glb_rel_path is None:
            continue
        kept = (prior.get(aid) or {}).get("built_at")
        if not rec.built_now and isinstance(kept, (int, float)):
            rec.built_at = int(kept)
        p = _paths(library_root, rec.key)
        entry: dict[str, Any] = {
            "nation": rec.key.nation,
            "category": rec.key.category,
            "geometry_vfs": rec.geometry_vfs,
            "glb": rec.glb_rel_path,
            "glb_bytes": rec.glb_bytes,
            "built_at": rec.built_at,
            "textures_dds": _rel(p["dds"], library_root) if p["dds"].is_dir() else None,
            "visual_json": _rel(p["visual"], library_root),
            "material_mappings": _rel(p["mm"], library_root) if p["mm"].is_file() else None,
        }
        entry.update(rec.description)
        entry["materials"] = rec.materials
        entry["texture_sets"] = rec.texture_sets
        entry["gameparams"] = (
            (prior.get(aid) or {}).get("gameparams") or []
            if keep_prior_gameparams else rec.gameparams
        )
        # The toolkit build that exported this airframe (carried forward
        # while the airframe is not re-exported).
        entry["toolkit"] = rec.toolkit if rec.built_now else (prior.get(aid) or {}).get("toolkit")
        shared_textures.repair_refs(entry, p["dir"])
        assets[aid] = entry
    doc = {
        "schema": INDEX_SCHEMA,
        "version": time.strftime("%Y-%m-%d", time.gmtime()),
        "asset_count": len(assets),
        "assets": assets,
    }
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def validate_index(library_root: Path, *, workspace: Path | None = None) -> list[str]:
    """Check an aircraft library index against the files beside it.

    Every entry's GLB, visual JSON and texture files exist; every GLB mesh
    is described and sits in a LOD; every LOD, part, rack and propeller
    names existing meshes or render sets; every hardpoint and moving-part
    pivot is a node of the GLB. Returns the problems found (empty when the
    index is valid)."""
    problems: list[str] = []
    idx_path = library_root / "index.json"
    try:
        doc = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"index unreadable: {e}"]
    if doc.get("schema") != INDEX_SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {INDEX_SCHEMA!r}")
    assets = doc.get("assets") or {}
    if doc.get("asset_count") != len(assets):
        problems.append(f"asset_count {doc.get('asset_count')} != {len(assets)} entries")
    ws = Path(workspace) if workspace else shared_textures.find_workspace(library_root)

    def tex_path(base: Path, ref: str) -> Path:
        if ref.startswith("libraries/") and ws is not None:
            return ws.joinpath(*ref.split("/"))
        return base.joinpath(*ref.split("/"))

    for aid, e in sorted(assets.items()):
        tag = f"{aid}:"
        glb = library_root.joinpath(*(e.get("glb") or "").split("/"))
        if not glb.is_file():
            problems.append(f"{tag} GLB missing ({e.get('glb')})")
            continue
        base = glb.parent
        vj = library_root.joinpath(*(e.get("visual_json") or "").split("/"))
        if not vj.is_file():
            problems.append(f"{tag} visual JSON missing")
        gltf = _read_glb_json(glb)
        glb_meshes = {m.get("name") for m in gltf.get("meshes", [])}
        glb_nodes = {n.get("name") for n in gltf.get("nodes", [])}
        meshes = e.get("meshes") or {}
        # Static meshes hang under their render set's node (see
        # `parent_static_render_sets`).
        glb_pivots = {n.get("name") for n in gltf.get("nodes", []) if "mesh" not in n}
        parent_of: dict[str, str] = {}
        for n in gltf.get("nodes", []):
            for c in n.get("children") or []:
                child = gltf["nodes"][c]
                if "mesh" in child:
                    parent_of[child.get("name") or ""] = n.get("name") or ""
        for mname, m in meshes.items():
            if not m.get("skinned") and m.get("render_set") in glb_pivots \
                    and parent_of.get(mname) != m.get("render_set"):
                problems.append(f"{tag} static mesh {mname} is not under its node {m.get('render_set')}")
        if set(meshes) != glb_meshes:
            problems.append(f"{tag} meshes differ from the GLB: "
                            f"+{sorted(glb_meshes - set(meshes))} -{sorted(set(meshes) - glb_meshes)}")
        for mname, m in meshes.items():
            if not m.get("lods"):
                problems.append(f"{tag} mesh {mname} is in no LOD")
        lods = e.get("lods") or []
        if not lods:
            problems.append(f"{tag} no LODs")
        for li, lod in enumerate(lods):
            for mname in lod.get("meshes") or []:
                if mname not in glb_meshes:
                    problems.append(f"{tag} LOD {li} names missing mesh {mname}")
                elif li not in (meshes.get(mname) or {}).get("lods", []):
                    problems.append(f"{tag} mesh {mname} does not list LOD {li}")
        rsets = e.get("render_sets") or {}
        for carrier, rs in rsets.items():
            for mname in rs.get("meshes") or []:
                if mname not in glb_meshes:
                    problems.append(f"{tag} render set {carrier} names missing mesh {mname}")
        for part, carriers in (e.get("parts") or {}).items():
            for c in carriers:
                if c not in rsets:
                    problems.append(f"{tag} part {part} names unknown render set {c}")
        if "airframe" not in (e.get("parts") or {}):
            problems.append(f"{tag} no airframe render set")
        for r in e.get("racks") or []:
            if r.get("render_set") not in rsets:
                problems.append(f"{tag} rack {r.get('render_set')} is not a render set")
        for pr in e.get("propellers") or []:
            if pr.get("node") not in glb_nodes:
                problems.append(f"{tag} propeller node {pr.get('node')} not in the GLB")
            for c in pr.get("render_sets") or []:
                if c not in rsets:
                    problems.append(f"{tag} propeller {pr.get('node')} names unknown render set {c}")
        for group, ns in (e.get("moving_parts") or {}).items():
            for n in ns:
                if n not in glb_nodes:
                    problems.append(f"{tag} moving part {group}/{n} not in the GLB")
        for kind, ns in (e.get("hardpoints") or {}).items():
            for n in ns:
                if n not in glb_nodes:
                    problems.append(f"{tag} hardpoint {kind}/{n} not in the GLB")
        if not e.get("materials"):
            problems.append(f"{tag} no materials")
        for mat in e.get("materials") or []:
            mid = mat.get("material_id")
            main = (mat.get("texture_sets") or {}).get("main") or {}
            if mat.get("shader_intent") in ("opaque_pbr", "cutout", "transparent") and not main:
                problems.append(f"{tag} material {mid} has no textures")
            for slot, manifest in main.items():
                refs = manifest.get("dds_mips") if isinstance(manifest, dict) else manifest
                if not refs:
                    problems.append(f"{tag} material {mid} slot {slot} has no files")
                for ref in refs or []:
                    if not tex_path(base, ref).is_file():
                        problems.append(f"{tag} material {mid} slot {slot} file missing: {ref}")
        b = e.get("bounds_m") or {}
        if not (b.get("min") and b.get("max")):
            problems.append(f"{tag} no bounds")
        mb = e.get("mesh_bounds_m") or {}
        if not (mb.get("min") and mb.get("max")):
            problems.append(f"{tag} no mesh bounds")
        elif not isinstance(e.get("ground_y_m"), (int, float)) or not \
                mb["min"][1] - 1e-3 <= e["ground_y_m"] <= mb["max"][1] + 1e-3:
            problems.append(f"{tag} ground_y_m {e.get('ground_y_m')!r} missing or outside the mesh bounds")
    return problems


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def _library_session_roots(
    *_args: object,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    **_kwargs: object,
) -> tuple[Path, list[Path]]:
    """Export session of a library build: it claims the library root, so no
    other export's adoption moves a copy this build's index still names."""
    ws = Path(workspace or (config or PipelineConfig.load()).workspace)
    return ws, [Path(library_root) if library_root else ws / "libraries" / "aircraft"]


@shared_textures.with_export_session(_library_session_roots)
def build_aircraft_library(
    *,
    select: Iterable[str] | None = None,
    all_aircraft: bool = False,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    rebuild: bool = False,
    gameparams: bool = True,
    manifest_path: Path | None = None,
    refresh_manifest: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> AircraftLibraryResult:
    """Build / refresh the aircraft library.

    Parameters:
        select          Model folder names (``AAB702_Grumman_TBF_proto``)
                        and/or GameParams aircraft keys
                        (``PAAB508_Enterprise_stock``) to export.
        all_aircraft    Export every aircraft model of the VFS instead.
                        With neither, nothing is exported and the index is
                        rewritten from the airframes on disk.
        library_root    ``<workspace>/libraries/aircraft`` when None.
        rebuild         Re-export selected airframes already on disk.
        gameparams      List the GameParams aircraft entries of each model
                        (loads the GameParams cache) and resolve GameParams
                        keys in ``select``.
        manifest_path / refresh_manifest
                        The VFS manifest (see :func:`toolkit.ensure_manifest`).
        cancel          Cooperative cancel between steps.

    Raises :class:`StepError` (``step`` set to a canonical step name) when a
    step fails, including when the toolkit lacks ``export_model.visual_json``.
    """
    cfg = config or PipelineConfig.load()
    ws = Path(workspace or cfg.workspace).resolve()
    lib_root = Path(library_root or (ws / "libraries" / "aircraft")).resolve()
    manifest = Path(manifest_path or toolkit.default_manifest_path(cfg)).resolve()
    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    wanted = [s for s in (select or []) if s and s.strip()]

    # ── discover_aircraft ─────────────────────────────────────────────
    try:
        with runner.step("discover_aircraft") as st:
            toolkit.ensure_manifest(manifest_path=manifest, refresh=refresh_manifest, config=cfg)
            records = discover_aircraft(manifest)
            by_model: dict[str, list[dict]] = {}
            stats_entries: dict[str, dict] | None = None
            if gameparams:
                from ..read import gameparams as read_gameparams
                gp_full = read_gameparams.load_full(config=cfg)
                by_model = gameparams_aircraft_by_model(gp_full)
                # The stats sidecar's blocks, for every entry with a model
                # folder; write_stats keeps those of the indexed airframes.
                _, stats_entries, stats_problems = aircraft_stats.compute_stats(gp_full)
                warnings.extend(f"stats: {p}" for p in stats_problems)
                del gp_full
                read_gameparams.unload_full()
            for aid, rows in by_model.items():
                if aid in records:
                    records[aid].gameparams = rows
            if all_aircraft:
                targets = sorted(records)
                missing: list[str] = []
            else:
                targets, missing = resolve_selection(wanted, records, by_model)
            for m in missing:
                warnings.append(f"not an aircraft model or GameParams aircraft key with a geometry: {m}")
            st.annotate(
                f"{len(records)} aircraft model(s) in the VFS; {len(targets)} selected",
                data={"models": len(records), "selected": targets, "unresolved": missing},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="discover_aircraft", underlying=e, detail=str(e)) from e

    lib_root.mkdir(parents=True, exist_ok=True)

    # ── plan_batch ────────────────────────────────────────────────────
    with runner.step("plan_batch") as st:
        todo = [
            a for a in targets
            if rebuild or not (_paths(lib_root, records[a].key)["glb"].is_file()
                               and _paths(lib_root, records[a].key)["visual"].is_file())
        ]
        st.annotate(f"{len(todo)} airframe(s) to export (rebuild={rebuild})", data={"planned": todo})

    # ── batch_export ──────────────────────────────────────────────────
    built = 0
    if todo:
        try:
            with runner.step("batch_export") as st:
                bin_path = cfg.require_toolkit_bin()
                lacking = [f for f in REQUIRED_TOOLKIT_FEATURES if not toolkit_supports(bin_path, f)]
                if lacking:
                    detail = f"the toolkit at {bin_path} lacks {lacking}; rebuild wowsunpack"
                    raise StepError(step="batch_export", underlying=RuntimeError(detail), detail=detail)
                built, failed, failures = build_aircraft_batch(
                    records, todo, lib_root, rebuild=rebuild, config=cfg,
                )
                warnings.extend(failures)
                st.annotate(f"built={built} failed={failed}", data={"built": built, "failed": failed})
        except StepError:
            raise
        except Exception as e:
            raise StepError(step="batch_export", underlying=e, detail=str(e)) from e
    else:
        runner.emit("batch_export", "skipped", detail="nothing to export")

    # Records on disk (this run's and earlier ones) go into the index.
    on_disk = [a for a in records if _paths(lib_root, records[a].key)["glb"].is_file()]
    for aid in on_disk:
        rec = records[aid]
        p = _paths(lib_root, rec.key)
        rec.glb_rel_path = _rel(p["glb"], lib_root)
        stat = p["glb"].stat()
        rec.glb_bytes = stat.st_size
        rec.built_at = int(stat.st_mtime)

    # Shared texture store: route this run's raw-DDS folders.
    adopt = [_paths(lib_root, records[a].key)["dds"] for a in on_disk if records[a].built_now]
    if adopt:
        shared_textures.adopt_dirs(adopt, workspace=ws)

    # ── populate_materials ────────────────────────────────────────────
    with runner.step("populate_materials") as st:
        n = 0
        for aid in on_disk:
            rec = records[aid]
            p = _paths(lib_root, rec.key)
            try:
                rec.materials = resolve_sidecar.materials_from_glb(
                    p["glb"],
                    textures_dds_dir=p["dds"] if p["dds"].is_dir() else None,
                    material_mappings_json=p["mm"] if p["mm"].is_file() else None,
                )
                rec.texture_sets = (
                    resolve_sidecar.texture_sets_from_dir(p["dds"]) if p["dds"].is_dir() else {}
                )
                _classify_plane_materials(rec.materials)
                n += 1
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{aid}: material manifest failed: {e}")
        st.annotate(f"{n} airframe(s) with material manifests")

    # ── describe_airframes ────────────────────────────────────────────
    with runner.step("describe_airframes") as st:
        for aid in on_disk:
            rec = records[aid]
            p = _paths(lib_root, rec.key)
            if not p["visual"].is_file():
                warnings.append(f"{aid}: no visual JSON (exported by an older toolkit); re-export it")
                continue
            try:
                visual = json.loads(p["visual"].read_text(encoding="utf-8"))
                if parent_static_render_sets(p["glb"], visual):
                    rec.glb_bytes = p["glb"].stat().st_size
                gltf_json, gltf_bin = parse_glb(p["glb"].read_bytes())
                desc, problems = describe_airframe(visual, gltf_json, gltf_bin)
                desc["visual_vfs"] = (visual.get("model") or {}).get("visual_path")
                rec.description = desc
                warnings.extend(f"{aid}: {x}" for x in problems)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{aid}: description failed: {e}")
        st.annotate(f"{len(on_disk)} airframe(s) described")

    # ── write_index ───────────────────────────────────────────────────
    try:
        with runner.step("write_index") as st:
            index_path = write_index(records, lib_root, keep_prior_gameparams=not gameparams)
            problems = validate_index(lib_root, workspace=ws)
            warnings.extend(f"index: {x}" for x in problems)
            st.annotate(f"wrote {index_path.name}; {len(problems)} validation problem(s)",
                        data={"problems": problems[:50]})
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="write_index", underlying=e, detail=str(e)) from e

    # ── write_stats ───────────────────────────────────────────────────
    # The per-plane stats sidecar beside the index (aircraft_stats). A
    # run without the GameParams join keeps the previous file.
    n_stats: int | None = None
    if stats_entries is not None:
        with runner.step("write_stats") as st:
            want = aircraft_stats.index_airframes(lib_root)
            entries = {k: stats_entries[k] for keys in want.values() for k in keys if k in stats_entries}
            missing = sorted({k for keys in want.values() for k in keys} - set(entries))
            warnings.extend(f"stats: {k} has no stats block" for k in missing)
            from ..toolkit import build as _build
            path = aircraft_stats.write_stats(
                lib_root, want, entries, game_build=_build.current_build(cfg),
            )
            problems = aircraft_stats.validate_stats(lib_root)
            warnings.extend(f"stats: {x}" for x in problems)
            n_stats = len(entries)
            st.annotate(f"wrote {path.name}: {n_stats} entr(ies); {len(problems)} problem(s)")
    else:
        runner.emit("write_stats", "skipped", detail="no GameParams join; stats.json kept")

    return AircraftLibraryResult(
        library_root=lib_root,
        aircraft_built=built,
        aircraft_indexed=sum(1 for r in records.values() if r.glb_rel_path),
        aircraft_available=len(records),
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
        stats_entries=n_stats,
    )


__all__ = [
    "INDEX_SCHEMA",
    "AircraftKey",
    "AircraftRecord",
    "build_aircraft_batch",
    "build_aircraft_library",
    "describe_airframe",
    "discover_aircraft",
    "gameparams_aircraft_by_model",
    "output_dir_for",
    "resolve_selection",
    "validate_index",
    "write_index",
]
