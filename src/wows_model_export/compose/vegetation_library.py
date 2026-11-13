"""Shared vegetation library: every SpeedTree species of the exported client
maps, once, under ``<workspace>/libraries/vegetation/``.

A client map export (``wows-export-map``) carries its vegetation as
``vegetation_manifest.json`` + ``vegetation_meshes.bin``: one decoded mesh per
species (the ``.stsdk`` geometry) plus the coarser index ranges the toolkit
generates for it, in the map's units. It does not say which species is which.
This builder joins each exported map's species to the client's own data and
writes each species once, whatever the number of maps that plant it:

* **Name.** The species table of the space's ``forest.bin`` (its string table
  of ``content/location/speedtree/**.stsdk`` paths, in species order) names
  every species index of the map's vegetation manifest. The join is checked:
  the vertex and index counts of each named ``.stsdk`` must equal the
  manifest entry's, or the space is refused.
* **Geometry.** The map export's mesh of that species (bit for bit), every
  LOD: LOD0 is the authored mesh, LOD1.. are the toolkit's generated index
  ranges into the same vertices. Written in metres (map units x 15, the
  geometry scale every model library of the workspace uses) in the map-GLB
  frame (the native frame with z negated, +Y up).
* **Switch distances.** The client's forest LOD profile
  (``system/data/graphics_preferences.scheme.xml``, ``<forest>``: per quality
  preset ``meshEnd``, ``imposterStart``, ``imposterEnd``, ``fadeStart``). The
  forest culling shader (``shaders/forest/wg_spt_culling_cs``) compares the
  camera's distance to each tree's bounding sphere with those numbers in map
  units: the authored mesh is drawn closer than ``meshEnd``, an impostor
  (billboard) from ``imposterStart`` to ``imposterEnd``, dithered out from
  ``fadeStart``. The library has no impostor: the authored mesh stands in
  for it over the near part of the impostor range and the generated levels
  over the rest (:func:`lod_ranges`, :data:`AUTHORED_LOD_SHARE`; the one
  producer choice here).
* **Textures.** The species' albedo (``<name>_spt_a``) and SpeedTree normal
  (``<name>_spt_n``) DDS sets, copied raw into ``<id>/textures_dds/`` and
  routed through the shared texture store. The GLB embeds the albedo's top
  mip as PNG (alpha-tested, double-sided, as the map export draws trees).

Output (``reference/schemas/VEGETATION_LIBRARY.md``)::

    libraries/vegetation/
    ├── index.json                 schema wows_vegetation_library/v1
    └── <id>/
        ├── <id>.glb               LOD0..LODn nodes, metres
        └── textures_dds/          raw DDS sets (or store redirects)

Read-only on the map exports; writes only under ``libraries/vegetation/``
(and the shared texture store, through :mod:`..shared_textures`).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import shared_textures
from ..config import PipelineConfig
from ..toolkit import vfs
from ._atomic import atomic_write_text

SCHEMA = "wows_vegetation_library/v1"
LIB_REL = "libraries/vegetation"
INDEX_NAME = "index.json"
#: Map-export units (BigWorld) to the library's metres: the geometry scale
#: of every model library in the workspace (a map GLB's meshes x 15 are the
#: ``batch-export-model`` GLB of the same model).
M_PER_MAP_UNIT = 15.0
LOD_PROFILE_VFS = "/system/data/graphics_preferences.scheme.xml"
LOD_PROFILE_PATH = LOD_PROFILE_VFS.lstrip("/")
#: Quality preset whose profile sets the library's switch distances when the
#: scheme names no default.
FALLBACK_QUALITY = "HIGH"
#: How far LOD0 (the authored mesh) is drawn, as a share of the client's
#: impostor range ``[meshEnd, imposterEnd]`` on a log scale: LOD0 ends at
#: ``meshEnd * (imposterEnd / meshEnd) ** AUTHORED_LOD_SHARE``. 0 ends it
#: where the client stops drawing the mesh (``meshEnd``, 45 m on ``HIGH``);
#: 0.5, the default, at the geometric middle of the impostor range (604 m
#: on ``HIGH``). Why not 0: from ``meshEnd`` on the client draws an impostor
#: of the authored tree, which looks as full as the tree, while the
#: generated levels are a mesh simplification that drops foliage cards
#: (about 1/3 and 1/9 of the triangles) and look visibly thinner from a few
#: hundred metres. With 0.5 a map view from 300 m differs from an all-LOD0
#: view in under 0.5 % of its pixels (0 gave 5-10 %), at about twice the
#: triangles of the 0 rule in such views. ``wows-build-vegetation-library
#: --authored-share`` overrides it; a change is a re-run with ``--force``.
AUTHORED_LOD_SHARE = 0.5
TEXTURE_SLOTS = (("baseColor", "_spt_a"), ("speedtreeNormal", "_spt_n"))
FOREST_LAYERS = ("Primary", "PrimaryRare", "Underwater", "UnderwaterRare")
_GLB_MAGIC, _JSON, _BIN = 0x46546C67, 0x4E4F534A, 0x004E4942


class VegetationLibraryError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Client data readers
# ---------------------------------------------------------------------------


def read_forest(data: bytes) -> tuple[list[str], dict[str, list[int]]]:
    """Species paths and per-layer instance counts of a ``forest.bin``.

    Layout (``wowsunpack`` ``models/forest.rs``, engine-verified): ``u32
    num_species`` + pad, ``i64`` string-table offset (from the file start);
    four 0x90-byte layers at 0x10 (``i64`` buffer offset, ``u32`` count, pad,
    then ``num_species`` x ``(u32 start, u32 count)``); the string table is
    ``num_species`` x ``(u64 length, i64 offset from the entry)``.
    Returns ``(paths, {layer: [count per species]})``.
    """
    if len(data) < 0x250:
        raise VegetationLibraryError("forest.bin shorter than its header")
    n = struct.unpack_from("<I", data, 0)[0]
    table = struct.unpack_from("<q", data, 8)[0]
    if n > 16 or table <= 0 or table + 16 * n > len(data):
        raise VegetationLibraryError(f"forest.bin header out of range (species {n}, table {table:#x})")
    paths: list[str] = []
    for i in range(n):
        ln, rel = struct.unpack_from("<Qq", data, table + 16 * i)
        a = table + 16 * i + rel
        raw = data[a:a + ln]
        paths.append(raw.split(b"\0", 1)[0].decode("utf-8").strip())
    counts: dict[str, list[int]] = {}
    for li, name in enumerate(FOREST_LAYERS):
        base = 0x10 + 0x90 * li
        per = [struct.unpack_from("<II", data, base + 0x10 + 8 * s)[1] for s in range(n)]
        counts[name] = per
    return paths, counts


def stsdk_counts(data: bytes) -> tuple[int, int] | None:
    """``(vertex_count, index_count)`` of the first draw call of a CTable
    (``SpeedTreeSDK___``) ``.stsdk``, following the offset chain the
    toolkit's ``speedtree.rs`` reads; ``None`` for another format."""
    if not data.startswith(b"SpeedTreeSDK___"):
        return None

    def rel(base: int, off: int) -> int:
        return base + struct.unpack_from("<I", data, base + off)[0]

    base = 15
    draw_table = rel(base, 0xA4)
    if struct.unpack_from("<I", data, draw_table)[0] == 0:
        return None
    dc0 = rel(draw_table, 4)
    vert_header = rel(rel(rel(dc0, 4), 4), 8)
    idx_sub = rel(dc0, 8)
    return struct.unpack_from("<I", data, vert_header)[0], struct.unpack_from("<I", data, idx_sub)[0]


