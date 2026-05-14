"""Per-asset forward-axis sign for variant-swap correction.

When an Exterior `peculiarityModels` entry swaps a base accessory to a
variant (e.g. AGM019 → AGM622, JGS158 → JGS3094), WG sometimes
re-authors the variant's geometry pre-flipped 180° around Y *and* sets
the variant's ``Rotate_Y_BlendBone`` rest pose to identity instead of
Z-mirror. The toolkit's
``mount.transform = HP * inverse(source_bone)`` is correct at base-
export time, but the pipeline rewrites ``asset_id`` post-toolkit
without recomputing the placement. When the bones disagree, the stale
correction over- or under-rotates the variant's mesh by exactly 180°.

This module exposes a single signal: the GLB's forward-axis sign. It's
a proxy for ``Rotate_Y_BlendBone.col2.z`` sign — empirically 1:1
across the 364-asset library on 2026-05-09:

    +1  →  mesh barrels at +Z  →  ``col2.z = -1``  (Z-mirror bone)
    -1  →  mesh barrels at -Z  →  ``col2.z = +1``  (identity bone)
     0  →  axially symmetric / non-gun asset (no opinion)

Use it at swap time: if
``glb_forward_z_sign(source_glb) != glb_forward_z_sign(target_glb)``
and neither is zero, post-multiply the placement matrix by Ry(180°).

The signal is computed from the GLB's accessor min/max bounds (no
buffer parse). All toolkit-exported accessories include
``min`` / ``max`` on position accessors, so the computation is cheap.

Both functions are pure transforms in the resolve sense: no writes,
deterministic outputs. `glb_forward_z_sign` touches disk to read the
GLB header but produces no side effects.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

# Threshold for the forward-axis verdict. The proxy is the sum
# ``z_max + z_min`` over every VEC3 accessor's ``min`` / ``max`` —
# positive when the mesh extends further in +Z than -Z. A small
# threshold rejects axially-symmetric assets (rangefinders, periscopes,
# narrow radar arrays) where neither direction dominates and the bone
# correction is undefined. Empirically 0.5m separates the gun assets
# (which span 5-25m along the firing axis) from the symmetric ones
# (which sit within ±0.5m of zero).
AXIAL_THRESHOLD_M: float = 0.5


def glb_forward_z_sign(glb_path: str | Path) -> int:
    """Return +1 / -1 / 0 — the GLB's forward-axis sign as a bone proxy.

    Returns ``0`` when the GLB doesn't exist, can't be parsed, or has
    no clear forward direction (axially-symmetric or empty).
    """
    p = Path(glb_path)
    try:
        data = p.read_bytes()
    except OSError:
        return 0
    if len(data) < 12 or data[:4] != b"glTF":
        return 0

    try:
        length = struct.unpack("<I", data[8:12])[0]
    except struct.error:
        return 0

    pos = 12
    js: dict | None = None
    while pos + 8 <= length and pos + 8 <= len(data):
        try:
            clen, ctype = struct.unpack("<II", data[pos : pos + 8])
        except struct.error:
            return 0
        body_end = pos + 8 + clen
        if body_end > len(data):
            return 0
        body = data[pos + 8 : body_end]
        pos = body_end
        if ctype == 0x4E4F534A:  # JSON chunk magic
            try:
                js = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 0
            break
    if js is None:
        return 0

    z_sum = 0.0
    seen = 0
    for acc in js.get("accessors") or []:
        if acc.get("type") != "VEC3":
            continue
        amin = acc.get("min")
        amax = acc.get("max")
        if not (isinstance(amin, list) and isinstance(amax, list)):
            continue
        if len(amin) < 3 or len(amax) < 3:
            continue
        try:
            z_sum += float(amax[2]) + float(amin[2])
            seen += 1
        except (TypeError, ValueError):
            continue
    if seen == 0:
        return 0
    if z_sum > AXIAL_THRESHOLD_M:
        return 1
    if z_sum < -AXIAL_THRESHOLD_M:
        return -1
    return 0


# Column-major Ry(180°). Negates cols 0 and 2 of a 4×4 when post-
# multiplied; translation column unchanged.
_RY_180_COL_NEGATIONS = (0, 1, 2, 3, 8, 9, 10, 11)


def post_multiply_ry180(
    matrix16: list[float] | tuple[float, ...],
) -> list[float]:
    """Return ``matrix16 * Ry(180°)`` — a fresh 16-float list.

    Equivalent to negating cols 0 and 2 of the column-major 4×4. Used
    by variant-asset-swap correction when the swap target's bone
    direction disagrees with the source's. Does not mutate the input.
    """
    if len(matrix16) != 16:
        raise ValueError(f"matrix16 must have 16 entries, got {len(matrix16)}")
    out = list(matrix16)
    for i in _RY_180_COL_NEGATIONS:
        out[i] = -out[i]
    return out


__all__ = ["glb_forward_z_sign", "post_multiply_ry180", "AXIAL_THRESHOLD_M"]
