"""Layer 3 — pure transforms.

Take structured input, return structured output. No file writes, no
subprocess. May touch disk for read-only inspection (e.g.
`bone_orientation.glb_forward_z_sign` reads a GLB's JSON header to
analyse vertex bounds) but produce no side effects.

Public surface — two access patterns:

1. **Submodule namespaces** — for callers who want module-scoped names::

       from wows_model_export.resolve import bone_orientation, skel_ext_hashes, gameparams_autofill
       sign = bone_orientation.glb_forward_z_sign(glb_path)
       res  = skel_ext_hashes.resolve_candidates(candidates, table=tbl)
       swap = gameparams_autofill.resolve_variant_accessory_swaps(ship, exterior)

2. **Specific symbols** flattened — for distinctive names::

       from wows_model_export.resolve import (
           glb_forward_z_sign, post_multiply_ry180,
           murmur3_32, resolve_candidates,
           resolve_variant_accessory_swaps, autofill_for_hp,
       )

Lifted modules so far:

    bone_orientation     — variant-swap Ry(180°) signals
                           (from wg_bone_orientation.py)
    skel_ext_hashes      — Murmur3 hash table + placement-string parser
                           (from skel_ext_hashes.py)
    gameparams_autofill  — GameParams entity transforms for sidecar
                           autofill passes (from gameparams.py)
"""

from __future__ import annotations

# Submodule namespaces
from . import bone_orientation, gameparams_autofill, skel_ext_hashes

# bone_orientation — pure math + GLB header read
from .bone_orientation import (
    AXIAL_THRESHOLD_M,
    glb_forward_z_sign,
    post_multiply_ry180,
)

# gameparams_autofill — sidecar payload synthesis
from .gameparams_autofill import (
    autofill_for_hp,
    class_from_caliber,
    classify_splash_boxes,
    collect_barbettes,
    collect_mount_armor,
    cross_validate_armor,
    derive_class_from_placements,
    find_vehicle_by_native_permoflage,
    resolve_components,
    resolve_variant_accessory_swaps,
    resolve_variant_model_dir,
    shell_effects_extras,
    shell_visual_extras,
    ship_metadata_extras,
    torpedo_effects_extras,
    torpedo_profile_extras,
    torpedo_visual_extras,
    variants_summary,
)

# skel_ext_hashes — hash builder + p0_hash → asset_id resolver
from .skel_ext_hashes import (
    build_table,
    extract_placement_strings,
    load_or_build,
    load_table,
    murmur3_32,
    parse_placement_string,
    resolve_candidates,
    save_table,
)

__all__ = [
    # Submodules
    "bone_orientation",
    "gameparams_autofill",
    "skel_ext_hashes",
    # bone_orientation
    "glb_forward_z_sign",
    "post_multiply_ry180",
    "AXIAL_THRESHOLD_M",
    # gameparams_autofill
    "autofill_for_hp",
    "class_from_caliber",
    "classify_splash_boxes",
    "collect_barbettes",
    "collect_mount_armor",
    "cross_validate_armor",
    "derive_class_from_placements",
    "find_vehicle_by_native_permoflage",
    "resolve_components",
    "resolve_variant_accessory_swaps",
    "resolve_variant_model_dir",
    "shell_effects_extras",
    "shell_visual_extras",
    "ship_metadata_extras",
    "torpedo_effects_extras",
    "torpedo_profile_extras",
    "torpedo_visual_extras",
    "variants_summary",
    # skel_ext_hashes
    "build_table",
    "extract_placement_strings",
    "load_or_build",
    "load_table",
    "murmur3_32",
    "parse_placement_string",
    "resolve_candidates",
    "save_table",
]
