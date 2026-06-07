"""Schema constants, vocabularies, key-order tuples, and the schema-error class.

These are pure data: no functions with side effects, no GLB / I/O. Every
public constant on the package surface re-exports from here via
``resolve/sidecar/__init__.py``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema version + file-system conventions
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3
SIDECAR_SUFFIX = ".meta.json"

# Per-ship subdirectory name — kept centralised so renaming it later
# (e.g. `gamemodels3d` → `models` in 2026-04-23) is a one-line change.
# Holds the toolkit-exported hull GLB, placements + accessories JSON,
# and the raw DDS mip chain that streaming consumers read.
MODELS_SUBDIR = "models"

# FBX custom-property keys — the three keys the downstream consumer
# reads from the ``_PipelineMetadata`` empty to locate + validate the
# sidecar.


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: Mount species (toolkit MountSpecies) → sidecar section name. Everything
#: not in this map (``FireControl``, ``Search``, ``MissileGun``, ``Decoration``,
#: ``DCharge``, or ``None``) folds into ``accessories[]``.
SPECIES_TO_SECTION: dict[str, str] = {
    "Main": "turrets",
    "Secondary": "secondaries",
    "AAircraft": "antiair",
    "Torpedo": "torpedoes",
}

#: Inverse lookup, useful for build callers that hand us section names.
SECTION_TO_SPECIES: dict[str, str] = {v: k for k, v in SPECIES_TO_SECTION.items()}

#: All placement sections, in document order.
PLACEMENT_SECTIONS: tuple[str, ...] = (
    "turrets",
    "secondaries",
    "antiair",
    "torpedoes",
    "accessories",
)

#: Ship-class codes we recognise. ``CL``/``CA``/``BC``/``CB`` distinguish
#: cruiser sub-types, ``SS`` = submarine, ``AUX`` = auxiliary.
VALID_SHIP_CLASSES: tuple[str, ...] = (
    "DD", "CL", "CA", "BB", "CV", "BC", "CB", "SS", "AUX",
)

#: Canonical hitbox vocabulary used across classes. DDs add ``torpedoes`` /
#: ``depthcharges``; CVs add ``flightdeck`` / ``hangar`` / ``auxiliaryroom``;
#: SSes add ``over_citadel`` / ``sonar``. See ``reference/class_pattern_matrix.md``.
#: ``steeringgear`` is the non-typo canonical form — the raw WG data uses
#: ``stearinggear`` / ``ruder`` / ``SteeringGear``, all mapped here via
#: :data:`HITBOX_TOKEN_MAP`.
UNIVERSAL_HITBOX_ZONES: tuple[str, ...] = (
    "bow",
    "stern",
    "superstructure",
    "engine",
    "steeringgear",
    "citadel",
    "casemate",
    "antiaircraft",
)

#: Raw ``CM_SB_*`` splash-box tokens (or toolkit-corrected equivalents) →
#: our normalized zone name. Any token not listed here passes through as-is.
#:
#: - ``ruder``: German for "rudder"; the splash file uses the German root.
#: - ``stearinggear``: the raw GameParams ``hitLocations`` token (WG typo).
#: - ``SteeringGear``: the toolkit's English-corrected form.
#:   All three of the above normalise to ``steeringgear`` (correct spelling)
#:   at the sidecar output.
#: - ``ss`` / ``cit`` / ``cas``: short forms in splash data.
#: - ``gk``: German "Geschützkasten" (gun box / barbette) — one per main
#:   turret, a new sub-zone exposed by the toolkit migration.
#: - ``fdck`` / ``hang`` / ``aux`` / ``cas_hang`` / ``ssc``: CV-specific
#:   tokens. Verified on Essex (2026-04-26 probe). See
#:   ``reference/cv_ss_pipeline_scoping.md``.
#: - ``ovcit`` / ``sonar``: SS-specific tokens. Verified on U-2501.
HITBOX_TOKEN_MAP: dict[str, str] = {
    "ruder": "steeringgear",
    "stearinggear": "steeringgear",
    "SteeringGear": "steeringgear",
    "ss": "superstructure",
    "ssc": "superstructure",   # CV: structure-conning sub-volume; same damage class
    "cit": "citadel",
    "cas": "casemate",
    "cas_hang": "casemate",    # CV: casemate-hangar combo; treat as casemate for damage purposes
    "gk": "citadel",  # falls into citadel unless caller tracks barbette sub-zone
    "engine": "engine",
    "bow": "bow",
    "stern": "stern",
    "aa": "antiaircraft",
    "aaircraft": "antiaircraft",
    # CV-specific (Essex/Shinano) — substitute for main-battery/magazine zones
    "fdck": "flightdeck",
    "hang": "hangar",
    "aux": "auxiliaryroom",
    # SS-specific (U-2501)
    "ovcit": "over_citadel",
    "sonar": "sonar",
}

#: Valid shader intents (carry-over from v1).
VALID_SHADER_INTENTS: tuple[str, ...] = (
    "opaque_pbr",
    "transparent",
    "cutout",
    "emissive",
    "water_surface",
    "decal",
    "self_illum",
)

#: Pipeline stage numbers. 0 = toolkit export; 1 = organise; 2 = armor/hitbox;
#: 3 = textures; 4 = turret rig (pre-toolkit only); 5 = materials; 6 = sidecar
#: + FBX; 7 = accessories library (once-per-patch, not per-ship).
VALID_STAGES: tuple[int, ...] = tuple(range(8))

# ---------------------------------------------------------------------------
# Canonical key order — controls on-disk layout for diffability.
# ---------------------------------------------------------------------------

_TOP_LEVEL_ORDER: tuple[str, ...] = (
    "schema_version",
    "pipeline",
    "ship",
    "variants",
    # v3.2: per-hull-tier mount snapshots. Each ``ShipUpgradeInfo._Hull``
    # entry contributes one record carrying its own turrets / secondaries /
    # antiair / torpedoes / accessories lists. The top-level placement
    # arrays (``turrets`` / ``secondaries`` / ...) below remain the
    # active-hull alias so existing consumers keep working unchanged.
    # See ``make_hull_entry`` and ``absorb_per_hull_placements``.
    "hulls",
    "geometry",
    "armor",
    "hitbox",
    "turrets",
    "secondaries",
    "antiair",
    "torpedoes",
    "accessories",
    "ballistics",
    "materials",
    "skins",
    # v3 (additive, 2026-05-29): indexable Exterior overlays on the canonical
    # base ship — each entry is one WG Exterior (permoflage) carrying a resolved
    # per-mount mesh-swap delta + a cross-link into skins[] for its paint scheme.
    # Mirrors WG's ``Vehicle -> permoflages[] of Exteriors``. Sibling to skins[];
    # DISTINCT from ``variants`` (hull/module/research). Emitting this collapses
    # the legacy ``<Base>__<Variant>`` separate-ship folders into one ship.
    # Schema stays 3 — the key is purely additive (strict consumers ignore it).
    # See docs/SHIP_EXTERIOR_UNIFICATION_HANDOFF.md + resolve/exterior_unify.py.
    "exteriors",
)

_PIPELINE_ORDER: tuple[str, ...] = (
    "version",
    "exported_at",
    "exported_by",
    "dcc_version",
    "toolkit_version",
    "stages_completed",
    "tool_commits",
)

_SHIP_ORDER: tuple[str, ...] = (
    "ship_key",
    "display_name",
    "wg_asset_id",
    "wg_ship_id",
    # Full GameParams entity ID (``PASB018_Iowa_1944``) — added v3.1 for
    # the gameparams-driven autofill passes that key off the full ID.
    "wg_ship_full_id",
    "wg_numeric_id",
    "nation",
    "class",
    "tier",
    # Catalogue-friendly typed metadata pulled straight from the Vehicle
    # GameParams root. ``archetype`` (BB_Mid / CA_Spammer / …) drives AI
    # pairing + UI tags; ``peculiarity`` / ``peculiarity_flag`` flag
    # special-edition variants (``azurlane``, ``sabaton``, ``al_us``);
    # ``paper_ship`` is a service-history filter.
    "archetype",
    "peculiarity",
    "peculiarity_flag",
    "paper_ship",
    "displacement_t",
    "hp_rated",
    "service_date",
)

_GEOMETRY_ORDER: tuple[str, ...] = ("bounds", "hull", "simhull_path")

_ARMOR_ORDER: tuple[str, ...] = (
    "source_glb",
    "class_canonical",
    "plate_count",
    "triangles",
    "zones",
    "materials_table",
    # Per-mount armor pulled from GameParams ``A_*.<group>.HP_*.armor`` —
    # ``{HP_AGM_1: {material_id: thickness_mm}}``. Lets the downstream
    # consumer resolve turret/barbette penetration without having to
    # invent a separate mounted-armor data path.
    "mount_armor",
    # ``A_Hull.barbettes`` (``{HP_AGM_*: [material_id, …]}``). Pairs with
    # ``hitbox.boxes.CM_SB_gk_*.owner_hp`` for shell → barbette → mount
    # damage attribution.
    "barbettes",
    "hidden_zones",
)

_HITBOX_ORDER: tuple[str, ...] = (
    "source_glb",
    "region_count",
    "regions",
    # GameParams-derived per-cube classification:
    # ``boxes[<CM_SB_*>] = {section, hl_type, parent_hl[, owner_hp]}``.
    # ``section`` is the GameParams-internal short name (``Bow``/``Cit``
    # /``SS``/``SG``/``Ammo_1``/…) for hull-side cubes, or the lowercase
    # group name (``artillery``/``atba``/``torpedoes``/…) for per-mount
    # cubes (turret barbettes etc.). Replaces the "all gk_* falls into
    # citadel" heuristic with WG-authoritative mapping.
    "boxes",
    # Per-section damage-state numbers, keyed by the same short-name
    # vocabulary as ``boxes[*].section`` for hull-side hit-locations:
    # ``hit_locations.Bow = {max_hp, regen_part, parent_hl, hl_type, …}``.
    # Drives Phase F state machine + repair-party math.
    "hit_locations",
)

_PLACEMENT_ORDER: tuple[str, ...] = (
    # Common placement fields (every typed section + accessories share this).
    "instance_id",
    "asset_id",
    "dead_asset_id",
    "hp_name",
    # Hull section (Bow / MidFront / MidBack / Stern / Full) the placement
    # rides on. For HP_-bound mounts it's read from the .visual file's node
    # tree (toolkit). For legacy/skel_ext-sourced decoratives it's resolved
    # by mesh-AABB overlap against the hull GLB's section meshes
    # (skel_ext_resolve.py). Drives Phase E sinking transform parenting.
    "parent_section",
    # Specific hull mesh the placement visually rests on, e.g.
    # `Bow_DeckHouseShape` or `Bow_patch_MidFront_DeckHouseShape`. Drives
    # per-variant visibility: when a damage state hides this mesh
    # (intact ↔ patch ↔ crack toggles), the placement hides too. May be
    # null for placements outside the hull AABBs or when the asset is
    # missing from the accessory library.
    "parent_mesh",
    "scope",
    "category",
    "subcategory",
    "species",
    "transform",
    # Turret-family extras (main/secondary/AA/torpedo). Absent on pure
    # accessories entries.
    "display_name",
    "caliber_mm",
    "barrel_count",
    # Per-mount link into ``ballistics.shells`` — names of Projectile
    # GameParams loadable by this mount, in declared order. Empty / absent
    # for non-firing mounts (directors, finders, radars, decoratives).
    "ammo_ids",
    "ammo_types",
    "dispersion",
    "yaw_range_deg",
    "elev_range_deg",
    # Fire-arc dead zones — wedges inside the traverse range where the mount
    # can rotate but won't fire (points at the ship's own structure). Lists
    # of [start_deg, end_deg] pairs, same frame as yaw_range_deg.
    "yaw_dead_zones_deg",
    "pitch_dead_zones_deg",
    "traverse_rate",
    "elev_rate",
    "reload_s",
    # AA-specific.
    "aa_range_km",
    "aa_dps",
    # Torpedo-specific.
    "tube_count",
    "shoot_sector_deg",
    "additional_aim_sector_deg",
    "torpedo_angles_deg",
    # Hand-authored.
    "attach_to",
    "casts_shadow",
)

_MATERIAL_ORDER: tuple[str, ...] = (
    "material_id",
    "display_name",
    "shader_intent",
    "render_queue",
    "double_sided",
    "mesh_slots",
    # v3: scheme-keyed texture sets. `texture_sets["main"]` is the default
    # appearance; `texture_sets["camo_01"]`, `texture_sets["camo_01_B"]`,
    # `texture_sets["dead"]`, etc. are per-skin overrides. Slots absent from
    # a non-main scheme inherit from main at render time. Replaces v2's
    # flat `textures: {slot: {...}}` field.
    "texture_sets",
    "factors",
    "uv_channels",
)

_SKIN_ORDER: tuple[str, ...] = (
    "skin_id",
    "display_name",
    # v3: which `materials[i].texture_sets[<scheme_key>]` block to sample
    # when the skin is material-scheme backed. Official WG camos may instead
    # bind from `categories` / `mat_textures`; consumers then inherit `main`
    # PBR slots and use `scheme_key` as provenance.
    "scheme_key",
    "camo_pattern",
    # v3: subvariant identifier within a `camo_pattern` (e.g. "B" / "G")
    # — colour rolls of the same base pattern.
    "color_roll",
    "tier_unlock",
    "source",
    # v3.2 skin packs: per-library-asset texture overrides for accessory
    # meshes (turrets, directors, AA, etc.). Lets a player skin pack ship
    # custom textures for a vanilla mesh without mutating the shared
    # accessory library. Shape:
    #     "asset_overrides": {
    #         "AGM034_16in50_Mk7": {
    #             "verdict": "uv_stable",     # from compare_skin_meshes
    #             "texture_sets": {
    #                 "main": { "baseColor": {"dds_mips": [...]}, ... }
    #             }
    #         },
    #         "AD001_Director_Mk37": {
    #             "verdict": "mismatched",
    #             "skip_reason": "mod re-meshed; UV layout differs",
    #             "fallback": "vanilla"
    #         }
    #     }
    "asset_overrides",
    "overrides",
)

#: Per-asset-override entry shape (under ``Skin.asset_overrides[<asset_id>]``).
_ASSET_OVERRIDE_ORDER: tuple[str, ...] = (
    "verdict",
    "skip_reason",
    "fallback",
    "texture_sets",
)

_TRANSFORM_ORDER: tuple[str, ...] = ("matrix", "position")

#: Per-exterior entry key order (additive, 2026-05-29). One entry per WG
#: Exterior (permoflage) in ``exteriors[]``. ``exterior_id`` is the index key
#: (the WG Exterior param-name); ``camo_scheme_key`` cross-links into this
#: ship's ``skins[].scheme_key`` so the same selector flips geometry + paint;
#: ``hull`` is null when the hull mesh is shared (a HullDelta only when a
#: genuine ``/ship/`` swap exists). See resolve/exterior_unify.py.
_EXTERIOR_ORDER: tuple[str, ...] = (
    "exterior_id",
    "display_name",
    "wg_asset_id",
    "species",
    "peculiarity",
    "is_native",
    "camo_scheme_key",
    "hull",
    "swap_table",
    "mounts",
    "variant_swapped_asset_ids",
)

#: Per-mount swap key order (entries of ``exteriors[].mounts[]``).
#: ``transform`` and ``misc_filter`` are stored VERBATIM (the schema_v6
#: Ry180-baked matrix + the nodesConfig miscFilter override) — neither is
#: reconstructable from the base mount. ``attached_y_flip`` is diagnostic only;
#: consumers do NOT re-apply Ry180.
_MOUNT_SWAP_ORDER: tuple[str, ...] = (
    "hp_name",
    "base_asset_id",
    "asset_id",
    "dead_asset_id",
    "transform",
    "misc_filter",
    "attached_y_flip",
)

#: Top-level ``variants`` section keys (schema v3.1).
_VARIANTS_ORDER: tuple[str, ...] = (
    "active_hull",
    "stock_hull",
    "research_path",
    "next_ships",
    "modules",
)

#: Per-hull entry key order (schema v3.2). One such entry per hull name in
#: the ``hulls`` dict. Stats are intentionally minimal — the diff between
#: tiers (HP, rudder time, burn-node timing) is what makes the entry useful;
#: full GameParams stats remain in the source dump.
_HULL_ENTRY_ORDER: tuple[str, ...] = (
    "module_id",            # PAUH802_Baltimore_1948
    "is_stock",             # bool — True iff prev == "" in ShipUpgradeInfo
    "is_active",            # bool — matches variants.active_hull
    "stats",                # dict, see _HULL_STATS_ORDER
    "turrets",
    "secondaries",
    "antiair",
    "torpedoes",
    "accessories",
)

#: Per-hull stats subset. Survival- and movement-relevant numbers only;
#: every value is float-or-null. Per-zone HPs go under ``zone_hp``
#: keyed by the GameParams hit-zone token (``Hull`` / ``Bow`` / ``SS`` /
#: ``Ammo_1`` / ``Ammo_2`` / ``SG`` / ``St`` / ``SSC`` / ``Engine`` /
#: …) — keys are passed through verbatim because the inventory varies
#: by class and we don't want to drop fields we don't recognise yet.
_HULL_STATS_ORDER: tuple[str, ...] = (
    "health",
    "rudder_time_s",
    "burn_node_time_s",
    "zone_hp",
)

#: Top-level ``ballistics`` section keys, in document order. ``source`` records
#: the toolkit version + which game build the data was extracted from;
#: ``ranges`` carries aggregate per-hull battery / detection ranges; ``shells``
#: maps gun-fired ``ammo_id`` → shell profile; ``torpedoes`` maps each
#: torpedo ``ammo_id`` → torpedo profile (split out from ``shells`` in
#: schema v3.1 — PAPT* projectiles have a fundamentally different field
#: set from PAPA* shells, so co-locating them in one dict forced every
#: torpedo entry to carry ~12 null gun fields).
_BALLISTICS_ORDER: tuple[str, ...] = (
    "source",
    "ranges",
    "shells",
    "torpedoes",
)

#: Aggregate-range subsection inside ``ballistics``. All values are floats
#: (or null when WG didn't provide one — e.g. ``torpedo_max_m`` on a
#: torpedoless ship).
_RANGES_ORDER: tuple[str, ...] = (
    "main_battery_m",
    "secondary_battery_m",
    "torpedo_max_m",
    "detection_km",
    "air_detection_km",
)

#: Per-shell entry order inside ``ballistics.shells[<ammo_id>]``. Mirrors the
#: toolkit's ``Projectile`` field set verbatim — see ``wowsunpack ammo`` for
#: the source contract. AP/HE/SAP shells emit ``null`` for fields that don't
#: apply (mass/velocity for torpedoes; ricochet/krupp for HE-style); the key
#: is always present so downstream consumers see a stable schema per shell.
_SHELL_ORDER: tuple[str, ...] = (
    "ammo_type",
    "caliber_mm",
    "mass_kg",
    "muzzle_velocity_mps",
    "air_drag_coefficient",
    "krupp",
    "cap",
    "cap_normalize_max_deg",
    "fuze_arming_threshold_mm",
    "fuze_delay_s",
    "ricochet_min_deg",
    "ricochet_always_deg",
    "alpha_damage",
    "alpha_piercing_he_mm",
    "alpha_piercing_cs_mm",
    "burn_probability",
    "max_range_m",
)

#: Per-torpedo entry order inside ``ballistics.torpedoes[<ammo_id>]``.
#: Toolkit-emitted prefix (``ammo_type`` / ``caliber_mm`` / ``alpha_damage``
#: / ``alpha_piercing_he_mm`` / ``max_range_m``) is shared with shell
#: profiles; the rest comes from the GameParams autofill pass and is
#: torpedo-only (speed / depth / fuze / debuff flags / detection
#: coefficient / PTZ interaction).
_TORPEDO_PROFILE_ORDER: tuple[str, ...] = (
    "ammo_type",
    "caliber_mm",
    "alpha_damage",
    "alpha_piercing_he_mm",
    "max_range_m",
    "speed_kts",
    "running_depth_m",
    "arming_time_s",
    "flood_capable",
    "is_deep_water",
    "with_parachute",
    "visibility_factor",
    "splash_armor_coeff",
    "splash_radius_m",
    "alert_distance_m",
    "affected_by_ptz",
    "burn_probability",
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SidecarSchemaError(ValueError):
    """Raised for sidecars that don't conform to v2 schema.

    Most commonly raised by :func:`read` when it sees a v1 document (which
    we refuse to auto-migrate — v1 ships regenerate through the new
    pipeline).
    """


DDS_MIP_SUFFIXES: tuple[str, ...] = (".dd0", ".dd1", ".dd2", ".dds")


# MFM-stem suffixes the toolkit strips when resolving the actual VFS
# texture name (mirrors `MFM_STRIP_SUFFIXES` in the Rust `texture.rs`).
# A turret's MFM might be `AGM034_..._skinned.mfm` but its albedo DDS is
# authored as `AGM034_..._a.dd0` in the VFS. The glTF PNG keeps the full
# MFM stem for output naming; the raw DDS keeps WG's verbatim filename.
_MFM_STRIP_SUFFIXES = ("_skinned", "_wire", "_dead", "_blaze", "_alpha")

# Channel suffixes on glTF PNG output (from `create_textured_material`).
# Maps the glTF slot suffix to its WG DDS channel equivalent. BaseColor
# is special: the glTF PNG has no suffix, but WG's DDS carries `_a`.
_CHANNEL_SUFFIX_MAP: tuple[tuple[str, str], ...] = (
    ("_n",  "_n"),    # normal
    ("_mg", "_mg"),   # metallic-roughness
    ("_ao", "_ao"),   # occlusion
)
