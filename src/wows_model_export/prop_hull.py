"""Prop hulls: a minimal ship sidecar around a static prop model.

Some vessels a consumer wants to sail exist only as static models: map and
port props (barges, lighters, launches, buoys), or a static model from
another source. A *prop hull* wraps one such model in the ship export
layout, so a consumer that builds ships from ship exports treats it as a
hull: it has a hull GLB, an armour-less hitbox, no mounts and its
buoyancy dimensions, and nothing else.

    <workspace>/ships/<id>/
      <id>.meta.json                  <- ship sidecar (schema 3) + ``prop_hull`` block
      build_deps.json                 <- the game inputs it was made from
      models/<id>_hull.glb            <- ``Hull`` group (the prop's meshes) +
                                         ``Hitboxes`` group (one box, CM_SB_hull_1);
                                         no ``Armor`` group
      models/<id>_accessories.json    <- placements doc with every section empty
      models/<id>_material_mappings.json, models/textures_dds/
                                      <- WoWS models only (textures by the store rule)

Sources (one per prop):

- ``{"model": "content/.../X.model"}``: a WoWS model in the old format
  (a loose ``.geometry`` beside it). The toolkit exports every render set
  (all LODs) with its raw DDS textures and material mappings, so the
  sidecar binds WG textures as a ship's does.
- ``{"prop": "OSV056_Spitz_Schute"}``: a WoWS map / port prop by its id,
  resolved to its ``.model`` path through the VFS manifest.
- ``{"glb": "path/to/model.glb"}``: any static ``.glb`` / ``.gltf`` on disk
  (a relative path resolves against the workspace root). Its own glTF
  materials and textures are kept in the GLB; the sidecar lists the
  materials with no ``texture_sets`` and ``prop_hull.textures`` is
  ``"glb"``, which tells a consumer to keep the GLB's own materials.

Models whose ``.geometry`` the client does not ship (only their assets.bin
prototypes exist) are refused with a clear error; no reader can export them
(``reference/toolkit/UNSHIPPED_MODELS.md``).

Options per prop: ``drop_nodes`` (regex: nodes whose name matches are
left out with their subtree), ``drop_materials`` (regex: primitives whose
material name matches are left out; default drops WG's water occluders),
``rename`` (``[[regex, replacement], ...]`` applied to node and mesh
names), ``scale`` (uniform, on the ``Hull`` node), ``name``, ``nation``,
``class`` and ``notes``.

The model's own frame is kept: metres, +Y up, the bow toward -Z (the frame
of the toolkit's ship GLBs), the waterline at y = 0. WG builds its boat
props at real size and places them with y = 0 on the water, so a WoWS prop
needs no correction; a model from elsewhere must already be in that frame.

Buoyancy is measured from the LOD-0 render triangles of the ``Hull`` group
by column integration over a grid on the water plane: for each cell the
lowest surface below y = 0 gives the submerged depth (the hull is taken as
solid from its bottom up to the waterline). See ``buoyancy_from_triangles``
and ``reference/schemas/METADATA_SPEC.md`` section 17.

The spec (``schema: "wows_prop_hulls/v1"``) the caller supplies:

    {"schema": "wows_prop_hulls/v1",
     "props": [{
        "id": "OSV056_Spitz_Schute",            # ships/<id>/ folder and sidecar name
        "name": "Spitzschute barge",           # ship.display_name
        "source": {"prop": "OSV056_Spitz_Schute"},
        "nation": "common", "class": "AUX",    # optional (these defaults)
        "scale": 1.0,                          # optional
        "drop_nodes": "regex", "drop_materials": "regex",
        "rename": [["_lod1$", ""]],
        "notes": ["why this model"]}]}
"""

from __future__ import annotations

import base64
import copy
import datetime as _dt
import hashlib
import json
import math
import re
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import numpy as np

from . import build_deps, shared_textures
from ._glb import COMP_FLOAT, COMP_UNSIGNED_INT, parse_glb, write_glb
from .building_compose import _box_mesh, _read_accessor
from .building_export import _model_materials, _write_json_atomic
from .compose.build_tracking import write_export_deps_safely
from .config import PipelineConfig
from .resolve import sidecar as resolve_sidecar
from .toolkit import ensure_manifest
from .toolkit._subprocess import toolkit_capabilities
from .toolkit.ship import batch_export_model

SPEC_SCHEMA = "wows_prop_hulls/v1"
BLOCK_SCHEMA = "wows_prop_hull/v1"
GENERATOR = "wows-build-prop-hull"
PIPELINE_KIND = "prop_hull"

#: Primitives with these materials are left out by default: WG's water
#: occluder (a mesh that keeps the water plane out of an open hull), not
#: something that renders.
DEFAULT_DROP_MATERIALS = r"^WATEROCCLUDER$"

#: The one hit box of a prop hull: the ``Hull`` hit location of WG's
#: vocabulary, covering the hull's render bounds.
HITBOX_NAME = "CM_SB_hull_1"
HITBOX_SECTION = "Hull"
HITBOX_HL_TYPE = "hull_hitlocation"
HITBOX_REGION = "hull"
#: Material the toolkit writes on hit-box cubes (``hitbox_<name>``).
_HITBOX_COLOR = [0.4, 0.7, 1.0, 0.25]

#: Seawater density (t per m3) for the displacement.
SEAWATER_T_PER_M3 = 1.025

#: Meshes whose names carry these tokens are not LOD 0 (the ship GLB
#: convention: ``_lod<N>Shape`` / ``_lodShape<N>``; damage pieces).
_NOT_LOD0 = re.compile(r"_lod(?:Shape)?[1-9]|_crack_|_patch_", re.IGNORECASE)

_GROUPS = ("Hull", "Armor", "Hitboxes")
_IDENT = np.eye(4)


class PropHullError(ValueError):
    """A prop hull that cannot be built (bad spec, unreadable source)."""


# ── layout ────────────────────────────────────────────────────────────


def ship_dir(cfg: PipelineConfig, pid: str) -> Path:
    return (cfg.workspace / "ships" / pid).resolve()


def sidecar_path(cfg: PipelineConfig, pid: str) -> Path:
    return ship_dir(cfg, pid) / f"{pid}{resolve_sidecar.SIDECAR_SUFFIX}"


def is_prop_hull(doc: Any) -> bool:
    """True when a sidecar document is a prop hull."""
    return isinstance(doc, dict) and (doc.get("pipeline") or {}).get("kind") == PIPELINE_KIND


def is_prop_hull_dir(folder: Path) -> bool:
    """True when ``ships/<id>/<id>.meta.json`` is a prop hull sidecar."""
    path = Path(folder) / f"{Path(folder).name}{resolve_sidecar.SIDECAR_SUFFIX}"
    try:
        return is_prop_hull(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def workspace_prop_hulls(cfg: PipelineConfig) -> list[str]:
    root = cfg.workspace / "ships"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and is_prop_hull_dir(p))


