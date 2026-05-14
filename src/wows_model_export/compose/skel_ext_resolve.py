"""Compose `resolve_decorative_placements` -- merge HP_ placements +
skel_ext decorative candidates into the unified accessories JSON.

Lifted from ``tools/ship/skel_ext_resolve.py`` on the I:-side warships
repo. This is the Layer 4 orchestrator that reads two toolkit-produced
inputs:

  * ``<Ship>_placements.json``  -- HP_-only mounts from
    ``wowsunpack export-ship --placements-json``.
  * ``<Ship>_skel_ext.json``    -- decorative placement candidates from
    ``wowsunpack export-ship --skel-ext-candidates-json``.

and emits ``<Ship>_accessories.json`` -- the canonical sidecar input
consumed by :mod:`wows_model_export.compose.scaffold_ship`.

Mode ``"hash"`` is the default (and the only path actively maintained
for new ships, as of 2026-05-10). It resolves each candidate's
``p0_hash`` via :func:`wows_model_export.resolve.skel_ext_hashes.resolve_candidates`
against a Murmur3_32 lookup table built from ``assets.bin``'s
``MP_*`` / ``SP_*`` string corpus, catching the full set of WG
decoratives (5-10x more entries than legacy gmconvert).

Legacy modes (``"legacy-direct"`` / ``"legacy-anchor"``) were retired
2026-05-10 once every in-tree ship had been migrated to hash mode and
are not lifted here -- callers passing those modes raise
:class:`StepError` immediately. The mode parameter is preserved on
the public signature so the orchestrator can detect old call sites
deliberately.

Canonical :class:`StepEvent` names emitted at step boundaries:

    "load_inputs"     -- read both JSON files + auto-discover side files
    "resolve_hashes"  -- p0_hash table lookup via resolve.skel_ext_hashes
    "merge_placements" -- HP_ dedup + on-hull filter + classification
    "write_output"    -- emit final accessories JSON

Per-step failures are wrapped in :class:`StepError` with ``step=`` set
to one of the names above; ``raise ... from e`` preserves the chain.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from .. import toolkit
from ..config import PipelineConfig
from ..errors import StepError
from ..resolve import skel_ext_hashes
from ..types import OnEvent
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# Hull-section classification constants (lifted)
# ---------------------------------------------------------------------------

# Hull section meshes are emitted by the toolkit as flat children under a
# "Hull" group; the leading word before the first underscore identifies the
# hull section. Patch meshes are legit placement surfaces; crack/wire/hide
# variants are damage-state interiors that don't carry accessories.
SECTION_PREFIXES = ("Bow", "MidFront", "MidBack", "Stern")
EXCLUDED_VARIANT_TOKENS = ("crack", "wire", "hide", "lod")

# VFS manifest path resolution lives in :mod:`wows_model_export.toolkit.vfs`.
# The composer entry resolves a path via :func:`toolkit.default_manifest_path`
# (override: ``WOWS_VFS_MANIFEST`` env var). Hash-mode is read-only over the
# manifest, so the file is not built on demand here -- a missing manifest
# degrades into a warning rather than triggering an extract pass.

# Position-dedup tolerance for "same physical placement" between a
# candidate and an HP_-bound mount -- catches peculiarityModels swap
# variants at the same slot. 10cm tolerance handles float drift between
# the two emit paths.
_SWAP_POSITION_TOL_M = 0.1



# ---------------------------------------------------------------------------
# Hull-mesh AABB parsing (lifted verbatim)
# ---------------------------------------------------------------------------


def _section_of_mesh_name(name: str) -> str | None:
    """Return the hull section (``Bow`` / ``MidFront`` / ``MidBack`` /
    ``Stern``) a hull-mesh node name belongs to, or ``None`` if it's an
    excluded variant (``_crack_*``, ``_wire*``, LOD>0) or a non-section
    node. Patch meshes do classify -- they're legit accessory surfaces in
    damage state.
    """
    if not name:
        return None
    short = name.split(" / ")[-1]
    short_low = short.lower()
    if any(tok in short_low for tok in EXCLUDED_VARIANT_TOKENS):
        return None
    for s in SECTION_PREFIXES:
        if short.startswith(s + "_") or short.startswith(f"{s}Shape"):
            return s
    return None


def parse_hull_meshes(
    glb_path: Path,
) -> tuple[
    dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    dict[str, tuple[tuple[tuple[float, ...], tuple[float, ...]], str]],
]:
    """Parse a toolkit-emitted hull GLB; return ``(section_aabbs,
    mesh_aabbs)``. Reads only the GLB JSON chunk and uses pre-populated
    POSITION accessor min/max values for AABB extraction.

    Returns ``({}, {})`` on any IO/parse failure -- callers degrade to
    no-section assignment.
    """
    try:
        data = Path(glb_path).read_bytes()
    except (OSError, FileNotFoundError):
        return {}, {}
    if len(data) < 28 or data[:4] != b"glTF":
        return {}, {}
    json_chunk_len = struct.unpack_from("<I", data, 12)[0]
    try:
        gltf = json.loads(data[20:20 + json_chunk_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}, {}

    accessors = gltf.get("accessors", [])
    meshes = gltf.get("meshes", [])
    nodes = gltf.get("nodes", [])

    INF = float("inf")
    section_acc: dict[str, list[list[float]]] = {
        s: [[INF, INF, INF], [-INF, -INF, -INF]] for s in SECTION_PREFIXES
    }
    mesh_aabbs: dict[str, tuple[tuple[tuple[float, ...], tuple[float, ...]], str]] = {}

    for n in nodes:
        nm = n.get("name", "") or ""
        section = _section_of_mesh_name(nm)
        if section is None or "mesh" not in n:
            continue
        mesh = meshes[n["mesh"]] if n["mesh"] < len(meshes) else None
        if mesh is None:
            continue
        m_mn = [INF, INF, INF]
        m_mx = [-INF, -INF, -INF]
        for prim in mesh.get("primitives", []):
            pos_idx = prim.get("attributes", {}).get("POSITION")
            if pos_idx is None or pos_idx >= len(accessors):
                continue
            acc = accessors[pos_idx]
            mn, mx = acc.get("min"), acc.get("max")
            if not (mn and mx and len(mn) == 3 and len(mx) == 3):
                continue
            for i in range(3):
                if mn[i] < m_mn[i]:
                    m_mn[i] = mn[i]
                if mx[i] > m_mx[i]:
                    m_mx[i] = mx[i]
        if INF in m_mn:
            continue
        short = nm.split(" / ")[-1]
        mesh_aabbs[short] = ((tuple(m_mn), tuple(m_mx)), section)
        s_box = section_acc[section]
        for i in range(3):
            if m_mn[i] < s_box[0][i]:
                s_box[0][i] = m_mn[i]
            if m_mx[i] > s_box[1][i]:
                s_box[1][i] = m_mx[i]

    section_aabbs: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    for s, (mn, mx) in section_acc.items():
        if INF not in mn:
            section_aabbs[s] = (tuple(mn), tuple(mx))
    return section_aabbs, mesh_aabbs


def build_library_asset_aabbs(
    lib_root: Path,
) -> dict[str, tuple[tuple[float, ...], tuple[float, ...]]]:
    """Walk an accessory library (one ``.glb`` per asset_id) and return
    one local-space AABB per asset, keyed by the GLB filename stem.

    Returns ``{}`` if ``lib_root`` doesn't exist. Empty / unparseable
    GLBs are silently skipped.
    """
    out: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    if not lib_root or not Path(lib_root).is_dir():
        return out
    INF = float("inf")
    for glb in Path(lib_root).rglob("*.glb"):
        try:
            data = glb.read_bytes()
        except OSError:
            continue
        if len(data) < 28 or data[:4] != b"glTF":
            continue
        json_chunk_len = struct.unpack_from("<I", data, 12)[0]
        try:
            gltf = json.loads(data[20:20 + json_chunk_len].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        accs = gltf.get("accessors", [])
        mn = [INF, INF, INF]
        mx = [-INF, -INF, -INF]
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                pos_idx = prim.get("attributes", {}).get("POSITION")
                if pos_idx is None or pos_idx >= len(accs):
                    continue
                acc = accs[pos_idx]
                a_mn, a_mx = acc.get("min"), acc.get("max")
                if not (a_mn and a_mx and len(a_mn) == 3):
                    continue
                for i in range(3):
                    if a_mn[i] < mn[i]:
                        mn[i] = a_mn[i]
                    if a_mx[i] > mx[i]:
                        mx[i] = a_mx[i]
        if INF not in mn:
            out[glb.stem] = (tuple(mn), tuple(mx))
    return out


# ---------------------------------------------------------------------------
# AABB geometry helpers (lifted verbatim)
# ---------------------------------------------------------------------------


def transform_aabb(
    local_mn: tuple[float, ...],
    local_mx: tuple[float, ...],
    M: list[float],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Apply a 4x4 column-major matrix to an axis-aligned bounding box
    and return the world-space AABB containing all 8 transformed corners.
    """
    INF = float("inf")
    out_mn = [INF, INF, INF]
    out_mx = [-INF, -INF, -INF]
    for ix in (local_mn[0], local_mx[0]):
        for iy in (local_mn[1], local_mx[1]):
            for iz in (local_mn[2], local_mx[2]):
                wx = M[0] * ix + M[4] * iy + M[8] * iz + M[12]
                wy = M[1] * ix + M[5] * iy + M[9] * iz + M[13]
                wz = M[2] * ix + M[6] * iy + M[10] * iz + M[14]
                if wx < out_mn[0]:
                    out_mn[0] = wx
                if wy < out_mn[1]:
                    out_mn[1] = wy
                if wz < out_mn[2]:
                    out_mn[2] = wz
                if wx > out_mx[0]:
                    out_mx[0] = wx
                if wy > out_mx[1]:
                    out_mx[1] = wy
                if wz > out_mx[2]:
                    out_mx[2] = wz
    return tuple(out_mn), tuple(out_mx)


