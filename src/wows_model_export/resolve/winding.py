"""Triangle-winding auto-detection heuristic for accessory GLBs.

Partial lift from ``tools/shared/glb_flip_winding.py`` (private I:-side
repo). The byte-level winding flipper lives at
:mod:`wows_model_export._glb` (``parse_glb`` / ``write_glb`` /
``flip_winding``); this module carries the **scoring + verdict** layer
on top of those primitives.

Two area-weighted signals per file:

- Signal B (``signal_b``) — geometric face normal vs centroid-outward
  direction (mesh centroid → triangle centroid). Pure geometry,
  independent of authored normals. ``B > 0.5`` means the current
  winding is consistent with outward-facing geometry — what the
  orientation shader paints blue.

- Signal A (``signal_a``) — geometric face normal vs averaged stored
  vertex normal. The toolkit's natural output convention has these
  OPPOSE, so a correctly-wound file has ``A`` near 0; a winding-
  inverted file has ``A`` near 1.

Joint test on the combined correctness ``c = (B + (1 - A)) / 2``:

- ``flip`` when ``c < 0.5 - margin``
- ``keep`` when ``c > 0.5 + margin``
- ``ambiguous`` otherwise (signals don't oppose — pathological
  thin / planar / open geometry where the centroid heuristic is
  unreliable)

The CLI surface and dry-run plumbing from the original module stay in
the I: tree until a dedicated CLI wrapper lands in
``wows_model_export.cli``.
"""

from __future__ import annotations

import struct

from .._glb import (
    COMP_BYTES,
    COMP_FLOAT,
    COMP_UNSIGNED_BYTE,
    COMP_UNSIGNED_INT,
    COMP_UNSIGNED_SHORT,
    MODE_TRIANGLES,
)

# Index-buffer component type → little-endian unpack format.
_INDEX_FMT = {
    COMP_UNSIGNED_BYTE:  "<B",
    COMP_UNSIGNED_SHORT: "<H",
    COMP_UNSIGNED_INT:   "<I",
}