# ── spec ──────────────────────────────────────────────────────────────


def _check_entry(e: dict[str, Any], where: str) -> dict[str, Any]:
    pid = e.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", pid or ""):
        raise PropHullError(f"{where}: id {pid!r} must be a folder name (letters, digits, _ . -)")
    src = e.get("source")
    if not isinstance(src, dict) or len([k for k in ("model", "prop", "glb") if src.get(k)]) != 1:
        raise PropHullError(f"{pid}: source needs exactly one of model, prop, glb")
    if src.get("model") and not str(src["model"]).endswith(".model"):
        raise PropHullError(f"{pid}: source.model must be a .model VFS path")
    for key in ("drop_nodes", "drop_materials"):
        if e.get(key) is not None:
            try:
                re.compile(e[key])
            except re.error as err:
                raise PropHullError(f"{pid}: {key} is not a regex: {err}") from err
    for pair in e.get("rename") or []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise PropHullError(f"{pid}: rename entries are [regex, replacement] pairs")
        re.compile(pair[0])
    scale = e.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or scale <= 0:
        raise PropHullError(f"{pid}: scale must be a positive number")
    cls = e.get("class", "AUX")
    if cls not in resolve_sidecar.VALID_SHIP_CLASSES:
        raise PropHullError(f"{pid}: class {cls!r} is not one of {resolve_sidecar.VALID_SHIP_CLASSES}")
    return e