def read_lod_profiles(xml_bytes: bytes) -> dict[str, Any]:
    """The ``<forest>`` quality presets of ``graphics_preferences.scheme.xml``:
    ``{"default": preset, "presets": {preset: {"primary": {...}, "underwater":
    {...}, "binocular": {...}}}}`` with the attributes as floats."""
    root = ET.fromstring(xml_bytes)
    forest = root.find(".//forest")
    if forest is None:
        raise VegetationLibraryError("no <forest> block in the graphics preferences scheme")
    quality = forest.find("quality")
    if quality is None:
        raise VegetationLibraryError("no <forest><quality> in the graphics preferences scheme")
    default = (quality.text or "").strip() or FALLBACK_QUALITY
    presets: dict[str, Any] = {}
    cons = quality.find("constraints")
    for p in list(cons) if cons is not None else []:
        def attrs(tag: str, p: ET.Element = p) -> dict[str, float]:
            el = p.find(tag)
            return {k: float(v) for k, v in el.attrib.items()} if el is not None else {}
        presets[p.tag] = {
            "primary": attrs("primaryLodProfile"),
            "underwater": attrs("underwaterLodProfile"),
            "binocular": attrs("binocularLimit"),
            "enabled": p.attrib.get("enabled", "true") != "false",
        }
    if default not in presets:
        default = FALLBACK_QUALITY if FALLBACK_QUALITY in presets else next(iter(presets), default)
    return {"default": default, "presets": presets}