def aabb_overlap_volume(
    a_mn: tuple[float, ...], a_mx: tuple[float, ...],
    b_mn: tuple[float, ...], b_mx: tuple[float, ...],
) -> float:
    """Volume of the intersection of two AABBs; 0 when disjoint along
    any axis.
    """
    vol = 1.0
    for i in range(3):
        d = min(a_mx[i], b_mx[i]) - max(a_mn[i], b_mn[i])
        if d <= 0.0:
            return 0.0
        vol *= d
    return vol


def is_placement_on_hull(
    asset_id: str,
    matrix: list[float] | None,
    section_aabbs: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    mesh_aabbs: dict[str, tuple[tuple[tuple[float, ...], tuple[float, ...]], str]],
    library_aabbs: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    margin_m: float = 1.0,
) -> bool:
    """Deterministic on-hull check; see the I:-side module's docstring
    for the three-path rationale (mesh overlap / origin inside / margin
    fallback).
    """
    if not section_aabbs or matrix is None or len(matrix) != 16:
        return True
    pos = (matrix[12], matrix[13], matrix[14])

    extent = library_aabbs.get(asset_id)
    if extent is not None and mesh_aabbs:
        world_mn, world_mx = transform_aabb(extent[0], extent[1], matrix)
        is_point = all(world_mx[i] - world_mn[i] < 1e-6 for i in range(3))
        if not is_point:
            for _, ((m_mn, m_mx), _sec) in mesh_aabbs.items():
                if aabb_overlap_volume(world_mn, world_mx, m_mn, m_mx) > 0.0:
                    return True

    for mn, mx in section_aabbs.values():
        if all(mn[i] <= pos[i] <= mx[i] for i in range(3)):
            return True

    if margin_m > 0:
        for mn, mx in section_aabbs.values():
            if all(mn[i] - margin_m <= pos[i] <= mx[i] + margin_m for i in range(3)):
                return True

    return False