def load_spec(path: Path) -> dict[str, Any]:
    """Read and check a prop hull spec. Raises PropHullError."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != SPEC_SCHEMA:
        raise PropHullError(f"{path}: schema must be {SPEC_SCHEMA!r}, got {doc.get('schema')!r}")
    seen: set[str] = set()
    for e in doc.get("props") or []:
        _check_entry(e, str(path))
        if e["id"] in seen:
            raise PropHullError(f"{path}: id {e['id']} appears twice")
        seen.add(e["id"])
    return doc


# ── source resolution ─────────────────────────────────────────────────


def _manifest_paths(cfg: PipelineConfig) -> set[str]:
    """Every path of the VFS manifest, without its leading slash."""
    path = ensure_manifest(config=cfg)
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return {str(r.get("path", "")).lstrip("/") for r in rows if not r.get("is_directory")}


def _resolve_source(e: dict[str, Any], cfg: PipelineConfig, vfs: Callable[[], set[str]]) -> dict[str, Any]:
    """``{"kind": "model"|"glb", ...}`` for one spec entry."""
    pid = e["id"]
    src = e["source"]
    if src.get("glb"):
        p = Path(src["glb"])
        if not p.is_absolute():
            p = (cfg.workspace / p).resolve()
        if not p.is_file():
            raise PropHullError(f"{pid}: GLB {p} not found")
        if p.suffix.lower() not in (".glb", ".gltf"):
            raise PropHullError(f"{pid}: {p} is not a .glb or .gltf")
        return {"kind": "glb", "glb": str(p)}
    paths = vfs()
    if src.get("prop"):
        stem = src["prop"]
        found = sorted(p for p in paths if p.endswith(f"/{stem}/{stem}.model"))
        if not found:
            raise PropHullError(f"{pid}: no {stem}.model in the VFS manifest")
        if len(found) > 1:
            raise PropHullError(f"{pid}: prop {stem} is ambiguous ({', '.join(found)}); name source.model")
        model = found[0]
    else:
        model = str(src["model"]).lstrip("/")
        if model not in paths:
            raise PropHullError(f"{pid}: {model} is not in the VFS manifest")
    geometry = model[: -len(".model")] + ".geometry"
    if geometry not in paths:
        raise PropHullError(
            f"{pid}: {model} has no .geometry in the VFS; the client does not ship "
            "this model's geometry, so it cannot be exported"
        )
    return {"kind": "model", "model": model, "geometry": geometry, "prop": src.get("prop")}


# ── glTF loading (GLB or .gltf with external buffers / images) ───────


def _data_uri_bytes(uri: str) -> bytes:
    head, _, data = uri.partition(",")
    return base64.b64decode(data) if ";base64" in head else unquote(data).encode("latin-1")


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".ktx2": "image/ktx2", ".dds": "image/vnd-ms.dds"}


def load_gltf(path: Path) -> tuple[dict[str, Any], list[bytes]]:
    """``(gltf, buffers)`` of a .glb or .gltf. Images given by URI are
    loaded into extra buffers, so every image is a bufferView afterwards."""
    path = Path(path)
    if path.suffix.lower() == ".glb":
        gltf, bin_data = parse_glb(path.read_bytes())
        buffers = [bin_data]
        for b in (gltf.get("buffers") or [])[1:]:
            uri = b.get("uri") or ""
            buffers.append(_data_uri_bytes(uri) if uri.startswith("data:") else (path.parent / unquote(uri)).read_bytes())
    else:
        gltf = json.loads(path.read_text(encoding="utf-8"))
        buffers = []
        for b in gltf.get("buffers") or []:
            uri = b.get("uri") or ""
            buffers.append(_data_uri_bytes(uri) if uri.startswith("data:") else (path.parent / unquote(uri)).read_bytes())
    for img in gltf.get("images") or []:
        uri = img.get("uri")
        if not uri:
            continue
        data = _data_uri_bytes(uri) if uri.startswith("data:") else (path.parent / unquote(uri)).read_bytes()
        mime = img.get("mimeType") or (uri.split(";")[0][5:] if uri.startswith("data:")
                                      else _MIME.get(Path(unquote(uri)).suffix.lower(), "image/png"))
        gltf.setdefault("buffers", []).append({"byteLength": len(data)})
        buffers.append(data)
        gltf.setdefault("bufferViews", []).append({"buffer": len(buffers) - 1, "byteOffset": 0, "byteLength": len(data)})
        img.pop("uri", None)
        img["bufferView"] = len(gltf["bufferViews"]) - 1
        img["mimeType"] = mime
        img.setdefault("name", Path(unquote(uri)).stem if not uri.startswith("data:") else None)
        if img.get("name") is None:
            img.pop("name")
    for ext in gltf.get("extensionsRequired") or []:
        if ext not in ("KHR_texture_transform", "KHR_materials_unlit"):
            raise PropHullError(f"{path}: needs glTF extension {ext}, which this builder does not read")
    return gltf, buffers


# ── restructure + compaction ──────────────────────────────────────────


class _Writer:
    """One fresh binary buffer that copied bufferViews are appended to."""

    def __init__(self) -> None:
        self.bin = bytearray()
        self.views: list[dict[str, Any]] = []

    def view(self, data: bytes, **extra: Any) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        v.update({k: val for k, val in extra.items() if val is not None})
        self.bin.extend(data)
        self.views.append(v)
        return len(self.views) - 1


def _fix_position_bounds(gltf: dict[str, Any], buffers: list[bytes]) -> int:
    """Set every POSITION accessor's ``min`` / ``max`` from its data (glTF
    requires them, consumers cull and measure by them, and some writers
    leave stale values). Returns how many changed."""
    changed = 0
    done: set[int] = set()
    for m in gltf.get("meshes") or []:
        for p in m.get("primitives") or []:
            i = (p.get("attributes") or {}).get("POSITION")
            if i is None or i in done:
                continue
            done.add(i)
            a = gltf["accessors"][i]
            if "bufferView" not in a or a.get("sparse") or a.get("componentType") != COMP_FLOAT:
                continue
            v = gltf["bufferViews"][a["bufferView"]]
            off = v.get("byteOffset", 0) + a.get("byteOffset", 0)
            stride = v.get("byteStride") or 12
            raw = buffers[v.get("buffer", 0)]
            if stride == 12:
                pos = np.frombuffer(raw, dtype=np.float32, count=a["count"] * 3, offset=off).reshape(-1, 3)
            else:
                pos = np.stack([np.frombuffer(raw, dtype=np.float32, count=3, offset=off + k * stride)
                                for k in range(a["count"])])
            if not len(pos):
                continue
            lo, hi = [float(x) for x in pos.min(0)], [float(x) for x in pos.max(0)]
            if a.get("min") != lo or a.get("max") != hi:
                a["min"], a["max"] = lo, hi
                changed += 1
    return changed


def _compact(gltf: dict[str, Any], buffers: list[bytes]) -> tuple[dict[str, Any], bytes, dict[str, int]]:
    """A new glTF holding only what the default scene reaches, in one
    buffer. Animations and cameras are left out (a prop hull is static)."""
    nodes = gltf.get("nodes") or []
    scene = (gltf.get("scenes") or [{"nodes": []}])[gltf.get("scene", 0)]
    order: list[int] = []
    seen: set[int] = set()

    def reach(i: int) -> None:
        if i in seen:
            return
        seen.add(i)
        order.append(i)
        for c in nodes[i].get("children") or []:
            reach(c)

    for r in scene.get("nodes") or []:
        reach(r)
    node_map = {old: new for new, old in enumerate(order)}
    w = _Writer()
    maps: dict[str, dict[int, int]] = {k: {} for k in ("bv", "acc", "img", "samp", "tex", "mat", "mesh", "skin")}
    out: dict[str, Any] = {k: [] for k in ("accessors", "images", "samplers", "textures", "materials", "meshes",
                                            "skins", "nodes")}

    def bv(old: int) -> int:
        if old in maps["bv"]:
            return maps["bv"][old]
        v = gltf["bufferViews"][old]
        off = v.get("byteOffset", 0)
        data = bytes(buffers[v.get("buffer", 0)][off:off + v["byteLength"]])
        maps["bv"][old] = w.view(data, byteStride=v.get("byteStride"), target=v.get("target"))
        return maps["bv"][old]

    def acc(old: int) -> int:
        if old in maps["acc"]:
            return maps["acc"][old]
        a = copy.deepcopy(gltf["accessors"][old])
        if "bufferView" in a:
            a["bufferView"] = bv(a["bufferView"])
        sparse = a.get("sparse")
        if sparse:
            sparse["indices"]["bufferView"] = bv(sparse["indices"]["bufferView"])
            sparse["values"]["bufferView"] = bv(sparse["values"]["bufferView"])
        out["accessors"].append(a)
        maps["acc"][old] = len(out["accessors"]) - 1
        return maps["acc"][old]

    def ref(kind: str, old: int, build: Callable[[int], dict[str, Any]]) -> int:
        if old not in maps[kind]:
            key = {"img": "images", "samp": "samplers", "tex": "textures", "mat": "materials",
                   "mesh": "meshes", "skin": "skins"}[kind]
            out[key].append(build(old))
            maps[kind][old] = len(out[key]) - 1
        return maps[kind][old]

    def build_img(old: int) -> dict[str, Any]:
        img = copy.deepcopy(gltf["images"][old])
        if "bufferView" in img:
            img["bufferView"] = bv(img["bufferView"])
        return img

    def build_tex(old: int) -> dict[str, Any]:
        t = copy.deepcopy(gltf["textures"][old])
        if "source" in t:
            t["source"] = ref("img", t["source"], build_img)
        if "sampler" in t:
            t["sampler"] = ref("samp", t["sampler"], lambda i: copy.deepcopy(gltf["samplers"][i]))
        return t

    def remap_textures(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.endswith("Texture") and isinstance(v, dict) and "index" in v:
                    v["index"] = ref("tex", v["index"], build_tex)
                remap_textures(v)
        elif isinstance(obj, list):
            for v in obj:
                remap_textures(v)

    def build_mat(old: int) -> dict[str, Any]:
        m = copy.deepcopy(gltf["materials"][old])
        m.setdefault("name", f"material_{old}")
        remap_textures(m)
        return m

    def build_mesh(old: int) -> dict[str, Any]:
        m = copy.deepcopy(gltf["meshes"][old])
        for p in m.get("primitives") or []:
            p["attributes"] = {k: acc(v) for k, v in p["attributes"].items()}
            if "indices" in p:
                p["indices"] = acc(p["indices"])
            if "material" in p:
                p["material"] = ref("mat", p["material"], build_mat)
            if p.get("targets"):
                p["targets"] = [{k: acc(v) for k, v in t.items()} for t in p["targets"]]
        return m

    def build_skin(old: int) -> dict[str, Any]:
        s = copy.deepcopy(gltf["skins"][old])
        missing = [j for j in s.get("joints") or [] if j not in node_map]
        if missing:
            raise PropHullError(f"skin {old} has joints outside the kept scene ({missing[:5]})")
        s["joints"] = [node_map[j] for j in s["joints"]]
        if "skeleton" in s:
            s["skeleton"] = node_map.get(s["skeleton"], s["joints"][0] if s["joints"] else 0)
        if "inverseBindMatrices" in s:
            s["inverseBindMatrices"] = acc(s["inverseBindMatrices"])
        return s

    for old in order:
        n = copy.deepcopy(nodes[old])
        n.pop("camera", None)
        if n.get("children"):
            n["children"] = [node_map[c] for c in n["children"]]
        if "mesh" in n:
            n["mesh"] = ref("mesh", n["mesh"], build_mesh)
        if "skin" in n:
            n["skin"] = ref("skin", n["skin"], build_skin)
        out["nodes"].append(n)
    doc: dict[str, Any] = {
        "asset": copy.deepcopy(gltf.get("asset") or {"version": "2.0"}),
        "scene": 0,
        "scenes": [{"nodes": [node_map[r] for r in scene.get("nodes") or []]}],
    }
    doc.update({k: v for k, v in out.items() if v})
    for key in ("extensionsUsed", "extensionsRequired"):
        if gltf.get(key):
            doc[key] = list(gltf[key])
    doc["bufferViews"] = w.views
    doc["buffers"] = [{"byteLength": len(w.bin)}]
    dropped = {"animations": len(gltf.get("animations") or []), "cameras": len(gltf.get("cameras") or [])}
    return doc, bytes(w.bin), dropped


def _restructure(
    gltf: dict[str, Any],
    buffers: list[bytes],
    *,
    drop_nodes: str | None,
    drop_materials: str | None,
    rename: list[list[str]],
    scale: float,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Drop and rename nodes / primitives, compact, and hang the scene
    under a ``Hull`` group node. Returns the new glTF, its buffer and a
    report of what was left out."""
    gltf = copy.deepcopy(gltf)
    nodes = gltf.get("nodes") or []
    scene = (gltf.get("scenes") or [{"nodes": []}])[gltf.get("scene", 0)]
    roots = list(scene.get("nodes") or [])
    names = {nodes[r].get("name") for r in roots}
    clash = sorted(n for n in names if n in _GROUPS)
    if clash:
        raise PropHullError(f"source already has top-level {clash} groups; it is a hull export, not a prop")
    report: dict[str, Any] = {"nodes": [], "primitives": []}
    node_re = re.compile(drop_nodes) if drop_nodes else None
    mat_re = re.compile(drop_materials) if drop_materials else None
    mats = gltf.get("materials") or []

    def keep(i: int) -> bool:
        name = nodes[i].get("name") or ""
        if node_re is not None and node_re.search(name):
            report["nodes"].append(name)
            return False
        return True

    for n in nodes:
        if n.get("children"):
            n["children"] = [c for c in n["children"] if keep(c)]
    scene["nodes"] = [r for r in roots if keep(r)]
    if mat_re is not None:
        for m in gltf.get("meshes") or []:
            kept = []
            for p in m.get("primitives") or []:
                mname = mats[p["material"]].get("name", "") if "material" in p else ""
                if mat_re.search(mname or ""):
                    report["primitives"].append(f"{m.get('name')}:{mname}")
                else:
                    kept.append(p)
            m["primitives"] = kept
        for n in nodes:
            if "mesh" in n and not gltf["meshes"][n["mesh"]]["primitives"]:
                del n["mesh"]
    if rename:
        pats = [(re.compile(a), b) for a, b in rename]
        for coll in (nodes, gltf.get("meshes") or []):
            for item in coll:
                nm = item.get("name")
                if nm:
                    for rx, rep in pats:
                        nm = rx.sub(rep, nm)
                    item["name"] = nm
    report["position_bounds_fixed"] = _fix_position_bounds(gltf, buffers)
    doc, bin_data, dropped = _compact(gltf, buffers)
    report.update(dropped)
    if not any("mesh" in n for n in doc.get("nodes") or []):
        raise PropHullError("nothing to render is left after the drops")
    hull = {"name": "Hull", "children": list(doc["scenes"][0]["nodes"])}
    if scale != 1.0:
        hull["scale"] = [float(scale)] * 3
    doc["nodes"].append(hull)
    doc["scenes"][0]["nodes"] = [len(doc["nodes"]) - 1]
    return doc, bin_data, report


