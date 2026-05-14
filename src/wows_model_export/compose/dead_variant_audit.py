"""Alive vs dead GLB orientation audit for the accessory library.

Lifted from ``tools/ship/dead_variant_audit.py`` (private I:-side repo).
Layer 4 (composer) — runs disk I/O and writes the audit JSON sidecar
at the library root.

Scans every ``<asset_id>.glb`` in an accessory library for a sibling
``<asset_id>_dead.glb``. Reads the POSITION-accessor min/max of each
GLB's top-LOD primitive and classifies the dead variant as:

  Z-MIRRORED — dead is the alive flipped 180° around Y. Surfaces the
               OI-7 bug (`tools/reference/forward_axis_flip_audit.md`):
               since alive + dead share one placement, dead renders
               180° backwards at the alive's world transform.

  X-MIRRORED — flipped 180° around Z. Rare; flag for review.

  SAME       — dead matches alive's frame. Mesh-swap is safe.

  AMBIGUOUS  — Z extents differ enough that we can't tell (very
               asymmetric debris). Manual review recommended.

  NO_DEAD    — asset has no dead variant.

Used by :mod:`wows_model_export.compose.accessory_library` as the
``dead_variant_audit`` step: after the first ``index.json`` write,
classify every asset, persist the audit JSON, then rewrite the index
with ``dead_orientation`` embedded per asset.
"""

from __future__ import annotations

import json
import re
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# GLB parsing — minimal JSON-chunk read; same approach as turret_autorig.
# ---------------------------------------------------------------------------

def parse_glb_json(path: Path) -> dict:
    """Read just the JSON chunk from a GLB.

    Cheap: avoids materialising the BIN chunk in memory. Suitable for
    walking the entire accessory library.
    """
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ValueError(f"not a GLB: {path}")
    pos = 12
    while pos + 8 <= len(data):
        cl, ct = struct.unpack_from("<I4s", data, pos)
        pos += 8
        payload = data[pos:pos + cl]
        pos += cl
        if ct == b"JSON":
            return json.loads(payload)
    raise ValueError(f"no JSON chunk in {path}")


_LOD_SUFFIX_RE = re.compile(r"_lod(?:shape)?[1-9]")


def top_lod_bbox(gltf: dict) -> tuple[list[float], list[float]] | None:
    """Return (min, max) of the primitive most likely to be the top-res LOD.

    Heuristic: pick the first mesh whose name lacks a LOD-suffix marker.
    Two WG naming conventions appear in the accessory library:
    ``<base>_lod<N>Shape`` (standard) and ``<base>_lodShape<N>``
    (anomalous — ~30% of GLBs); both indicate non-LOD0 and are skipped.
    """
    meshes = gltf.get("meshes") or []
    accessors = gltf.get("accessors") or []
    for m in meshes:
        nm = (m.get("name") or "").lower()
        if _LOD_SUFFIX_RE.search(nm):
            continue
        prim = m["primitives"][0]
        pos_idx = prim["attributes"].get("POSITION")
        if pos_idx is None:
            continue
        a = accessors[pos_idx]
        if "min" in a and "max" in a:
            return a["min"], a["max"]
    return None


# ---------------------------------------------------------------------------
# Mirror classification
# ---------------------------------------------------------------------------

@dataclass
class AxisStats:
    a_min: float
    a_max: float
    d_min: float
    d_max: float

    @property
    def a_extent(self) -> float:
        return self.a_max - self.a_min

    @property
    def d_extent(self) -> float:
        return self.d_max - self.d_min

    @property
    def a_center(self) -> float:
        return (self.a_min + self.a_max) * 0.5

    @property
    def d_center(self) -> float:
        return (self.d_min + self.d_max) * 0.5