def classify_accessory(
    asset_id: str,
    matrix: list[float] | None,
    section_aabbs: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    mesh_aabbs: dict[str, tuple[tuple[tuple[float, ...], tuple[float, ...]], str]],
    library_aabbs: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
    force_section: str | None = None,
) -> tuple[str | None, str | None]:
    """Pick ``(parent_section, parent_mesh)`` for a placed accessory.

    Library-hit path uses summed mesh-AABB overlap volume; fallback
    uses origin-point classification with nearest-centroid tie-break and
    a 30m off-hull cutoff.
    """
    if not section_aabbs or not mesh_aabbs or matrix is None or len(matrix) != 16:
        return force_section, None

    pos = (matrix[12], matrix[13], matrix[14])
    extent = library_aabbs.get(asset_id)

    # Path A: mesh AABB overlap classification.
    if extent is not None:
        world_mn, world_mx = transform_aabb(extent[0], extent[1], matrix)
        is_point = all(world_mx[i] - world_mn[i] < 1e-6 for i in range(3))
        if not is_point:
            section_overlap: dict[str, float] = {s: 0.0 for s in section_aabbs}
            for _, ((m_mn, m_mx), m_sect) in mesh_aabbs.items():
                ov = aabb_overlap_volume(world_mn, world_mx, m_mn, m_mx)
                if ov > 0.0:
                    section_overlap[m_sect] = section_overlap.get(m_sect, 0.0) + ov

            section = force_section
            if section is None:
                best = max(section_overlap, key=section_overlap.get)
                if section_overlap[best] > 0.0:
                    section = best

            if section is not None:
                best_mesh = None
                best_ov = 0.0
                for name, ((m_mn, m_mx), m_sect) in mesh_aabbs.items():
                    if m_sect != section:
                        continue
                    ov = aabb_overlap_volume(world_mn, world_mx, m_mn, m_mx)
                    if ov > best_ov:
                        best_ov = ov
                        best_mesh = name
                if best_mesh is not None:
                    return section, best_mesh

    # Path B: origin-point fallback.
    contained_sections = [
        s for s, (mn, mx) in section_aabbs.items()
        if all(mn[i] <= pos[i] <= mx[i] for i in range(3))
    ]
    if force_section is not None:
        section = force_section
    elif len(contained_sections) == 1:
        section = contained_sections[0]
    else:
        candidates = contained_sections if contained_sections else list(section_aabbs.keys())

        def dist2_centroid(s: str) -> float:
            mn, mx = section_aabbs[s]
            cx = (mn[0] + mx[0]) * 0.5
            cy = (mn[1] + mx[1]) * 0.5
            cz = (mn[2] + mx[2]) * 0.5
            return (pos[0] - cx) ** 2 + (pos[1] - cy) ** 2 + (pos[2] - cz) ** 2

        section = min(candidates, key=dist2_centroid)

        if not contained_sections:
            mn, mx = section_aabbs[section]
            cx = (mn[0] + mx[0]) * 0.5
            cy = (mn[1] + mx[1]) * 0.5
            cz = (mn[2] + mx[2]) * 0.5
            d2 = (pos[0] - cx) ** 2 + (pos[1] - cy) ** 2 + (pos[2] - cz) ** 2
            if d2 > 30.0 * 30.0:
                return None, None

    in_section = [
        (name, mn, mx) for name, ((mn, mx), sec) in mesh_aabbs.items() if sec == section
    ]
    contained_meshes = [
        (name, mn, mx) for name, mn, mx in in_section
        if all(mn[i] <= pos[i] <= mx[i] for i in range(3))
    ]
    if contained_meshes:
        def vol(t: tuple[str, tuple[float, ...], tuple[float, ...]]) -> float:
            _, mn, mx = t
            return (mx[0] - mn[0]) * (mx[1] - mn[1]) * (mx[2] - mn[2])
        return section, min(contained_meshes, key=vol)[0]
    if in_section:
        def face_dist2(t: tuple[str, tuple[float, ...], tuple[float, ...]]) -> float:
            _, mn, mx = t
            d = 0.0
            for i in range(3):
                if pos[i] < mn[i]:
                    d += (mn[i] - pos[i]) ** 2
                elif pos[i] > mx[i]:
                    d += (pos[i] - mx[i]) ** 2
            return d
        return section, min(in_section, key=face_dist2)[0]
    return section, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_accessories_lib(start_path: Path) -> Path | None:
    """Walk up from ``start_path`` looking for ``libraries/accessories/``."""
    p = Path(start_path).absolute()
    for ancestor in [p] + list(p.parents):
        candidate = ancestor / "libraries" / "accessories"
        if candidate.is_dir():
            return candidate
    return None