# ── geometry ──────────────────────────────────────────────────────────


def _quat(q: list[float]) -> np.ndarray:
    x, y, z, w_ = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w_), 2 * (x * z + y * w_)],
                     [2 * (x * y + z * w_), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w_)],
                     [2 * (x * z - y * w_), 2 * (y * z + x * w_), 1 - 2 * (x * x + y * y)]])


def _local(n: dict[str, Any]) -> np.ndarray:
    if "matrix" in n:
        return np.array(n["matrix"], dtype=float).reshape(4, 4).T
    m = np.eye(4)
    m[:3, :3] = _quat(n.get("rotation", [0, 0, 0, 1])) @ np.diag(n.get("scale", [1, 1, 1]))
    m[:3, 3] = n.get("translation", [0, 0, 0])
    return m


def hull_triangles(gltf: dict[str, Any], bin_data: bytes, *, lod0_only: bool = True) -> np.ndarray:
    """Render triangles under the ``Hull`` group in the scene frame, (N, 3, 3).
    With ``lod0_only`` meshes named as lower LODs or damage pieces are
    left out."""
    nodes = gltf.get("nodes") or []
    out: list[np.ndarray] = []

    def walk(i: int, parent: np.ndarray) -> None:
        n = nodes[i]
        world = parent @ _local(n)
        if "mesh" in n:
            mesh = gltf["meshes"][n["mesh"]]
            label = f"{n.get('name') or ''} {mesh.get('name') or ''}"
            if not (lod0_only and _NOT_LOD0.search(label)):
                for p in mesh.get("primitives") or []:
                    if p.get("mode", 4) != 4:
                        continue
                    pos = _read_accessor(gltf, bin_data, p["attributes"]["POSITION"]).astype(float)
                    pos = pos @ world[:3, :3].T + world[:3, 3]
                    if "indices" in p:
                        idx = _read_accessor(gltf, bin_data, p["indices"]).astype(np.int64).reshape(-1, 3)
                    else:
                        idx = np.arange(len(pos) - len(pos) % 3).reshape(-1, 3)
                    out.append(pos[idx])
        for c in n.get("children") or []:
            walk(c, world)

    for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        if nodes[r].get("name") == "Hull":
            walk(r, _IDENT)
    if not out:
        raise PropHullError("the Hull group has no render triangles")
    return np.concatenate(out)


def geometry_from_triangles(tris: np.ndarray) -> dict[str, Any]:
    """The ship ``geometry`` section from the ``Hull`` group's triangles
    (every LOD): the ship rule (length = the longer horizontal extent,
    beam = the shorter, waterline at y = 0, keel = the lowest point), but
    measured on the vertices the triangles use. Accessor ``min`` / ``max``
    are not used: a primitive that shares a vertex stream with others
    (allowed by glTF) reports the whole stream's box."""
    flat = np.asarray(tris, dtype=float).reshape(-1, 3)
    lo, hi = flat.min(0), flat.max(0)
    ex, ez = float(hi[0] - lo[0]), float(hi[2] - lo[2])
    keel = float(min(lo[1], 0.0))
    return resolve_sidecar.make_geometry(
        length_m=round(max(ex, ez), 4), beam_m=round(min(ex, ez), 4), height_m=round(float(hi[1] - lo[1]), 4),
        draft_m=round(-keel, 4), waterline_y=0.0, keel_y=round(keel, 4),
    )


def hull_material_names(gltf: dict[str, Any]) -> set[str]:
    """Names of the materials the ``Hull`` group's meshes use."""
    nodes = gltf.get("nodes") or []
    out: set[str] = set()

    def walk(i: int) -> None:
        n = nodes[i]
        if "mesh" in n:
            for pr in gltf["meshes"][n["mesh"]].get("primitives") or []:
                if "material" in pr:
                    out.add(gltf["materials"][pr["material"]].get("name") or "")
        for c in n.get("children") or []:
            walk(c)

    for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        if nodes[r].get("name") == "Hull":
            walk(r)
    return out


