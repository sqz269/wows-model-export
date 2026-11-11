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

The decision needs the ``Rotate_Y_BlendBone.col2.z`` sign of both
assets. This module reads it two ways, both from the library GLB's JSON
chunk (no buffer parse):

- :func:`glb_rotate_y_bone_sign` — the bone itself. Toolkit-exported
  rotating mounts carry their skeleton as glTF nodes, including
  ``Rotate_Y_BlendBone`` with its local matrix, so this is the exact
  value the toolkit inverted into the base placement. ``None`` when the
  GLB has no such node.
- :func:`glb_forward_z_sign` — the mesh-extent proxy: which way the
  mesh extends further along Z. It agrees with the bone for ordinary
  gun barrels, but not for meshes whose bulk sits behind the pivot
  (a Gearing crossover turret whose body outweighs its barrels, a
  Lewis gun, catapults, directors, radars): of the 240 library GLBs
  that carry the bone on 2026-11-10, 30 have the opposite extent sign
  and 29 have no extent opinion.

Both return the same convention:

    +1  →  ``col2.z = -1``  (Z-mirror bone; mesh barrels at +Z)
    -1  →  ``col2.z = +1``  (identity bone; mesh barrels at -Z)
     0  →  no opinion (axially symmetric mesh / non-axis-aligned bone)

At swap time compare the bones when both GLBs carry one, and fall back
to the extent proxy otherwise; when the two signs are confidently
opposite, post-multiply the placement matrix by Ry(180°).

All functions are pure transforms in the resolve sense: no writes,
deterministic outputs. The GLB readers touch disk to read the JSON chunk
but produce no side effects.
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

# Name of the bone whose rest pose the toolkit inverts into a rotating
# mount's placement (``mount.transform = HP * inverse(bone)``).
ROTATE_Y_BLEND_BONE: str = "Rotate_Y_BlendBone"


def _read_glb_json(glb_path: str | Path) -> dict | None:
    """Return the parsed JSON chunk of a binary glTF, or ``None``."""
    p = Path(glb_path)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"glTF":
        return None

    try:
        length = struct.unpack("<I", data[8:12])[0]
    except struct.error:
        return None

    pos = 12
    while pos + 8 <= length and pos + 8 <= len(data):
        try:
            clen, ctype = struct.unpack("<II", data[pos : pos + 8])
        except struct.error:
            return None
        body_end = pos + 8 + clen
        if body_end > len(data):
            return None
        body = data[pos + 8 : body_end]
        pos = body_end
        if ctype == 0x4E4F534A:  # JSON chunk magic
            try:
                js = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return js if isinstance(js, dict) else None
    return None


def glb_rotate_y_bone_sign(glb_path: str | Path) -> int | None:
    """Return the forward sign implied by the GLB's ``Rotate_Y_BlendBone``.

    ``+1`` when the bone's column-2 Z is negative (Z-mirror), ``-1`` when
    it is positive (identity), ``0`` when it is near zero (a bone that is
    not axis-aligned). Returns ``None`` when the GLB can't be read or has
    no ``Rotate_Y_BlendBone`` node with a matrix — the caller then falls
    back to :func:`glb_forward_z_sign`.
    """
    js = _read_glb_json(glb_path)
    if js is None:
        return None
    for node in js.get("nodes") or []:
        if not isinstance(node, dict) or node.get("name") != ROTATE_Y_BLEND_BONE:
            continue
        matrix = node.get("matrix")
        if not (isinstance(matrix, list) and len(matrix) == 16):
            return None
        try:
            col2_z = float(matrix[10])  # column-major: column 2, row 2
        except (TypeError, ValueError):
            return None
        if col2_z < -0.5:
            return 1
        if col2_z > 0.5:
            return -1
        return 0
    return None


def glb_forward_z_sign(glb_path: str | Path) -> int:
    """Return +1 / -1 / 0 — the GLB's forward-axis sign as a bone proxy.

    Returns ``0`` when the GLB doesn't exist, can't be parsed, or has
    no clear forward direction (axially-symmetric or empty).
    """
    js = _read_glb_json(glb_path)
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


__all__ = [
    "glb_forward_z_sign",
    "glb_rotate_y_bone_sign",
    "post_multiply_ry180",
    "AXIAL_THRESHOLD_M",
    "ROTATE_Y_BLEND_BONE",
]
