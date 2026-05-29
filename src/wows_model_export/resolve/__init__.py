"""Layer 3 — pure transforms.

Take structured input, return structured output. No file writes (with
two side-effecting bridge functions documented below), no subprocess.
May touch disk for read-only inspection.

Public surface — two access patterns:

1. **Submodule namespaces** — for callers who want module-scoped names::

       from wows_model_export.resolve import (
           bone_orientation, skel_ext_hashes, gameparams_autofill,
           camo, sidecar, synth_emission,
       )
       sign = bone_orientation.glb_forward_z_sign(glb_path)
       res  = skel_ext_hashes.resolve_candidates(candidates, table=tbl)
       swap = gameparams_autofill.resolve_variant_accessory_swaps(ship, exterior)
       cats = camo.categories_for_entry(entry, ...)
       doc  = sidecar.read(path)
       emi  = synth_emission.synth_emissive_dds(diffuse_dds, mg_dds, ...)

2. **Specific symbols** flattened — for distinctive names. See
   ``__all__`` for the full list.

Lifted modules so far:

    bone_orientation     — variant-swap Ry(180°) signals
                           (from wg_bone_orientation.py)
    skel_ext_hashes      — Murmur3 hash table + placement-string parser
                           (from skel_ext_hashes.py)
    gameparams_autofill  — GameParams entity transforms for sidecar
                           autofill passes (from gameparams.py)
    camo                 — WG camo pipeline + CamouflageDb parser +
                           sidecar adapters (from wg_camo.py).
    sidecar              — sidecar schema authority — every make_*
                           constructor, absorb_* pass, mutating
                           transform, and the GLB walkers
                           (from tools/ship/sidecar.py).
    synth_emission       — diffuse * mg.B emissive synthesis for
                           ARP / Azur Lane / Sabaton crossover skins
                           (from synth_emission.py).
    mesh_compare         — pure mesh-diff transforms for skin-pack
                           vanilla / mod compare (from
                           compare_skin_meshes.py library half).
    exterior_compare     — Exterior peculiarityModels diff helpers
                           (from compare_exterior_swaps.py library half).
    rig_normalize_bones  — strip the DCC-side bone-axis bake from a
                           turret rig.glb so consumers can drive bone
                           rotations via plain Euler angles (from
                           rig_normalize_bones.py).
"""

from __future__ import annotations

# Submodule namespaces
from . import (
    bone_orientation,
    camo,
    exterior_compare,
    gameparams_autofill,
    mesh_compare,
    rig_normalize_bones,
    sidecar,
    skel_ext_hashes,
    synth_emission,
)

# bone_orientation — pure math + GLB header read
from .bone_orientation import (
    AXIAL_THRESHOLD_M,
    glb_forward_z_sign,
    post_multiply_ry180,
)