def classify_axis(
    s: AxisStats,
    *,
    extent_match_tol: float = 0.10,
    centre_match_tol: float = 0.15,
    flip_min_centre_offset: float = 0.10,
) -> str:
    """Return one of: ``'mirrored'`` / ``'same'`` / ``'ambiguous'``.

    ``'mirrored'``: extents agree AND centres are equal-magnitude opposite signs.
    ``'same'``:    extents agree AND centres are close to equal.

    Tolerances are tuned against the in-tree corpus:
    ``extent_match_tol=0.10`` (±10%), ``centre_match_tol=0.15`` (centres
    within 15% of extent), ``flip_min_centre_offset=0.10`` (need real
    signal — pure-symmetric meshes never trigger MIRRORED).
    """
    a_ext = s.a_extent
    d_ext = s.d_extent
    if a_ext < 0.05 or d_ext < 0.05:
        return "ambiguous"
    extent_diff = abs(a_ext - d_ext) / max(a_ext, d_ext)
    if extent_diff > extent_match_tol:
        return "ambiguous"
    centre_diff = abs(s.a_center - s.d_center)
    centre_sum = abs(s.a_center + s.d_center)
    # SAME: centres are nearly equal.
    if centre_diff < centre_match_tol * a_ext:
        return "same"
    # MIRRORED: centres are roughly opposite (a_center ≈ -d_center).
    # Check magnitude of either is non-trivial so we don't flag noise.
    if (centre_sum < centre_match_tol * a_ext
            and abs(s.a_center) > flip_min_centre_offset * a_ext):
        return "mirrored"
    return "ambiguous"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class AssetResult:
    """One asset's audit row — alive bbox, dead bbox, verdict.

    Serialised verbatim into ``dead_variant_audit.json``'s ``assets``
    array. ``verdict`` is the human-readable classification string the
    accessory-library index propagates as ``dead_orientation`` on each
    asset entry.
    """

    asset_id: str
    category: str
    subcategory: str | None
    alive_glb: str
    dead_glb: str | None
    alive_bbox: dict | None     # {"min": [x,y,z], "max": [...]}
    dead_bbox: dict | None
    z_class: str                # mirrored / same / ambiguous / no_dead
    x_class: str
    verdict: str                # human-readable verdict
    note: str = ""


def classify_asset(asset_id: str, entry: dict, library_root: Path) -> AssetResult:
    """Classify one asset by comparing alive vs dead GLB bounding boxes.

    ``entry`` is the per-asset dict from ``index.json`` — carries
    ``category`` / ``subcategory`` / ``glb`` / ``glb_dead`` keys with
    library-relative GLB paths.

    ``library_root`` is the directory that contains ``index.json`` and
    every asset's ``<scope>/<cat>/<asset_id>/`` subdirectory. Paths in
    ``entry`` are resolved relative to this root.
    """
    cat = entry.get("category")
    sub = entry.get("subcategory")
    alive_rel = entry.get("glb")
    dead_rel = entry.get("glb_dead")
    alive_path = library_root / alive_rel if alive_rel else None
    dead_path = library_root / dead_rel if dead_rel else None

    if not alive_path or not alive_path.is_file():
        return AssetResult(
            asset_id=asset_id, category=cat, subcategory=sub,
            alive_glb=str(alive_rel),
            dead_glb=str(dead_rel) if dead_rel else None,
            alive_bbox=None, dead_bbox=None,
            z_class="ambiguous", x_class="ambiguous",
            verdict="ALIVE_MISSING",
            note=f"alive GLB not found at {alive_path}",
        )

    alive_g = parse_glb_json(alive_path)
    alive_bbox = top_lod_bbox(alive_g)
    if alive_bbox is None:
        return AssetResult(
            asset_id=asset_id, category=cat, subcategory=sub,
            alive_glb=str(alive_rel),
            dead_glb=str(dead_rel) if dead_rel else None,
            alive_bbox=None, dead_bbox=None,
            z_class="ambiguous", x_class="ambiguous",
            verdict="NO_BBOX",
            note="alive GLB had no usable POSITION accessor min/max",
        )
    a_min, a_max = alive_bbox
    alive_dict = {
        "min": [round(v, 4) for v in a_min],
        "max": [round(v, 4) for v in a_max],
    }

    if not dead_path or not dead_path.is_file():
        return AssetResult(
            asset_id=asset_id, category=cat, subcategory=sub,
            alive_glb=str(alive_rel), dead_glb=None,
            alive_bbox=alive_dict, dead_bbox=None,
            z_class="no_dead", x_class="no_dead",
            verdict="NO_DEAD",
        )

    dead_g = parse_glb_json(dead_path)
    dead_bbox = top_lod_bbox(dead_g)
    if dead_bbox is None:
        return AssetResult(
            asset_id=asset_id, category=cat, subcategory=sub,
            alive_glb=str(alive_rel), dead_glb=str(dead_rel),
            alive_bbox=alive_dict, dead_bbox=None,
            z_class="ambiguous", x_class="ambiguous",
            verdict="NO_BBOX",
            note="dead GLB had no usable POSITION accessor min/max",
        )
    d_min, d_max = dead_bbox
    dead_dict = {
        "min": [round(v, 4) for v in d_min],
        "max": [round(v, 4) for v in d_max],
    }

    z = classify_axis(AxisStats(a_min[2], a_max[2], d_min[2], d_max[2]))
    x = classify_axis(AxisStats(a_min[0], a_max[0], d_min[0], d_max[0]))

    if z == "mirrored":
        verdict = "Z-MIRRORED"
    elif x == "mirrored":
        verdict = "X-MIRRORED"
    elif z == "same" and x == "same":
        verdict = "SAME"
    else:
        verdict = "AMBIGUOUS"

    return AssetResult(
        asset_id=asset_id, category=cat, subcategory=sub,
        alive_glb=str(alive_rel), dead_glb=str(dead_rel),
        alive_bbox=alive_dict, dead_bbox=dead_dict,
        z_class=z, x_class=x, verdict=verdict,
    )