def buoyancy_from_triangles(tris: np.ndarray, *, cell_m: float | None = None) -> dict[str, Any]:
    """Waterline dimensions and displacement of a hull floating with its
    waterline at y = 0, by column integration.

    A grid of square cells covers the part of the hull below y = 0 seen
    from above. In each cell the lowest triangle surface gives the
    submerged depth d = -y (the hull counts as solid from its bottom up to
    the waterline, so an open barge displaces its whole outline). Then:
    displaced volume = sum(d) x cell area; waterplane = the cells with
    d > 0; length / beam = the waterplane's extent along z / x; the centre
    of buoyancy = the volume-weighted centre of the columns (y at d / 2
    below the waterline)."""
    tris = np.asarray(tris, dtype=float)
    hi_all = tris.reshape(-1, 3).max(0)
    under = tris[tris[:, :, 1].min(1) < 0.0]
    if len(under) == 0:
        raise PropHullError("no part of the hull lies below the waterline (y = 0)")
    lo, hi = under.reshape(-1, 3).min(0), under.reshape(-1, 3).max(0)
    span = max(hi[0] - lo[0], hi[2] - lo[2])
    # Rounded as stored, so a re-measure with the stored cell size repeats it exactly.
    h = round(float(cell_m) if cell_m else float(min(max(span / 400.0, 0.02), 0.5)), 4)
    nx = max(1, int(math.ceil((hi[0] - lo[0]) / h)))
    nz = max(1, int(math.ceil((hi[2] - lo[2]) / h)))
    cx = lo[0] + (np.arange(nx) + 0.5) * h
    cz = lo[2] + (np.arange(nz) + 0.5) * h
    bottom = np.full((nx, nz), np.inf)
    for t in under:
        x0, x1 = t[:, 0].min(), t[:, 0].max()
        z0, z1 = t[:, 2].min(), t[:, 2].max()
        i0, i1 = np.searchsorted(cx, x0, "left"), np.searchsorted(cx, x1, "right")
        k0, k1 = np.searchsorted(cz, z0, "left"), np.searchsorted(cz, z1, "right")
        if i0 >= i1 or k0 >= k1:
            continue
        px, pz = np.meshgrid(cx[i0:i1], cz[k0:k1], indexing="ij")
        a, b, c = t
        v0x, v0z = c[0] - a[0], c[2] - a[2]
        v1x, v1z = b[0] - a[0], b[2] - a[2]
        den = v0x * v1z - v1x * v0z
        if abs(den) < 1e-12:
            continue
        wx, wz = px - a[0], pz - a[2]
        u = (wx * v1z - v1x * wz) / den
        v = (v0x * wz - wx * v0z) / den
        inside = (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
        if not inside.any():
            continue
        y = a[1] + u * (c[1] - a[1]) + v * (b[1] - a[1])
        sub = bottom[i0:i1, k0:k1]
        np.minimum(sub, np.where(inside, y, np.inf), out=sub)
    depth = np.where(np.isfinite(bottom), np.maximum(-bottom, 0.0), 0.0)
    wet = depth > 0
    if not wet.any():
        raise PropHullError("no submerged column found under the hull")
    area = h * h
    vol = float(depth.sum() * area)
    ii, kk = np.nonzero(wet)
    xs, zs = cx[ii], cz[kk]
    length = float(zs.max() - zs.min() + h)
    beam = float(xs.max() - xs.min() + h)
    draft_max = float(depth.max())
    wp_area = float(wet.sum() * area)
    d = depth[wet]
    cob = [float((xs * d).sum() * area / vol), float(-(d * d / 2.0).sum() * area / vol),
           float((zs * d).sum() * area / vol)]
    return {
        "method": "column",
        "cell_m": h,
        "waterline_y": 0.0,
        "length_wl_m": round(length, 3),
        "beam_wl_m": round(beam, 3),
        "draft_max_m": round(draft_max, 3),
        "draft_mean_m": round(vol / wp_area, 3),
        "waterplane_area_m2": round(wp_area, 3),
        "displaced_volume_m3": round(vol, 3),
        "displacement_t": round(vol * SEAWATER_T_PER_M3, 3),
        "block_coefficient": round(vol / (length * beam * draft_max), 4) if length * beam * draft_max > 0 else None,
        "center_of_buoyancy": [round(v, 3) for v in cob],
        "freeboard_max_m": round(float(hi_all[1]), 3),
        "waterline_center": [round(float((xs.min() + xs.max()) / 2.0), 3), 0.0,
                             round(float((zs.min() + zs.max()) / 2.0), 3)],
        "bow": "-z",
    }


def _add_hitbox_group(gltf: dict[str, Any], bin_data: bytes, lo: np.ndarray, hi: np.ndarray) -> bytes:
    """Append ``Hitboxes`` / ``CM_SB_hull_1``: the render bounds as a box
    with outward, counter-clockwise faces, in the toolkit's hit-box shape."""
    buf = bytearray(bin_data)

    def add(arr: np.ndarray, comp: int, typ: str, target: int, minmax: bool = False) -> int:
        while len(buf) % 4:
            buf.append(0)
        data = np.ascontiguousarray(arr).tobytes()
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": len(buf), "byteLength": len(data), "target": target})
        buf.extend(data)
        a: dict[str, Any] = {"bufferView": len(gltf["bufferViews"]) - 1, "byteOffset": 0, "count": int(arr.shape[0]),
                             "componentType": comp, "type": typ}
        if minmax:
            a["min"] = [float(v) for v in arr.min(0)]
            a["max"] = [float(v) for v in arr.max(0)]
        gltf.setdefault("accessors", []).append(a)
        return len(gltf["accessors"]) - 1

    pos, nrm, idx = _box_mesh(lo, hi)
    gltf.setdefault("materials", []).append({
        "alphaMode": "BLEND", "doubleSided": True, "name": f"hitbox_{HITBOX_NAME}",
        "pbrMetallicRoughness": {"baseColorFactor": list(_HITBOX_COLOR), "metallicFactor": 0.0, "roughnessFactor": 1.0},
        "emissiveFactor": [0.0, 0.0, 0.0],
    })
    prim = {"attributes": {"POSITION": add(pos, COMP_FLOAT, "VEC3", 34962, True),
                           "NORMAL": add(nrm, COMP_FLOAT, "VEC3", 34962)},
            "indices": add(idx.reshape(-1), COMP_UNSIGNED_INT, "SCALAR", 34963),
            "material": len(gltf["materials"]) - 1}
    gltf.setdefault("meshes", []).append({"name": HITBOX_NAME, "primitives": [prim]})
    gltf["nodes"].append({"name": HITBOX_NAME, "mesh": len(gltf["meshes"]) - 1})
    gltf["nodes"].append({"name": "Hitboxes", "children": [len(gltf["nodes"]) - 1]})
    gltf["scenes"][0]["nodes"].append(len(gltf["nodes"]) - 1)
    while len(buf) % 4:
        buf.append(0)
    gltf["buffers"][0]["byteLength"] = len(buf)
    return bytes(buf)


# ── sidecar ───────────────────────────────────────────────────────────


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _glb_materials(glb: Path) -> list[dict[str, Any]]:
    """Materials of a GLB whose textures stay inside it: ship
    ``materials[]`` shape with empty ``texture_sets``."""
    gltf, _ = parse_glb(glb.read_bytes())
    out = []
    for m in gltf.get("materials") or []:
        name = m.get("name") or ""
        if name.startswith("hitbox_"):
            continue
        pbr = m.get("pbrMetallicRoughness") or {}
        alpha = m.get("alphaMode", "OPAQUE")
        mat = resolve_sidecar.make_material(
            material_id=name,
            shader_intent="transparent" if alpha == "BLEND" else "cutout" if alpha == "MASK" else "opaque_pbr",
            render_queue="transparent" if alpha == "BLEND" else "alpha_test" if alpha == "MASK" else "opaque",
            double_sided=bool(m.get("doubleSided", False)),
        )
        mat["texture_sets"] = {}
        mat["factors"] = {
            "baseColor": list(pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])),
            "metallic": float(pbr.get("metallicFactor", 1.0)),
            "roughness": float(pbr.get("roughnessFactor", 1.0)),
            "emissive": list(m.get("emissiveFactor", [0.0, 0.0, 0.0])),
        }
        mat["glb_textures"] = sorted(
            k for k, v in {**m, **pbr}.items() if k.endswith("Texture") and isinstance(v, dict)
        )
        out.append(mat)
    return out