def lod_ranges(profile: dict[str, float], levels: int,
               authored_share: float = AUTHORED_LOD_SHARE) -> list[tuple[float, float]]:
    """Switch distances, in map units, for ``levels`` LODs (LOD0 = authored).

    The client draws the authored mesh to ``meshEnd`` and an impostor from
    there to ``imposterEnd``; the library has no impostor. LOD0 runs to
    ``e0 = meshEnd * (imposterEnd / meshEnd) ** authored_share`` (the
    client's ``meshEnd`` for a share of 0). The generated levels share
    ``[e0, imposterEnd]``: level ``i`` of ``k = levels - 1`` ends at ``e0 *
    (imposterEnd / e0) ** (i / k)`` (a geometric split; the client draws no
    mesh there, so no client number decides it). A single level runs to
    ``imposterEnd``.
    """
    mesh_end = float(profile.get("meshEnd", 0.0))
    far = float(profile.get("imposterEnd", 0.0))
    if levels <= 0:
        return []
    if levels == 1 or mesh_end <= 0 or far <= mesh_end:
        return [(0.0, max(far, mesh_end))] + [(far, far)] * (levels - 1)
    share = min(max(float(authored_share), 0.0), 1.0)
    e0 = mesh_end * (far / mesh_end) ** share
    out = [(0.0, e0)]
    k = levels - 1
    for i in range(1, levels):
        a = out[-1][1]
        b = e0 * (far / e0) ** (i / k)
        out.append((a, b))
    return out


# ---------------------------------------------------------------------------
# GLB writer
# ---------------------------------------------------------------------------