def audit_library(
    library_root: Path,
    *,
    index_doc: dict | None = None,
    only: set[str] | None = None,
    write_sidecar: bool = True,
    out_path: Path | None = None,
) -> tuple[list[AssetResult], Path | None]:
    """Audit every asset under ``library_root`` and (optionally) write
    the JSON sidecar at ``library_root / "dead_variant_audit.json"``.

    ``index_doc`` is the parsed ``index.json``. When ``None`` we read it
    from ``library_root / "index.json"`` — pass it in to avoid a re-read
    when the caller already has it in memory.

    Returns ``(results, audit_path)`` where ``audit_path`` is the
    written sidecar path or ``None`` when ``write_sidecar=False`` /
    nothing to write.
    """
    if index_doc is None:
        idx_path = library_root / "index.json"
        if not idx_path.is_file():
            return [], None
        index_doc = json.loads(idx_path.read_text(encoding="utf-8"))

    targets: list[tuple[str, dict]] = []
    for aid, entry in (index_doc.get("assets") or {}).items():
        if only is not None and aid not in only:
            continue
        targets.append((aid, entry))
    targets.sort(key=lambda t: t[0])

    results: list[AssetResult] = []
    for aid, entry in targets:
        try:
            r = classify_asset(aid, entry, library_root)
        except Exception as e:
            r = AssetResult(
                asset_id=aid,
                category=entry.get("category"),
                subcategory=entry.get("subcategory"),
                alive_glb=str(entry.get("glb")),
                dead_glb=str(entry.get("glb_dead")) if entry.get("glb_dead") else None,
                alive_bbox=None, dead_bbox=None,
                z_class="ambiguous", x_class="ambiguous",
                verdict="ERROR", note=str(e),
            )
        results.append(r)

    if not write_sidecar:
        return results, None

    out = out_path or (library_root / "dead_variant_audit.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "wows_dead_variant_audit/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "asset_count": len(results),
        "assets": [asdict(r) for r in results],
    }
    out.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return results, out


__all__ = [
    "AssetResult",
    "AxisStats",
    "audit_library",
    "classify_asset",
    "classify_axis",
    "parse_glb_json",
    "top_lod_bbox",
]