def _placements_doc(pid: str, name: str) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "ship": {"model_dir": pid, "display_name": name},
        "pipeline": {"generator": GENERATOR, "generated_at": _now()},
    }
    for section in resolve_sidecar.PLACEMENT_SECTIONS:
        doc[section] = []
    return doc


def _compose_sidecar(
    e: dict[str, Any],
    source: dict[str, Any],
    glb: Path,
    models: Path,
    restructure: dict[str, Any],
    cfg: PipelineConfig,
    warn: Callable[[str], None],
) -> dict[str, Any]:
    pid = e["id"]
    gltf, bin_data = parse_glb(glb.read_bytes())
    geometry = geometry_from_triangles(hull_triangles(gltf, bin_data, lod0_only=False))
    bounds = geometry["bounds"]
    if not bounds.get("length_m"):
        raise PropHullError(f"{pid}: the Hull group of {glb.name} measured empty")
    buoy = buoyancy_from_triangles(hull_triangles(gltf, bin_data))
    hitbox = resolve_sidecar.make_hitbox(
        source_glb=glb.name, region_count=1, regions={HITBOX_REGION: {"box_count": 1}},
        boxes={HITBOX_NAME: {"section": HITBOX_SECTION, "hl_type": HITBOX_HL_TYPE}},
    )
    armor = resolve_sidecar.make_armor(source_glb=glb.name)
    if source["kind"] == "model":
        materials = _model_materials(glb, models / f"{pid}_material_mappings.json", warn, pid)
        textures = "texture_sets"
    else:
        materials = _glb_materials(glb)
        textures = "glb"
    used = hull_material_names(gltf)
    materials = [m for m in materials if m.get("material_id") in used]
    caps = toolkit_capabilities(cfg.toolkit_bin) if (source["kind"] == "model" and cfg.toolkit_bin) else {}
    tool = caps.get("tool") or {}
    pipeline = resolve_sidecar.make_pipeline(
        stages_completed=[0, 6] if source["kind"] == "model" else [6],
        toolkit_version=str(tool.get("package_version") or ""),
        tool_commits={"wows_toolkit": tool["git_commit"]} if tool.get("git_commit") else {},
    )
    pipeline["kind"] = PIPELINE_KIND
    pipeline["generator"] = GENERATOR
    nation = e.get("nation", "common")
    cls = e.get("class", "AUX")
    name = e.get("name") or pid
    stem = Path(source["model"]).stem if source["kind"] == "model" else Path(source["glb"]).stem
    ship = resolve_sidecar.make_ship(
        ship_key=resolve_sidecar.build_ship_key(nation, cls, pid),
        display_name=name,
        wg_asset_id=stem.lower() if source["kind"] == "model" else None,
        nation=nation,
        cls=cls,
    )
    doc = resolve_sidecar.new_document(
        pipeline=pipeline, ship=ship, geometry=geometry, armor=armor, hitbox=hitbox, materials=materials,
    )
    src_block: dict[str, Any] = {"kind": source["kind"]}
    if source["kind"] == "model":
        src_block.update({"model": source["model"], "prop": source.get("prop")})
    else:
        src_block.update({"glb": source["glb"], "glb_sha1": source.get("glb_sha1")})
    doc["prop_hull"] = {
        "schema": BLOCK_SCHEMA,
        "source": src_block,
        "textures": textures,
        "scale": float(e.get("scale", 1.0)),
        "frame": {"units": "m", "up": "+y", "bow": "-z", "waterline_y": 0.0},
        "dropped": {"nodes": restructure.get("nodes", []), "primitives": restructure.get("primitives", []),
                    "animations": restructure.get("animations", 0)},
        "repairs": {"position_bounds": restructure.get("position_bounds_fixed", 0)},
        "options": {k: e[k] for k in ("drop_nodes", "drop_materials", "rename") if e.get(k) is not None},
        "hitbox": {"box": HITBOX_NAME, "rule": "hull render bounds"},
        "buoyancy": buoy,
        "notes": list(e.get("notes") or []),
    }
    doc["provenance"] = {"generator": GENERATOR, "spec_entry": copy.deepcopy(e), "extracted_at": _now()}
    shared_textures.repair_refs(doc, models)
    return doc


# ── build ─────────────────────────────────────────────────────────────


