"""Composed buildings: an old-format base model plus GameParams gun mounts.

Some shore structures exist in GameParams but their model ships in the
merged ``.visual`` container the toolkit cannot read yet, and some
structures a consumer needs have no Building entry at all. A *composite*
stands in for them: a base model the toolkit can export (any old-format
``.model``: a building, a map prop, a sandbag ring) with one or more gun
mounts on it, each a GameParams ``Gun`` entry (its model, ammo, arcs,
rates and hit location) copied from a Building or Ship entry that mounts
that gun.

The output is an ordinary building export (``building_export``'s layout,
sidecar and gun library), so a consumer of building exports reads a
composite with no extra code:

    <workspace>/buildings/<id>/
      <id>.meta.json               <- building sidecar + ``composition`` block
      models/<id>_hull.glb         <- base model + HP_* mount nodes
                                      (+ Armor / Hitboxes groups, below)
      models/<id>_material_mappings.json, models/textures_dds/
    <workspace>/buildings/_shared/guns/<stem>/   <- gun GLBs (shared)

The composer is driven by a spec the caller supplies (JSON,
``schema: "wows_building_composites/v1"``); it holds only WoWS ids:

    {"schema": "wows_building_composites/v1",
     "composites": [{
        "id": "CMBC001",                       # folder / sidecar index
        "name": "120 mm gun in an earth revetment",
        "species": "CoastalArtillery",         # building.species
        "health": 5000,
        "base": {"model": "content/.../LMM021/LMM021.model",
                 "armor_mm": 5.0},             # or "armor_from": "<Building index>"
        "mounts": [{
           "hp_name": "HP_JGM_1",
           "source": {"params": "PCBX019", "component": "artillery", "hp": "HP_JGM_1"},
           "at": {"xz": "center", "y": 0.0},   # xz: "center" | [x, z]; y: number | "surface"
           "facing_deg": 0.0,                  # rest muzzle bearing, 0 = +Z, 90 = +X
           "overrides": {"vertSector": [-2.0, 27.0]},
           "component_overrides": {"maxDist": 15000.0}}],
        "notes": ["why these parts"]}]}

Mount placement. ``at`` is in the base GLB frame (glTF, metres as
exported): ``"center"`` is the centre of the base's render-mesh bounds,
``"surface"`` drops onto the highest render triangle under ``xz`` (a roof
or a floor), plus an optional ``y_offset``. The ``HP_*`` node is turned
about +Y so that the gun, placed by the ship-mount rule
(``building_export._mount_matrix``), rests with its muzzle along
``facing_deg``; the rest muzzle direction is measured from the gun GLB's
own elevation pivot and muzzle nodes (a static mount, which has no
pivot: from its muzzle nodes' forward axis).

Hit geometry. A base without collision groups (map props carry none)
gets an ``Armor`` group (``Armor_Hull``: the base's render triangles with
``_MATERIAL_ID`` 77, the id the toolkit's building collision models use)
and a ``Hitboxes`` group (``CM_SB_Hull_0``: the base's render bounds), in
the shapes the toolkit writes for buildings. ``armor_mm`` becomes that
material's thickness. A base that has its own groups keeps them, and its
thickness table comes from ``armor_from``; a run whose base kept its own
``Armor`` group but whose spec gives no ``armor_from`` warns, since only
material 77 then gets a thickness.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from . import shared_textures
from ._glb import COMP_FLOAT, COMP_UNSIGNED_INT, COMP_UNSIGNED_SHORT, parse_glb, write_glb
from .building_export import (
    MODELS_SUBDIR,
    _buildings_session_roots,
    _compose_sidecar,
    _flip_hull_winding,
    _geometry_vfs,
    _gun_glb_jobs,
    _gun_rig_nodes,
    _mat_mul,
    _mount_matrix,
    _node_local_matrix,
    _read_glb_json,
    _rest_muzzle_dir,
    _rotate_y_bone,
    _stem,
    _write_gun_index,
    _write_json_atomic,
    building_dir,
    buildings_root,
    shared_gun_dir,
    sidecar_path_for,
)
from .config import PipelineConfig
from .read import gameparams
from .toolkit.ship import batch_export_model

SPEC_SCHEMA = "wows_building_composites/v1"
GENERATOR = "wows-compose-building"

# Armour material of synthesized collision geometry: the material id and
# layer the toolkit's building collision models use for structural armour
# (PCBX007 / PCBA001: ids 77 / 78 on layers 1 / 2), and the vertex colour
# the toolkit writes on Armor_* meshes.
ARMOR_MATERIAL_ID = 77
ARMOR_LAYER = 1
ARMOR_COLOR = (0.8, 0.8, 0.8, 0.5)

_IDENT = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
_NP_TYPE = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
_N_COMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


# ── spec ──────────────────────────────────────────────────────────────


def load_spec(path: Path) -> dict[str, Any]:
    """Read and check a composite spec. Raises ValueError on a bad spec."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SPEC_SCHEMA:
        raise ValueError(f"{path}: schema must be {SPEC_SCHEMA!r}, got {doc.get('schema')!r}")
    seen: set[str] = set()
    for c in doc.get("composites") or []:
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or "/" in cid or "\\" in cid:
            raise ValueError(f"{path}: composite id {cid!r} is not a folder name")
        if cid in seen:
            raise ValueError(f"{path}: composite id {cid} appears twice")
        seen.add(cid)
        base = c.get("base") or {}
        if not str(base.get("model", "")).endswith(".model"):
            raise ValueError(f"{cid}: base.model must be a .model VFS path")
        if not isinstance(c.get("health"), (int, float)) or c["health"] <= 0:
            raise ValueError(f"{cid}: health must be a positive number")
        hps: set[str] = set()
        for m in c.get("mounts") or []:
            hp = m.get("hp_name")
            if not isinstance(hp, str) or not hp.startswith("HP_"):
                raise ValueError(f"{cid}: mount hp_name {hp!r} must start with HP_")
            if hp in hps:
                raise ValueError(f"{cid}: hp_name {hp} appears twice")
            hps.add(hp)
            src = m.get("source") or {}
            if not all(isinstance(src.get(k), str) for k in ("params", "component", "hp")):
                raise ValueError(f"{cid}: {hp} source needs params, component and hp")
            at = m.get("at") or {}
            xz, y = at.get("xz", "center"), at.get("y", 0.0)
            if not (xz == "center" or (isinstance(xz, list) and len(xz) == 2)):
                raise ValueError(f"{cid}: {hp} at.xz must be 'center' or [x, z]")
            if not (y == "surface" or isinstance(y, (int, float))):
                raise ValueError(f"{cid}: {hp} at.y must be a number or 'surface'")
    return doc


