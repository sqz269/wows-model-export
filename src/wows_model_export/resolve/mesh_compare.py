"""Pure mesh-diff transforms for skin-pack pre-flight checks.

Lifted from ``tools/ship/compare_skin_meshes.py`` (private I:-side repo).
Layer 3 (resolve): pure mesh comparison transforms — input is parsed
``.geometry`` files, output is structured verdicts. No subprocess, no
file writes; only reads on-disk geometry via
:mod:`wows_model_export.read.bw_geometry`.

The CLI half (``discover_mod_assets``, ``extract_vanilla``, text-report
writer, ``main()``) is intentionally dropped — composer
``compose.skin_pack.ingest_skin_pack`` owns the VFS-extract + driving
logic. This module exposes the comparison core that gates each
accessory swap:

  * :class:`MeshSnapshot`        — LOD0 mesh snapshot
  * :class:`CompareResult`       — verdict + UV-agreement stats
  * :func:`load_all_prims`       — parse + snapshot every primitive group
  * :func:`pair_lod0_via_best_match` — pick the most likely LOD0 pair
  * :func:`compare`              — produce the verdict + stats

Verdict vocabulary (consumer-facing):

    identical            — positions + UVs match within tight tolerance
    uv_stable            — positions differ (re-meshed) BUT UVs match;
                           mod textures will sample correctly on vanilla
    uv_drift             — UVs deviate at the tail; usually invisible
    mismatched           — UV layout differs; textures won't fit
    mirrored_uv_stable   — mod = vanilla mirrored on an axis; UVs match
                           after the flip; renderer needs the mod mesh
                           to make use of these textures
    mirrored_uv_partial  — mirror match, UV agreement 60-95% (partial)
    mirrored_uv_diverged — mirror match, UV agreement <60% (skip)

This module is intentionally not re-exported via ``resolve/__init__.py``
yet; the parent agent will wire it in alongside the rest of the resolve
surface. Consumers must import the submodule directly:

    from wows_model_export.resolve import mesh_compare
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..read import bw_geometry as bwg
from ._kdtree import KDTree

# ---------------------------------------------------------------------------
# Mesh snapshot
# ---------------------------------------------------------------------------


@dataclass
class MeshSnapshot:
    """LOD0 mesh data for comparison. Positions in native WG units."""
    asset_id: str
    label: str                             # 'mod' | 'vanilla'
    primitive_index: int                   # which mapping was picked
    vertex_count: int
    triangle_count: int
    positions: np.ndarray                  # (V, 3)
    uvs: np.ndarray                        # (V, 2)
    indices: np.ndarray                    # (3T,)
    bbox_min: np.ndarray                   # (3,)
    bbox_max: np.ndarray                   # (3,)
    uv_bbox_min: np.ndarray                # (2,)
    uv_bbox_max: np.ndarray                # (2,)
    format_name: str

    def to_summary(self) -> dict:
        return {
            "primitive_index": self.primitive_index,
            "vertex_count": int(self.vertex_count),
            "triangle_count": int(self.triangle_count),
            "bbox_min": self.bbox_min.tolist(),
            "bbox_max": self.bbox_max.tolist(),
            "bbox_extent": (self.bbox_max - self.bbox_min).tolist(),
            "uv_bbox_min": self.uv_bbox_min.tolist(),
            "uv_bbox_max": self.uv_bbox_max.tolist(),
            "format_name": self.format_name,
        }


def _snapshot_from_prim(p, label: str, asset_id: str, p_idx: int) -> MeshSnapshot:
    return MeshSnapshot(
        asset_id=asset_id,
        label=label,
        primitive_index=p_idx,
        vertex_count=p.vertex_count,
        triangle_count=p.triangle_count,
        positions=p.positions,
        uvs=p.uvs,
        indices=p.indices,
        bbox_min=p.positions.min(axis=0),
        bbox_max=p.positions.max(axis=0),
        uv_bbox_min=p.uvs.min(axis=0),
        uv_bbox_max=p.uvs.max(axis=0),
        format_name=p.format_name,
    )


def load_all_prims(geometry_path: Path, label: str, asset_id: str) -> list[MeshSnapshot]:
    """All primitive groups (one per LOD/material-group), as snapshots."""
    g = bwg.parse_geometry(geometry_path)
    prims = bwg.extract_primitives(g)
    if not prims:
        raise ValueError(f"{geometry_path}: no primitives")
    return [_snapshot_from_prim(p, label, asset_id, i) for i, p in enumerate(prims)]


def load_lod0(geometry_path: Path, label: str, asset_id: str) -> MeshSnapshot:
    """LOD0 = highest vertex count (default; caller should usually use
    :func:`pair_lod0_via_best_match` for a more robust pairing across
    mod/vanilla).
    """
    snaps = load_all_prims(geometry_path, label, asset_id)
    rank = sorted(range(len(snaps)), key=lambda i: -snaps[i].vertex_count)
    return snaps[rank[0]]


def pair_lod0_via_best_match(
    mod_snaps: list[MeshSnapshot],
    van_snaps: list[MeshSnapshot],
) -> tuple[MeshSnapshot, MeshSnapshot]:
    """Pick the most likely LOD0 pair across the two primitive sets.

    Strategy: LOD0 is "the most detailed primitive group". The reliable
    signal for "same logical primitive group" is triangle count — even if
    vertices got re-welded, same surface ⇒ similar triangle count. So:

      1. The LOD0 prim on each side is the one with the highest vertex
         count (most detail wins). Treat that as the canonical choice;
         we only override if a *closer triangle-count match* exists among
         the top-2 vertex-count primitives on each side. This handles the
         case where the on-disk mapping order swaps the top two prims (as
         happens for some accessories — e.g., AGM034 mod has 2651v/2082t
         and 2733v/1759t with the smaller-tri one being LOD0 against
         vanilla's 2205v/1760t).

      2. If multiple mod/van pairs of similar vertex counts both have
         comparable triangle counts, pick the one with the highest
         combined vertex count.

    We deliberately do *not* filter by UV-deviation here: the question
    "do mod textures fit vanilla LOD0" REQUIRES comparing actual LOD0,
    even when LOD0 is mismatched. A perfect-match LOD2 pair is irrelevant
    for that question.
    """
    if not mod_snaps or not van_snaps:
        raise ValueError("need at least one primitive on each side")

    # Top candidates by vertex count (top 2 typically suffice — sometimes
    # the on-disk order swaps LOD0/LOD1).
    mod_top = sorted(mod_snaps, key=lambda s: -s.vertex_count)[:2]
    van_top = sorted(van_snaps, key=lambda s: -s.vertex_count)[:2]

    # Score each candidate pair by vertex-count similarity (proxy for "same
    # logical primitive group"). Triangle counts would be a stronger signal,
    # but the meshoptimizer Python binding (0.2.30a0) silently zeros indices
    # past ~9600 elements in some cases — so triangle_count derived from
    # decoded indices is unreliable. Vertex-count is always trustworthy.
    def score(ms: MeshSnapshot, vs: MeshSnapshot) -> tuple[float, int]:
        a = max(ms.vertex_count, 1)
        b = max(vs.vertex_count, 1)
        ratio = max(a, b) / min(a, b)            # ≥1, smaller is closer
        return (ratio, -(ms.vertex_count + vs.vertex_count))

    best = min(((ms, vs) for ms in mod_top for vs in van_top), key=lambda p: score(*p))
    return best


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class CompareResult:
    asset_id: str
    category: str
    mod: dict | None = None
    vanilla: dict | None = None
    bbox_overlap: dict | None = None
    nn_stats: dict | None = None           # nearest-neighbour stats
    uv_stats: dict | None = None
    verdict: str = "unknown"
    notes: list[str] = field(default_factory=list)


# Tolerance bands for UV deviation, normalised to [0,1] UV space. Texel-wise
# error scales with texture resolution: at 4096×4096 a UV delta of 1/4096 ≈
# 0.000244 is "one texel". 0.01 in UV space ≈ 41 texels at 4K — visible drift
# on flat tiled regions but invisible on busy texturing. We grade in three
# buckets:
UV_TIGHT = 0.005      # 20 texels at 4K — invisible even on flat regions
UV_LOOSE = 0.02       # 80 texels at 4K — possibly visible but rarely jarring
# beyond UV_LOOSE: treat as outlier


def _uv_agreement(
    src: MeshSnapshot,
    dst: MeshSnapshot,
    *,
    eps_position: float,
) -> dict:
    """For each vertex in ``src``, look up all candidate vertices in ``dst``
    within ``eps_position`` (native WG units). Among those candidates pick
    the one with the closest UV; record that best UV deviation.

    Why "best in radius" not "nearest neighbour": at UV seams, a single 3D
    point becomes multiple co-located vertices with different UVs (one per
    UV island). Plain nearest-neighbour picks one of those duplicates
    arbitrarily; the resulting UV diff overstates real disagreement. Picking
    the duplicate with the closest UV correctly answers "is THIS surface
    point's UV preserved between mod and vanilla?".

    For src vertices with no candidate inside ``eps_position`` (possible when
    the other side simplified the mesh and removed nearby vertices), we
    fall back to the nearest-neighbour UV — flagged as "uncovered".
    """
    tree = KDTree(dst.positions)
    candidates_lists = tree.query_ball_point(src.positions, r=eps_position)

    nn_dists, nn_idx = tree.query(src.positions, k=1)
    best_uv_inf = np.empty(src.vertex_count, dtype=np.float32)
    uncovered_mask = np.zeros(src.vertex_count, dtype=bool)

    for i, candidates in enumerate(candidates_lists):
        if not candidates:
            best_uv_inf[i] = np.max(np.abs(src.uvs[i] - dst.uvs[nn_idx[i]]))
            uncovered_mask[i] = True
        else:
            cand_uvs = dst.uvs[candidates]
            dists = np.max(np.abs(cand_uvs - src.uvs[i]), axis=1)
            best_uv_inf[i] = float(dists.min())

    # Stats over the *covered* subset — that's the meaningful "where the
    # mod and vanilla overlap, do their UVs agree?" question.
    covered = ~uncovered_mask
    cov_uv = best_uv_inf[covered]
    if len(cov_uv) == 0:
        cov_uv = best_uv_inf

    return {
        "covered_pct":        float(covered.mean() * 100),
        "uncovered_count":    int(uncovered_mask.sum()),
        "p50":                float(np.percentile(cov_uv, 50)),
        "p95":                float(np.percentile(cov_uv, 95)),
        "p99":                float(np.percentile(cov_uv, 99)),
        "max":                float(cov_uv.max()),
        "pct_within_tight":   float(np.mean(cov_uv < UV_TIGHT) * 100),
        "pct_within_loose":   float(np.mean(cov_uv < UV_LOOSE) * 100),
        "nn_dist_p50":        float(np.median(nn_dists)),
        "nn_dist_p95":        float(np.percentile(nn_dists, 95)),
        "nn_dist_max":        float(nn_dists.max()),
    }


def _mirror_detect(mod: MeshSnapshot, vanilla: MeshSnapshot) -> dict | None:
    """Detect whether ``mod`` is ``vanilla`` reflected along one axis.

    WG's Azur-Lane and similar mesh-swap permoflages frequently ship a
    Z-flipped variant of the vanilla turret/director (so the artist can
    pose forward and aft mounts independently). When that's the case, the
    bounding boxes have matched extents but inverted ranges along one axis,
    and applying the flip to the mod's positions makes them bitwise-match
    the vanilla positions.

    Returns ``{axis, nn_dist_max, uv_p95, uv_pct_tight}`` if a mirror match
    is found (NN-after-flip max distance < bbox_diag * 0.001), else ``None``.
    """
    if mod.vertex_count != vanilla.vertex_count:
        return None
    bbox_diag = float(np.linalg.norm(vanilla.bbox_max - vanilla.bbox_min))
    eps = max(bbox_diag * 0.001, 1e-4)
    for axis in (0, 1, 2):
        flipped = mod.positions.copy()
        flipped[:, axis] *= -1
        ext_v = vanilla.bbox_max - vanilla.bbox_min
        ext_m_flipped_min = flipped.min(axis=0)
        ext_m_flipped_max = flipped.max(axis=0)
        ext_m = ext_m_flipped_max - ext_m_flipped_min
        if not np.allclose(ext_v, ext_m, atol=eps):
            continue
        tree = KDTree(flipped)
        d, idx = tree.query(vanilla.positions, k=1)
        if d.max() > eps:
            continue
        # Positions match under flip — check UVs at the matched vertices.
        uv_inf = np.max(np.abs(vanilla.uvs - mod.uvs[idx]), axis=1)
        return {
            "axis":         "xyz"[axis],
            "nn_dist_max":  float(d.max()),
            "uv_p95":       float(np.percentile(uv_inf, 95)),
            "uv_pct_tight": float(np.mean(uv_inf < UV_TIGHT) * 100),
        }
    return None


def compare(mod: MeshSnapshot, vanilla: MeshSnapshot) -> CompareResult:
    """Produce a verdict + UV-agreement statistics for ``mod`` vs ``vanilla``.

    See module-level docstring for the verdict vocabulary. The result's
    ``notes`` list carries human-readable rationale strings for each
    decision point; consumers may surface or discard them.
    """
    cr = CompareResult(asset_id=mod.asset_id, category="?",
                       mod=mod.to_summary(), vanilla=vanilla.to_summary())

    # Detect mirrored variants up-front — Azur-Lane permoflages ship many of
    # these (turrets, secondaries, directors) and the plain UV-correspondence
    # path can't tell them apart from re-meshed designs without a flip step.
    mirror = _mirror_detect(mod, vanilla)
    if mirror is not None:
        cr.notes.append(
            f"mod mesh = vanilla mesh mirrored along {mirror['axis']}-axis; "
            f"UV agreement after flip: {mirror['uv_pct_tight']:.1f}% within tight tolerance"
        )

    # Bounding-box overlap (Jaccard-ish): are the two meshes occupying the
    # same world-space region? Big mismatch suggests a re-pivoted or
    # rescaled mesh — texture work likely won't transfer cleanly.
    inter_min = np.maximum(mod.bbox_min, vanilla.bbox_min)
    inter_max = np.minimum(mod.bbox_max, vanilla.bbox_max)
    inter_size = np.maximum(inter_max - inter_min, 0.0)
    inter_vol = float(np.prod(inter_size))
    mod_vol     = float(np.prod(mod.bbox_max - mod.bbox_min))
    vanilla_vol = float(np.prod(vanilla.bbox_max - vanilla.bbox_min))
    union_vol = mod_vol + vanilla_vol - inter_vol if inter_vol > 0 else mod_vol + vanilla_vol
    iou = inter_vol / union_vol if union_vol > 0 else 0.0
    cr.bbox_overlap = {"iou": iou, "mod_vol": mod_vol, "vanilla_vol": vanilla_vol,
                       "intersection_vol": inter_vol}

    # Pick eps adaptive to mesh scale: 0.1% of the bounding-box diagonal,
    # clamped to a sane minimum. Native WG units; for accessories whose
    # bbox is ~0.3 native units, 0.1% = 3e-4 ≈ 4.5 mm in metres.
    bbox_diag = float(np.linalg.norm(vanilla.bbox_max - vanilla.bbox_min))
    eps = max(bbox_diag * 0.001, 1e-4)

    # The decision-making direction is vanilla→mod: for each VANILLA
    # surface vertex (we render the vanilla mesh), does the mod author paint
    # the same UV at that 3D point? If yes, mod texture applied to vanilla
    # mesh will sample correctly at that vertex.
    v2m = _uv_agreement(vanilla, mod, eps_position=eps)
    # Informational the other direction: how much of the mod's UV authoring
    # corresponds to vanilla? Useful for understanding what's "extra" in
    # the mod versus a re-mesh.
    m2v = _uv_agreement(mod, vanilla, eps_position=eps)

    cr.uv_stats = {
        "tolerance_tight": UV_TIGHT,
        "tolerance_loose": UV_LOOSE,
        "eps_position":    eps,
        "vanilla_to_mod":  v2m,        # decision-making
        "mod_to_vanilla":  m2v,        # informational
    }
    cr.nn_stats = {
        "median_dist": v2m["nn_dist_p50"],
        "p95_dist":    v2m["nn_dist_p95"],
        "max_dist":    v2m["nn_dist_max"],
    }

    # Verdict driven by vanilla→mod agreement.
    pct_tight = v2m["pct_within_tight"]
    pct_loose = v2m["pct_within_loose"]
    same_count = (mod.vertex_count == vanilla.vertex_count
                  and mod.triangle_count == vanilla.triangle_count)

    if same_count and v2m["nn_dist_max"] < 1e-5 and v2m["max"] < UV_TIGHT:
        cr.verdict = "identical"
    elif mirror is not None:
        if mirror["uv_pct_tight"] >= 95.0:
            cr.verdict = "mirrored_uv_stable"
        elif mirror["uv_pct_tight"] >= 60.0:
            cr.verdict = "mirrored_uv_partial"
        else:
            cr.verdict = "mirrored_uv_diverged"
        cr.notes.append(
            "renderer must use the mod's mesh (mirror operation isn't representable "
            "as a UV transform); textures painted for mod-side UVs won't sample "
            "correctly on vanilla mesh."
        )
    elif pct_tight >= 95.0:
        cr.verdict = "uv_stable"
        cr.notes.append(
            f"≥95% of vanilla vertices ({pct_tight:.1f}%) find a mod vertex with UV "
            f"within {UV_TIGHT:.3f} — mod textures will sample correctly on the vanilla mesh"
        )
    elif pct_loose >= 90.0:
        cr.verdict = "uv_drift"
        cr.notes.append(
            f"UVs match in bulk ({pct_loose:.1f}% within {UV_LOOSE:.2f}) but a tight-tolerance "
            f"check shows {pct_tight:.1f}% — minor texel drift, usually invisible except at seams"
        )
    else:
        cr.verdict = "mismatched"
        cr.notes.append(
            f"UV layout differs: only {pct_tight:.1f}% within tight tolerance, "
            f"{pct_loose:.1f}% within loose — applying mod textures will give wrong sampling"
        )

    if not same_count:
        cr.notes.append(
            f"vertex counts differ: mod {mod.vertex_count} vs vanilla {vanilla.vertex_count} "
            f"(triangles {mod.triangle_count} vs {vanilla.triangle_count}) — different welding "
            f"or simplification, but UV agreement is what matters"
        )

    if v2m["covered_pct"] < 95.0:
        cr.notes.append(
            f"only {v2m['covered_pct']:.1f}% of vanilla vertices have a nearby mod vertex within "
            f"{eps:.4f}u — mod simplified or shifted geometry; uncovered surface regions can't be "
            f"verified vertex-wise (interpolation may still cover them visually)"
        )

    if iou < 0.5:
        cr.notes.append(f"bbox IoU low ({iou:.2f}) — meshes occupy different spatial regions")

    return cr


__all__ = [
    # Snapshot types
    "MeshSnapshot",
    "CompareResult",
    # Loaders
    "load_all_prims",
    "load_lod0",
    "pair_lod0_via_best_match",
    # Comparison
    "compare",
    # Tolerance constants
    "UV_TIGHT",
    "UV_LOOSE",
]