def _write_hull_glb(
    e: dict[str, Any], source: dict[str, Any], models: Path, cfg: PipelineConfig, on_event: Callable[[str], None],
) -> dict[str, Any]:
    pid = e["id"]
    out = models / f"{pid}_hull.glb"
    if source["kind"] == "model":
        raw = models / f"{pid}_source.glb"
        on_event(f"  {pid}: exporting {source['geometry']}")
        batch_export_model([{
            "geometry": source["geometry"],
            "output": raw,
            "raw_dds_dir": models / "textures_dds",
            "material_mappings_json": models / f"{pid}_material_mappings.json",
        }], shared={"all_render_sets": True, "no_textures": True}, config=cfg)
        if not raw.is_file():
            raise PropHullError(f"{pid}: the toolkit wrote no GLB for {source['geometry']}")
        gltf, buffers = load_gltf(raw)
    else:
        src = Path(source["glb"])
        source["glb_sha1"] = _sha1(src)
        gltf, buffers = load_gltf(src)
    drop_materials = e.get("drop_materials", DEFAULT_DROP_MATERIALS)
    doc, bin_data, report = _restructure(
        gltf, buffers, drop_nodes=e.get("drop_nodes"), drop_materials=drop_materials,
        rename=[list(p) for p in e.get("rename") or []], scale=float(e.get("scale", 1.0)),
    )
    flat = hull_triangles(doc, bin_data, lod0_only=False).reshape(-1, 3)
    bin_data = _add_hitbox_group(doc, bin_data, flat.min(0), flat.max(0))
    doc.setdefault("asset", {})["generator"] = GENERATOR
    doc["extras"] = {"wows_prop_hull": {"id": pid, "source": {k: v for k, v in source.items() if k != "geometry"}}}
    write_glb(doc, bin_data, out)
    if source["kind"] == "model":
        (models / f"{pid}_source.glb").unlink(missing_ok=True)
    return report


def build_prop_hull(
    e: dict[str, Any],
    *,
    config: PipelineConfig | None = None,
    skip_export: bool = False,
    on_event: Callable[[str], None] = print,
    _vfs: Callable[[], set[str]] | None = None,
) -> dict[str, Any]:
    """Build one prop hull from a spec entry. Returns its report."""
    cfg = config or PipelineConfig.load()
    _check_entry(e, "entry")
    pid = e["id"]
    folder = ship_dir(cfg, pid)
    existing = sidecar_path(cfg, pid)
    if existing.is_file() and not is_prop_hull_dir(folder):
        raise PropHullError(f"{folder} holds a ship export, not a prop hull; pick another id")
    vfs_cache: dict[str, set[str]] = {}

    def vfs() -> set[str]:
        if "paths" not in vfs_cache:
            vfs_cache["paths"] = (_vfs or (lambda: _manifest_paths(cfg)))()
        return vfs_cache["paths"]

    source = _resolve_source(e, cfg, vfs)
    models = folder / resolve_sidecar.MODELS_SUBDIR
    warnings: list[str] = []
    glb = models / f"{pid}_hull.glb"
    with shared_textures.export_session(cfg.workspace, [folder]):
        with build_deps.recording() as deps:
            if skip_export:
                if not glb.is_file():
                    raise PropHullError(f"{pid}: --skip-export needs {glb}")
                prior = resolve_sidecar.read(existing) if existing.is_file() else {}
                restructure = dict(((prior.get("prop_hull") or {}).get("dropped")) or {})
                restructure["position_bounds_fixed"] = (
                    ((prior.get("prop_hull") or {}).get("repairs") or {}).get("position_bounds", 0))
                if source["kind"] == "glb":
                    source["glb_sha1"] = ((prior.get("prop_hull") or {}).get("source") or {}).get("glb_sha1")
            else:
                if source["kind"] == "model" and models.is_dir():
                    # A fresh export: textures of an earlier source must not linger.
                    shutil.rmtree(models / "textures_dds", ignore_errors=True)
                models.mkdir(parents=True, exist_ok=True)
                restructure = _write_hull_glb(e, source, models, cfg, on_event)
            doc = _compose_sidecar(e, source, glb, models, restructure, cfg, warnings.append)
            resolve_sidecar.write(doc, existing)
            _write_json_atomic(models / f"{pid}_accessories.json", _placements_doc(pid, doc["ship"]["display_name"]),
                               indent=2)
    write_export_deps_safely(
        folder, kind=PIPELINE_KIND, name=pid, collector=deps,
        reexport={"kind": PIPELINE_KIND, "entry": copy.deepcopy(e)}, config=cfg,
    )
    problems = validate_prop_hull(folder, config=cfg)
    buoy = doc["prop_hull"]["buoyancy"]
    on_event(
        f"  {pid}: {doc['geometry']['bounds']['length_m']:.1f} x {doc['geometry']['bounds']['beam_m']:.1f} m, "
        f"draft {buoy['draft_max_m']:.2f} m, {buoy['displacement_t']:.1f} t, {len(doc['materials'])} material(s)"
        + (f", {len(problems)} problem(s)" if problems else ", valid")
    )
    return {"id": pid, "sidecar": str(existing), "source": source, "dropped": restructure,
            "warnings": warnings, "problems": problems}