def _read_indices(gltf: dict, bin_data: bytes, acc_idx: int) -> list[int]:
    acc = gltf["accessors"][acc_idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"]
    ct = acc["componentType"]
    fmt = _INDEX_FMT[ct]
    sz = COMP_BYTES[ct]
    return [struct.unpack_from(fmt, bin_data, start + i * sz)[0] for i in range(n)]


def _read_vec3(
    gltf: dict, bin_data: bytes, acc_idx: int,
) -> list[tuple[float, float, float]]:
    acc = gltf["accessors"][acc_idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"]
    if acc["componentType"] != COMP_FLOAT:
        return []
    stride = bv.get("byteStride") or 12
    return [struct.unpack_from("<fff", bin_data, start + i * stride) for i in range(n)]


def score_winding(gltf: dict, bin_data: bytes) -> dict:
    """Compute area-weighted Signal A + Signal B per primitive and per file.

    Returns a dict::

        {
          "signal_a":   0.0..1.0 or NaN  (geom · stored, area-weighted)
          "signal_b":   0.0..1.0 or NaN  (geom · centroid-outward, area-weighted)
          "n_prim":     int              (TRIANGLES primitives scored)
          "primitives": [
              {"mesh_idx": int, "prim_idx": int,
               "mesh_name": str,
               "signal_a": float, "signal_b": float,
               "n_tri": int}, ...
          ],
        }

    Empty / non-triangle / unindexed primitives are skipped silently.
    """
    accessors    = gltf.get("accessors", [])
    buffer_views = gltf.get("bufferViews", [])
    if not accessors or not buffer_views:
        return {"signal_a": float("nan"), "signal_b": float("nan"),
                "n_prim": 0, "primitives": []}

    file_a_pos = file_a_neg = 0.0
    file_b_pos = file_b_neg = 0.0
    primitives: list[dict] = []

    for mi, mesh in enumerate(gltf.get("meshes", [])):
        for pi, prim in enumerate(mesh.get("primitives", [])):
            if prim.get("mode", MODE_TRIANGLES) != MODE_TRIANGLES:
                continue
            attrs = prim.get("attributes") or {}
            pos_idx = attrs.get("POSITION")
            nor_idx = attrs.get("NORMAL")
            idx_acc = prim.get("indices")
            if pos_idx is None or idx_acc is None:
                continue

            positions = _read_vec3(gltf, bin_data, pos_idx)
            if not positions:
                continue
            normals = _read_vec3(gltf, bin_data, nor_idx) if nor_idx is not None else []
            indices = _read_indices(gltf, bin_data, idx_acc)
            if len(indices) % 3 != 0:
                continue

            # Mesh centroid as the mean of vertex positions. Using vertex
            # mean rather than triangle-centroid mean keeps regions with
            # concentrated detail from biasing the centre.
            cx = cy = cz = 0.0
            for p in positions:
                cx += p[0]
                cy += p[1]
                cz += p[2]
            n_v = len(positions)
            mc = (cx / n_v, cy / n_v, cz / n_v)

            a_pos = a_neg = 0.0
            b_pos = b_neg = 0.0
            for ti in range(0, len(indices), 3):
                ia, ib, ic = indices[ti], indices[ti + 1], indices[ti + 2]
                pa, pb, pc = positions[ia], positions[ib], positions[ic]
                # Geometric face normal from current winding: (b-a) × (c-a)
                e1x = pb[0] - pa[0]
                e1y = pb[1] - pa[1]
                e1z = pb[2] - pa[2]
                e2x = pc[0] - pa[0]
                e2y = pc[1] - pa[1]
                e2z = pc[2] - pa[2]
                ngx = e1y * e2z - e1z * e2y
                ngy = e1z * e2x - e1x * e2z
                ngz = e1x * e2y - e1y * e2x
                ng_len_sq = ngx * ngx + ngy * ngy + ngz * ngz
                if ng_len_sq < 1e-18:
                    continue   # degenerate triangle
                ng_len = ng_len_sq ** 0.5
                area = 0.5 * ng_len

                # Triangle centroid → outward direction
                tcx = (pa[0] + pb[0] + pc[0]) / 3.0
                tcy = (pa[1] + pb[1] + pc[1]) / 3.0
                tcz = (pa[2] + pb[2] + pc[2]) / 3.0
                ox = tcx - mc[0]
                oy = tcy - mc[1]
                oz = tcz - mc[2]
                ow_len_sq = ox * ox + oy * oy + oz * oz
                if ow_len_sq > 1e-18:
                    cosB = (ngx * ox + ngy * oy + ngz * oz) / (
                        ng_len * ow_len_sq ** 0.5
                    )
                    if cosB > 0.0:
                        b_pos += area
                    else:
                        b_neg += area

                if normals:
                    na, nb, nc = normals[ia], normals[ib], normals[ic]
                    nsx = (na[0] + nb[0] + nc[0]) / 3.0
                    nsy = (na[1] + nb[1] + nc[1]) / 3.0
                    nsz = (na[2] + nb[2] + nc[2]) / 3.0
                    ns_len_sq = nsx * nsx + nsy * nsy + nsz * nsz
                    if ns_len_sq > 1e-12:
                        cosA = (ngx * nsx + ngy * nsy + ngz * nsz) / (
                            ng_len * ns_len_sq ** 0.5
                        )
                        if cosA > 0.0:
                            a_pos += area
                        else:
                            a_neg += area

            primitives.append({
                "mesh_idx":  mi,
                "prim_idx":  pi,
                "mesh_name": mesh.get("name", ""),
                "signal_a":  (a_pos / (a_pos + a_neg)) if (a_pos + a_neg) > 0 else float("nan"),
                "signal_b":  (b_pos / (b_pos + b_neg)) if (b_pos + b_neg) > 0 else float("nan"),
                "n_tri":     len(indices) // 3,
            })
            file_a_pos += a_pos
            file_a_neg += a_neg
            file_b_pos += b_pos
            file_b_neg += b_neg

    sig_a = (file_a_pos / (file_a_pos + file_a_neg)) if (file_a_pos + file_a_neg) > 0 else float("nan")
    sig_b = (file_b_pos / (file_b_pos + file_b_neg)) if (file_b_pos + file_b_neg) > 0 else float("nan")
    return {
        "signal_a":   sig_a,
        "signal_b":   sig_b,
        "n_prim":     len(primitives),
        "primitives": primitives,
    }


# Verdict strings returned by detect_winding_verdict.
VERDICT_FLIP      = "flip"        # signals confidently say flip
VERDICT_KEEP      = "keep"        # signals confidently say correct as-is
VERDICT_AMBIGUOUS = "ambiguous"   # signals don't oppose — geometry is pathological
VERDICT_UNSCORED  = "unscored"    # no scorable primitives (rare)


def winding_correctness(score: dict) -> float:
    """Combined area-weighted "correctness" score in [0, 1].

    1.0 means winding is consistent with both outward-facing geometry
    AND opposes the stored vertex normals (the toolkit-natural correct
    state). 0.0 is the inverse — the file is winding-flipped.

    Computed as ``(B + (1 - A)) / 2``. A and B are anti-correlated by
    construction (flipping winding negates the geometric normal, which
    flips both the centroid-outward dot product AND the dot with stored
    normals), so they reinforce each other. When one signal is noisy
    (e.g. B is unreliable on thin / planar geometry), the other
    typically still pulls the combined score to a confident side —
    that's what makes the joint signal more robust than either alone.
    """
    a = score.get("signal_a")
    b = score.get("signal_b")
    if a is None or b is None or a != a or b != b:
        return float("nan")
    return (b + (1.0 - a)) / 2.0


def detect_winding_verdict(score: dict, margin: float = 0.10) -> str:
    """Apply the combined A+B threshold to a score dict from ``score_winding``.

    Returns ``flip`` when the combined correctness score is below
    ``0.5 - margin``, ``keep`` when above ``0.5 + margin``, and
    ``ambiguous`` in the dead band. Default margin 0.10 (so flip needs
    correctness < 0.40 and keep needs correctness > 0.60). The empirical
    sweep on 2026-05-07 showed that:

      - the 31 user-curated flipped assets all score ≥ 0.66 in their
        post-flip state (firmly KEEP), confirming the user's choices.
      - the 24 candidates the audit flags for FLIP all score ≤ 0.40,
        with the 14 highest-confidence ones below 0.30.
      - the dead band catches ~5 % of the library (mostly thin/planar
        decoratives + a couple of geometrically pathological radars)
        — roughly the universe where the user's eyeball test is also
        uncertain. These warrant manual review rather than auto-flip.
    """
    correctness = winding_correctness(score)
    if correctness != correctness:   # NaN
        return VERDICT_UNSCORED
    if correctness < 0.5 - margin:
        return VERDICT_FLIP
    if correctness > 0.5 + margin:
        return VERDICT_KEEP
    return VERDICT_AMBIGUOUS


def flip_normals(gltf: dict, bin_data: bytes) -> tuple[bytes, dict]:
    """Negate NORMAL (and the xyz of TANGENT) vertex attributes.

    Only handles FLOAT (componentType 5126), which is the canonical
    layout the toolkit emits; other component types are skipped.

    Like ``wows_model_export._glb.flip_winding``, dedupes shared
    accessor byte ranges so two primitives pointing at one buffer view
    don't get negated twice.

    Used by the accessory-library re-apply path when a manual flip
    entry sets ``flip_normals: true`` — the rare case where stored
    normals are inverted in addition to the winding (a toolkit bug
    artefact, not the common winding-only inversion).
    """
    bin_ba = bytearray(bin_data)
    accessors = gltf.get("accessors", [])
    buffer_views = gltf.get("bufferViews", [])

    seen_keys: set[tuple[int, str]] = set()
    report = dict(normal_accessors=0, tangent_accessors=0, skipped_non_float=0)

    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            for attr, acc_idx in (prim.get("attributes") or {}).items():
                if attr not in ("NORMAL", "TANGENT"):
                    continue
                acc = accessors[acc_idx]
                if acc["componentType"] != COMP_FLOAT:
                    report["skipped_non_float"] += 1
                    continue
                bv = buffer_views[acc["bufferView"]]
                start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
                key = (start, attr)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                # NORMAL is vec3 (12 B/vertex). TANGENT is vec4 (16 B/vertex);
                # only negate xyz — the handedness sign stays.
                comp_count = 3
                stride_per_vertex = 12
                if attr == "TANGENT":
                    comp_count = 3      # negate xyz only
                    stride_per_vertex = 16
                    report["tangent_accessors"] += 1
                else:
                    report["normal_accessors"] += 1

                n_verts = acc["count"]
                for v in range(n_verts):
                    base = start + v * stride_per_vertex
                    for c in range(comp_count):
                        off = base + c * 4
                        (val,) = struct.unpack_from("<f", bin_ba, off)
                        struct.pack_into("<f", bin_ba, off, -val)

    return bytes(bin_ba), report


__all__ = [
    "score_winding",
    "winding_correctness",
    "detect_winding_verdict",
    "flip_normals",
    "VERDICT_FLIP",
    "VERDICT_KEEP",
    "VERDICT_AMBIGUOUS",
    "VERDICT_UNSCORED",
]
