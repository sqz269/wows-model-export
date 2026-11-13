"""Map-model library: WoWS map models (buildings, props, rocks, coast
pieces) exported once each, by model, into ``<workspace>/libraries/map_models/``.

A client map export holds every model it places inside the map's own GLB, in
map units and base colour only. A consumer that places a model somewhere
else (another map's dressing, a composed scene) needs the model alone, with
its LODs and full material, once for every map that uses it. This builder
runs the toolkit's ``batch-export-model`` over a list of models (by file stem
or model path) and indexes what it wrote:

* ``<stem>/<stem>.glb``: every authored LOD as a ``LOD<i>`` node
  (``lod_chain``), metres (a map GLB's meshes x 15), the native frame with z
  negated, +Y up; embedded PNG textures with glTF-conformant
  metallic-roughness (the toolkit repacks WG's ``_mg``).
* ``<stem>/<stem>.visual.json``: the model's ``.visual`` structure
  (``wowsunpack.visual/v1``), whose ``lods[].extent`` is the authored switch
  distance in map units.
* ``<stem>/textures_dds/``: the raw DDS sets with their glTF-conformant
  siblings, routed through the shared texture store.
* ``index.json`` (``wows_map_model_library/v1``): per model its source path,
  bounds, triangles per LOD and the LOD switch distances in metres
  (``extent x 15``).

Spec: ``reference/schemas/MAP_MODEL_LIBRARY.md``. Writes only under
``libraries/map_models/`` (and the store); models the index does not list
(GLBs placed in the folder by other means) are left alone.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import shared_textures
from ..config import PipelineConfig
from ..toolkit import build as _build
from ..toolkit import vfs
from ..toolkit.ship import batch_export_model
from ._atomic import atomic_write_text

SCHEMA = "wows_map_model_library/v1"
LIB_REL = "libraries/map_models"
INDEX_NAME = "index.json"
M_PER_MAP_UNIT = 15.0
EXPORT_OPTIONS = {"lod_chain": True}


class MapModelLibraryError(RuntimeError):
    pass


@dataclass
class MapModelLibraryResult:
    library_root: Path
    index_path: Path
    models: dict[str, str]
    written: list[str]
    skipped: list[str]
    failed: list[str]
    warnings: list[str]


def _manifest_geometries(cfg: PipelineConfig) -> dict[str, list[str]]:
    """``{file stem (lower case): [model path without extension, ...]}`` for
    every loose ``.geometry`` of the build's VFS."""
    path = vfs.ensure_manifest(config=cfg)
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for e in entries:
        p = e.get("path", "") if isinstance(e, dict) else str(e)
        if not p.endswith(".geometry") or (isinstance(e, dict) and e.get("is_directory")):
            continue
        model = p.lstrip("/")[: -len(".geometry")]
        out.setdefault(model.rsplit("/", 1)[-1].lower(), []).append(model)
    return out


def resolve_models(tokens: Iterable[str], cfg: PipelineConfig) -> dict[str, str]:
    """``{stem: model path}`` for model stems (``LBT013``) or model paths
    (``content/location/building/tribal/LBT013/LBT013[.geometry|.visual]``)."""
    out: dict[str, str] = {}
    index: dict[str, list[str]] | None = None
    for t in tokens:
        t = t.strip().replace("\\", "/")
        if not t:
            continue
        if "/" in t:
            model = t.lstrip("/")
            for ext in (".geometry", ".visual", ".model"):
                if model.endswith(ext):
                    model = model[: -len(ext)]
            stem = model.rsplit("/", 1)[-1]
        else:
            if index is None:
                index = _manifest_geometries(cfg)
            hits = index.get(t.lower(), [])
            if not hits:
                raise MapModelLibraryError(f"{t}: no loose .geometry of that name in the game files")
            if len(hits) > 1:
                raise MapModelLibraryError(f"{t}: several models have that name: {', '.join(sorted(hits))}; "
                                           "name it by model path")
            model = hits[0]
            stem = model.rsplit("/", 1)[-1]
        if stem in out and out[stem] != model:
            raise MapModelLibraryError(f"{stem}: named twice by different paths ({out[stem]}, {model})")
        out[stem] = model
    return out