def _stamp_dead_asset_id_in_place(placements: dict, dead_stems: set) -> int:
    """Stamp ``dead_asset_id: str | null`` on every placement entry.
    Returns the count of entries with a non-null dead variant.
    """
    n_with_dead = 0
    for section in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for entry in placements.get(section, []):
            aid = entry.get("asset_id", "") or ""
            if aid and aid.lower() in dead_stems:
                entry["dead_asset_id"] = f"{aid}_dead"
                n_with_dead += 1
            else:
                entry["dead_asset_id"] = None
    return n_with_dead


def build_vfs_name_index(
    manifest_path: Path,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Scan the VFS manifest for ``.geometry`` files; return
    ``(asset_name_index, dead_variant_stems)``.

    Raises :class:`FileNotFoundError` if the manifest is missing -- the
    caller should treat this as a degraded-mode warning, not a hard
    error.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"VFS manifest not found at {manifest_path}. "
            f"Generate it via `wowsunpack metadata --format json --output {manifest_path}`."
        )
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    index: dict[str, tuple[str, str]] = {}
    dead_stems: set[str] = set()
    for entry in manifest:
        p = entry.get("path", "")
        if not p.endswith(".geometry") or "/content/gameplay/" not in p:
            continue
        parts = p.strip("/").split("/")
        if len(parts) < 6:
            continue
        full_stem = parts[-1].removesuffix(".geometry")
        dir_path = "/".join(parts[:-1])

        if full_stem.endswith("_dead"):
            base_stem = full_stem[:-len("_dead")]
            dead_stems.add(base_stem.lower())
            continue

        index[full_stem.lower()] = (full_stem, dir_path)
    return index, dead_stems


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _parse_counter_suffix(instance_id: str) -> int:
    """Extract trailing integer counter from an instance_id like
    ``Montana_AGM034_16in50_Mk7_00``. Returns -1 if none.
    """
    if not instance_id:
        return -1
    tail = instance_id.rsplit("_", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return -1


def _build_resolve_context(
    placements: dict,
    manifest_path: Path,
    warnings: list[str],
) -> dict:
    """Build the VFS name index + HP dedup keys + asset counter state."""
    try:
        vfs_name_index, dead_stems = build_vfs_name_index(manifest_path)
    except FileNotFoundError as e:
        warnings.append(
            f"VFS manifest unavailable ({e}); asset metadata derivation degraded"
        )
        vfs_name_index = {}
        dead_stems = set()

    ship_info = placements.get("ship", {})
    ship_stem = ship_info.get("display_name") or ship_info.get("model_dir", "ship")

    asset_counts: dict[str, int] = {}
    for entry in placements.get("accessories", []):
        aid = entry.get("asset_id", "")
        if aid:
            asset_counts[aid] = max(
                asset_counts.get(aid, 0),
                _parse_counter_suffix(entry.get("instance_id", "")) + 1,
            )

    existing_hp_keys: set[tuple[str, str, str]] = set()
    existing_hp_positions: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for section in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for entry in placements.get(section, []):
            aid = entry.get("asset_id", "")
            if not aid:
                continue
            scope_lc = (entry.get("scope") or "").lower()
            category_lc = (entry.get("category") or "").lower()
            if scope_lc or category_lc:
                existing_hp_keys.add((scope_lc, category_lc, aid.lower()))
            pos = (entry.get("transform") or {}).get("position")
            if isinstance(pos, list) and len(pos) == 3:
                existing_hp_positions.setdefault((scope_lc, category_lc), []).append(
                    (float(pos[0]), float(pos[1]), float(pos[2]))
                )

    return {
        "vfs_name_index":          vfs_name_index,
        "dead_stems":              dead_stems,
        "ship_stem":               ship_stem,
        "asset_counts":            asset_counts,
        "existing_hp_keys":        existing_hp_keys,
        "existing_hp_positions":   existing_hp_positions,
    }


def _norm_offset(s: Any) -> str:
    if isinstance(s, int):
        return f"0x{s:X}"
    s = str(s).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return f"0x{int(s, 16):X}"


# ---------------------------------------------------------------------------
# Core hash-mode resolver (lifted; print()s removed in favor of warnings)
# ---------------------------------------------------------------------------


def _resolve_hash_mode(
    placements_json: Path,
    candidates_json: Path,
    output_json: Path,
    *,
    config: PipelineConfig,
    runner: StepRunner,
    manifest_path: Path | None = None,
    hull_glb: Path | None = None,
    accessories_lib: Path | None = None,
    include_dock: bool = False,
    drop_skinned: bool = True,
    hull_margin_m: float | None = 5.0,
    ship_nation: str | None = None,
    extra_scopes: tuple[str, ...] = ("common",),
    drop_degenerate: bool = True,
    origin_threshold_m: float = 0.001,
    keep_record_offsets: tuple[str, ...] | None = ("0x0",),
    warnings: list[str] | None = None,
) -> dict:
    """End-to-end hash-mode resolve. See the public
    :func:`resolve_decorative_placements` for the documented filter list.
    """
    if warnings is None:
        warnings = []
    if manifest_path is None:
        manifest_path = toolkit.default_manifest_path(config)

    # ── Step: load_inputs ─────────────────────────────────────────────
    with runner.step("load_inputs", detail=str(placements_json.name)) as st:
        placements = _load_json(placements_json)
        candidates_doc = _load_json(candidates_json)
        candidates = candidates_doc.get("candidates", [])

        if ship_nation is None:
            nation_raw = (placements.get("ship") or {}).get("nation") or ""
            resolved_nation = nation_raw.lower().strip()
        else:
            resolved_nation = ship_nation.lower().strip()
        allowed_scopes: set[str] | None
        if resolved_nation:
            allowed_scopes = {resolved_nation, *(s.lower() for s in extra_scopes)}
        else:
            allowed_scopes = None

        if keep_record_offsets is None or len(keep_record_offsets) == 0:
            allowed_offsets: set[str] | None = None
        else:
            allowed_offsets = {_norm_offset(s) for s in keep_record_offsets}

        if hull_glb is None:
            stem = placements_json.stem
            if stem.endswith("_placements"):
                stem = stem[:-len("_placements")]
            hull_glb = placements_json.with_name(f"{stem}_hull.glb")
        section_aabbs, mesh_aabbs = (
            parse_hull_meshes(hull_glb) if hull_glb.exists() else ({}, {})
        )

        hull_bbox: tuple[float, float, float, float, float, float] | None = None
        if section_aabbs and hull_margin_m is not None:
            all_mins = [a[0] for a in section_aabbs.values()]
            all_maxs = [a[1] for a in section_aabbs.values()]
            if all_mins:
                min_x = min(p[0] for p in all_mins)
                min_y = min(p[1] for p in all_mins)
                min_z = min(p[2] for p in all_mins)
                max_x = max(p[0] for p in all_maxs)
                max_y = max(p[1] for p in all_maxs)
                max_z = max(p[2] for p in all_maxs)
                hull_bbox = (
                    min_x - hull_margin_m, min_y - hull_margin_m,
                    min_z - hull_margin_m,
                    max_x + hull_margin_m, max_y + hull_margin_m,
                    max_z + hull_margin_m,
                )

        if accessories_lib is None:
            accessories_lib = find_accessories_lib(placements_json)
        library_aabbs = (
            build_library_asset_aabbs(accessories_lib) if accessories_lib else {}
        )

        ctx = _build_resolve_context(placements, manifest_path, warnings)
        st.annotate(
            f"candidates={len(candidates):,} hull_aabbs={len(section_aabbs)} "
            f"library_aabbs={len(library_aabbs)}",
            data={
                "candidates_total":   len(candidates),
                "section_aabbs":      len(section_aabbs),
                "library_aabbs":      len(library_aabbs),
                "allowed_scopes":     sorted(allowed_scopes) if allowed_scopes else None,
                "allowed_offsets":    sorted(allowed_offsets) if allowed_offsets else None,
            },
        )

    # ── Step: resolve_hashes ──────────────────────────────────────────
    with runner.step("resolve_hashes", detail=f"{len(candidates):,} candidates") as st:
        cache_path = config.require_cache_dir() / "skel_ext_hashes.json"
        table = skel_ext_hashes.load_or_build(cache=cache_path)
        resolved = skel_ext_hashes.resolve_candidates(candidates, table)
        st.annotate(
            f"resolved {resolved['summary']['resolved']:,}/{len(candidates):,} "
            f"({resolved['summary']['unique_asset_ids']:,} unique assets)",
            data=resolved["summary"],
        )

    # ── Step: merge_placements ────────────────────────────────────────
    with runner.step("merge_placements") as st:
        n_dead_stamped = _stamp_dead_asset_id_in_place(placements, ctx["dead_stems"])

        # Build asset_id → (scope, category) map from vfs_name_index.
        asset_meta: dict[str, tuple[str | None, str | None]] = {}
        for camel_id, vfs_dir in ctx["vfs_name_index"].values():
            if not vfs_dir:
                asset_meta[camel_id] = (None, None)
                continue
            parts = vfs_dir.replace("\\", "/").strip("/").split("/")
            if parts[:2] == ["content", "gameplay"]:
                parts = parts[2:]
            scope = parts[0] if parts else None
            category = parts[1] if len(parts) >= 2 else None
            asset_meta[camel_id] = (scope, category)

        merged: list[dict] = list(placements.get("accessories", []))
        n_hp_mesh_resolved = 0
        for entry in merged:
            if entry.get("parent_mesh") is not None:
                continue
            forced = entry.get("parent_section")
            ps, pm = classify_accessory(
                entry.get("asset_id", ""),
                entry.get("transform", {}).get("matrix"),
                section_aabbs, mesh_aabbs, library_aabbs,
                force_section=forced,
            )
            if pm is not None:
                entry["parent_mesh"] = pm
                n_hp_mesh_resolved += 1
            if forced is None and ps is not None:
                entry["parent_section"] = ps

        def _swap_variant_of_existing_hp(
            scope_low: str, category_low: str, position: list[float],
        ) -> bool:
            hp_positions = ctx["existing_hp_positions"].get(
                (scope_low, category_low), [],
            )
            for hx, hy, hz in hp_positions:
                dx = position[0] - hx
                dy = position[1] - hy
                dz = position[2] - hz
                if (dx * dx + dy * dy + dz * dz) ** 0.5 <= _SWAP_POSITION_TOL_M:
                    return True
            return False

        skipped_already_in_hp = 0
        skipped_swap_variant_at_hp = 0
        skipped_dock = 0
        skipped_skinned = 0
        skipped_off_hull = 0
        skipped_cross_nation = 0
        skipped_degenerate = 0
        skipped_near_origin = 0
        skipped_variant_block = 0
        skipped_unresolved = (
            len(candidates) - resolved["summary"]["resolved"]
        )
        emitted_by_category: dict[tuple, int] = defaultdict(int)
        cross_nation_by_scope: dict[str, int] = defaultdict(int)
        n_section_resolved = 0
        n_mesh_resolved = 0

        sorted_resolved = sorted(
            resolved["resolved"],
            key=lambda c: (
                c.get("segment", ""), c.get("p0_hash", ""),
                c.get("instance_index") or 0,
            ),
        )

        for cand in sorted_resolved:
            asset_id = cand["asset_id"]
            scope, category = asset_meta.get(asset_id, (None, None))

            if allowed_offsets is not None:
                try:
                    cand_off = _norm_offset(cand.get("record_offset"))
                except (TypeError, ValueError):
                    cand_off = ""
                if cand_off not in allowed_offsets:
                    skipped_variant_block += 1
                    continue

            segment = cand.get("segment") or ""
            if segment.endswith("_dock") and not include_dock:
                skipped_dock += 1
                continue

            prefix = cand.get("prefix") or ""
            if prefix == "SP_" and drop_skinned:
                skipped_skinned += 1
                continue

            scope_lc = (scope or "").lower()
            if allowed_scopes is not None and scope_lc not in allowed_scopes:
                skipped_cross_nation += 1
                cross_nation_by_scope[scope_lc or "(unknown)"] += 1
                continue

            hp_key = (scope_lc, (category or "").lower(), asset_id.lower())
            if hp_key in ctx["existing_hp_keys"]:
                skipped_already_in_hp += 1
                continue

            matrix = cand.get("transform", {}).get("matrix")
            if matrix is None or len(matrix) != 16:
                continue
            position = [matrix[12], matrix[13], matrix[14]]

            if drop_degenerate:
                cn0 = math.sqrt(matrix[0] ** 2 + matrix[4] ** 2 + matrix[8] ** 2)
                cn1 = math.sqrt(matrix[1] ** 2 + matrix[5] ** 2 + matrix[9] ** 2)
                cn2 = math.sqrt(matrix[2] ** 2 + matrix[6] ** 2 + matrix[10] ** 2)
                if (cn0 < 0.01 or cn1 < 0.01 or cn2 < 0.01
                        or cn0 > 100.0 or cn1 > 100.0 or cn2 > 100.0
                        or not (math.isfinite(cn0) and math.isfinite(cn1)
                                and math.isfinite(cn2))):
                    skipped_degenerate += 1
                    continue

            if origin_threshold_m > 0.0:
                if (abs(position[0]) <= origin_threshold_m
                        and abs(position[1]) <= origin_threshold_m
                        and abs(position[2]) <= origin_threshold_m):
                    skipped_near_origin += 1
                    continue

            if section_aabbs:
                if not is_placement_on_hull(
                    asset_id, matrix, section_aabbs, mesh_aabbs, library_aabbs,
                ):
                    skipped_off_hull += 1
                    continue
            elif hull_bbox is not None:
                x, y, z = position
                if (x < hull_bbox[0] or x > hull_bbox[3]
                        or y < hull_bbox[1] or y > hull_bbox[4]
                        or z < hull_bbox[2] or z > hull_bbox[5]):
                    skipped_off_hull += 1
                    continue

            if _swap_variant_of_existing_hp(scope_lc, (category or "").lower(), position):
                skipped_swap_variant_at_hp += 1
                continue

            parent_section, parent_mesh = classify_accessory(
                asset_id, matrix,
                section_aabbs, mesh_aabbs, library_aabbs,
            )
            if parent_section is not None:
                n_section_resolved += 1
            if parent_mesh is not None:
                n_mesh_resolved += 1

            counter = ctx["asset_counts"].get(asset_id, 0)
            ctx["asset_counts"][asset_id] = counter + 1
            has_dead = asset_id.lower() in ctx["dead_stems"]
            dead_asset_id = f"{asset_id}_dead" if has_dead else None

            merged.append({
                "instance_id":      f"{ctx['ship_stem']}_{asset_id}_{counter:02d}",
                "asset_id":         asset_id,
                "dead_asset_id":    dead_asset_id,
                "hp_name":          None,
                "parent_section":   parent_section,
                "parent_mesh":      parent_mesh,
                "scope":            scope,
                "category":         category,
                "subcategory":      None,
                "species":          None,
                "source":           "skel_ext_hash",
                "source_segment":   cand.get("segment"),
                "source_p0_hash":   cand.get("p0_hash"),
                "source_p1_hash":   cand.get("p1_hash"),
                "instance_index":   cand.get("instance_index"),
                "transform": {
                    "matrix":   [round(x, 6) for x in matrix],
                    "position": [round(x, 4) for x in position],
                },
            })
            emitted_by_category[(scope or "", category or "")] += 1
            if has_dead:
                n_dead_stamped += 1

        n_emitted = sum(emitted_by_category.values())
        st.annotate(
            f"emitted {n_emitted} new + kept {len(merged) - n_emitted} HP_-bound; "
            f"section/mesh resolved {n_section_resolved}/{n_mesh_resolved}",
            data={
                "emitted":                       n_emitted,
                "kept_hp_bound":                 len(merged) - n_emitted,
                "skipped_unresolved":            skipped_unresolved,
                "skipped_variant_block":         skipped_variant_block,
                "skipped_dock":                  skipped_dock,
                "skipped_skinned":               skipped_skinned,
                "skipped_cross_nation":          skipped_cross_nation,
                "skipped_degenerate":            skipped_degenerate,
                "skipped_near_origin":           skipped_near_origin,
                "skipped_off_hull":              skipped_off_hull,
                "skipped_already_in_hp":         skipped_already_in_hp,
                "skipped_swap_variant_at_hp":    skipped_swap_variant_at_hp,
                "section_resolved":              n_section_resolved,
                "mesh_resolved":                 n_mesh_resolved,
                "hp_mesh_resolved":              n_hp_mesh_resolved,
                "dead_asset_id_set":             n_dead_stamped,
            },
        )

        # Low-match-rate diagnostic.
        n_resolved_total = resolved["summary"]["resolved"]
        if n_emitted < 0.5 * n_resolved_total and n_resolved_total > 0:
            warnings.append(
                f"emitted {n_emitted} < 50% of resolved {n_resolved_total}; "
                f"review filter chain",
            )

    # ── Step: write_output ────────────────────────────────────────────
    with runner.step("write_output", detail=str(output_json.name)) as st:
        out = dict(placements)
        out["accessories"] = merged
        out["skel_ext_resolve"] = {
            "mode":                       "hash",
            "include_dock":               include_dock,
            "drop_skinned":               drop_skinned,
            "hull_margin_m":              hull_margin_m,
            "ship_nation":                resolved_nation or None,
            "allowed_scopes":             sorted(allowed_scopes) if allowed_scopes else None,
            "keep_record_offsets":        sorted(allowed_offsets) if allowed_offsets else None,
            "drop_degenerate":            drop_degenerate,
            "origin_threshold_m":         origin_threshold_m,
            "hull_bbox":                  list(hull_bbox) if hull_bbox else None,
            "candidates_total":           len(candidates),
            "candidates_resolved":        resolved["summary"]["resolved"],
            "candidates_unresolved":      skipped_unresolved,
            "skipped_variant_block":      skipped_variant_block,
            "skipped_dock":               skipped_dock,
            "skipped_skinned":            skipped_skinned,
            "skipped_cross_nation":       skipped_cross_nation,
            "skipped_degenerate":         skipped_degenerate,
            "skipped_near_origin":        skipped_near_origin,
            "skipped_off_hull":           skipped_off_hull,
            "skipped_already_in_hp":      skipped_already_in_hp,
            "skipped_swap_variant_at_hp": skipped_swap_variant_at_hp,
            "dead_asset_id_set":          n_dead_stamped,
            "hull_glb_for_sections":      str(hull_glb) if section_aabbs else None,
            "accessories_lib":            str(accessories_lib) if library_aabbs else None,
            "library_assets_indexed":     len(library_aabbs),
            "section_resolved":           n_section_resolved,
            "mesh_resolved":              n_mesh_resolved,
            "hp_mesh_resolved":           n_hp_mesh_resolved,
            "cross_nation_by_scope":      dict(sorted(
                cross_nation_by_scope.items(), key=lambda x: -x[1],
            )),
            "emit_by_category":           {
                f"{s}/{c}": n for (s, c), n in
                sorted(emitted_by_category.items(), key=lambda x: -x[1])
            },
        }
        out.pop("unmatched_legacy", None)

        output_json.parent.mkdir(parents=True, exist_ok=True)
        _save_json(output_json, out)
        try:
            sz = output_json.stat().st_size
        except OSError:
            sz = 0
        st.annotate(f"wrote {output_json.name} ({sz:,} bytes)", data={"bytes": sz})

    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def resolve_decorative_placements(
    placements_json: Path,
    *,
    candidates_json: Path,
    output_json: Path,
    mode: Literal["hash", "legacy-direct", "legacy-anchor"] = "hash",
    keep_record_offsets: tuple[str, ...] | None = ("0x0",),
    legacy_scan: Path | None = None,
    manifest_path: Path | None = None,
    hull_glb: Path | None = None,
    accessories_lib: Path | None = None,
    include_dock: bool = False,
    drop_skinned: bool = True,
    hull_margin_m: float | None = 5.0,
    ship_nation: str | None = None,
    extra_scopes: tuple[str, ...] = ("common",),
    drop_degenerate: bool = True,
    origin_threshold_m: float = 0.001,
    config: PipelineConfig | None = None,
    on_event: OnEvent | None = None,
) -> Path:
    """Merge HP_ placements + decorative candidates into a unified
    accessories JSON.  Returns the output path.

    Inputs:
        placements_json
            Toolkit's main placements JSON
            (``wowsunpack export-ship --placements-json``).  Carries HP_-only
            mounts + ship metadata.
        candidates_json
            Toolkit's skel_ext candidates JSON
            (``wowsunpack export-ship --skel-ext-candidates-json``).  Carries
            decorative placement candidates resolved via their ``p0_hash``.
        output_json
            Final merged accessories JSON.  Sidecar (and Unity / webview
            consumers) all read this file rather than the raw placements.
        mode
            ``"hash"`` (default) -- resolve every candidate's ``p0_hash``
            via :func:`wows_model_export.resolve.skel_ext_hashes.resolve_candidates`.
            Legacy modes (``"legacy-direct"`` / ``"legacy-anchor"``) were
            retired 2026-05-10 and raise :class:`StepError`; ``legacy_scan``
            is accepted on the signature for future re-introduction but
            is currently unused.
        keep_record_offsets
            Tuple of ``.skel_ext`` record-offset hex strings to keep.  Default
            ``("0x0",)`` retains only the base ship's record block.  Pass
            ``None`` (or an empty tuple) to keep all permoflage variant
            blocks.  Cherry-pick specific variants via e.g.
            ``("0x0", "0x14080")``.

    Filtering parameters mirror the legacy CLI's flags one-to-one;
    see the I:-side module docstring for the full semantics of each
    filter (off-hull, cross-nation, dock, skinned-mesh bone,
    degenerate-matrix, near-origin).
    """
    cfg = config or PipelineConfig.load()
    placements_json = Path(placements_json)
    candidates_json = Path(candidates_json)
    output_json = Path(output_json)

    if mode != "hash":
        # Surface a clear error rather than silently producing nothing --
        # callers that genuinely need the legacy mode should re-introduce
        # the I:-side implementation via git history.
        raise StepError(
            step="resolve_hashes",
            underlying=NotImplementedError(
                f"mode={mode!r} (legacy-direct / legacy-anchor) was retired "
                f"2026-05-10; only mode='hash' is supported.  Recover via "
                f"git history if a pre-migration ship needs it."
            ),
            detail=f"unsupported mode {mode!r}",
        )

    runner = StepRunner(on_event)
    warnings: list[str] = []
    manifest = manifest_path or toolkit.default_manifest_path(cfg)

    # ``legacy_scan`` is accepted for API stability but unused by the
    # hash-mode pathway.  Surface a warning so a stray legacy call site
    # doesn't get silently ignored.
    if legacy_scan is not None:
        warnings.append(
            f"legacy_scan={legacy_scan} ignored in hash mode "
            f"(only used by retired legacy-direct / legacy-anchor modes)"
        )

    try:
        _resolve_hash_mode(
            placements_json,
            candidates_json,
            output_json,
            config=cfg,
            runner=runner,
            manifest_path=manifest,
            hull_glb=hull_glb,
            accessories_lib=accessories_lib,
            include_dock=include_dock,
            drop_skinned=drop_skinned,
            hull_margin_m=hull_margin_m,
            ship_nation=ship_nation,
            extra_scopes=extra_scopes,
            drop_degenerate=drop_degenerate,
            origin_threshold_m=origin_threshold_m,
            keep_record_offsets=keep_record_offsets,
            warnings=warnings,
        )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="merge_placements",
            underlying=e,
            detail=str(e),
        ) from e

    # Surface accumulated warnings through stderr so consumers without an
    # on_event handler still see them; consumers that want structured access
    # listen to the StepEvent stream instead.
    for w in warnings:
        print(f"[skel_ext_resolve] warn: {w}", file=sys.stderr)

    return output_json


__all__ = [
    # Public composer entry
    "resolve_decorative_placements",
    # Geometry helpers (lifted public surface)
    "parse_hull_meshes",
    "build_library_asset_aabbs",
    "transform_aabb",
    "aabb_overlap_volume",
    "is_placement_on_hull",
    "classify_accessory",
    "find_accessories_lib",
    "build_vfs_name_index",
    # Constants
    "SECTION_PREFIXES",
    "EXCLUDED_VARIANT_TOKENS",
]