# camo — distinctive names; generic ``load`` stays in camo namespace
from .camo import (
    HULL_CATEGORIES,
    MASKS_BASE_DIR,
    MAT_BASE_DIR,
    TILE_BROADCAST_CATEGORIES,
    CamoEntry,
    CamouflageDb,
    ColorScheme,
    MgnParams,
    UvTransform,
    categories_for_entry,
    classify_part_category,
    display_name_for_camo_entry,
    display_name_for_exterior,
    ensure_camo_masks_for_entries,
    ensure_camouflages_xml,
    ensure_mat_camo_textures,
    list_extracted_mips,
    mat_textures_for_entry,
    mat_textures_from_palette_entry,
    mgn_params_to_json,
    palette_for_mask_paths,
    path_b_categories_for_entry,
    read_universal_exteriors,
    read_vehicle_permoflages,
    tile_categories_for_entry,
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

# sidecar — distinctive constructor + transform names; generic ``read``
# and ``write`` stay scoped to the submodule.
from .sidecar import (
    absorb_ballistics_json,
    absorb_gameparams_armor,
    absorb_gameparams_hitbox,
    absorb_gameparams_mounts,
    absorb_gameparams_ship,
    absorb_gameparams_torpedoes,
    absorb_gameparams_variants,
    absorb_per_hull_placements,
    absorb_placements_json,
    alias_active_hull_to_top_level,
    apply_variant_asset_swaps,
    derive_attach_to,
    discover_skins_from_materials,
    geometry_and_hitbox_from_hull_glb,
    geometry_from_hull_glb,
    hitbox_from_hull_glb,
    make_accessory,
    make_antiair,
    make_armor,
    make_ballistics,
    make_default_skin,
    make_geometry,
    make_hitbox,
    make_hull_entry,
    make_hull_stats,
    make_material,
    make_pipeline,
    make_secondary,
    make_shell,
    make_ship,
    make_skin,
    make_torpedo,
    make_torpedo_profile,
    make_turret,
    make_variants,
    materials_from_glb,
    merge_preserving,
    new_document,
    new_document_from_placements,
    ship_from_placements,
    texture_sets_from_dir,
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

# synth_emission — diffuse × mg.B emissive synthesis
from .synth_emission import (
    synth_emissive,
    synth_emissive_dds,
)

# rig_normalize_bones — strip the DCC-side bone-axis bake from a rig.glb
from .rig_normalize_bones import normalize as normalize_rig
from .rig_normalize_bones import normalize_file as normalize_rig_file

__all__ = [
    # Submodules
    "bone_orientation",
    "camo",
    "exterior_compare",
    "gameparams_autofill",
    "mesh_compare",
    "rig_normalize_bones",
    "sidecar",
    "skel_ext_hashes",
    "synth_emission",
    # bone_orientation
    "glb_forward_z_sign",
    "post_multiply_ry180",
    "AXIAL_THRESHOLD_M",
    # camo
    "CamouflageDb",
    "CamoEntry",
    "ColorScheme",
    "MgnParams",
    "UvTransform",
    "categories_for_entry",
    "classify_part_category",
    "display_name_for_camo_entry",
    "display_name_for_exterior",
    "ensure_camo_masks_for_entries",
    "ensure_camouflages_xml",
    "ensure_mat_camo_textures",
    "list_extracted_mips",
    "mat_textures_for_entry",
    "mat_textures_from_palette_entry",
    "mgn_params_to_json",
    "palette_for_mask_paths",
    "path_b_categories_for_entry",
    "read_universal_exteriors",
    "read_vehicle_permoflages",
    "tile_categories_for_entry",
    "HULL_CATEGORIES",
    "MASKS_BASE_DIR",
    "MAT_BASE_DIR",
    "TILE_BROADCAST_CATEGORIES",
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
    # sidecar
    "absorb_ballistics_json",
    "absorb_gameparams_armor",
    "absorb_gameparams_hitbox",
    "absorb_gameparams_mounts",
    "absorb_gameparams_ship",
    "absorb_gameparams_torpedoes",
    "absorb_gameparams_variants",
    "absorb_per_hull_placements",
    "absorb_placements_json",
    "alias_active_hull_to_top_level",
    "apply_variant_asset_swaps",
    "derive_attach_to",
    "discover_skins_from_materials",
    "geometry_and_hitbox_from_hull_glb",
    "geometry_from_hull_glb",
    "hitbox_from_hull_glb",
    "make_accessory",
    "make_antiair",
    "make_armor",
    "make_ballistics",
    "make_default_skin",
    "make_geometry",
    "make_hitbox",
    "make_hull_entry",
    "make_hull_stats",
    "make_material",
    "make_pipeline",
    "make_secondary",
    "make_shell",
    "make_ship",
    "make_skin",
    "make_torpedo",
    "make_torpedo_profile",
    "make_turret",
    "make_variants",
    "materials_from_glb",
    "merge_preserving",
    "new_document",
    "new_document_from_placements",
    "ship_from_placements",
    "texture_sets_from_dir",
    # skel_ext_hashes
    "build_table",
    "extract_placement_strings",
    "load_or_build",
    "load_table",
    "murmur3_32",
    "parse_placement_string",
    "resolve_candidates",
    "save_table",
    # synth_emission
    "synth_emissive",
    "synth_emissive_dds",
    # rig_normalize_bones (renamed to avoid clashing with generic ``normalize``)
    "normalize_rig",
    "normalize_rig_file",
]