def _glb_json(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        head = f.read(20)
        magic, _ver, _total, ln, kind = struct.unpack("<IIIII", head)
        if magic != 0x46546C67 or kind != 0x4E4F534A:
            raise MapModelLibraryError(f"{path}: not a GLB")
        return json.loads(f.read(ln))


def _lods_from_glb(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{"node", "triangles", "min", "max"}]`` per ``LOD<i>`` node (or the
    whole scene as one LOD when the GLB has no LOD nodes)."""
    acc = doc.get("accessors", [])
    meshes = doc.get("meshes", [])

    def mesh_stats(mi: int) -> tuple[int, list[float], list[float]]:
        tri, mn, mx = 0, [1e30] * 3, [-1e30] * 3
        for p in meshes[mi].get("primitives", []):
            a = acc[p["attributes"]["POSITION"]]
            mn = [min(x, y) for x, y in zip(mn, a.get("min", mn), strict=False)]
            mx = [max(x, y) for x, y in zip(mx, a.get("max", mx), strict=False)]
            n = acc[p["indices"]]["count"] if "indices" in p else a["count"]
            tri += n // 3
        return tri, mn, mx

    nodes = doc.get("nodes", [])
    lod_nodes = sorted(((int(n["name"][3:]), i) for i, n in enumerate(nodes)
                        if isinstance(n.get("name"), str) and n["name"].startswith("LOD") and n["name"][3:].isdigit()))
    out: list[dict[str, Any]] = []
    if lod_nodes:
        for lvl, ni in lod_nodes:
            stack, tri, mn, mx = [ni], 0, [1e30] * 3, [-1e30] * 3
            while stack:
                n = nodes[stack.pop()]
                if "mesh" in n:
                    t, a, b = mesh_stats(n["mesh"])
                    tri += t
                    mn = [min(x, y) for x, y in zip(mn, a, strict=False)]
                    mx = [max(x, y) for x, y in zip(mx, b, strict=False)]
                stack.extend(n.get("children", []))
            out.append({"node": f"LOD{lvl}", "triangles": tri, "min": mn, "max": mx})
    else:
        tri, mn, mx = 0, [1e30] * 3, [-1e30] * 3
        for mi in range(len(meshes)):
            t, a, b = mesh_stats(mi)
            tri += t
            mn = [min(x, y) for x, y in zip(mn, a, strict=False)]
            mx = [max(x, y) for x, y in zip(mx, b, strict=False)]
        out.append({"node": None, "triangles": tri, "min": mn, "max": mx})
    return out


def _entry(lib: Path, stem: str, model: str, build: int | None) -> dict[str, Any]:
    d = lib / stem
    glb = d / f"{stem}.glb"
    doc = _glb_json(glb)
    lods = _lods_from_glb(doc)
    vis_path = d / f"{stem}.visual.json"
    vis_lods: list[dict[str, Any]] = []
    bbox = None
    if vis_path.is_file():
        vis = json.loads(vis_path.read_text(encoding="utf-8"))
        vis_lods = vis.get("lods") or []
        bbox = vis.get("bounding_box")
    entries: list[dict[str, Any]] = []
    prev = 0.0
    for i, lv in enumerate(lods):
        ext = vis_lods[i].get("extent") if i < len(vis_lods) else None
        e: dict[str, Any] = {"node": lv["node"], "triangles": lv["triangles"],
                             "min_distance_m": round(prev, 3),
                             "max_distance_m": round(float(ext) * M_PER_MAP_UNIT, 3) if ext is not None else None}
        if i < len(vis_lods):
            e["render_sets"] = vis_lods[i].get("render_sets", [])
            e["casts_shadow"] = bool(vis_lods[i].get("casts_shadow", False))
        if e["max_distance_m"] is not None:
            prev = e["max_distance_m"]
        entries.append(e)
    lod0 = lods[0]
    textures: dict[str, Any] = {}
    for name, ref in shared_textures.dds_dir_entries(d / "textures_dds"):
        s = shared_textures.set_stem(name)
        if s:
            textures.setdefault(s, {"dds_mips": []})["dds_mips"].append(ref)
    return {
        "model_path": model,
        "glb": f"{stem}/{stem}.glb",
        "visual_json": f"{stem}/{stem}.visual.json" if vis_path.is_file() else None,
        "textures_dds": f"{stem}/textures_dds",
        "textures": textures,
        "materials": [m.get("name") for m in doc.get("materials", [])],
        "bounds_m": {"min": [round(v, 4) for v in lod0["min"]], "max": [round(v, 4) for v in lod0["max"]]},
        "size_m": [round(b - a, 3) for a, b in zip(lod0["min"], lod0["max"], strict=False)],
        "visual_bounding_box_m": bbox,
        "triangles": [lv["triangles"] for lv in lods],
        "lods": entries,
        "build": build,
        "export_options": dict(EXPORT_OPTIONS),
        "bytes": glb.stat().st_size,
    }


def build_map_model_library(
    models: Iterable[str],
    *,
    config: PipelineConfig | None = None,
    force: bool = False,
    log: Any = print,
) -> MapModelLibraryResult:
    """Export ``models`` (stems or model paths) into ``libraries/map_models/``
    with ``batch-export-model`` and index them. A model the index already
    lists from the same build and options is kept unless ``force``."""
    cfg = config or PipelineConfig.load()
    ws = cfg.workspace
    lib = ws.joinpath(*LIB_REL.split("/"))
    index_path = lib / INDEX_NAME
    old: dict[str, Any] = {}
    if index_path.is_file():
        old = json.loads(index_path.read_text(encoding="utf-8"))
    assets: dict[str, Any] = dict(old.get("assets") or {})
    build = cfg.build if cfg.build is not None else _build.install_current_build(cfg)
    wanted = resolve_models(models, cfg)
    if not wanted:
        raise MapModelLibraryError("no model named")
    todo: dict[str, str] = {}
    skipped: list[str] = []
    for stem, model in wanted.items():
        prev = assets.get(stem)
        if (not force and isinstance(prev, dict) and prev.get("model_path") == model
                and prev.get("build") == build and prev.get("export_options") == EXPORT_OPTIONS
                and (lib / stem / f"{stem}.glb").is_file()):
            skipped.append(stem)
        else:
            todo[stem] = model
    written: list[str] = []
    failed: list[str] = []
    warnings: list[str] = []
    lib.mkdir(parents=True, exist_ok=True)
    with shared_textures.export_session(ws, [lib]):
        if todo:
            items = []
            for stem, model in sorted(todo.items()):
                d = lib / stem
                dds = d / "textures_dds"
                if dds.is_dir():
                    for f in dds.iterdir():
                        if f.is_file():
                            f.unlink()
                for f in (d / f"{stem}.glb", d / f"{stem}.visual.json"):
                    if f.is_file():
                        f.unlink()
                items.append({"geometry": f"{model}.geometry", "output": str(d / f"{stem}.glb"),
                              "raw_dds_dir": str(dds), "visual_json": str(d / f"{stem}.visual.json")})
            log(f"map models: exporting {len(items)} model(s) with batch-export-model")
            res = batch_export_model(items, shared=dict(EXPORT_OPTIONS), config=cfg)
            for line in (res.stdout or "").splitlines():
                low = line.lower()
                if ("failed" in low and "failed=0" not in low) or "error" in low:
                    warnings.append(line.strip())
        for stem, model in sorted(wanted.items()):
            if stem in skipped:
                continue
            if not (lib / stem / f"{stem}.glb").is_file():
                failed.append(stem)
                continue
            assets[stem] = _entry(lib, stem, model, build)
            written.append(stem)
            e = assets[stem]
            log(f"map models: {stem}: {len(e['lods'])} LODs, {e['triangles'][0]} tris at LOD0, "
                f"{e['bytes'] // 1024} KiB")
        doc = {
            "schema": SCHEMA,
            "generator": "wows-build-map-model-library",
            "units": "m",
            "unit_scale_to_map": round(1.0 / M_PER_MAP_UNIT, 12),
            "frame": "the model's native frame with z negated, +Y up (the frame a map GLB places it in); "
                     "metres = map units x 15",
            "lod_rule": "lods[i].max_distance_m = the .visual's lods[i].extent (map units) x 15; LOD i is drawn "
                        "from the previous level's max_distance_m to its own",
            "assets": {k: assets[k] for k in sorted(assets)},
        }
        atomic_write_text(index_path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    if failed:
        warnings.append(f"not exported: {', '.join(failed)}")
    return MapModelLibraryResult(lib, index_path, wanted, written, skipped, failed, warnings)