def _source_gun(gp: dict[str, Any], cid: str, mount: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """``(target component, gun dict copy, component-level keys)`` of a
    mount's GameParams source, with the spec's overrides applied."""
    src = mount["source"]
    entry = gp.get(src["params"])
    if not isinstance(entry, dict):
        raise ValueError(f"{cid}: source {src['params']} is not in GameParams")
    comp = entry.get(src["component"])
    if not isinstance(comp, dict):
        raise ValueError(f"{cid}: {src['params']} has no component {src['component']}")
    gun = comp.get(src["hp"])
    ti = gun.get("typeinfo") if isinstance(gun, dict) else None
    if not (isinstance(ti, dict) and ti.get("type") == "Gun" and str(gun.get("model", "")).endswith(".model")):
        raise ValueError(f"{cid}: {src['params']}/{src['component']}/{src['hp']} is not a Gun mount")
    target = src.get("as")
    if target is None:
        aa = "airdefense" in src["component"].lower() or ti.get("species") == "AAircraft"
        target = "airDefense" if aa else "artillery"
    if target not in ("artillery", "airDefense"):
        raise ValueError(f"{cid}: source.as must be artillery or airDefense, got {target!r}")
    gun = copy.deepcopy(gun)
    gun.update(copy.deepcopy(mount.get("overrides") or {}))
    comp_keys = {k: copy.deepcopy(v) for k, v in comp.items() if not isinstance(v, dict)}
    comp_keys.update(copy.deepcopy(mount.get("component_overrides") or {}))
    return target, gun, comp_keys


def _building_entry(
    gp: dict[str, Any], comp: dict[str, Any], warn: Callable[[str], None]
) -> dict[str, Any]:
    """A GameParams-Building-shaped dict for one composite, so the
    building sidecar composer reads it like any Building entry."""
    cid = comp["id"]
    base = comp["base"]
    armor_from = base.get("armor_from")
    if armor_from:
        src = gp.get(armor_from)
        hull_src = src.get("hull") if isinstance(src, dict) else None
        if not isinstance(hull_src, dict):
            raise ValueError(f"{cid}: armor_from {armor_from} has no hull")
        armor = copy.deepcopy(hull_src.get("armor") or {})
    else:
        armor = {str((ARMOR_LAYER << 16) | ARMOR_MATERIAL_ID): float(base.get("armor_mm", 0.0))}
    entry: dict[str, Any] = {
        # Typed consumers read building.id / level as integers; 0 marks
        # "no GameParams entry".
        "id": 0,
        "name": cid,
        "level": int(comp.get("level", 0)),
        "typeinfo": {"type": "Building", "species": comp.get("species"), "nation": "Common"},
        "hull": {
            "model": base["model"],
            "deadModel": "",
            "health": float(comp["health"]),
            "armor": armor,
            "canBeSuppressed": bool(comp.get("can_be_suppressed", False)),
            "permanentlyVisibleByEnemies": False,
            "visionDistance": comp.get("vision_distance"),
            "deathModelReplacementDelay": None,
        },
    }
    for mount in comp.get("mounts") or []:
        target, gun, comp_keys = _source_gun(gp, cid, mount)
        dst = entry.setdefault(target, {})
        for k, v in comp_keys.items():
            if k in dst and dst[k] != v:
                warn(f"{cid}: {target}.{k} differs between mount sources ({dst[k]!r} kept, {v!r} dropped)")
                continue
            dst[k] = v
        dst[mount["hp_name"]] = gun
    return entry


# ── GLB reading / writing helpers ─────────────────────────────────────


def _read_accessor(gltf: dict[str, Any], bin_data: bytes, idx: int) -> np.ndarray:
    a = gltf["accessors"][idx]
    bv = gltf["bufferViews"][a["bufferView"]]
    dt = np.dtype(_NP_TYPE[a["componentType"]])
    n = _N_COMP[a["type"]]
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride") or dt.itemsize * n
    if stride == dt.itemsize * n:
        return np.frombuffer(bin_data, dtype=dt, count=a["count"] * n, offset=off).reshape(a["count"], n)
    rows = [np.frombuffer(bin_data, dtype=dt, count=n, offset=off + k * stride) for k in range(a["count"])]
    return np.stack(rows)


def _np_matrix(m: list[float]) -> np.ndarray:
    return np.array(m, dtype=float).reshape(4, 4).T


def _render_triangles(gltf: dict[str, Any], bin_data: bytes) -> np.ndarray:
    """Every render triangle of the GLB in the scene-root frame, (N, 3, 3).
    Meshes under the ``Armor`` / ``Hitboxes`` groups are left out."""
    nodes = gltf.get("nodes") or []
    out: list[np.ndarray] = []

    def walk(i: int, parent: list[float], skip: bool) -> None:
        node = nodes[i]
        world = _mat_mul(parent, _node_local_matrix(node))
        skip = skip or node.get("name") in ("Armor", "Hitboxes")
        if "mesh" in node and not skip:
            m = _np_matrix(world)
            for prim in gltf["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                pos = _read_accessor(gltf, bin_data, prim["attributes"]["POSITION"]).astype(float)
                pos = pos @ m[:3, :3].T + m[:3, 3]
                if "indices" in prim:
                    idx = _read_accessor(gltf, bin_data, prim["indices"]).astype(np.int64).reshape(-1, 3)
                else:
                    idx = np.arange(len(pos) - len(pos) % 3).reshape(-1, 3)
                out.append(pos[idx])
        for c in node.get("children", []) or []:
            walk(c, world, skip)

    for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        walk(r, _IDENT, False)
    if not out:
        raise ValueError("base GLB has no render triangles")
    return np.concatenate(out)


def _surface_y(tris: np.ndarray, x: float, z: float) -> float | None:
    """Highest Y where the vertical line through (x, z) meets a triangle."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    v0 = c[:, [0, 2]] - a[:, [0, 2]]
    v1 = b[:, [0, 2]] - a[:, [0, 2]]
    v2 = np.array([x, z]) - a[:, [0, 2]]
    d00 = (v0 * v0).sum(1)
    d01 = (v0 * v1).sum(1)
    d11 = (v1 * v1).sum(1)
    d20 = (v2 * v0).sum(1)
    d21 = (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    u = (d11 * d20 - d01 * d21) / den
    v = (d00 * d21 - d01 * d20) / den
    hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
    if not hit.any():
        return None
    y = a[:, 1] + u * (c[:, 1] - a[:, 1]) + v * (b[:, 1] - a[:, 1])
    return float(y[hit].max())


class _BinWriter:
    """Appends accessors to a GLB's single binary buffer."""

    def __init__(self, gltf: dict[str, Any], bin_data: bytes) -> None:
        self.gltf = gltf
        self.bin = bytearray(bin_data)
        if not gltf.get("buffers"):
            gltf["buffers"] = [{"byteLength": 0}]
        gltf.setdefault("bufferViews", [])
        gltf.setdefault("accessors", [])

    def add(self, arr: np.ndarray, comp: int, typ: str, target: int | None, minmax: bool = False) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        data = np.ascontiguousarray(arr).tobytes()
        view: dict[str, Any] = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        self.bin.extend(data)
        self.gltf["bufferViews"].append(view)
        acc: dict[str, Any] = {
            "bufferView": len(self.gltf["bufferViews"]) - 1,
            "componentType": comp,
            "count": int(arr.shape[0]),
            "type": typ,
        }
        if minmax:
            acc["min"] = [float(v) for v in arr.min(0)]
            acc["max"] = [float(v) for v in arr.max(0)]
        self.gltf["accessors"].append(acc)
        return len(self.gltf["accessors"]) - 1

    def finish(self) -> bytes:
        while len(self.bin) % 4:
            self.bin.append(0)
        self.gltf["buffers"][0]["byteLength"] = len(self.bin)
        return bytes(self.bin)


def _add_material(gltf: dict[str, Any], name: str) -> int:
    mats = gltf.setdefault("materials", [])
    mats.append({
        "name": name,
        "alphaMode": "BLEND",
        "doubleSided": True,
        "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.8},
        "emissiveFactor": [0.0, 0.0, 0.0],
    })
    return len(mats) - 1


def _add_root_group(gltf: dict[str, Any], name: str, child: int) -> None:
    gltf["nodes"].append({"name": name, "children": [child], "extras": {"wows_composite": True}})
    gltf["scenes"][gltf.get("scene", 0)]["nodes"].append(len(gltf["nodes"]) - 1)


def _add_armor_group(gltf: dict[str, Any], w: _BinWriter, tris: np.ndarray) -> None:
    """``Armor`` / ``Armor_Hull``: the render triangles as a triangle soup
    with per-face normals, ``_MATERIAL_ID`` and the toolkit's colour."""
    pos = tris.reshape(-1, 3).astype(np.float32)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(ln > 1e-12, n / np.where(ln > 1e-12, ln, 1.0), np.array([0.0, 1.0, 0.0]))
    nrm = np.repeat(n, 3, axis=0).astype(np.float32)
    mid = np.full((len(pos), 1), ARMOR_MATERIAL_ID, dtype=np.uint16)
    col = np.tile(np.array(ARMOR_COLOR, dtype=np.float32), (len(pos), 1))
    idx = np.arange(len(pos), dtype=np.uint32).reshape(-1, 1)
    attrs = {
        "_MATERIAL_ID": w.add(mid, COMP_UNSIGNED_SHORT, "SCALAR", 34962),
        "POSITION": w.add(pos, COMP_FLOAT, "VEC3", 34962, minmax=True),
        "NORMAL": w.add(nrm, COMP_FLOAT, "VEC3", 34962),
        "COLOR_0": w.add(col, COMP_FLOAT, "VEC4", 34962),
    }
    prim = {"attributes": attrs, "indices": w.add(idx, COMP_UNSIGNED_INT, "SCALAR", 34963),
            "material": _add_material(gltf, "armor_Armor_Hull")}
    gltf.setdefault("meshes", []).append({"name": "Armor_Hull", "primitives": [prim]})
    gltf["nodes"].append({"name": "Armor_Hull", "mesh": len(gltf["meshes"]) - 1})
    _add_root_group(gltf, "Armor", len(gltf["nodes"]) - 1)


def _box_mesh(lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """24-vertex axis-aligned box with outward normals and counter-
    clockwise (outward-facing) triangles."""
    pos, nrm, idx = [], [], []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            u, v = [a for a in range(3) if a != axis]
            normal = [0.0, 0.0, 0.0]
            normal[axis] = sign
            corners = []
            for cu, cv in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = [0.0, 0.0, 0.0]
                p[axis] = hi[axis] if sign > 0 else lo[axis]
                p[u] = hi[u] if cu else lo[u]
                p[v] = hi[v] if cv else lo[v]
                corners.append(p)
            base = len(pos)
            pos += corners
            nrm += [normal] * 4
            quad = [(0, 1, 2), (0, 2, 3)]
            e1 = np.subtract(corners[1], corners[0])
            e2 = np.subtract(corners[2], corners[0])
            if np.dot(np.cross(e1, e2), normal) < 0:
                quad = [(0, 2, 1), (0, 3, 2)]
            for t in quad:
                idx += [base + t[0], base + t[1], base + t[2]]
    return (np.array(pos, dtype=np.float32), np.array(nrm, dtype=np.float32),
            np.array(idx, dtype=np.uint32).reshape(-1, 1))


def _add_hitbox_group(gltf: dict[str, Any], w: _BinWriter, lo: np.ndarray, hi: np.ndarray) -> None:
    pos, nrm, idx = _box_mesh(lo, hi)
    prim = {
        "attributes": {"POSITION": w.add(pos, COMP_FLOAT, "VEC3", 34962, minmax=True),
                       "NORMAL": w.add(nrm, COMP_FLOAT, "VEC3", 34962)},
        "indices": w.add(idx, COMP_UNSIGNED_INT, "SCALAR", 34963),
        "material": _add_material(gltf, "armor_CM_SB_Hull_0"),
    }
    gltf.setdefault("meshes", []).append({"name": "CM_SB_Hull_0", "primitives": [prim]})
    gltf["nodes"].append({"name": "CM_SB_Hull_0", "mesh": len(gltf["meshes"]) - 1})
    _add_root_group(gltf, "Hitboxes", len(gltf["nodes"]) - 1)


# ── mount placement ───────────────────────────────────────────────────


def _ry(theta_deg: float) -> list[float]:
    """Column-major rotation about +Y; maps +Z to (sin t, 0, cos t)."""
    t = math.radians(theta_deg)
    c, s = math.cos(t), math.sin(t)
    return [c, 0.0, -s, 0.0, 0.0, 1.0, 0.0, 0.0, s, 0.0, c, 0.0, 0.0, 0.0, 0.0, 1.0]


def _bearing(v: list[float]) -> float:
    return math.degrees(math.atan2(v[0], v[2]))


def _hp_yaw_for_facing(gun_gltf: dict[str, Any], facing_deg: float) -> tuple[float, float | None]:
    """HP yaw (degrees about +Y) that makes the gun, placed by the
    ship-mount rule, rest with its muzzle bearing ``facing_deg``; also
    the horizontal share of the rest muzzle direction (None: no muzzle)."""
    d = _rest_muzzle_dir(_gun_rig_nodes(gun_gltf))
    if d is None:
        return facing_deg, None
    c = _mount_matrix(_IDENT, _rotate_y_bone(gun_gltf))
    v = [c[0] * d[0] + c[4] * d[1] + c[8] * d[2],
         c[1] * d[0] + c[5] * d[1] + c[9] * d[2],
         c[2] * d[0] + c[6] * d[1] + c[10] * d[2]]
    horiz = math.hypot(v[0], v[2])
    if horiz < 1e-6:
        return facing_deg, horiz
    return (facing_deg - _bearing(v) + 180.0) % 360.0 - 180.0, horiz


def _model_node(gltf: dict[str, Any], stem: str) -> tuple[int | None, list[float]]:
    """Index and world matrix of the base model's own node (the toolkit
    names it after the model stem and hangs its hardpoints under it)."""
    nodes = gltf.get("nodes") or []
    found: list[tuple[int, list[float]]] = []

    def walk(i: int, parent: list[float]) -> None:
        world = _mat_mul(parent, _node_local_matrix(nodes[i]))
        if nodes[i].get("name") == stem:
            found.append((i, world))
        for c in nodes[i].get("children", []) or []:
            walk(c, world)

    for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        walk(r, _IDENT)
    return found[0] if found else (None, _IDENT)


def _compose_hull_glb(
    glb_path: Path,
    comp: dict[str, Any],
    cfg: PipelineConfig,
    entry: dict[str, Any],
    warn: Callable[[str], None],
) -> dict[str, Any]:
    """Add the mount nodes (and missing collision groups) to a freshly
    exported base GLB, in place. Returns what was placed."""
    data = glb_path.read_bytes()
    gltf, bin_data = parse_glb(data)
    if (gltf.get("extras") or {}).get("wows_composite"):
        raise ValueError(f"{glb_path} is already composed; export the base again before composing")
    cid = comp["id"]
    stem = _stem(comp["base"]["model"])
    tris = _render_triangles(gltf, bin_data)
    flat = tris.reshape(-1, 3)
    lo, hi = flat.min(0), flat.max(0)
    centre = (lo + hi) / 2.0

    parent_idx, parent_world = _model_node(gltf, stem)
    parent_inv = np.linalg.inv(_np_matrix(parent_world))
    placed: list[dict[str, Any]] = []
    for mount in comp.get("mounts") or []:
        hp = mount["hp_name"]
        at = mount.get("at") or {}
        xz = at.get("xz", "center")
        x, z = (float(centre[0]), float(centre[2])) if xz == "center" else (float(xz[0]), float(xz[1]))
        y_spec = at.get("y", 0.0)
        if y_spec == "surface":
            y = _surface_y(tris, x, z)
            if y is None:
                raise ValueError(f"{cid}: {hp} has no base surface under ({x:.2f}, {z:.2f})")
        else:
            y = float(y_spec)
        y += float(at.get("y_offset", 0.0))
        gun = None
        for comp_name in ("artillery", "airDefense"):
            g = (entry.get(comp_name) or {}).get(hp)
            if isinstance(g, dict):
                gun = g
        gun_stem = _stem(gun["model"])
        gun_glb = shared_gun_dir(cfg, gun_stem) / f"{gun_stem}.glb"
        if not gun_glb.is_file():
            raise ValueError(f"{cid}: gun GLB {gun_glb} missing; it is exported before the base is composed")
        facing = float(mount.get("facing_deg", 0.0))
        yaw, horiz = _hp_yaw_for_facing(_read_glb_json(gun_glb), facing)
        if horiz is None:
            warn(f"{cid}: {hp} {gun_stem} has no muzzle nodes; HP yaw = facing")
        elif horiz < 0.2:
            warn(f"{cid}: {hp} {gun_stem} rests nearly vertical (horizontal share {horiz:.2f}); facing is approximate")
        world = _ry(yaw)
        world[12], world[13], world[14] = x, y, z
        local = (parent_inv @ _np_matrix(world)).T.reshape(-1)
        node = {"name": hp, "matrix": [round(float(v), 6) for v in local], "extras": {"wows_composite": True}}
        gltf["nodes"].append(node)
        if parent_idx is None:
            gltf["scenes"][gltf.get("scene", 0)]["nodes"].append(len(gltf["nodes"]) - 1)
        else:
            gltf["nodes"][parent_idx].setdefault("children", []).append(len(gltf["nodes"]) - 1)
        placed.append({"hp_name": hp, "position": [round(v, 4) for v in (x, y, z)], "hp_yaw_deg": round(yaw, 3),
                       "facing_deg": facing})

    roots = {gltf["nodes"][r].get("name") for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]}
    w = _BinWriter(gltf, bin_data)
    synthesized = []
    if "Armor" not in roots:
        _add_armor_group(gltf, w, tris)
        synthesized.append("Armor")
    if "Hitboxes" not in roots:
        _add_hitbox_group(gltf, w, lo, hi)
        synthesized.append("Hitboxes")
    result = {
        "mounts": placed,
        "synthesized_groups": synthesized,
        "base_bounds": {"min": [round(float(v), 4) for v in lo], "max": [round(float(v), 4) for v in hi]},
    }
    gltf.setdefault("extras", {})["wows_composite"] = {"id": cid, "base": stem, "synthesized": synthesized,
                                                       "placement": result}
    write_glb(gltf, w.finish(), glb_path)
    return result


# ── public entry point ────────────────────────────────────────────────


@shared_textures.with_export_session(_buildings_session_roots)
def run_compose(
    *,
    spec_path: Path,
    ids: Iterable[str] | None = None,
    skip_glb: bool = False,
    refresh_guns: bool = False,
    config: PipelineConfig | None = None,
    on_event: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Compose the spec's buildings (or the ``ids`` among them). Returns
    the run report, also written to ``buildings/compose.json``.

    ``skip_glb`` keeps the GLBs on disk and recomposes only the sidecars
    (the mount nodes are already in the hull GLB). Gun GLBs already in
    the shared gun library are reused unless ``refresh_guns``.
    """
    cfg = config or PipelineConfig.load()
    spec_path = Path(spec_path)
    spec = load_spec(spec_path)
    composites = spec.get("composites") or []
    want = set(ids or [])
    if want:
        missing = want - {c["id"] for c in composites}
        if missing:
            raise ValueError(f"ids not in {spec_path.name}: {sorted(missing)}")
        composites = [c for c in composites if c["id"] in want]
    if not composites:
        raise ValueError(f"{spec_path}: no composites selected")

    on_event("loading GameParams (full dump; a minute cold)...")
    gp = gameparams.load_full(config=cfg)
    report: dict[str, Any] = {"spec": str(spec_path), "buildings": {}, "warnings": []}
    entries: dict[str, dict[str, Any]] = {}
    warnings: dict[str, list[str]] = {}
    for comp in composites:
        warnings[comp["id"]] = []
        entries[comp["id"]] = _building_entry(gp, comp, warnings[comp["id"]].append)

    placements: dict[str, dict[str, Any]] = {}
    if not skip_glb:
        # Guns first: the base's mount nodes are turned from the gun GLBs.
        gun_jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for comp in composites:
            for job in _gun_glb_jobs(entries[comp["id"]], cfg, seen):
                if refresh_guns or not Path(job["output"]).is_file():
                    gun_jobs.append(job)
        if gun_jobs:
            on_event(f"exporting {len(gun_jobs)} gun GLB(s) ...")
            batch_export_model(gun_jobs, shared={"no_textures": True, "emit_hardpoints": True, "all_render_sets": True},
                               config=cfg)
        hull_jobs = []
        for comp in composites:
            models = building_dir(cfg, comp["id"]) / MODELS_SUBDIR
            hull_jobs.append({
                "geometry": _geometry_vfs(comp["base"]["model"]),
                "output": models / f"{comp['id']}_hull.glb",
                "raw_dds_dir": models / "textures_dds",
                "material_mappings_json": models / f"{comp['id']}_material_mappings.json",
            })
        on_event(f"exporting {len(hull_jobs)} base GLB(s) ...")
        batch_export_model(hull_jobs, shared={"no_textures": True, "emit_hardpoints": True, "collision_hitbox_groups": True},
                           config=cfg)
        for comp, job in zip(composites, hull_jobs, strict=True):
            out = Path(job["output"])
            if not out.is_file():
                warnings[comp["id"]].append(f"base GLB missing after export: {comp['base']['model']}")
                continue
            _flip_hull_winding(out)
            placements[comp["id"]] = _compose_hull_glb(out, comp, cfg, entries[comp["id"]], warnings[comp["id"]].append)

    for comp in composites:
        cid = comp["id"]
        hull_glb = building_dir(cfg, cid) / MODELS_SUBDIR / f"{cid}_hull.glb"
        if not hull_glb.is_file():
            report["buildings"][cid] = {"skipped": "hull GLB missing", "warnings": warnings[cid]}
            on_event(f"  {cid}: SKIPPED (hull GLB missing)")
            continue
        glb_extras = (_read_glb_json(hull_glb).get("extras") or {}).get("wows_composite") or {}
        synthesized = glb_extras.get("synthesized")
        if (isinstance(synthesized, list) and "Armor" not in synthesized
                and not comp["base"].get("armor_from")):
            warnings[cid].append(
                f"base {_stem(comp['base']['model'])} keeps its own Armor group, but the spec "
                f"gives no armor_from: only material {ARMOR_MATERIAL_ID} gets a thickness "
                f"(armor_mm {comp['base'].get('armor_mm', 0.0)}); the base's other material ids "
                f"get none"
            )
        sidecar = _compose_sidecar(cid, entries[cid], cfg, gp, warnings[cid])
        sidecar["pipeline"] = {"kind": "building", "generator": GENERATOR, "composite": True}
        sidecar["provenance"]["generator"] = GENERATOR
        sidecar["building"]["display_name"] = comp.get("name")
        sidecar["composition"] = {
            "schema": SPEC_SCHEMA,
            "spec": spec_path.name,
            "name": comp.get("name"),
            "base": {
                "model": comp["base"]["model"],
                "armor_mm": comp["base"].get("armor_mm"),
                "armor_from": comp["base"].get("armor_from"),
                "armor_material_id": None if comp["base"].get("armor_from") else ARMOR_MATERIAL_ID,
                "synthesized_groups": glb_extras.get("synthesized"),
            },
            "mounts": [
                {
                    "hp_name": m["hp_name"],
                    "source": m["source"],
                    "overrides": m.get("overrides") or {},
                    "component_overrides": m.get("component_overrides") or {},
                    "at": m.get("at") or {},
                    "facing_deg": float(m.get("facing_deg", 0.0)),
                }
                for m in comp.get("mounts") or []
            ],
            "notes": comp.get("notes") or [],
        }
        shared_textures.repair_refs(sidecar, building_dir(cfg, cid) / MODELS_SUBDIR)
        out = sidecar_path_for(cfg, cid)
        _write_json_atomic(out, sidecar)
        report["buildings"][cid] = {
            "sidecar": str(out),
            "turrets": len(sidecar["turrets"]),
            "boxes": len(sidecar["hitbox"]["boxes"]),
            "shells": len(sidecar["ballistics"]["shells"]),
            "materials": len(sidecar["materials"]),
            "placement": placements.get(cid) or glb_extras.get("placement"),
            "warnings": warnings[cid],
        }
        report["warnings"] += [f"{cid}: {w}" for w in warnings[cid]]
        on_event(f"  {cid}: sidecar ok, {len(sidecar['turrets'])} mount(s), {len(sidecar['hitbox']['boxes'])} hit box(es)"
                 + (f", {len(warnings[cid])} warning(s)" if warnings[cid] else ""))

    gun_warnings: list[str] = []
    gun_index = _write_gun_index(cfg, gun_warnings)
    report["warnings"] += gun_warnings
    if gun_index is not None:
        report["gun_index"] = str(gun_index)
    report_path = buildings_root(cfg) / "compose.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    on_event(f"report: {report_path}")
    return report


__all__ = ["SPEC_SCHEMA", "load_spec", "run_compose"]
