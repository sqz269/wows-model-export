"""Layer 3 — pure transforms.

Take structured input, return structured output. No file writes, no
subprocess. May touch disk for read-only inspection (e.g.
`bone_orientation.glb_forward_z_sign` reads a GLB's JSON header to
analyse vertex bounds) but produce no side effects.

Lifted so far:

    bone_orientation   — variant-swap Ry(180°) correction signals
                         (glb_forward_z_sign, post_multiply_ry180)
                         from wg_bone_orientation.py
"""

from __future__ import annotations

from .bone_orientation import (
    AXIAL_THRESHOLD_M,
    glb_forward_z_sign,
    post_multiply_ry180,
)

__all__ = [
    "glb_forward_z_sign",
    "post_multiply_ry180",
    "AXIAL_THRESHOLD_M",
]