def run_spec(
    spec_path: Path,
    *,
    ids: Iterable[str] | None = None,
    skip_export: bool = False,
    config: PipelineConfig | None = None,
    on_event: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Build the spec's prop hulls (or the ``ids`` among them)."""
    cfg = config or PipelineConfig.load()
    spec = load_spec(spec_path)
    props = spec.get("props") or []
    want = set(ids or [])
    if want:
        missing = want - {p["id"] for p in props}
        if missing:
            raise PropHullError(f"ids not in {Path(spec_path).name}: {sorted(missing)}")
        props = [p for p in props if p["id"] in want]
    if not props:
        raise PropHullError(f"{spec_path}: no props selected")
    paths: dict[str, set[str]] = {}

    def vfs() -> set[str]:
        if "p" not in paths:
            paths["p"] = _manifest_paths(cfg)
        return paths["p"]

    report: dict[str, Any] = {"spec": str(spec_path), "props": {}}
    for e in props:
        report["props"][e["id"]] = build_prop_hull(e, config=cfg, skip_export=skip_export, on_event=on_event,
                                                   _vfs=vfs)
    return report


# ── validation ────────────────────────────────────────────────────────


def _texture_paths(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "dds_mips" and isinstance(v, list):
                yield from (s for s in v if isinstance(s, str))
            else:
                yield from _texture_paths(v)
    elif isinstance(node, list):
        for v in node:
            yield from _texture_paths(v)


def validate_prop_hull(folder: Path, *, config: PipelineConfig | None = None) -> list[str]:
    """Problems with one prop hull folder (empty list: valid).

    Checks the layout, the sidecar's sections against the spec (section 17
    of METADATA_SPEC), that the hull GLB has a ``Hull`` group with render
    triangles, a ``Hitboxes`` group holding exactly the boxes the sidecar
    names and no ``Armor`` group, that ``geometry`` and
    ``prop_hull.buoyancy`` equal a fresh measurement of the GLB, that every
    material a ``Hull`` mesh uses is listed, and that every texture path
    resolves (the shared texture store rule)."""
    cfg = config or PipelineConfig.load()
    folder = Path(folder)
    pid = folder.name
    p: list[str] = []
    meta = folder / f"{pid}{resolve_sidecar.SIDECAR_SUFFIX}"
    models = folder / resolve_sidecar.MODELS_SUBDIR
    glb = models / f"{pid}_hull.glb"
    acc = models / f"{pid}_accessories.json"
    try:
        doc = resolve_sidecar.read(meta)
    except Exception as err:  # noqa: BLE001
        return [f"{pid}: sidecar unreadable: {err}"]
    if not is_prop_hull(doc):
        p.append(f"{pid}: pipeline.kind is not {PIPELINE_KIND!r}")
    for key in ("pipeline", "ship", "geometry", "armor", "hitbox", "ballistics", "materials", "skins", "prop_hull",
                *resolve_sidecar.PLACEMENT_SECTIONS):
        if key not in doc:
            p.append(f"{pid}: section {key} missing")
    for key in resolve_sidecar.PLACEMENT_SECTIONS:
        if doc.get(key):
            p.append(f"{pid}: {key} is not empty (a prop hull has no mounts)")
    ship = doc.get("ship") or {}
    if ship.get("wg_ship_id") or ship.get("wg_ship_full_id"):
        p.append(f"{pid}: ship names a GameParams entry; a prop hull has none")
    if ship.get("class") not in resolve_sidecar.VALID_SHIP_CLASSES:
        p.append(f"{pid}: ship.class {ship.get('class')!r} invalid")
    if not [s for s in doc.get("skins") or [] if s.get("skin_id") == "default"]:
        p.append(f"{pid}: skins has no default entry")
    block = doc.get("prop_hull") or {}
    if block.get("schema") != BLOCK_SCHEMA:
        p.append(f"{pid}: prop_hull.schema is not {BLOCK_SCHEMA}")
    if block.get("textures") not in ("texture_sets", "glb"):
        p.append(f"{pid}: prop_hull.textures must be texture_sets or glb")
    if not glb.is_file():
        return p + [f"{pid}: {glb.name} missing"]
    if not acc.is_file():
        p.append(f"{pid}: {acc.name} missing")
    else:
        try:
            adoc = json.loads(acc.read_text(encoding="utf-8"))
            for key in resolve_sidecar.PLACEMENT_SECTIONS:
                if adoc.get(key) != []:
                    p.append(f"{pid}: {acc.name} {key} is not an empty list")
        except (OSError, ValueError) as err:
            p.append(f"{pid}: {acc.name} unreadable: {err}")
    gltf, bin_data = parse_glb(glb.read_bytes())
    nodes = gltf.get("nodes") or []
    roots = {nodes[r].get("name"): r for r in gltf["scenes"][gltf.get("scene", 0)]["nodes"]}
    if "Hull" not in roots:
        p.append(f"{pid}: GLB has no top-level Hull group")
    if "Armor" in roots:
        p.append(f"{pid}: GLB has an Armor group; a prop hull is armour-less")
    boxes = sorted(nodes[c].get("name") for c in (nodes[roots["Hitboxes"]].get("children") or [])) \
        if "Hitboxes" in roots else []
    hb = doc.get("hitbox") or {}
    if boxes != sorted((hb.get("boxes") or {}).keys()):
        p.append(f"{pid}: Hitboxes group {boxes} differs from hitbox.boxes {sorted(hb.get('boxes') or {})}")
    if hb.get("region_count") != len(boxes):
        p.append(f"{pid}: hitbox.region_count {hb.get('region_count')} != {len(boxes)} boxes")
    armor = doc.get("armor") or {}
    if armor.get("plate_count") or armor.get("materials_table") or armor.get("zones"):
        p.append(f"{pid}: armor is not empty")
    if "Hull" not in roots:
        return p
    try:
        tris = hull_triangles(gltf, bin_data)
    except PropHullError as err:
        return p + [f"{pid}: {err}"]
    geo = geometry_from_triangles(hull_triangles(gltf, bin_data, lod0_only=False))
    for k in ("length_m", "beam_m", "height_m"):
        a, b = (doc.get("geometry") or {}).get("bounds", {}).get(k), geo["bounds"][k]
        if not isinstance(a, (int, float)) or abs(a - b) > 0.01:
            p.append(f"{pid}: geometry.bounds.{k} {a} != measured {b:.3f}")
    hull = (doc.get("geometry") or {}).get("hull") or {}
    if abs(float(hull.get("draft_m", 0.0)) - geo["hull"]["draft_m"]) > 0.01:
        p.append(f"{pid}: geometry.hull.draft_m {hull.get('draft_m')} != measured {geo['hull']['draft_m']:.3f}")
    stored = block.get("buoyancy") or {}
    try:
        fresh = buoyancy_from_triangles(tris, cell_m=stored.get("cell_m"))
    except PropHullError as err:
        p.append(f"{pid}: buoyancy: {err}")
        fresh = {}
    for k in ("length_wl_m", "beam_wl_m", "draft_max_m", "displaced_volume_m3", "displacement_t"):
        a, b = stored.get(k), fresh.get(k)
        if not isinstance(a, (int, float)) or b is None or abs(a - b) > max(0.01, 0.005 * abs(b)):
            p.append(f"{pid}: prop_hull.buoyancy.{k} {a} != measured {b}")
    if fresh and not (fresh["displacement_t"] > 0 and fresh["draft_max_m"] > 0):
        p.append(f"{pid}: the hull does not float (no displacement below y = 0)")
    hull_mats = hull_material_names(gltf)
    listed = {m.get("material_id") for m in doc.get("materials") or []}
    missing = sorted(m for m in hull_mats if m not in listed)
    if missing:
        p.append(f"{pid}: Hull materials not in materials[]: {missing}")
    if block.get("textures") == "texture_sets":
        for m in doc.get("materials") or []:
            if not ((m.get("texture_sets") or {}).get("main") or {}).get("baseColor"):
                p.append(f"{pid}: material {m.get('material_id')} binds no baseColor")
    ws = cfg.workspace
    for ref in _texture_paths(doc.get("materials") or []):
        target = (ws / ref) if ref.startswith("libraries/") else (models / ref)
        if not target.is_file():
            p.append(f"{pid}: texture {ref} does not resolve")
    return p


def validate_workspace(*, config: PipelineConfig | None = None, ids: Iterable[str] | None = None) -> dict[str, list[str]]:
    """``{id: problems}`` for every prop hull in the workspace (or ``ids``)."""
    cfg = config or PipelineConfig.load()
    names = list(ids) if ids else workspace_prop_hulls(cfg)
    return {n: validate_prop_hull(ship_dir(cfg, n), config=cfg) for n in names}


__all__ = [
    "BLOCK_SCHEMA",
    "PIPELINE_KIND",
    "SPEC_SCHEMA",
    "PropHullError",
    "build_prop_hull",
    "buoyancy_from_triangles",
    "is_prop_hull",
    "is_prop_hull_dir",
    "load_gltf",
    "load_spec",
    "run_spec",
    "validate_prop_hull",
    "validate_workspace",
    "workspace_prop_hulls",
]