def _pad4(b: bytes, fill: bytes = b"\0") -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def write_species_glb(
    path: Path,
    *,
    name: str,
    positions: bytes,
    normals: bytes,
    uvs: bytes,
    vertex_count: int,
    lod_indices: list[bytes],
    albedo_png: bytes | None,
    node_extras: list[dict[str, Any]],
    scene_extras: dict[str, Any],
) -> dict[str, Any]:
    """Write one species GLB: one node per LOD (``LOD0``..), every LOD a mesh
    over the same vertex accessors with its own index accessor. Returns
    ``{"bounds_min", "bounds_max", "bytes"}``. Positions are f32 x3 metres."""
    import numpy as np

    pos = np.frombuffer(positions, "<f4").reshape(-1, 3)
    bmin = [float(v) for v in pos.min(axis=0)]
    bmax = [float(v) for v in pos.max(axis=0)]
    small = vertex_count < 65536
    blobs: list[bytes] = []
    views: list[dict[str, Any]] = []
    offset = 0

    def view(data: bytes, target: int | None) -> int:
        nonlocal offset
        data = _pad4(data)
        v: dict[str, Any] = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            v["target"] = target
        views.append(v)
        blobs.append(data)
        offset += len(data)
        return len(views) - 1

    accessors: list[dict[str, Any]] = [
        {"bufferView": view(positions, 34962), "componentType": 5126, "count": vertex_count,
         "type": "VEC3", "min": bmin, "max": bmax},
        {"bufferView": view(normals, 34962), "componentType": 5126, "count": vertex_count, "type": "VEC3"},
        {"bufferView": view(uvs, 34962), "componentType": 5126, "count": vertex_count, "type": "VEC2"},
    ]
    meshes: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    for i, raw in enumerate(lod_indices):
        idx = np.frombuffer(raw, "<u4")
        data = idx.astype("<u2").tobytes() if small else idx.tobytes()
        accessors.append({"bufferView": view(data, 34963), "componentType": 5123 if small else 5125,
                          "count": int(idx.size), "type": "SCALAR"})
        meshes.append({"name": f"{name}_LOD{i}", "primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
            "indices": len(accessors) - 1, "material": 0, "mode": 4}]})
        nodes.append({"name": f"LOD{i}", "mesh": i, "extras": node_extras[i]})
    material: dict[str, Any] = {
        "name": name, "alphaMode": "MASK", "alphaCutoff": 0.5, "doubleSided": True,
        "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                                 "metallicFactor": 0.0, "roughnessFactor": 1.0},
    }
    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "wows-model-export vegetation library"},
        "scene": 0,
        "scenes": [{"name": name, "nodes": list(range(len(nodes))), "extras": scene_extras}],
        "nodes": nodes, "meshes": meshes, "materials": [material], "accessors": accessors,
        "bufferViews": views,
    }
    if albedo_png:
        bv = view(albedo_png, None)
        doc["images"] = [{"name": f"{name}_spt_a", "mimeType": "image/png", "bufferView": bv}]
        doc["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        doc["textures"] = [{"source": 0, "sampler": 0}]
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0, "texCoord": 0}
    binary = b"".join(blobs)
    doc["buffers"] = [{"byteLength": len(binary)}]
    js = _pad4(json.dumps(doc, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(js) + 8 + len(binary)
    out = io.BytesIO()
    out.write(struct.pack("<III", _GLB_MAGIC, 2, total))
    out.write(struct.pack("<II", len(js), _JSON))
    out.write(js)
    out.write(struct.pack("<II", len(binary), _BIN))
    out.write(binary)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(out.getvalue())
    os.replace(tmp, path)
    return {"bounds_min": bmin, "bounds_max": bmax, "bytes": total}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@dataclass
class _SpaceVeg:
    space: str
    build: int | None
    manifest: dict[str, Any]
    meshes: Path
    paths: list[str] = field(default_factory=list)
    counts: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class VegetationLibraryResult:
    library_root: Path
    index_path: Path
    species: list[str]
    written: list[str]
    skipped: list[str]
    spaces: list[str]
    warnings: list[str]


def species_id(path: str, taken: dict[str, str]) -> str:
    """Library id of a species path: its file stem (``Cedar_01``), or, when
    another path already has that stem, the stem joined with its parent
    folders until unique."""
    parts = path.replace("\\", "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    cand = stem
    i = len(parts) - 2
    while cand in taken and taken[cand] != path and i >= 0:
        cand = f"{parts[i]}_{cand}"
        i -= 1
    return cand


def _exported_spaces(ws: Path, only: Iterable[str] | None) -> list[_SpaceVeg]:
    maps = ws / "maps"
    names = sorted(only) if only else sorted(p.name for p in maps.iterdir() if p.is_dir()) if maps.is_dir() else []
    out: list[_SpaceVeg] = []
    for name in names:
        d = maps / name
        man = d / "vegetation_manifest.json"
        if not man.is_file():
            if only:
                raise VegetationLibraryError(f"{name}: no vegetation_manifest.json in {d}")
            continue
        doc = json.loads(man.read_text(encoding="utf-8"))
        if not doc.get("species"):
            continue
        build = None
        deps = d / "build_deps.json"
        if deps.is_file():
            try:
                build = int(json.loads(deps.read_text(encoding="utf-8")).get("build") or 0) or None
            except (ValueError, TypeError):
                build = None
        out.append(_SpaceVeg(name, build, doc, d / doc.get("meshes_file", "vegetation_meshes.bin")))
    return out


def _extract(files: list[str], out: Path, cfg: PipelineConfig) -> None:
    res = vfs.extract(files, out, config=cfg, record_deps=False)
    if "Wrote 0 files" in (res.stdout or ""):
        raise VegetationLibraryError(f"toolkit extracted nothing for {files[:3]}...")


def _local(out: Path, vfs_path: str) -> Path:
    return out.joinpath(*vfs_path.lstrip("/").split("/"))


def _top_mip_png(files: list[Path]) -> tuple[bytes, list[int]] | None:
    """PNG of the largest mip among a DDS set's files (``.dd0`` holds the
    top mip when the set is split, else ``.dds``)."""
    from PIL import Image

    best: tuple[int, Path] | None = None
    for f in files:
        try:
            with Image.open(f) as im:
                px = im.size[0] * im.size[1]
        except Exception:  # noqa: BLE001 - a member Pillow can't read is skipped
            continue
        if best is None or px > best[0]:
            best = (px, f)
    if best is None:
        return None
    with Image.open(best[1]) as im:
        rgba = im.convert("RGBA")
        buf = io.BytesIO()
        rgba.save(buf, format="PNG", optimize=False)
        return buf.getvalue(), [rgba.size[0], rgba.size[1]]


def build_vegetation_library(
    *,
    config: PipelineConfig | None = None,
    spaces: Iterable[str] | None = None,
    force: bool = False,
    quality: str | None = None,
    authored_share: float | None = None,
    log: Any = print,
) -> VegetationLibraryResult:
    """Export every species of the exported client maps (or of ``spaces``)
    into ``libraries/vegetation/``. Species already in the index with the
    same geometry hash, profile, LOD rule and source build are kept unless
    ``force``. ``authored_share`` overrides :data:`AUTHORED_LOD_SHARE`.
    Maps the index lists from an earlier run stay listed."""
    share = AUTHORED_LOD_SHARE if authored_share is None else float(authored_share)
    if not 0.0 <= share <= 1.0:
        raise VegetationLibraryError(f"authored share {share} is outside 0..1")
    cfg = config or PipelineConfig.load()
    ws = cfg.root_workspace
    lib = ws.joinpath(*LIB_REL.split("/"))
    index_path = lib / INDEX_NAME
    old: dict[str, Any] = {}
    if index_path.is_file():
        old = json.loads(index_path.read_text(encoding="utf-8"))
    old_assets: dict[str, Any] = dict(old.get("assets") or {})
    warnings: list[str] = []

    found = _exported_spaces(ws, spaces)
    if not found:
        raise VegetationLibraryError("no exported map with vegetation in the workspace")

    # 1. forest.bin of every space and the LOD profile, per game build.
    by_build: dict[int | None, list[_SpaceVeg]] = {}
    for s in found:
        by_build.setdefault(s.build, []).append(s)
    current = cfg.for_build(None)
    profiles: dict[str, Any] | None = None
    profile_build: int | None = None
    stsdk_bytes: dict[tuple[int | None, str], bytes] = {}
    tex_files: dict[tuple[int | None, str], dict[str, list[Path]]] = {}
    with tempfile.TemporaryDirectory(prefix="vegetation_lib_") as tmp_s:
        tmp = Path(tmp_s)
        for build in sorted(by_build, key=lambda b: (b is None, b or 0), reverse=True):
            group = by_build[build]
            bcfg = cfg.for_build(build) if build is not None else current
            out = tmp / str(build or "current")
            files = [f"/spaces/{s.space}/forest.bin" for s in group] + [LOD_PROFILE_VFS]
            log(f"vegetation: build {build or 'current'}: reading forest.bin of {len(group)} space(s)")
            _extract(files, out, bcfg)
            if profiles is None:
                prof = _local(out, LOD_PROFILE_VFS)
                if prof.is_file():
                    profiles = read_lod_profiles(prof.read_bytes())
                    profile_build = build
            for s in group:
                fb = _local(out, f"/spaces/{s.space}/forest.bin")
                if not fb.is_file():
                    raise VegetationLibraryError(f"{s.space}: forest.bin not in build {build}")
                s.paths, s.counts = read_forest(fb.read_bytes())
                if len(s.paths) != len(s.manifest["species"]) and max(
                    int(e["index"]) for e in s.manifest["species"]
                ) >= len(s.paths):
                    raise VegetationLibraryError(
                        f"{s.space}: the export has species the forest.bin does not ({len(s.paths)} named)")
            # 2. the species files of this build.
            wanted: set[str] = set()
            for s in group:
                for e in s.manifest["species"]:
                    wanted.add(s.paths[int(e["index"])])
            pats: list[str] = []
            for p in sorted(wanted):
                base = "/" + p.rsplit(".", 1)[0]
                pats += [f"/{p}"] + [f"{base}{suffix}.dd*" for _slot, suffix in TEXTURE_SLOTS]
            log(f"vegetation: build {build or 'current'}: reading {len(wanted)} species")
            _extract(pats, out, bcfg)
            for p in sorted(wanted):
                f = _local(out, "/" + p)
                if not f.is_file():
                    raise VegetationLibraryError(f"{p}: not in build {build}")
                stsdk_bytes[(build, p)] = f.read_bytes()
                base = f.with_suffix("")
                tex_files[(build, p)] = {
                    slot: sorted(base.parent.glob(f"{base.name}{suffix}.dd*"))
                    for slot, suffix in TEXTURE_SLOTS
                }

        if profiles is None:
            raise VegetationLibraryError(f"{LOD_PROFILE_PATH} not found")
        qname = quality or profiles["default"]
        if qname not in profiles["presets"]:
            raise VegetationLibraryError(f"quality preset {qname} not in {sorted(profiles['presets'])}")
        preset = profiles["presets"][qname]

        # 3. join, check and pick one source per species.
        ids: dict[str, str] = {p.get("source_path"): k for k, p in old_assets.items() if isinstance(p, dict)}
        taken: dict[str, str] = {k: v.get("source_path") for k, v in old_assets.items() if isinstance(v, dict)}
        species: dict[str, dict[str, Any]] = {}
        for s in sorted(found, key=lambda s: (-(s.build or 1 << 40), s.space)):
            blob = s.meshes.read_bytes()
            for e in s.manifest["species"]:
                si = int(e["index"])
                path = s.paths[si]
                counts = stsdk_counts(stsdk_bytes[(s.build, path)])
                if counts is None:
                    warnings.append(f"{s.space}: species {si} ({path}) has no SpeedTreeSDK___ draw table: "
                                    "its join to the export is not checked")
                elif counts != (int(e["vertex_count"]), int(e["index_count"])):
                    raise VegetationLibraryError(
                        f"{s.space}: species {si} has {e['vertex_count']} vertices / {e['index_count']} indices "
                        f"in the export but {counts[0]} / {counts[1]} in {path}: the species order does not join")
                vc = int(e["vertex_count"])
                geo = {
                    "positions": blob[e["positions_offset"]:e["positions_offset"] + 12 * vc],
                    "normals": blob[e["normals_offset"]:e["normals_offset"] + 12 * vc],
                    "uvs": blob[e["uvs_offset"]:e["uvs_offset"] + 8 * vc],
                    "lods": [blob[e["indices_offset"]:e["indices_offset"] + 4 * int(e["index_count"])]]
                    + [blob[lv["indices_offset"]:lv["indices_offset"] + 4 * int(lv["index_count"])]
                       for lv in e.get("lods") or []],
                }
                h = hashlib.sha1()
                for k in ("positions", "normals", "uvs"):
                    h.update(geo[k])
                for lv in geo["lods"]:
                    h.update(lv)
                sha = h.hexdigest()
                sid = ids.get(path) or species_id(path, taken)
                ids[path] = sid
                taken[sid] = path
                use = {"build": s.build, "species_index": si, "geometry_sha1": sha,
                       "join_checked": counts is not None,
                       "instances": {ln: s.counts[ln][si] for ln in FOREST_LAYERS if s.counts[ln][si]}}
                sp = species.get(sid)
                if sp is None:
                    species[sid] = {"path": path, "source": s, "geo": geo, "sha": sha, "vertex_count": vc,
                                    "uses": {s.space: use}}
                else:
                    sp["uses"][s.space] = use
                    if sha != sp["sha"]:
                        warnings.append(f"{sid}: {s.space} (build {s.build}) has other geometry than "
                                        f"{sp['source'].space} (build {sp['source'].build}); kept the newer")

        # 4. write.
        lib.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        skipped: list[str] = []
        new_assets: dict[str, Any] = dict(old_assets)
        dds_dirs: list[Path] = []
        with shared_textures.export_session(ws, [lib]):
            for sid in sorted(species):
                sp = species[sid]
                src: _SpaceVeg = sp["source"]
                path = sp["path"]
                kind = "underwater" if "/underwater/" in path else "land"
                prof = preset["underwater" if kind == "underwater" else "primary"]
                levels = len(sp["geo"]["lods"])
                ranges = lod_ranges(prof, levels, share)
                fade = prof.get("fadeStart"), prof.get("imposterEnd")
                prev = old_assets.get(sid) or {}
                glb_rel = f"{sid}/{sid}.glb"
                glb = lib / glb_rel
                stamp = {"geometry_sha1": sp["sha"], "build": src.build, "quality": qname,
                         "profile": prof, "authored_share": share}
                if (not force and glb.is_file() and prev.get("stamp") == stamp):
                    skipped.append(sid)
                    entry = dict(prev)
                    entry["spaces"] = {**(prev.get("spaces") or {}), **sp["uses"]}
                    new_assets[sid] = entry
                    continue
                # textures: raw sets into textures_dds/, PNG top mip into the GLB
                dds_dir = lib / sid / "textures_dds"
                if dds_dir.is_dir():
                    for f in dds_dir.iterdir():
                        if f.is_file():
                            f.unlink()
                dds_dir.mkdir(parents=True, exist_ok=True)
                texs = tex_files[(src.build, path)]
                for slot_files in texs.values():
                    for f in slot_files:
                        shutil.copyfile(f, dds_dir / f.name)
                png = _top_mip_png(texs.get("baseColor") or [])
                if png is None:
                    warnings.append(f"{sid}: no albedo texture")
                geo = sp["geo"]
                scaled = struct.pack(
                    f"<{3 * sp['vertex_count']}f",
                    *(v * M_PER_MAP_UNIT for v in struct.unpack(f"<{3 * sp['vertex_count']}f", geo["positions"])))
                lod_entries: list[dict[str, Any]] = []
                extras: list[dict[str, Any]] = []
                for i, (a, b) in enumerate(ranges):
                    tri = len(geo["lods"][i]) // 12
                    ent = {
                        "node": f"LOD{i}", "triangles": tri,
                        "min_distance_m": round(a * M_PER_MAP_UNIT, 3),
                        "max_distance_m": round(b * M_PER_MAP_UNIT, 3),
                        "source": "authored" if i == 0 else "generated",
                    }
                    lod_entries.append(ent)
                    extras.append({"lod": i, "min_distance_m": ent["min_distance_m"],
                                   "max_distance_m": ent["max_distance_m"], "source": ent["source"]})
                info = write_species_glb(
                    glb, name=sid, positions=scaled, normals=geo["normals"], uvs=geo["uvs"],
                    vertex_count=sp["vertex_count"], lod_indices=geo["lods"],
                    albedo_png=png[0] if png else None, node_extras=extras,
                    scene_extras={"schema": SCHEMA, "species": sid, "source_path": path, "units": "m"},
                )
                dds_dirs.append(dds_dir)
                size = [round(b - a, 3) for a, b in zip(info["bounds_min"], info["bounds_max"], strict=False)]
                new_assets[sid] = {
                    "name": sid,
                    "source_path": path,
                    "name_source": "forest.bin species table",
                    "kind": kind,
                    "glb": glb_rel,
                    "textures_dds": f"{sid}/textures_dds",
                    "vertices": sp["vertex_count"],
                    "triangles": [e["triangles"] for e in lod_entries],
                    "bounds_m": {"min": [round(v, 4) for v in info["bounds_min"]],
                                 "max": [round(v, 4) for v in info["bounds_max"]]},
                    "size_m": size,
                    "albedo_px": png[1] if png else None,
                    "lods": lod_entries,
                    "fade_m": [round((fade[0] or 0) * M_PER_MAP_UNIT, 3), round((fade[1] or 0) * M_PER_MAP_UNIT, 3)],
                    "geometry_source": {"space": src.space, "build": src.build,
                                        "file": f"maps/{src.space}/{src.meshes.name}"},
                    "spaces": {**(prev.get("spaces") or {}), **sp["uses"]},
                    "stamp": stamp,
                    "bytes": info["bytes"],
                }
                written.append(sid)
                log(f"vegetation: {sid}: {lod_entries[0]['triangles']} tris, {len(lod_entries)} LODs, "
                    f"{info['bytes'] // 1024} KiB")
            if dds_dirs:
                shared_textures.adopt_dirs(dds_dirs, workspace=ws, reason="vegetation library")
            # texture lists after adoption (store paths for moved sets)
            for sid, entry in new_assets.items():
                d = lib / sid / "textures_dds"
                if not d.is_dir():
                    continue
                listing = shared_textures.dds_dir_entries(d)
                tex: dict[str, Any] = {}
                for slot, suffix in TEXTURE_SLOTS:
                    mips = [ref for name, ref in listing if name.rsplit(".", 1)[0].lower().endswith(suffix)]
                    if mips:
                        tex[slot] = {"dds_mips": mips}
                entry["textures"] = tex

        space_map: dict[str, Any] = dict(old.get("spaces") or {})
        for s in found:
            space_map[s.space] = {
                "build": s.build,
                "species": [ids.get(p) if any(int(e["index"]) == i for e in s.manifest["species"]) else None
                            for i, p in enumerate(s.paths)],
                "species_paths": s.paths,
            }
        doc = {
            "schema": SCHEMA,
            "generator": "wows-build-vegetation-library",
            "units": "m",
            "unit_scale_to_map": round(1.0 / M_PER_MAP_UNIT, 12),
            "frame": "map-GLB frame: the native frame with z negated, +Y up; metres = map units x 15",
            "lod_profile": {
                "source": LOD_PROFILE_PATH,
                "source_build": profile_build,
                "quality": qname,
                "quality_default": profiles["default"],
                "units": "map units (the culling shader compares them with the camera's distance to a tree's "
                         "bounding sphere in map coordinates)",
                "presets": profiles["presets"],
                "authored_share": share,
                "rule": "LOD0 (authored mesh) to e0 = meshEnd * (imposterEnd / meshEnd) ** authored_share; the "
                        "generated LODs split e0..imposterEnd geometrically (the client draws an impostor from "
                        "meshEnd on; the library has none); fade_m = fadeStart..imposterEnd",
            },
            "assets": {k: new_assets[k] for k in sorted(new_assets)},
            "spaces": {k: space_map[k] for k in sorted(space_map)},
        }
        atomic_write_text(index_path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return VegetationLibraryResult(lib, index_path, sorted(species), written, skipped,
                                   [s.space for s in found], warnings)
