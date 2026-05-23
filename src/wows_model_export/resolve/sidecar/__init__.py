"""WoWS sidecar schema authority -- v3.
Split from the legacy single-file ``sidecar.py`` 2026-05-13. Public
surface is identical: every symbol previously importable from
``wows_model_export.resolve.sidecar`` is re-exported here.

Submodule layout (private; use the flat re-exports above):

- ``_constants``  -- schema vocabularies, key-order, errors.
- ``_helpers``    -- pure normalisers + datetime util.
- ``_makers``     -- ``make_*`` constructors.
- ``_materials``  -- material / texture / skin discovery from GLB+DDS.
- ``_io``         -- canonicalise + dumps + write + read + merge_preserving.
- ``_absorb``     -- ``absorb_*`` passes + variant swap + attach_to.
- ``_documents``  -- new_document + hull-GLB walkers + ship key helpers.
"""

from __future__ import annotations

from ._constants import (
    SCHEMA_VERSION,
    SIDECAR_SUFFIX,
    MODELS_SUBDIR,
    SPECIES_TO_SECTION,
    SECTION_TO_SPECIES,
    PLACEMENT_SECTIONS,
    VALID_SHIP_CLASSES,
    UNIVERSAL_HITBOX_ZONES,
    HITBOX_TOKEN_MAP,
    VALID_SHADER_INTENTS,
    VALID_STAGES,
    DDS_MIP_SUFFIXES,
    SidecarSchemaError,
)

from ._helpers import (
    normalise_hitbox_token,
)

from ._makers import (
    make_pipeline,
    make_ship,
    make_variants,
    make_hull_entry,
    make_hull_stats,
    make_geometry,
    make_armor,
    make_hitbox,
    make_turret,
    make_secondary,
    make_antiair,
    make_torpedo,
    make_accessory,
    make_material,
    make_shell,
    make_torpedo_profile,
    make_ballistics,
    make_default_skin,
    make_skin,
)

from ._materials import (
    texture_sets_from_dir,
    materials_from_glb,
    discover_skins_from_materials,
)

from ._io import (
    dumps,
    write,
    read,
    merge_preserving,
)

from ._absorb import (
    absorb_placements_json,
    apply_variant_asset_swaps,
    derive_attach_to,
    absorb_gameparams_ship,
    absorb_per_hull_placements,
    alias_active_hull_to_top_level,
    absorb_gameparams_variants,
    absorb_gameparams_mounts,
    absorb_gameparams_armor,
    absorb_gameparams_hitbox,
    absorb_gameparams_torpedoes,
    absorb_gameparams_effects,
    absorb_ballistics_json,
)

from ._documents import (
    new_document,
    new_document_from_placements,
    sidecar_path_for,
    build_ship_key,
    ship_from_placements,
    geometry_from_hull_glb,
    hitbox_from_hull_glb,
    geometry_and_hitbox_from_hull_glb,
)

__all__ = [
    'SCHEMA_VERSION',
    'SIDECAR_SUFFIX',
    'MODELS_SUBDIR',
    'SPECIES_TO_SECTION',
    'SECTION_TO_SPECIES',
    'PLACEMENT_SECTIONS',
    'VALID_SHIP_CLASSES',
    'UNIVERSAL_HITBOX_ZONES',
    'HITBOX_TOKEN_MAP',
    'VALID_SHADER_INTENTS',
    'VALID_STAGES',
    'DDS_MIP_SUFFIXES',
    'SidecarSchemaError',
    'normalise_hitbox_token',
    'make_pipeline',
    'make_ship',
    'make_variants',
    'make_hull_entry',
    'make_hull_stats',
    'make_geometry',
    'make_armor',
    'make_hitbox',
    'make_turret',
    'make_secondary',
    'make_antiair',
    'make_torpedo',
    'make_accessory',
    'make_material',
    'make_shell',
    'make_torpedo_profile',
    'make_ballistics',
    'make_default_skin',
    'make_skin',
    'texture_sets_from_dir',
    'materials_from_glb',
    'discover_skins_from_materials',
    'dumps',
    'write',
    'read',
    'merge_preserving',
    'absorb_placements_json',
    'apply_variant_asset_swaps',
    'derive_attach_to',
    'absorb_gameparams_ship',
    'absorb_per_hull_placements',
    'alias_active_hull_to_top_level',
    'absorb_gameparams_variants',
    'absorb_gameparams_mounts',
    'absorb_gameparams_armor',
    'absorb_gameparams_hitbox',
    'absorb_gameparams_torpedoes',
    'absorb_gameparams_effects',
    'absorb_ballistics_json',
    'new_document',
    'new_document_from_placements',
    'sidecar_path_for',
    'build_ship_key',
    'ship_from_placements',
    'geometry_from_hull_glb',
    'hitbox_from_hull_glb',
    'geometry_and_hitbox_from_hull_glb',
]
