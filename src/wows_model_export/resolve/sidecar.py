"""WoWS ship pipeline — sidecar schema v3 library.

Lifted from ``tools/ship/sidecar.py``. This is the schema authority —
the canonical schema + writer for ``<Ship>.meta.json`` sidecar files
produced by the ship pipeline. Thin shims in
:mod:`wows_model_export.read.sidecar` and
:mod:`wows_model_export.compose.sidecar` re-export the public read /
write surfaces; all implementation lives here.

Pure Python 3.10+ stdlib. No bpy. Safely importable from Blender scripts,
standalone CLI tools, and Unity-side cross-validation scripts.

The ``SCHEMA_VERSION`` constant below is authoritative — see
``tools/contracts/METADATA_SPEC.md`` for the full §1.2 evolution table
(v1 → v2 → v3 → v3.1 → v3.2). v1 and v2 documents are rejected on read
with a clear ``SidecarSchemaError``; ships must regenerate through
``scaffold_ship.py`` to land on v3.

Target state: see ``tools/toolkit_integration/ARCHITECTURE.md``.
Full spec:    see ``tools/contracts/METADATA_SPEC.md``.

Design goals (mirror the spec):
  - Deterministic output: stable key order, 2-space indent, LF newlines,
    trailing newline. Re-exporting with no authoring change produces a
    byte-identical file.
  - Merge-preserving: keyed by stable IDs (``instance_id`` for placements,
    ``material_id`` / ``skin_id`` for registries). Re-running the automated
    build pass only overwrites fields explicitly passed; hand-authored
    extras like ``attach_to`` / ``casts_shadow`` / custom ``ammo_types``
    survive round-trips.
  - Typed sections: ``turrets[]``, ``secondaries[]``, ``antiair[]``,
    ``torpedoes[]``, ``accessories[]``. Every placement entry shares a
    common shape; typed sections add category-specific fields.
  - No Blender, no third-party deps. Stdlib + typing only.

Stearinggear typo note: the raw WoWS data uses ``stearinggear``
(canonical sic in GameParams hitLocations) and ``ruder`` (German root
in .splash files); the toolkit internally English-corrects both to
``SteeringGear``. At the *sidecar output canonical* layer we go the
rest of the way — all three raw forms normalise to the non-typo
``steeringgear`` (matching the lowercase single-word pattern of the
other canonical zones). Raw WG data sources are still left as-is upstream
so parsers continue to match the game's source data. See
``HITBOX_TOKEN_MAP``.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema version + file-system conventions
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3
SIDECAR_SUFFIX = ".meta.json"

# Per-ship subdirectory names — kept centralised so renaming them later
# (e.g. `gamemodels3d` → `models` in 2026-04-23) is a one-line change.
# The MODELS subdir holds the toolkit-exported hull GLB, placements +
# accessories JSON, and the raw DDS mip chain that Unity streams. The
# LEGACY_MODELS subdir holds the per-ship gamemodels3d.com visual.glb
# (used by `skel_ext_resolve.py` to recover decorative placements) plus
# the scanner's `*_accessories_scan.json` output.
MODELS_SUBDIR = "models"
LEGACY_MODELS_SUBDIR = "legacy_models"

# FBX custom-property keys — the three keys Unity reads from the
# ``_PipelineMetadata`` empty to locate + validate the sidecar.


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
)

_PIPELINE_ORDER: tuple[str, ...] = (
    "version",
    "exported_at",
    "exported_by",
    "blender_version",
    "toolkit_version",
    "stages_completed",
    "tool_commits",
    "native_scale_m",
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
    # ``{HP_AGM_1: {material_id: thickness_mm}}``. Lets Unity resolve
    # turret/barbette penetration without having to invent a separate
    # mounted-armor data path.
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
    "sigma",
    "yaw_range_deg",
    "elev_range_deg",
    "traverse_rate",
    "elev_rate",
    "reload_s",
    # AA-specific.
    "aa_range_km",
    "aa_dps",
    # Torpedo-specific.
    "tube_count",
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
    # v3: which `materials[i].texture_sets[<scheme_key>]` block to sample.
    # `"main"` for the default skin; `"camo_01"` / `"camo_01_B"` /
    # `"dead"` etc. for camo + damage variants.
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


# ---------------------------------------------------------------------------
# Factory functions — each returns a plain dict in spec shape.
# ---------------------------------------------------------------------------


def make_pipeline(
    *,
    version: str | None = None,
    stages_completed: Iterable[int] = (),
    blender_version: str = "",
    toolkit_version: str = "",
    native_scale_m: float = 1.0,
    tool_commits: dict[str, str] | None = None,
    exported_by: str | None = None,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``pipeline`` section.

    ``native_scale_m`` defaults to ``1.0`` for the new pipeline: the toolkit
    emits metric-scaled placements and Blender exports FBX with
    ``global_scale=15`` so the sidecar is in metres. Set to ``15.0`` only if
    you're exporting the FBX in native WoWS units and letting Unity apply
    the 15× at import.
    """
    return {
        "version": version or _today_iso_date(),
        "exported_at": exported_at or _now_iso(),
        "exported_by": exported_by or _default_exporter(),
        "blender_version": blender_version,
        "toolkit_version": toolkit_version,
        "stages_completed": sorted({int(s) for s in stages_completed}),
        "tool_commits": dict(tool_commits or {}),
        "native_scale_m": float(native_scale_m),
    }


def make_ship(
    *,
    ship_key: str,
    display_name: str | None = None,
    wg_asset_id: str | None = None,
    wg_ship_id: str | None = None,
    wg_ship_full_id: str | None = None,
    wg_numeric_id: int | None = None,
    nation: str | None = None,
    cls: str | None = None,
    tier: int | None = None,
    archetype: str | None = None,
    peculiarity: str | None = None,
    peculiarity_flag: str | None = None,
    paper_ship: bool | None = None,
    displacement_t: int | None = None,
    hp_rated: int | None = None,
    service_date: str | None = None,
) -> dict[str, Any]:
    """Build a ``ship`` section.

    ``ship_key`` is the stable player-facing handle (shared by all skins of
    one ship). ``wg_asset_id`` is WG's per-variant asset handle (lowercased
    res_unpack dir name, e.g. ``asb017_montana_1945``). ``wg_ship_id`` is
    the bare GameParams param_index (``PASB018``); ``wg_ship_full_id`` is
    the full entity key (``PASB018_Iowa_1944``) for callers that index
    GameParams.json directly.
    """
    if not ship_key:
        raise ValueError("ship_key is required")
    if cls is not None and cls not in VALID_SHIP_CLASSES:
        # Don't reject — downstream tools may tolerate unknown classes.
        pass
    out: dict[str, Any] = {"ship_key": ship_key}
    if display_name is not None:
        out["display_name"] = display_name
    if wg_asset_id is not None:
        out["wg_asset_id"] = wg_asset_id
    if wg_ship_id is not None:
        out["wg_ship_id"] = wg_ship_id
    if wg_ship_full_id is not None:
        out["wg_ship_full_id"] = wg_ship_full_id
    if wg_numeric_id is not None:
        out["wg_numeric_id"] = int(wg_numeric_id)
    if nation is not None:
        out["nation"] = nation
    if cls is not None:
        out["class"] = cls
    if tier is not None:
        out["tier"] = int(tier)
    if archetype is not None and archetype:
        out["archetype"] = archetype
    if peculiarity is not None and peculiarity:
        out["peculiarity"] = peculiarity
    if peculiarity_flag is not None and peculiarity_flag:
        out["peculiarity_flag"] = peculiarity_flag
    if paper_ship is True:
        out["paper_ship"] = True
    if displacement_t is not None:
        out["displacement_t"] = int(displacement_t)
    if hp_rated is not None:
        out["hp_rated"] = int(hp_rated)
    if service_date is not None:
        out["service_date"] = service_date
    return out


def make_variants(
    *,
    active_hull: str | None = None,
    stock_hull: str | None = None,
    research_path: Iterable[str] = (),
    next_ships: Iterable[str] = (),
    modules: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build the top-level ``variants`` section (schema v3.1).

    Captures the ship's upgrade tree as read from
    ``Vehicle.ShipUpgradeInfo``: which hull the sidecar's stats come from
    (``active_hull``), the corresponding stock entry (``stock_hull``), the
    research path stock → upgraded, the ``nextShips`` references on the
    chain end, and the per-ucType module chains (``_Hull`` / ``_Engine`` /
    ``_Suo`` / ``_Artillery`` / ``_Torpedoes``).
    """
    return {
        "active_hull":   active_hull,
        "stock_hull":    stock_hull,
        "research_path": list(research_path),
        "next_ships":    list(next_ships),
        "modules":       dict(modules or {}),
    }


def make_hull_entry(
    *,
    module_id: str | None = None,
    is_stock: bool = False,
    is_active: bool = False,
    stats: dict[str, Any] | None = None,
    turrets: Iterable[dict[str, Any]] = (),
    secondaries: Iterable[dict[str, Any]] = (),
    antiair: Iterable[dict[str, Any]] = (),
    torpedoes: Iterable[dict[str, Any]] = (),
    accessories: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build one entry of the ``hulls`` block (schema v3.2).

    ``module_id`` is the ``ShipUpgradeInfo`` entry name (``PAUH802_…``).
    ``is_stock`` mirrors the chain-root rule (``prev == ""``); ``is_active``
    is ``True`` for the hull whose top-level placement lists alias to here.
    The placement lists carry the same per-instance shape as the top-level
    sections — see ``absorb_per_hull_placements`` for how they're populated
    from a per-hull export-ship JSON.
    """
    return {
        "module_id":   module_id,
        "is_stock":    bool(is_stock),
        "is_active":   bool(is_active),
        "stats":       dict(stats or {}),
        "turrets":     list(turrets or []),
        "secondaries": list(secondaries or []),
        "antiair":     list(antiair or []),
        "torpedoes":   list(torpedoes or []),
        "accessories": list(accessories or []),
    }


def make_hull_stats(
    *,
    health: float | None = None,
    rudder_time_s: float | None = None,
    burn_node_time_s: float | None = None,
    zone_hp: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the ``stats`` sub-block of a hull entry. All fields nullable."""
    out: dict[str, Any] = {}
    if health is not None:
        out["health"] = float(health)
    if rudder_time_s is not None:
        out["rudder_time_s"] = float(rudder_time_s)
    if burn_node_time_s is not None:
        out["burn_node_time_s"] = float(burn_node_time_s)
    if zone_hp:
        out["zone_hp"] = {k: float(v) for k, v in zone_hp.items()}
    return out


def make_geometry(
    *,
    length_m: float = 0.0,
    beam_m: float = 0.0,
    height_m: float = 0.0,
    draft_m: float = 0.0,
    waterline_y: float = 0.0,
    keel_y: float = 0.0,
    pivot_offset: Iterable[float] = (0.0, 0.0, 0.0),
    simhull_path: str | None = None,
) -> dict[str, Any]:
    """Build a ``geometry`` section."""
    return {
        "bounds": {
            "length_m": float(length_m),
            "beam_m": float(beam_m),
            "height_m": float(height_m),
        },
        "hull": {
            "waterline_y": float(waterline_y),
            "keel_y": float(keel_y),
            "draft_m": float(draft_m),
            "pivot_offset": [float(v) for v in pivot_offset],
        },
        "simhull_path": simhull_path,
    }


def make_armor(
    *,
    source_glb: str | None = None,
    class_canonical: bool = True,
    plate_count: int = 0,
    triangles: int = 0,
    zones: dict[str, dict[str, Any]] | None = None,
    materials_table: dict[str, dict[str, Any]] | None = None,
    mount_armor: dict[str, dict[str, float]] | None = None,
    barbettes: dict[str, list[str]] | None = None,
    hidden_zones: Iterable[str] = (),
) -> dict[str, Any]:
    """Build an ``armor`` section.

    ``zones`` is keyed by canonical zone name
    (``citadel`` / ``casemate`` / ``superstructure`` / ``bow`` / ``stern`` /
    ``steeringgear``); values carry
    ``{default_thickness_mm, max_thickness_mm, plate_count}``.

    ``materials_table`` is keyed by the integer-as-string ``material_id``
    the toolkit emits per armor triangle; values carry
    ``{thickness_mm, layers, zones[, hidden]}``. Unity resolves
    ``RaycastHit.triangleIndex`` → per-vertex ``_MATERIAL_ID`` → this
    table at runtime.

    ``mount_armor`` is keyed by hardpoint name (``HP_AGM_1``); values are
    ``{material_id_str: thickness_mm}`` from GameParams ``A_*.HP_*.armor``.
    ``barbettes`` is ``{HP_AGM_*: [material_id_str, …]}`` from
    ``A_Hull.barbettes`` — pairs with ``hitbox.boxes.CM_SB_gk_*.owner_hp``
    for shell-into-barbette damage attribution.
    """
    return {
        "source_glb": source_glb,
        "class_canonical": bool(class_canonical),
        "plate_count": int(plate_count),
        "triangles": int(triangles),
        "zones": _normalise_zone_dict(zones or {}),
        "materials_table": _normalise_materials_table(materials_table or {}),
        "mount_armor": dict(mount_armor or {}),
        "barbettes": dict(barbettes or {}),
        "hidden_zones": [normalise_hitbox_token(z) for z in hidden_zones],
    }


def make_hitbox(
    *,
    source_glb: str | None = None,
    region_count: int = 0,
    regions: dict[str, dict[str, Any]] | None = None,
    boxes: dict[str, dict[str, Any]] | None = None,
    hit_locations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``hitbox`` section.

    ``regions`` is keyed by canonical zone name; values carry
    ``{box_count[, raw_name]}``. ``raw_name`` is populated when the
    canonical token differs from the raw splash token (e.g.
    ``ruder → steeringgear``) so the alias is inspectable.

    ``boxes`` is the GameParams-driven per-cube classification (added in
    schema v3.1): ``{CM_SB_<name>: {section, hl_type, parent_hl[, owner_hp]}}``.
    Empty when GameParams isn't available — callers fall back to the raw
    ``regions`` summary.

    ``hit_locations`` is the per-section damage-state dict, keyed by the
    GameParams short-section name (``Bow``/``Cit``/``SS``/``SG``/…) and
    valued by ``{max_hp, regen_part, parent_hl, hl_type[, …]}``.
    """
    regs: dict[str, dict[str, Any]] = {}
    for zone, info in (regions or {}).items():
        entry = dict(info)
        entry.setdefault("box_count", 0)
        regs[zone] = entry
    return {
        "source_glb": source_glb,
        "region_count": int(region_count),
        "regions": regs,
        "boxes": dict(boxes or {}),
        "hit_locations": dict(hit_locations or {}),
    }


# -- Placement factories ----------------------------------------------------

def _make_placement_common(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    category: str,
    subcategory: str | None,
    transform: dict[str, Any],
    attach_to: str | None,
    casts_shadow: bool,
) -> dict[str, Any]:
    if not instance_id:
        raise ValueError("instance_id is required")
    if not asset_id:
        raise ValueError("asset_id is required")
    if not scope:
        raise ValueError("scope is required")
    if not category:
        raise ValueError("category is required")
    return {
        "instance_id": instance_id,
        "asset_id": asset_id,
        "hp_name": hp_name,
        "scope": scope,
        "category": category,
        "subcategory": subcategory,
        "transform": _normalise_transform(transform),
        "attach_to": attach_to,
        "casts_shadow": bool(casts_shadow),
    }


def _add_gameplay_fields(
    out: dict[str, Any],
    *,
    display_name: str | None,
    caliber_mm: int | None,
    barrel_count: int | None,
    ammo_types: Iterable[str] | None,
    sigma: float | None,
    yaw_range_deg: Iterable[float] | None,
    elev_range_deg: Iterable[float] | None,
    traverse_rate: float | None,
    elev_rate: float | None,
    reload_s: float | None,
) -> None:
    if display_name is not None:
        out["display_name"] = display_name
    if caliber_mm is not None:
        out["caliber_mm"] = int(caliber_mm)
    if barrel_count is not None:
        out["barrel_count"] = int(barrel_count)
    if ammo_types is not None:
        out["ammo_types"] = list(ammo_types)
    if sigma is not None:
        out["sigma"] = float(sigma)
    if yaw_range_deg is not None:
        out["yaw_range_deg"] = [float(v) for v in yaw_range_deg]
    if elev_range_deg is not None:
        out["elev_range_deg"] = [float(v) for v in elev_range_deg]
    if traverse_rate is not None:
        out["traverse_rate"] = float(traverse_rate)
    if elev_rate is not None:
        out["elev_rate"] = float(elev_rate)
    if reload_s is not None:
        out["reload_s"] = float(reload_s)


def make_turret(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    transform: dict[str, Any],
    category: str = "gun",
    subcategory: str | None = "main",
    display_name: str | None = None,
    caliber_mm: int | None = None,
    barrel_count: int | None = None,
    ammo_types: Iterable[str] | None = None,
    sigma: float | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    elev_range_deg: Iterable[float] | None = None,
    traverse_rate: float | None = None,
    elev_rate: float | None = None,
    reload_s: float | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``turrets[]`` (main battery)."""
    out = _make_placement_common(
        instance_id=instance_id, asset_id=asset_id, hp_name=hp_name,
        scope=scope, category=category, subcategory=subcategory,
        transform=transform, attach_to=attach_to, casts_shadow=casts_shadow,
    )
    _add_gameplay_fields(
        out,
        display_name=display_name, caliber_mm=caliber_mm,
        barrel_count=barrel_count, ammo_types=ammo_types, sigma=sigma,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        traverse_rate=traverse_rate, elev_rate=elev_rate, reload_s=reload_s,
    )
    return out


def make_secondary(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    transform: dict[str, Any],
    category: str = "gun",
    subcategory: str | None = "secondary",
    display_name: str | None = None,
    caliber_mm: int | None = None,
    barrel_count: int | None = None,
    ammo_types: Iterable[str] | None = None,
    sigma: float | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    elev_range_deg: Iterable[float] | None = None,
    traverse_rate: float | None = None,
    elev_rate: float | None = None,
    reload_s: float | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``secondaries[]`` (secondary battery)."""
    out = _make_placement_common(
        instance_id=instance_id, asset_id=asset_id, hp_name=hp_name,
        scope=scope, category=category, subcategory=subcategory,
        transform=transform, attach_to=attach_to, casts_shadow=casts_shadow,
    )
    _add_gameplay_fields(
        out,
        display_name=display_name, caliber_mm=caliber_mm,
        barrel_count=barrel_count, ammo_types=ammo_types, sigma=sigma,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        traverse_rate=traverse_rate, elev_rate=elev_rate, reload_s=reload_s,
    )
    return out


def make_antiair(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    transform: dict[str, Any],
    category: str = "gun",
    subcategory: str | None = "aaircraft",
    display_name: str | None = None,
    caliber_mm: int | None = None,
    barrel_count: int | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    elev_range_deg: Iterable[float] | None = None,
    traverse_rate: float | None = None,
    elev_rate: float | None = None,
    aa_range_km: float | None = None,
    aa_dps: float | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``antiair[]`` (AA mount).

    AA mounts add ``aa_range_km`` + ``aa_dps`` for aura math. Standard
    gun-gameplay fields (``ammo_types``, ``reload_s``, ``sigma``) are
    omitted by convention — auto-AA doesn't expose them to players.
    """
    out = _make_placement_common(
        instance_id=instance_id, asset_id=asset_id, hp_name=hp_name,
        scope=scope, category=category, subcategory=subcategory,
        transform=transform, attach_to=attach_to, casts_shadow=casts_shadow,
    )
    _add_gameplay_fields(
        out,
        display_name=display_name, caliber_mm=caliber_mm,
        barrel_count=barrel_count,
        ammo_types=None, sigma=None,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        traverse_rate=traverse_rate, elev_rate=elev_rate, reload_s=None,
    )
    if aa_range_km is not None:
        out["aa_range_km"] = float(aa_range_km)
    if aa_dps is not None:
        out["aa_dps"] = float(aa_dps)
    return out


def make_torpedo(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    transform: dict[str, Any],
    category: str = "torpedo",
    subcategory: str | None = "torpedo",
    display_name: str | None = None,
    tube_count: int | None = None,
    reload_s: float | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    traverse_rate: float | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``torpedoes[]`` (torpedo tube mount)."""
    out = _make_placement_common(
        instance_id=instance_id, asset_id=asset_id, hp_name=hp_name,
        scope=scope, category=category, subcategory=subcategory,
        transform=transform, attach_to=attach_to, casts_shadow=casts_shadow,
    )
    if display_name is not None:
        out["display_name"] = display_name
    if tube_count is not None:
        out["tube_count"] = int(tube_count)
    if reload_s is not None:
        out["reload_s"] = float(reload_s)
    if yaw_range_deg is not None:
        out["yaw_range_deg"] = [float(v) for v in yaw_range_deg]
    if traverse_rate is not None:
        out["traverse_rate"] = float(traverse_rate)
    return out


def make_accessory(
    *,
    instance_id: str,
    asset_id: str,
    hp_name: str | None,
    scope: str,
    transform: dict[str, Any],
    category: str = "misc",
    subcategory: str | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``accessories[]`` (everything non-gun — directors,
    radar, catapults, rangefinders, bollards, vents, hatches, misc).

    Minimum placement shape. No gameplay fields. ``attach_to`` parents the
    accessory under a turret's Yaw transform at Unity import time (Phase 2
    feature) — always preserved across merges, never auto-set.
    """
    return _make_placement_common(
        instance_id=instance_id, asset_id=asset_id, hp_name=hp_name,
        scope=scope, category=category, subcategory=subcategory,
        transform=transform, attach_to=attach_to, casts_shadow=casts_shadow,
    )


# -- Materials + skins ------------------------------------------------------

def make_material(
    *,
    material_id: str,
    display_name: str | None = None,
    shader_intent: str = "opaque_pbr",
    render_queue: str = "opaque",
    double_sided: bool = False,
    mesh_slots: Iterable[dict[str, Any]] | None = None,
    texture_sets: dict[str, dict[str, Any]] | None = None,
    uv_channels: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build an entry for ``materials[]``.

    ``texture_sets`` is a dict-of-dicts keyed by scheme name (``"main"``,
    ``"camo_01"``, ``"camo_01_B"``, ``"dead"``, ...). Each scheme value is
    a slot manifest like ``{"baseColor": {"dds_mips": [...]}}``. Slots
    absent from a non-main scheme inherit from ``texture_sets["main"]`` at
    render time. ``texture_sets["main"]`` should be present for any
    material that has any textures bound at all.
    """
    if not material_id:
        raise ValueError("material_id is required")
    return {
        "material_id": material_id,
        "display_name": display_name or material_id,
        "shader_intent": shader_intent,
        "render_queue": render_queue,
        "double_sided": bool(double_sided),
        "mesh_slots": list(mesh_slots or []),
        "texture_sets": dict(texture_sets or {}),
        "uv_channels": dict(uv_channels or {}),
    }


def _glb_json_chunk(glb_path: str | Path) -> dict[str, Any]:
    """Extract and parse the JSON chunk from a GLB file.

    Minimal standalone reader (no dependency on the glTF SDK) so this module
    stays stdlib-only.
    """
    import struct
    with open(glb_path, "rb") as f:
        magic = f.read(4)
        if magic != b"glTF":
            raise ValueError(f"{glb_path}: not a GLB file")
        _version, _total = struct.unpack("<II", f.read(8))
        json_len, json_type = struct.unpack("<II", f.read(8))
        if json_type != 0x4E4F534A:
            raise ValueError(f"{glb_path}: first chunk is not JSON")
        return json.loads(f.read(json_len).decode("utf-8"))


# Ordered glTF mip-suffix preferences for WG's split-mip DDS convention.
# `.dd0` = top mip (highest resolution); `.dd1`, `.dd2` are intermediate;
# `.dds` is the bundled low-res mip tail. Unity consumers pick the best
# available set for their streaming quality tier. Ordering is high-to-low.
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


def _png_stem_to_dds_candidates(png_stem: str) -> list[str]:
    """Given a glTF PNG stem (no extension), produce DDS-side stem candidates.

    The toolkit's PNG output uses MFM-stem naming; the DDS dump uses the
    VFS texture filename verbatim. They differ in two ways:

    - **MFM suffix stripping** — PNG has `_skinned`/`_dead`/etc; DDS often
      strips them (`AGM034_..._skinned` MFM → `AGM034_..._a.dd0` DDS).
    - **Albedo-slot suffix** — for standard PBS materials the DDS has
      `_a` (`_..._a.dd0`); for CAMO variants the DDS has no `_a`
      (`_..._camo_01.dd0`). BaseColor slot must try both.

    Returns a list of stem candidates to try in order; first one whose
    mip files exist on disk wins.
    """
    # Identify the channel suffix (if any).
    channel_suffix = ""
    base = png_stem
    for png_sfx, dds_sfx in _CHANNEL_SUFFIX_MAP:
        if base.endswith(png_sfx):
            base = base[: -len(png_sfx)]
            channel_suffix = dds_sfx
            break

    # Enumerate bases. The MFM marker (`_skinned`/`_wire`/etc) lives at
    # the end for simple PBS slots (`foo_skinned_n`) but in the middle
    # for camo variants (`foo_skinned_camo_01_B`). We try both.
    bases = [base]
    for strip in _MFM_STRIP_SUFFIXES:
        # End-of-stem strip.
        if base.endswith(strip):
            bases.append(base[: -len(strip)])
        # Mid-stem strip: remove `{strip}_` anywhere it occurs (e.g.
        # `AGM034_..._skinned_camo_01_B` → `AGM034_..._camo_01_B`).
        token = strip + "_"
        if token in base:
            bases.append(base.replace(token, "_", 1).lstrip("_"))
            # Also try removing the FIRST occurrence + also the leading `_` it left.
            idx = base.find(token)
            if idx >= 0:
                variant = base[:idx] + base[idx + len(strip):]
                if variant != base and variant not in bases:
                    bases.append(variant)

    cands: list[str] = []
    for b in bases:
        if channel_suffix:
            # Non-baseColor: stem is just base + channel.
            cands.append(f"{b}{channel_suffix}")
        else:
            # BaseColor: try `_a` suffix first (standard PBS), then bare
            # (camo variants don't use `_a`).
            cands.append(f"{b}_a")
            cands.append(f"{b}")
    return cands


def _texture_manifest_for_slot(
    png_name: str | None,
    textures_dir: Path | None,
    textures_dds_dir: Path | None,
    *,
    png_uri_prefix: str = "textures/",
    dds_uri_prefix: str = "textures_dds/",
) -> dict[str, Any] | None:
    """Given a glTF image URI (e.g. ``textures/Foo_skinned_n.png``), build the
    per-slot texture manifest with relative paths to both the PNG and every
    available raw DDS mip level.

    Returns ``None`` when nothing resolves.
    """
    if not png_name:
        return None
    filename = Path(png_name).name
    png_stem = filename.rsplit(".", 1)[0]

    out: dict[str, Any] = {}
    if textures_dir is not None:
        png_path = textures_dir / filename
        if png_path.is_file():
            out["png"] = f"{png_uri_prefix}{filename}"

    # WG's raw DDS can live under a different stem than the PNG because
    # the toolkit's PNG export keeps the full MFM stem while the DDS
    # dump keeps the VFS filename. Try each candidate stem in order
    # until one produces mip files.
    if textures_dds_dir is not None:
        for dds_stem in _png_stem_to_dds_candidates(png_stem):
            dds_mips: list[str] = []
            for suffix in DDS_MIP_SUFFIXES:
                candidate = textures_dds_dir / f"{dds_stem}{suffix}"
                if candidate.is_file():
                    dds_mips.append(f"{dds_uri_prefix}{dds_stem}{suffix}")
            if dds_mips:
                out["dds_mips"] = dds_mips
                break  # first-hit wins (longest match if needed)

    return out or None


def texture_sets_from_dir(
    textures_dds_dir: str | Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> dict[str, dict[str, list[str]]]:
    """Enumerate DDS files in a directory and group them into a structured
    per-variant, per-slot texture manifest.

    The pipeline emits WG DDS files with canonical naming:

        <asset-stem>[<_variant>][<_camo_scheme>][<_channel>].<dd0|dd1|dd2|dds>

        where:
          variant   ∈ {"", "_dead"}                     (intact or destroyed)
          scheme    ∈ {"", "_camo_01", "_camo_01_B", …} (camo variant, if any)
          channel   ∈ {"_a", "_n", "_mg", "_ao"}        (PBR slot)
                      or omitted for camo base (which has no _a)

    We group on two dimensions:
      * **variant key** — what the ship looks like right now. ``"main"`` is
        the default intact + no-camo appearance. ``"dead"`` is the
        destroyed mesh's default appearance. ``"camo_01"`` /
        ``"camo_01_B"`` / etc. are per-camouflage overrides.
      * **slot name** — ``baseColor`` / ``normal`` / ``metallicRoughness``
        / ``occlusion``, matching the sidecar `materials[].textures` keys.

    Returns a dict shaped:

    .. code-block:: json

        {
          "main":      { "baseColor": ["<uri>", "<uri>", ...],
                         "normal":    [...], "metallicRoughness": [...],
                         "occlusion": [...] },
          "dead":      { "baseColor": [...], ... },
          "camo_01":   { "baseColor": [...] },
          "camo_01_B": { "baseColor": [...] }
        }

    Each slot's value is the mip-chain path list in priority order (top
    mip first). Consumers pick the top mip or let their importer stream
    through the chain.

    Returns an empty dict if the directory doesn't exist.
    """
    d = Path(textures_dds_dir)
    if not d.is_dir():
        return {}

    # Gather all DDS files, grouped by "stem" (filename minus the mip suffix).
    per_stem: dict[str, list[str]] = {}
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        stem: str | None = None
        for sfx in DDS_MIP_SUFFIXES:
            if name.endswith(sfx):
                stem = name[: -len(sfx)]
                break
        if stem is None:
            continue
        per_stem.setdefault(stem, []).append(f"{dds_uri_prefix}{name}")

    # Sort each stem's mips by DDS_MIP_SUFFIXES order (so the `.dd0` top mip
    # is always first). The paths already end in a suffix from the known set.
    for _stem, paths in per_stem.items():
        def mip_rank(p: str) -> int:
            for i, sfx in enumerate(DDS_MIP_SUFFIXES):
                if p.endswith(sfx):
                    return i
            return len(DDS_MIP_SUFFIXES)
        paths.sort(key=mip_rank)

    # Group stems into (variant, slot) using the shared classifier from
    # `_classify_dds_filename`. That function understands the full suffix
    # vocabulary including the toolkit-emitted conformant siblings
    # (`_normal`, `_mr`, `_nbmask`) and routes WG originals (`_n`, `_mg`)
    # to internal "raw" slot names that the finalisation step
    # (`_promote_legacy_raw_slots`) collapses.
    sets: dict[str, dict[str, list[str]]] = {}
    for stem, paths in per_stem.items():
        # `_classify_dds_filename` strips the channel suffix, normalises
        # the camo / dead scheme key, and returns (asset_base, scheme,
        # slot). We don't need asset_base here (the directory boundary
        # already isolates one asset's stems).
        _asset_base, scheme, slot = _classify_dds_filename(stem)
        sets.setdefault(scheme, {}).setdefault(slot, []).extend(paths)

    # Apply the same conformant-sibling-wins finalisation as the per-ship
    # binder. After this, raw slots (`_normalRawOrMgRaw_*`) are gone:
    # promoted to the canonical name when no conformant sibling is on
    # disk, dropped when one exists.
    for scheme_slots in sets.values():
        _promote_legacy_raw_slots_for_dir(scheme_slots)

    return sets


def _promote_legacy_raw_slots_for_dir(slots: dict[str, list[str]]) -> None:
    """Variant of [`_promote_legacy_raw_slots`] for the
    [`texture_sets_from_dir`] manifest shape (slot → list[str] of mip
    URIs, not slot → {dds_mips: [...]}). Same behaviour matrix.
    """
    for raw_slot in [s for s in slots if s.startswith(_LEGACY_RAW_SLOT_PREFIX)]:
        canonical = raw_slot[len(_LEGACY_RAW_SLOT_PREFIX):]
        raw_value = slots.pop(raw_slot)
        if canonical and canonical not in slots:
            slots[canonical] = raw_value


def materials_from_glb(
    glb_path: str | Path,
    *,
    textures_dir: str | Path | None = None,
    textures_dds_dir: str | Path | None = None,
    material_mappings_json: str | Path | None = None,
    png_uri_prefix: str = "textures/",
    dds_uri_prefix: str = "textures_dds/",
) -> list[dict[str, Any]]:
    """Extract an authoritative per-material texture manifest from a GLB +
    its sibling texture directories.

    The glTF file is read only for material names + PBR slot → image URI
    mappings + scalar factors + alpha/cull state. Each slot's manifest
    then carries BOTH the PNG reference (what the glTF spec allows) AND
    the raw WG DDS mip chain (for Unity Texture Streaming).

    Consumers (Unity's AccessoryLibraryImporter, ShipAccessoryRig) read
    this manifest and build materials programmatically instead of parsing
    the glTF's internal material section. That makes the Unity side
    robust against glTF-importer quirks and gives every mesh access to
    the full BC-compressed mip pyramid.

    Returns a list of material entries shaped per :func:`make_material`,
    with ``textures`` populated as a dict of slot → manifest:

    .. code-block:: json

        {
          "material_id":   "TL2_SHIPMAT_PBS_Gun_skinned",
          "display_name":  "TL2_SHIPMAT_PBS_Gun_skinned",
          "shader_intent": "opaque_pbr",
          "render_queue":  "opaque",
          "double_sided":  false,
          "mesh_slots":    [],
          "textures": {
            "baseColor": {
              "png": "textures/Foo_a.png",
              "dds_mips": ["textures_dds/Foo_a.dd0", "textures_dds/Foo_a.dd1",
                           "textures_dds/Foo_a.dd2", "textures_dds/Foo_a.dds"]
            },
            "normal":            { ... },
            "metallicRoughness": { ... },
            "occlusion":         { ... }
          },
          "factors": {
            "baseColor":         [1.0, 1.0, 1.0, 1.0],
            "metallic":          1.0,
            "roughness":         1.0,
            "emissive":          [0.0, 0.0, 0.0]
          },
          "uv_channels": {}
        }
    """
    doc = _glb_json_chunk(glb_path)
    tex_dir = Path(textures_dir) if textures_dir is not None else None
    dds_dir = Path(textures_dds_dir) if textures_dds_dir is not None else None

    gltf_mats = doc.get("materials", []) or []
    gltf_texs = doc.get("textures", []) or []
    gltf_imgs = doc.get("images", []) or []

    def tex_uri(slot_ref: dict[str, Any] | None) -> str | None:
        """Resolve a glTF material slot (`{"index": N, "texCoord": 0, ...}`)
        to the image's URI field. Returns None if unresolvable."""
        if not slot_ref or "index" not in slot_ref:
            return None
        tex_idx = slot_ref["index"]
        if tex_idx >= len(gltf_texs):
            return None
        img_idx = gltf_texs[tex_idx].get("source")
        if img_idx is None or img_idx >= len(gltf_imgs):
            return None
        return gltf_imgs[img_idx].get("uri")

    out: list[dict[str, Any]] = []
    for mat in gltf_mats:
        name = mat.get("name") or f"material_{len(out)}"
        pbr = mat.get("pbrMetallicRoughness", {})

        # Resolve each slot → image URI → sidecar texture manifest.
        slots: dict[str, dict[str, Any]] = {}
        for slot_name, slot_ref in (
            ("baseColor", pbr.get("baseColorTexture")),
            ("metallicRoughness", pbr.get("metallicRoughnessTexture")),
            ("normal", mat.get("normalTexture")),
            ("occlusion", mat.get("occlusionTexture")),
            ("emissive", mat.get("emissiveTexture")),
        ):
            uri = tex_uri(slot_ref)
            if not uri:
                continue
            manifest = _texture_manifest_for_slot(
                uri, tex_dir, dds_dir,
                png_uri_prefix=png_uri_prefix,
                dds_uri_prefix=dds_uri_prefix,
            )
            if manifest:
                slots[slot_name] = manifest

        factors: dict[str, Any] = {}
        if "baseColorFactor" in pbr:
            factors["baseColor"] = [float(v) for v in pbr["baseColorFactor"]]
        if "metallicFactor" in pbr:
            factors["metallic"] = float(pbr["metallicFactor"])
        if "roughnessFactor" in pbr:
            factors["roughness"] = float(pbr["roughnessFactor"])
        if "emissiveFactor" in mat:
            factors["emissive"] = [float(v) for v in mat["emissiveFactor"]]

        # Derive a schema-valid shader_intent from the glTF alpha mode.
        # VALID_SHADER_INTENTS constrains the output vocabulary.
        alpha_mode = mat.get("alphaMode", "OPAQUE")
        if alpha_mode == "BLEND":
            shader_intent = "transparent"
            render_queue = "transparent"
        elif alpha_mode == "MASK":
            shader_intent = "cutout"
            render_queue = "cutout"
        else:
            shader_intent = "opaque_pbr"
            render_queue = "opaque"

        # v3: wrap the resolved slots under `texture_sets["main"]`. Per-camo
        # variants are added by `_bind_dds_textures_by_name()` below.
        entry = {
            "material_id": name,
            "display_name": name,
            "shader_intent": shader_intent,
            "render_queue": render_queue,
            "double_sided": bool(mat.get("doubleSided", False)),
            "mesh_slots": [],
            "texture_sets": {"main": slots} if slots else {},
            "factors": factors,
            "uv_channels": {},
        }
        out.append(entry)

    # DDS-only mode: when the glTF has no texture references, populate
    # `texture_sets[*]` from the on-disk DDS dir. Two passes:
    #
    #   1. Toolkit-emitted material_mappings.json (deterministic, when
    #      available). Pre-fills `texture_sets["main"]` from authoritative
    #      .mfm material descriptors — every material identifier maps to
    #      a definite mfm_stem regardless of WG's filename quirks.
    #
    #   2. Filename-heuristic resolver (`_bind_dds_textures_by_name`).
    #      Catches materials the deterministic pass didn't cover (older
    #      ships exported pre-toolkit-feature, library shaders not in
    #      .visual chain, ...) AND fills in per-camo + dead variants on
    #      every material (the JSON only carries the main appearance —
    #      camos are a disk-side discovery).
    #
    # Either pass alone is functional; running both gives best coverage.
    if dds_dir is not None and any(not m.get("texture_sets", {}).get("main") for m in out):
        if material_mappings_json is not None:
            _apply_material_mappings_json(
                out, material_mappings_json, dds_dir,
                dds_uri_prefix=dds_uri_prefix,
            )
        unresolved = _bind_dds_textures_by_name(out, dds_dir, dds_uri_prefix=dds_uri_prefix)
        # Surface opaque-PBR / cutout materials neither resolver could find
        # a stem for — they're almost always new WG-library material variants
        # we haven't taught `_LIBRARY_MATERIAL_STEMS` about yet. Hitboxes and
        # armor materials are `shader_intent: transparent` so they don't reach
        # the resolver in the first place and correctly don't show up here.
        if unresolved:
            import sys
            uniq = sorted(set(unresolved))
            print(
                f"[sidecar] WARNING: {len(uniq)} material(s) unresolved — "
                f"no matching DDS stem. These will render untextured in Unity: "
                f"{', '.join(uniq)}",
                file=sys.stderr,
            )

    return out


# -- DDS stem ↔ material name resolver ---------------------------------------
#
# glTF material names follow WG's convention `<SHADER>_PBS_<Part>`:
#   SHIPMAT_PBS_Hull        → base ship texture (no _<Part> suffix in DDS)
#   SHIPMAT_PBS_DeckHouse   → base stem + "_Deckhouse" (case-insensitive)
#   SHIPWIRE_PBS_Hull       → dead/wireframe variant of the hull
#   SHIPMAT_PBS_Crack       → shared-across-ships damage-interior texture.
#                             WG's asset library stores this as `CxxxRazlom*`
#                             (Russian "разлом" = cleavage / fracture) —
#                             Montana ships `C002_Razlom`, Baltimore ships
#                             `C003_Razlom_old`. The `_HINT_ALIASES` table
#                             below maps the material-name hint to the
#                             DDS-stem substring we should look for.
#   armor_*, hitbox_*       → vertex-colored, no PBR textures
#
# Some ships (Iowa → `TL2_SHIPMAT_PBS_Hull`, Fletcher → `TL2_SHIPWIRE_PBS_Hull`)
# prefix the shader with `TL<N>_`. Observed values so far are TL2 only; the
# prefix appears to tag a texture-level / quality variant but the material
# semantics and the DDS stem layout are identical to the un-prefixed form,
# so we strip it before matching. Without stripping, the hull bindings
# silently disappear for those ships.
#
# We index the DDS directory by stem (filename minus mip suffix minus channel
# suffix), then for each opaque_pbr material extract a "part hint" from its
# name and look up the matching stem.

_SHADER_PREFIXES = (
    # Order matters: longest prefix first so SHIPMAT_EMISSIVE_PBS_ matches
    # before the bare SHIPMAT_PBS_ would (it wouldn't, but defensively).
    "SHIPMAT_EMISSIVE_PBS_", "shipmat_emissive_pbs_",
    "SHIPMAT_PBS_", "SHIPWIRE_PBS_", "shipmat_pbs_", "shipwire_pbs_",
)

# When the direct `<base_stem>_<hint>` / `_<hint>` match fails, try each alias
# substring in order. Match is lowercased + substring-in-stem; first match
# wins. Keys are lowercased hint strings (e.g. "crack" for SHIPMAT_PBS_Crack).
_HINT_ALIASES: dict[str, tuple[str, ...]] = {
    "crack": ("razlom",),
    # Atago: WG names the deckhouse DDS `Deck_house` (snake_case) instead of
    # the `Deckhouse` (run-together) used by every other ship. Without this
    # alias the SHIPMAT_PBS_DeckHouse material on Atago resolves to no stem
    # and ships with empty texture_sets.
    "deckhouse": ("deck_house",),
    # Myoko: SHIPMAT_PBS_Bulge is bound to ~50 hull mesh primitives (the
    # Bow_BuglesShape / MidFront_BuglesShape "torpedo-blister" sections
    # all along the lower hull), but its textures are named ``_bools_*``
    # instead of ``_bulge_*`` — Russian-derived "бугель" via "bools".
    # Additionally, the textures use the JSC032_Myoko_1941 hull prefix
    # (the 1941 ancestor) on every Myoko variant: WG ships these once and
    # reuses them across every Myoko hull tier (1945, ARP). Without the
    # ``bools`` alias the Bulge material binds to no stem and the
    # underwater hull renders as solid grey.
    "bulge": ("bools",),
}

# Library-material aliases. WG ships several "shared" materials that are not
# per-ship (don't use the SHIPMAT_/SHIPWIRE_ shader prefix) — their MFMs live
# under `content/gameplay/common/textures/` and the DDS stems don't include
# any ship-specific tokens. Key is a lowercase prefix that must appear at the
# start of the material name; value is (stem_tokens, shader_intent). First
# prefix-match wins, then first stem-token that resolves. When a library
# material resolves here, we bind whatever channels the stem exposes
# (typically only baseColor — these are usually single-map alpha-tested
# shaders like nets and glass panes) AND override the material's
# shader_intent + render_queue based on the entry's classification, because
# the source GLB flags them all as OPAQUE even though the underlying WG
# shader (`assets.bin shader_id=0x00010000`) needs alpha handling.
#
# Observed so far:
#   GRID_Misc (Fletcher)  → MFM C008_Grid_5_alpha      → stem contains "grid"
#   GRID_Misc (Baltimore) → MFM C001_Net_alpha         → stem contains "net"
#   SHIPGLASS_PBS_Hull    → MFM transparent_glass_alpha → stem contains "glass"
# The same material name can point at different library MFMs across ships;
# the tuple of fallback tokens captures that spread.
_LIBRARY_MATERIAL_STEMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # name_prefix,  stem_tokens,       shader_intent
    ("grid_",       ("grid", "net"),   "cutout"),       # nets: alpha-tested
    ("shipglass_",  ("glass",),        "transparent"),  # glass: alpha-blended
)

# Shared-library token list used by base-stem ranking + permoflage
# fallback resolver to skip stems that aren't ship-owned (damage
# interior, glass, nets, defaults, transparent surfaces). Mirror of
# `_HINT_ALIASES` keys + `_LIBRARY_MATERIAL_STEMS` tokens.
_SHARED_LIBRARY_TOKENS: tuple[str, ...] = (
    "razlom", "glass", "grid", "net", "default", "transparent",
)


def _is_library_stem(stem: str) -> bool:
    """True if ``stem`` matches a known shared-library token (damage
    interior / glass / nets / defaults). Used to keep permoflage
    fallback search from latching onto cross-ship texture sources."""
    return any(t in stem for t in _SHARED_LIBRARY_TOKENS)

# Channel suffix → glTF slot name. Order matters in two ways:
#
# 1. Longer/more-specific suffixes must come first so we don't strip the
#    wrong one. `_normal` must be checked before `_n`, `_mr` before `_mg`,
#    `_nbmask` is unique.
# 2. WG-original `_n` / `_mg` files map to "raw" slot names that the
#    finalisation step (`_promote_legacy_raw_slots`) demotes/promotes
#    based on whether the conformant sibling is present. Keeps backwards
#    compat: a ship extracted with the pre-2026-04-30 toolkit still binds
#    correctly via the WG originals; a ship extracted with the new
#    toolkit binds the conformant siblings and ignores the originals.
#
# Conformant siblings (toolkit-emitted, glTF-spec):
#   `_normal` → tangent-space normal map (B = reconstructed Z)
#   `_nbmask` → camo no-camo-region mask (BC4 single-channel)
#   `_mr`     → metallic-roughness (G = roughness, B = metallic)
#
# WG originals (kept for archaeology / RE — see
# `tools/reference/shared/texture_conventions.md`):
#   `_n`      → carries categorical mask in B (NOT Z) — wrong for shading
#   `_mg`     → R cavity / G metallic / B gloss — non-glTF channel order
_DDS_CHANNEL_TO_SLOT: tuple[tuple[str, str], ...] = (
    ("_emissive", "emissive"),    # synthesized by tools/shared/synth_emission.py
    ("_normal",   "normal"),
    ("_nbmask",   "camoMask"),
    ("_mr",       "metallicRoughness"),
    ("_mg",       "_normalRawOrMgRaw_metallicRoughness"),  # demoted in finalisation
    ("_ao",       "occlusion"),
    ("_n",        "_normalRawOrMgRaw_normal"),              # demoted in finalisation
    ("_a",        "baseColor"),
)

# Slots whose name starts with this prefix are treated as legacy-raw and
# get promoted to their canonical slot name in finalisation **only if**
# the canonical slot is empty (i.e. no conformant sibling shipped).
_LEGACY_RAW_SLOT_PREFIX = "_normalRawOrMgRaw_"


_YEAR_TOKEN_RE = re.compile(r"_\d{4}(?=_|$)")


def _strip_year_token(stem: str) -> str:
    """Drop a `_<4digit>` year token (boundaries: `_` or end-of-string)
    anywhere in the stem.

    WG names some camo files without the year suffix that the base
    albedo files carry. E.g. Baltimore ships ``ASC017_Baltimore_1944_a.dd0``
    but ``ASC017_Baltimore_camo_01.dd0`` — both refer to the same hull
    but the camo path drops `_1944`. Canonicalising stems with this
    helper lets the per-scheme grouping line both files up under one
    material. Yamato + Iowa carry `_1945`/`_1944` consistently across
    both files, so stripping is a uniform no-op for them. Montana has
    no year in its texture stems, so stripping is also a no-op.

    The regex matches a 4-digit token at a `_` boundary, so 2-digit
    calibres (`16in50`, `Mk32`) and 5-digit IDs (`AB12345`) don't fire.
    """
    return _YEAR_TOKEN_RE.sub("", stem, count=1)


def _classify_dds_filename(stem_and_channel: str) -> tuple[str, str, str]:
    """Decompose a DDS filename (without mip suffix) into
    ``(material_stem, scheme_key, slot)``.

    Examples:
        ``ASB017_Montana_a``                     → ("asb017_montana", "main",            "baseColor")
        ``ASB017_Montana_n``                     → ("asb017_montana", "main",            "normal")
        ``ASB017_Montana_camo_01``               → ("asb017_montana", "camo_01",         "baseColor")
        ``ASB017_Montana_camo_01_B``             → ("asb017_montana", "camo_01_B",       "baseColor")
        ``ASB017_Montana_Deckhouse_camo_01``     → ("asb017_montana_deckhouse", "camo_01", "baseColor")
        ``ASB017_Montana_dead_a``                → ("asb017_montana", "dead",            "baseColor")
        ``ASB017_Montana_dead_camo_01``          → ("asb017_montana", "dead_camo_01",    "baseColor")
        ``ASC017_Baltimore_1944_a``              → ("asc017_baltimore", "main",          "baseColor")  ← year stripped
        ``ASC017_Baltimore_camo_01``             → ("asc017_baltimore", "camo_01",       "baseColor")  ← already without year

    Camo variants don't carry an `_a` channel suffix in WG's naming
    (they're authored as the baseColor directly), so the channel
    detection loop comes BEFORE the camo/dead detection. The `_<year>`
    token is stripped from the material stem at the end so WG's
    inconsistent year-suffix authoring (e.g. Baltimore) doesn't
    fragment the stem index.
    """
    rest = stem_and_channel

    # 1. Trailing channel suffix (only present on `_a`/`_n`/`_mg`/`_ao`
    #    files; camo variants without a channel default to baseColor).
    slot = "baseColor"
    for chan, slot_name in _DDS_CHANNEL_TO_SLOT:
        if rest.endswith(chan):
            slot = slot_name
            rest = rest[: -len(chan)]
            break

    # 2. Camo scheme — `_camo_<rest>` token. Everything after `_camo_` is
    #    the scheme suffix (e.g. "01", "01_B", "Halloween20").
    scheme = "main"
    low = rest.lower()
    if "_camo_" in low:
        idx = low.index("_camo_")
        camo_suffix = rest[idx + len("_camo_"):]
        scheme = f"camo_{camo_suffix}"
        rest = rest[:idx]
        low = rest.lower()

    # 3. Dead variant. Can stack with camo (e.g. `_dead_camo_01_B`).
    if low.endswith("_dead"):
        rest = rest[: -len("_dead")]
        scheme = "dead" if scheme == "main" else f"dead_{scheme}"

    # 4. Strip year token so WG's inconsistent year-suffixing doesn't
    #    fragment the stem index. Mirrors the Rust toolkit's
    #    `texture_base_names` helper.
    rest = _strip_year_token(rest)

    return rest.lower(), scheme, slot


_MFM_PROP_TO_PBR_SLOTS: dict[str, tuple[str, ...]] = {
    # MFM property → canonical PBR slots that come from that texture's
    # channel family. Walking per-MFM-property lets mesh-swap variants
    # (where each slot can come from a different stem) bind correctly:
    # ARP Takao Red's Hull material has diffuse=JSC508_Red_Arpeggio,
    # mg=JSC507_Arpeggio (Blue inheritance), normal+ao=JSC038_Atago (base).
    #
    # `_n` files carry both shading normal AND camo no-camo-region mask
    # (the conformant `_normal` + `_nbmask` siblings split them); we pull
    # all three slots from the normalMap's stem so camoMask follows the
    # base ship's UV layout.
    #
    # `_normalRawOrMgRaw_*` slots are populated only when the toolkit
    # extracted WG-original `_n` / `_mg` without conformant siblings;
    # `_promote_legacy_raw_slots` (called in finalisation) collapses them
    # onto the canonical name.
    "diffuseMap":           ("baseColor",),
    "normalMap":            ("normal", "_normalRawOrMgRaw_normal", "camoMask"),
    "metallicGlossMap":     ("metallicRoughness", "_normalRawOrMgRaw_metallicRoughness"),
    "ambientOcclusionMap":  ("occlusion",),
}


def _apply_material_mappings_json(
    materials: list[dict[str, Any]],
    material_mappings_json: str | Path,
    textures_dds_dir: Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> int:
    """Pre-fill ``materials[*].texture_sets["main"]`` from the toolkit's
    deterministic material → texture-stem mapping JSON.

    The JSON's ``materials[*].textures`` dict carries one entry per MFM
    property (``diffuseMap`` / ``normalMap`` / ``metallicGlossMap`` /
    ``ambientOcclusionMap``) with the exact ``stem`` and ``vfs_path``
    WG resolved for that slot. For mesh-swap permoflage variants (Takao
    Arpeggio, ARP Takao Red, Iowa AzurLane, …), each slot can point at a
    different source stem — the variant ships only a partial texture
    set and inherits the rest from the base ship + a sibling variant.
    Walking per-slot (not per-material-mfm_stem) is the only way to
    honour that inheritance correctly.

    For each material:
      1. Look up the matching ``materials[]`` entry by
         ``material_identifier`` (case + TL-prefix tolerant).
      2. Skip if the entry's ``mfm_stem`` is a shared library stem
         (C002_Razlom etc.) — those are handled by the heuristic's
         ``_LIBRARY_MATERIAL_STEMS`` table.
      3. For each MFM property we recognise, take the per-slot ``stem``,
         normalise (lowercase + ``_strip_year_token``), and pull the
         canonical PBR slots from the dds_index built earlier.
      4. Synthesised emissive is keyed off the diffuseMap stem (synth
         writes ``<diffuse_stem>_emissive.dd?`` in
         ``textures_dds_dir``); we look up ``emissive`` under that stem.

    Mutates ``materials`` in place. Returns the count of materials that
    got a main appearance from this pass. Materials whose stems are
    library / unrecognised / missing DDS on disk are left untouched and
    fall through to the heuristic resolver, which still adds per-camo /
    dead schemes from disk.

    See ``write_material_mappings_json`` in the toolkit's
    ``crates/wowsunpack/src/export/ship.rs`` for the JSON shape.
    """
    import sys
    from collections import defaultdict

    p = Path(material_mappings_json)
    if not p.is_file():
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[sidecar] WARNING: material_mappings_json {p} unreadable ({e}); "
            f"falling back to heuristic resolver only",
            file=sys.stderr,
        )
        return 0

    # Build index: material_identifier (lowercase, post TL\d+_ strip) →
    # list of full entry dicts. Each glTF material name maps to ONE or
    # MORE entries (one per sub-model the same material is used in).
    # We pick the first non-library entry; the per-slot stems are read
    # from that entry's ``textures`` dict.
    by_ident: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in doc.get("materials", []):
        ident = (entry.get("material_identifier") or "").strip()
        if not ident:
            continue
        # Drop optional TL\d+_ prefix to align with glTF material names
        # that ship pre-stripped (Iowa: TL2_SHIPMAT_PBS_Hull → SHIPMAT_PBS_Hull
        # if the toolkit ever stripped it). Today the toolkit emits the
        # raw identifier so we index BOTH forms.
        norm = _strip_tl_prefix(ident).lower()
        by_ident[norm].append(entry)
        if norm != ident.lower():
            by_ident[ident.lower()].append(entry)

    if not by_ident:
        return 0

    # Build a stem-channel-mip index so we can resolve `<stem>_<channel>.dd?`
    # paths without rescanning the directory per material. Key is the
    # ``_classify_dds_filename`` stem (lowercase + year-stripped); slot
    # is the canonical PBR slot name (or `_normalRawOrMgRaw_*` for raw
    # WG `_n` / `_mg` files awaiting promotion in finalisation).
    dds_index: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for f in sorted(textures_dds_dir.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        mip_sfx = next((s for s in DDS_MIP_SUFFIXES if name.endswith(s)), None)
        if mip_sfx is None:
            continue
        stem_and_channel = name[: -len(mip_sfx)]
        if "_blaze" in stem_and_channel.lower():
            continue
        # Per-scheme variants (camo_NN, dead, dead_camo_NN) are handled
        # by the heuristic resolver's `_bind_dds_textures_by_name`, which
        # buckets by scheme. The deterministic pass only covers the
        # "main" appearance — let the classifier decide. Note: a bare
        # `endswith("_dead")` check would miss `<stem>_dead_<channel>`
        # files (e.g. `JGM024_..._RF_dead_mr`), which DO classify as
        # scheme=dead but end in `_mr` not `_dead`. Trust the classifier
        # output instead of the suffix shape.
        stem, scheme, slot = _classify_dds_filename(stem_and_channel)
        if scheme != "main":
            continue
        dds_index[stem][slot].append(f"{dds_uri_prefix}{name}")

    def _mip_rank(path: str) -> int:
        for i, sfx in enumerate(DDS_MIP_SUFFIXES):
            if path.endswith(sfx):
                return i
        return len(DDS_MIP_SUFFIXES)

    for slots_ in dds_index.values():
        for paths in slots_.values():
            paths.sort(key=_mip_rank)

    def _normalise_stem(raw: str) -> tuple[str, str]:
        """Return (lower, year_stripped_lower) candidate keys for the
        given raw stem from material_mappings.json."""
        low = raw.lower()
        return low, _strip_year_token(low)

    n_resolved = 0
    for mat in materials:
        if mat.get("shader_intent") not in ("opaque_pbr", "cutout"):
            continue
        existing_main = (mat.get("texture_sets") or {}).get("main") or {}
        if existing_main:
            continue  # already populated by the GLB-driven path

        raw_id = mat.get("material_id") or ""
        norm = _strip_tl_prefix(raw_id).lower()
        entries = by_ident.get(norm) or by_ident.get(raw_id.lower()) or []
        if not entries:
            continue

        # Pick the first non-library entry. Library mfm_stems
        # (C002_Razlom — shared damage interior, transparent_glass_alpha,
        # …) are handled by the heuristic's `_LIBRARY_MATERIAL_STEMS`
        # table which also fixes up shader_intent. Letting them fall
        # through preserves that path.
        chosen: dict[str, Any] | None = None
        for entry in entries:
            mfm_stem_low = (entry.get("mfm_stem") or "").lower()
            if mfm_stem_low and _is_library_stem(mfm_stem_low):
                continue
            chosen = entry
            break
        if chosen is None:
            continue

        textures = chosen.get("textures") or {}
        if not textures:
            continue

        # Walk per-MFM-property stems and pull canonical slots.
        slots: dict[str, dict[str, list[str]]] = {}
        diffuse_keys: tuple[str, str] | None = None
        for mfm_prop, pbr_slots in _MFM_PROP_TO_PBR_SLOTS.items():
            tex = textures.get(mfm_prop) or {}
            stem_raw = (tex.get("stem") or "").strip()
            if not stem_raw:
                continue
            # Skip library / shared-default per-slot stems (e.g. an AO
            # slot pointing at "default"). The heuristic / library
            # fallbacks pick the right defaults; binding `default_ao`
            # here would just attach a generic occlusion that overrides
            # the variant's actual AO from another stem.
            if _is_library_stem(stem_raw.lower()):
                continue
            for key in _normalise_stem(stem_raw):
                if key not in dds_index:
                    continue
                slot_table = dds_index[key]
                for pbr_slot in pbr_slots:
                    if pbr_slot in slots:
                        continue  # earlier slot from this prop already won
                    paths = slot_table.get(pbr_slot)
                    if paths:
                        slots[pbr_slot] = {"dds_mips": list(paths)}
                if mfm_prop == "diffuseMap":
                    diffuse_keys = (key,) if diffuse_keys is None else diffuse_keys
                break  # stop at first matching key (raw vs. year-stripped)

        # Synthesised emissive: `tools/shared/synth_emission.py` writes
        # `<diffuse_stem>_emissive.{dd0,dds}` for any stem with a sibling
        # `*_emissive.mfm` in the VFS (ARP / Azur Lane / Sabaton crossover
        # skins). The classifier indexes those under the diffuse stem
        # keyed `emissive`. For mesh-swap variants the diffuse stem is
        # the variant's (e.g. JSC508_Red_Arpeggio), so emissive follows
        # the variant's UV automatically.
        if diffuse_keys:
            for key in diffuse_keys:
                if key in dds_index and "emissive" in dds_index[key]:
                    slots["emissive"] = {"dds_mips": list(dds_index[key]["emissive"])}
                    break
        else:
            diffuse_tex = textures.get("diffuseMap") or {}
            stem_raw = (diffuse_tex.get("stem") or "").strip()
            if stem_raw and not _is_library_stem(stem_raw.lower()):
                for key in _normalise_stem(stem_raw):
                    if key in dds_index and "emissive" in dds_index[key]:
                        slots["emissive"] = {"dds_mips": list(dds_index[key]["emissive"])}
                        break

        # Require at least baseColor before claiming we resolved the
        # material. Otherwise the heuristic should still get a shot —
        # e.g. a material whose JSON entry only lists shared `default`
        # AO + glass-token diffuse falls through cleanly.
        if "baseColor" not in slots:
            continue

        ts = mat.setdefault("texture_sets", {})
        ts["main"] = slots
        n_resolved += 1

    return n_resolved


def _strip_tl_prefix(name: str) -> str:
    """Drop a leading ``TL<N>_`` texture-level prefix (Iowa / Fletcher use
    ``TL2_SHIPMAT_PBS_Hull`` instead of bare ``SHIPMAT_PBS_Hull``). No-op
    if the prefix isn't present."""
    if len(name) > 3 and name[:2].upper() == "TL" and name[2].isdigit():
        i = 3
        while i < len(name) and name[i].isdigit():
            i += 1
        if i < len(name) and name[i] == "_":
            return name[i + 1:]
    return name


def _bind_dds_textures_by_name(
    materials: list[dict[str, Any]],
    textures_dds_dir: Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> list[str]:
    """Populate ``materials[*].texture_sets`` by matching material names to
    the on-disk DDS stems. Mutates the list in place.

    Acts on ``opaque_pbr`` / ``cutout`` materials whose ``texture_sets``
    is empty or missing its ``main`` entry. The "main" appearance is
    populated from `<stem>_a/_n/_mg/_ao` files; per-camo + dead variants
    are bucketed under their scheme key (`camo_01`, `dead`,
    `dead_camo_01`, etc.). Unresolvable materials are left untouched and
    their ``material_id`` is returned as a list; callers should log /
    act on this to surface new shared-library variants the resolver
    doesn't yet know about.
    """
    from collections import defaultdict

    unresolved: list[str] = []

    # 1. Index: stem_lower → scheme_key → slot → [mip paths sorted top-first]
    stem_index: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for f in sorted(textures_dds_dir.iterdir()):
        if not f.is_file():
            continue
        name = f.name

        # Strip mip suffix (.dd0/.dd1/.dd2/.dds).
        mip_sfx = next((s for s in DDS_MIP_SUFFIXES if name.endswith(s)), None)
        if mip_sfx is None:
            continue
        stem_and_channel = name[: -len(mip_sfx)]

        # Skip blaze (camo-on-fire variants — present on some WG ships,
        # render-time-only, not part of player-selectable skin set).
        if "_blaze" in stem_and_channel.lower():
            continue

        stem, scheme, slot = _classify_dds_filename(stem_and_channel)
        stem_index[stem][scheme][slot].append(f"{dds_uri_prefix}{name}")

    def _mip_rank(p: str) -> int:
        for i, sfx in enumerate(DDS_MIP_SUFFIXES):
            if p.endswith(sfx):
                return i
        return len(DDS_MIP_SUFFIXES)

    for schemes in stem_index.values():
        for slots in schemes.values():
            for paths in slots.values():
                paths.sort(key=_mip_rank)

    if not stem_index:
        return unresolved

    # 2. "Base stem" = hull-wide texture stem. Heuristic: among stems that
    # carry a `main`/baseColor slot (so utility-only files like `default_ao`
    # don't win), prefer the one with the most main-scheme slots; break
    # ties on fewest underscore tokens, then shortest length. That picks
    # e.g. `ASB017_Montana` over both `default` (no baseColor) and
    # `ASB017_Montana_Deckhouse` (deeper part suffix).
    #
    # Exclude shared-library tokens from the candidate pool. Stems like
    # `c002_razlom` (damage interior) carry a full slot set and would
    # otherwise outrank a permoflage variant whose own diffuse/mg+mr live
    # alongside but whose normal/AO are inherited from the base ship's
    # stem (so the variant gets only ~4 slots in its own stem). Without
    # the exclusion, every `Hull` material on a permoflage binds to
    # `c002_razlom` instead of the variant.
    candidates = [
        s for s, schemes in stem_index.items()
        if "baseColor" in schemes.get("main", {})
        and not _is_library_stem(s)
    ]
    if not candidates:
        # Fallback: if filtering left nothing (e.g. a ship that genuinely
        # only uses razlom), allow library stems back in. Better to bind
        # to a wrong stem than to leave every material untextured.
        candidates = [
            s for s, schemes in stem_index.items()
            if "baseColor" in schemes.get("main", {})
        ]
    if not candidates:
        return unresolved
    base_stem = max(
        candidates,
        key=lambda s: (
            len(stem_index[s].get("main", {})),
            -s.count("_"),
            -len(s),
        ),
    )

    # 3. Resolve each opaque material to its stem. We DON'T early-skip
    #    materials that already have `texture_sets["main"]` from a
    #    GLB-driven resolution — they still need their per-camo +
    #    `_dead` scheme variants attached from the disk scan.
    for mat in materials:
        if mat.get("shader_intent") not in ("opaque_pbr", "cutout"):
            continue
        existing_main = (mat.get("texture_sets") or {}).get("main") or {}

        raw_name = mat.get("material_id") or ""
        # Strip optional leading `TL\d+_` texture-level prefix (see header
        # comment). "TL2_SHIPMAT_PBS_Hull" → "SHIPMAT_PBS_Hull".
        name = raw_name
        if len(name) > 3 and name[:2].upper() == "TL" and name[2].isdigit():
            i = 3
            while i < len(name) and name[i].isdigit():
                i += 1
            if i < len(name) and name[i] == "_":
                name = name[i + 1:]

        part_hint: str | None = None
        for prefix in _SHADER_PREFIXES:
            if name.startswith(prefix):
                part_hint = name[len(prefix):]
                break
        if part_hint is None:
            # Not a SHIPMAT_/SHIPWIRE_ material. Before giving up, try the
            # library-material table — some shared materials (GRID_Misc,
            # SHIPGLASS_PBS_Hull, …) live outside the SHIPMAT namespace but
            # still point at DDS stems we can find.
            name_low = name.lower()
            lib_target: str | None = None
            lib_intent: str | None = None
            for lib_prefix, stem_tokens, intent in _LIBRARY_MATERIAL_STEMS:
                if name_low.startswith(lib_prefix):
                    for token in stem_tokens:
                        lib_target = next(
                            (s for s in stem_index if token in s),
                            None,
                        )
                        if lib_target is not None:
                            lib_intent = intent
                            break
                    break
            if lib_target is None:
                if not existing_main:
                    unresolved.append(raw_name)
                continue
            # Library materials are typically schemeless (one stem, one
            # scheme = main). Project the stem's per-scheme slots into
            # `texture_sets`; if WG ever ships e.g. a camo'd net, the
            # scheme keys flow through naturally. Existing GLB-derived
            # main wins for slots it already resolved; disk fills in the
            # rest.
            _merge_multi_targets(mat, [stem_index[lib_target]], existing_main)
            # Library materials ship through the GLB as alphaMode=OPAQUE even
            # though the underlying WG shader (0x00010000) is alpha-tested /
            # blended. Override based on the entry's classification so Unity
            # picks the right Standard-shader mode.
            if lib_intent is not None:
                mat["shader_intent"] = lib_intent
                mat["render_queue"]  = lib_intent
            continue

        targets = _resolve_target_stems(
            part_hint, base_stem, stem_index,
        )
        if not targets:
            if not existing_main:
                unresolved.append(raw_name)
            continue

        # Project the stems' per-scheme slots into `texture_sets`,
        # earlier stems winning per-slot. Mesh-swap permoflage variants
        # (Takao Arpeggio, Iowa AzurLane, ...) get [variant_stem,
        # base_ship_stem]; the variant carries baseColor / mg / mr / and
        # synthesised emissive on its own stem, while normal / camoMask /
        # AO are inherited from the base ship's stem. Self-contained
        # ships get a single-element list and behave as before.
        target_schemes = [stem_index[t] for t in targets if t in stem_index]
        _merge_multi_targets(mat, target_schemes, existing_main)

    # Finalisation pass: collapse legacy-raw slots (`_n` / `_mg` files)
    # onto their canonical names only when the conformant sibling is
    # absent. New extracts (with `_normal` / `_mr` siblings on disk)
    # ignore the originals; old extracts (no siblings) fall back to the
    # WG packing.
    for mat in materials:
        ts = mat.get("texture_sets") or {}
        for scheme_slots in ts.values():
            _promote_legacy_raw_slots(scheme_slots)

    return unresolved


def _promote_legacy_raw_slots(slots: dict[str, Any]) -> None:
    """In-place: for each `_normalRawOrMgRaw_<canonical>` entry, copy it
    onto `<canonical>` if `<canonical>` isn't already populated, then
    drop the raw form. Always drops the raw form even if no promotion
    occurred — raw slots are internal-only, never serialised.

    Behaviour matrix:
        canonical present, raw present  → keep canonical, drop raw  (new extract)
        canonical absent,  raw present  → promote raw → canonical   (old extract)
        canonical present, raw absent   → no-op
        canonical absent,  raw absent   → no-op
    """
    for raw_slot in [s for s in slots if s.startswith(_LEGACY_RAW_SLOT_PREFIX)]:
        canonical = raw_slot[len(_LEGACY_RAW_SLOT_PREFIX):]
        raw_value = slots.pop(raw_slot)
        if canonical and canonical not in slots:
            slots[canonical] = raw_value


def _resolve_target_stems(
    part_hint: str,
    base_stem: str,
    stem_index: dict[str, dict[str, dict[str, list[str]]]],
) -> list[str]:
    """Return an ordered list of stems contributing to one material's
    texture set. Earlier entries win per-slot.

    For self-contained ships (Iowa, Montana, Maya ARP — variants whose
    own stem carries every channel), this returns a single-element list:
    the canonical ship stem matching the part hint.

    For mesh-swap permoflages where the variant inherits some channels
    from the base ship (Takao Arpeggio's variant stem
    ``jsc507_takao_arpeggio`` ships only ``baseColor`` / ``metallicRoughness``
    / synthesised ``emissive`` — its ``normal`` / ``camoMask`` / ``occlusion``
    live on the base Atago's stem ``jsc038_atago_hull``), this returns
    ``[variant_stem, base_stem]``: the variant wins per-slot, and the
    base ship fills the gaps.

    Detection: when multiple stems match the same part hint, the one
    with ``baseColor`` in its main scheme is the variant (it owns the
    diffuse paint); the others are inherited base-ship sources.
    """
    hint_low = part_hint.lower()

    # Collect candidate stems for this part hint. Three sources, in
    # priority order:
    #   1. The hull-wide `base_stem` (if hint is "hull" or empty).
    #   2. `<base_stem>_<hint>` exact match, then any stem ending in
    #      `_<hint>` (the part-specific texture stems WG ships).
    #   3. `_HINT_ALIASES` mapping for snake-case quirks (Atago's
    #      `Deck_house` → "deck_house") + library-token aliases.
    candidates: list[str] = []
    if hint_low in ("hull", ""):
        # Primary: the hull-wide `base_stem` itself.
        if base_stem in stem_index:
            candidates.append(base_stem)
        # Fallbacks #1: other stems ending in `_hull`. Catches the case
        # where WG's base ship texture is named `..._Hull_n.dd0` (Atago,
        # Takao, etc.) — the base stem itself ends in `_hull`.
        for s in stem_index:
            if s in candidates:
                continue
            if s.endswith("_hull"):
                candidates.append(s)
        # Fallbacks #2: non-library, non-part-specific ship stems with
        # hull-like slots. Catches mesh-swap permoflages where the base
        # ship's hull texture is named WITHOUT a `_Hull` suffix — e.g.
        # `ASB028_Iowa_1945_n.dd0` (stem `asb028_iowa` after year strip)
        # is the base hull for the `ASB077_Iowa_AzurLane` variant. Those
        # base stems don't satisfy any suffix scan but ARE the right
        # source for normal / camoMask / AO on the variant's hull.
        _HULL_LIKE_SLOTS = (
            "normal", "_normalRawOrMgRaw_normal", "camoMask", "occlusion",
        )
        for s in stem_index:
            if s in candidates:
                continue
            if _is_library_stem(s):
                continue
            # Skip part-specific stems (deck_house, deckhouse, etc.) —
            # those belong to other materials.
            if any(s.endswith(f"_{p}") for p in ("deck_house", "deckhouse", "casemate")):
                continue
            main = stem_index[s].get("main", {})
            if any(slot in main for slot in _HULL_LIKE_SLOTS):
                candidates.append(s)
    else:
        exact = f"{base_stem}_{hint_low}"
        if exact in stem_index:
            candidates.append(exact)
        suffix = f"_{hint_low}"
        for s in stem_index:
            if s != base_stem and s not in candidates and s.endswith(suffix):
                candidates.append(s)
        for alias in _HINT_ALIASES.get(hint_low, ()):
            for s in stem_index:
                if s in candidates:
                    continue
                if alias in s:
                    candidates.append(s)

    if not candidates:
        return []

    # Order: variants (have baseColor in main) before fallbacks (don't).
    # Within each group, preserve discovery order (which roughly matches
    # the canonical → suffix-scan → alias precedence).
    def has_basecolor(s: str) -> bool:
        return "baseColor" in stem_index[s].get("main", {})
    variants = [s for s in candidates if has_basecolor(s)]
    fallbacks = [s for s in candidates if not has_basecolor(s)]

    return variants + fallbacks


def _merge_multi_targets(
    mat: dict[str, Any],
    target_schemes: list[dict[str, dict[str, list[str]]]],
    existing_main: dict[str, Any],
) -> None:
    """Merge multiple stems' schemes into ``mat["texture_sets"]``.
    Earlier entries in ``target_schemes`` win per-slot; later entries
    fill missing slots only.

    ``existing_main`` (slots already resolved by the GLB-driven path)
    always wins. Used both for self-contained ships (single target) and
    permoflage variants (variant stem first, base-ship stem second).

    Side effect: replaces ``mat["texture_sets"]`` entirely. Caller is
    responsible for snapshotting prior content if it needs preserving.
    """
    accumulated: dict[str, dict[str, dict[str, list[str]]]] = {}

    for schemes in target_schemes:
        for scheme_key, slots in schemes.items():
            scheme_acc = accumulated.setdefault(scheme_key, {})
            for slot, paths in slots.items():
                if not paths:
                    continue
                if slot in scheme_acc:
                    # Earlier (higher-priority) target already filled
                    # this slot. Fallback can only fill gaps.
                    continue
                scheme_acc[slot] = {"dds_mips": list(paths)}

    # GLB-resolved main slots win unconditionally.
    if existing_main:
        main_acc = accumulated.setdefault("main", {})
        for slot, manifest in existing_main.items():
            main_acc[slot] = manifest

    # Drop empty schemes (none of the targets contributed anything for
    # that scheme key).
    accumulated = {k: v for k, v in accumulated.items() if v}
    mat["texture_sets"] = accumulated


def _merge_scheme_dict(
    mat: dict[str, Any],
    schemes: dict[str, dict[str, list[str]]],
    existing_main: dict[str, Any],
) -> None:
    """Merge disk-derived per-scheme slots into ``mat["texture_sets"]``.

    The GLB-driven path may have already populated ``texture_sets["main"]``
    from PNG references; that pre-existing data wins for slots it
    resolved. Disk-derived schemes (`camo_01`, `dead`, etc.) are added
    wholesale.
    """
    ts: dict[str, dict[str, dict[str, list[str]]]] = {}
    for scheme_key, slots in schemes.items():
        slot_manifests: dict[str, dict[str, list[str]]] = {}
        for slot, paths in slots.items():
            if not paths:
                continue
            # Preserve any GLB-resolved manifest for this slot in main;
            # disk-derived data fills in slots GLB didn't provide.
            if scheme_key == "main" and slot in existing_main:
                slot_manifests[slot] = existing_main[slot]
            else:
                slot_manifests[slot] = {"dds_mips": paths}
        if slot_manifests:
            ts[scheme_key] = slot_manifests
    # If GLB resolved any main slot the disk scan didn't have, keep it.
    if existing_main:
        ts.setdefault("main", {})
        for slot, manifest in existing_main.items():
            ts["main"].setdefault(slot, manifest)
    mat["texture_sets"] = ts


def make_shell(
    *,
    ammo_type: str | None = None,
    caliber_mm: float | None = None,
    mass_kg: float | None = None,
    muzzle_velocity_mps: float | None = None,
    air_drag_coefficient: float | None = None,
    krupp: float | None = None,
    cap: bool | None = None,
    cap_normalize_max_deg: float | None = None,
    fuze_arming_threshold_mm: float | None = None,
    fuze_delay_s: float | None = None,
    ricochet_min_deg: float | None = None,
    ricochet_always_deg: float | None = None,
    alpha_damage: float | None = None,
    alpha_piercing_he_mm: float | None = None,
    alpha_piercing_cs_mm: float | None = None,
    burn_probability: float | None = None,
    max_range_m: float | None = None,
) -> dict[str, Any]:
    """Build one entry for ``ballistics.shells[<ammo_id>]``.

    All fields are optional. Caller (typically the toolkit's ``ammo`` JSON
    output) populates whichever the source ``Projectile`` GameParam carried;
    others stay ``None`` so consumers can branch on availability.
    """
    out: dict[str, Any] = {}
    if ammo_type is not None:
        out["ammo_type"] = str(ammo_type)
    if caliber_mm is not None:
        out["caliber_mm"] = float(caliber_mm)
    if mass_kg is not None:
        out["mass_kg"] = float(mass_kg)
    if muzzle_velocity_mps is not None:
        out["muzzle_velocity_mps"] = float(muzzle_velocity_mps)
    if air_drag_coefficient is not None:
        out["air_drag_coefficient"] = float(air_drag_coefficient)
    if krupp is not None:
        out["krupp"] = float(krupp)
    if cap is not None:
        out["cap"] = bool(cap)
    if cap_normalize_max_deg is not None:
        out["cap_normalize_max_deg"] = float(cap_normalize_max_deg)
    if fuze_arming_threshold_mm is not None:
        out["fuze_arming_threshold_mm"] = float(fuze_arming_threshold_mm)
    if fuze_delay_s is not None:
        out["fuze_delay_s"] = float(fuze_delay_s)
    if ricochet_min_deg is not None:
        out["ricochet_min_deg"] = float(ricochet_min_deg)
    if ricochet_always_deg is not None:
        out["ricochet_always_deg"] = float(ricochet_always_deg)
    if alpha_damage is not None:
        out["alpha_damage"] = float(alpha_damage)
    if alpha_piercing_he_mm is not None:
        out["alpha_piercing_he_mm"] = float(alpha_piercing_he_mm)
    if alpha_piercing_cs_mm is not None:
        out["alpha_piercing_cs_mm"] = float(alpha_piercing_cs_mm)
    if burn_probability is not None:
        out["burn_probability"] = float(burn_probability)
    if max_range_m is not None:
        out["max_range_m"] = float(max_range_m)
    return out


def make_torpedo_profile(
    *,
    ammo_type: str = "torpedo",
    caliber_mm: float | None = None,
    alpha_damage: float | None = None,
    alpha_piercing_he_mm: float | None = None,
    max_range_m: float | None = None,
    speed_kts: float | None = None,
    running_depth_m: float | None = None,
    arming_time_s: float | None = None,
    flood_capable: bool | None = None,
    is_deep_water: bool | None = None,
    with_parachute: bool | None = None,
    visibility_factor: float | None = None,
    splash_armor_coeff: float | None = None,
    splash_radius_m: float | None = None,
    alert_distance_m: float | None = None,
    affected_by_ptz: bool | None = None,
    burn_probability: float | None = None,
) -> dict[str, Any]:
    """Build one entry for ``ballistics.torpedoes[<ammo_id>]`` (schema v3.1).

    Toolkit-emitted core (``ammo_type`` / ``caliber_mm`` / ``alpha_damage``
    / ``alpha_piercing_he_mm`` / ``max_range_m`` / ``burn_probability``)
    matches what ``make_shell`` carries for backward compat; the
    torpedo-specific suffix comes from the GameParams autofill pass.

    All fields are optional. Per-field handling:

    * ``running_depth_m``: ``GameParams.depth × 15`` (WoWS native unit ≈ 15 m).
    * ``arming_time_s``: ``GameParams.armingTime`` (already in seconds).
    * ``flood_capable``: bool from ``GameParams.floodGeneration``.
    * ``splash_radius_m``: not currently emitted (GameParams.splashCubeSize
      semantics ambiguous — see implementation note in
      :func:`gameparams.torpedo_profile_extras`).
    * ``alert_distance_m``: ``GameParams.alertDist × 15`` (best-effort
      conversion; emitted as float).
    """
    out: dict[str, Any] = {}
    if ammo_type is not None:
        out["ammo_type"] = str(ammo_type)
    if caliber_mm is not None:
        out["caliber_mm"] = float(caliber_mm)
    if alpha_damage is not None:
        out["alpha_damage"] = float(alpha_damage)
    if alpha_piercing_he_mm is not None:
        out["alpha_piercing_he_mm"] = float(alpha_piercing_he_mm)
    if max_range_m is not None:
        out["max_range_m"] = float(max_range_m)
    if speed_kts is not None:
        out["speed_kts"] = float(speed_kts)
    if running_depth_m is not None:
        out["running_depth_m"] = float(running_depth_m)
    if arming_time_s is not None:
        out["arming_time_s"] = float(arming_time_s)
    if flood_capable is not None:
        out["flood_capable"] = bool(flood_capable)
    if is_deep_water is not None:
        out["is_deep_water"] = bool(is_deep_water)
    if with_parachute is not None:
        out["with_parachute"] = bool(with_parachute)
    if visibility_factor is not None:
        out["visibility_factor"] = float(visibility_factor)
    if splash_armor_coeff is not None:
        out["splash_armor_coeff"] = float(splash_armor_coeff)
    if splash_radius_m is not None:
        out["splash_radius_m"] = float(splash_radius_m)
    if alert_distance_m is not None:
        out["alert_distance_m"] = float(alert_distance_m)
    if affected_by_ptz is not None:
        out["affected_by_ptz"] = bool(affected_by_ptz)
    if burn_probability is not None:
        out["burn_probability"] = float(burn_probability)
    return out


def make_ballistics(
    *,
    source: dict[str, Any] | None = None,
    ranges: dict[str, Any] | None = None,
    shells: dict[str, dict[str, Any]] | None = None,
    torpedoes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the top-level ``ballistics`` section.

    * ``source`` — provenance: ``{toolkit_version, generated_at}`` typically.
    * ``ranges`` — aggregate per-ship ranges:
      ``{main_battery_m, secondary_battery_m, torpedo_max_m, detection_km,
      air_detection_km}``. Any field may be ``None``.
    * ``shells`` — ``{ammo_id: shell_entry}``; each ``shell_entry`` is
      built by :func:`make_shell`. Holds gun-fired projectiles
      (``ammo_type`` ∈ ``AP``/``HE``/``CS``, plus any non-torpedo
      projectile types like bombs / rockets / depth charges).
    * ``torpedoes`` — ``{ammo_id: torpedo_entry}``; each ``torpedo_entry``
      is built by :func:`make_torpedo_profile`. Holds PAPT* projectiles
      with torpedo-specific kinematics + behaviour flags.

    Empty subsections fall through to ``{}`` placeholders so the section
    always satisfies the schema.
    """
    return {
        "source":    dict(source)    if source    is not None else {},
        "ranges":    dict(ranges)    if ranges    is not None else {},
        "shells":    dict(shells)    if shells    is not None else {},
        "torpedoes": dict(torpedoes) if torpedoes is not None else {},
    }


def make_default_skin() -> dict[str, Any]:
    """The mandatory ``default`` skin — always has empty overrides and
    points at the ``"main"`` scheme."""
    return {
        "skin_id": "default",
        "display_name": "Standard",
        "scheme_key": "main",
        "camo_pattern": None,
        "color_roll": None,
        "overrides": [],
    }


def make_skin(
    *,
    skin_id: str,
    display_name: str | None = None,
    scheme_key: str | None = None,
    camo_pattern: str | None = None,
    color_roll: str | None = None,
    color_scheme: dict[str, Any] | None = None,
    categories: dict[str, dict[str, Any]] | None = None,
    kind: str | None = None,
    mat_textures: dict[str, dict[str, Any]] | None = None,
    exterior_id: str | None = None,
    peculiarity: str | None = None,
    tier_unlock: str | None = None,
    source: str | None = None,
    asset_overrides: dict[str, dict[str, Any]] | None = None,
    overrides: Iterable[dict[str, Any]] | None = None,
    flip_v: bool | None = None,
) -> dict[str, Any]:
    """Build an entry for ``skins[]``.

    ``scheme_key`` selects which ``materials[i].texture_sets[<scheme_key>]``
    block to sample. Defaults to the ``skin_id`` itself, which lines up
    with the convention used by :func:`discover_skins_from_materials`
    (e.g. skin ``camo_01`` → scheme key ``camo_01``).

    ``color_roll`` records the subvariant identifier within a parent
    ``camo_pattern`` (e.g. ``"B"`` / ``"G"`` for blue/grey rolls of the
    same base camo).

    ``color_scheme`` carries the 4-color RGBA palette pulled from
    ``camouflages.xml`` for this skin's color roll, shaped as
    ``{"name": "colorSchemeUSNP01", "colors": [[r,g,b,a]×4]}``. Consumers
    composite ``mask + palette → albedo`` at decode time. When omitted,
    the consumer falls back to binding the mask raw (the legacy "Camo 01
    is a mask" rendering).

    ``categories`` carries per-camo-category shared masks + UV
    transforms — the data the per-stem ``texture_sets`` cascade can't
    surface (see camo_pipeline_survey.md §9). Shape::

        {
          "gun":   {"mask": {"dds_mips": [...]}, "uv": {"scale": [...], "offset": [...]}},
          "plane": {"mask": {...}, "uv": {...}},
          "float": {...},
          ...
        }

    Hull / deckhouse / bulge are deliberately NOT in this dict — they
    flow through per-ship ``texture_sets[<scheme>]``. Empty / missing
    means "no per-category overrides for this skin"; consumer renders
    accessories with base albedo (matching the §1ter Layer 1
    fall-through behaviour).

    Only ``overrides`` slots that differ from the resolved scheme need
    listing; unspecified slots inherit via the resolution order
    (override → scheme → main).
    """
    if not skin_id:
        raise ValueError("skin_id is required")
    out: dict[str, Any] = {
        "skin_id": skin_id,
        "display_name": display_name or skin_id,
        "scheme_key": scheme_key if scheme_key is not None else skin_id,
        "camo_pattern": camo_pattern,
        "color_roll": color_roll,
    }
    if kind is not None:
        out["kind"] = kind
    if exterior_id is not None:
        out["exterior_id"] = exterior_id
    if peculiarity is not None:
        out["peculiarity"] = peculiarity
    if color_scheme is not None:
        out["color_scheme"] = color_scheme
    if categories:
        out["categories"] = categories
    if mat_textures:
        out["mat_textures"] = mat_textures
    if tier_unlock is not None:
        out["tier_unlock"] = tier_unlock
    if source is not None:
        out["source"] = source
    if asset_overrides:
        out["asset_overrides"] = dict(asset_overrides)
    out["overrides"] = list(overrides or [])
    # Per-skin V-flip override. ``None`` (the default) leaves the
    # decision to consumers — Unity-side `ShipMaterialBuilder` and
    # `SkinControllerBuilder` derive the flip from `source` (loose
    # mods → off, vanilla / VFS extracts → on). Setting ``True`` /
    # ``False`` explicitly forces the convention regardless of source
    # — useful when a loose mod is authored top-down (rare but
    # possible if the artist used a tool that preserves DDS row
    # order) or when a VFS variant happens to ship bottom-up. The
    # field round-trips through ``merge_preserving`` so a hand-edit
    # of the sidecar JSON survives re-runs of the ingester.
    if flip_v is not None:
        out["flip_v"] = bool(flip_v)
    return out


# Regex matching `camo_<base>[_<roll>]` scheme keys. `<base>` is the
# numeric/named pattern identifier (`01`, `Halloween20`); optional
# `<roll>` is a single-uppercase-letter colour-roll suffix (`B`, `G`,
# `R`). Used to derive a `camo_pattern` parent + `color_roll` subvariant
# tag from a flat scheme key.
_CAMO_SCHEME_REGEX = re.compile(r"^camo_(?P<base>[A-Za-z0-9]+?)(?:_(?P<roll>[A-Z]))?$")


def discover_skins_from_materials(
    materials: Iterable[dict[str, Any]],
    *,
    palette_resolver: callable = None,  # type: ignore[name-defined]
    name_resolver: callable = None,  # type: ignore[name-defined]
) -> list[dict[str, Any]]:
    """Scan ``materials[i].texture_sets`` keys and emit one ``skins[]``
    entry per unique scheme found.

    Always emits the mandatory ``default`` skin first. Each non-``main``
    scheme becomes its own skin with ``scheme_key`` matching the texture
    set key. ``camo_<NN>[_<R>]`` keys are decomposed into a
    ``camo_pattern`` parent + ``color_roll`` subvariant tag so consumers
    can group colour rolls of the same base pattern.

    ``dead`` and ``dead_<scheme>`` schemes are NOT emitted as skins —
    they're damage states, surfaced through `HullDamageState` not skin
    selection. This keeps the player-facing skin list clean.

    When ``palette_resolver`` is supplied, each non-default scheme is
    expanded into ONE skin per color roll (default + alternate, etc.)
    with a populated ``color_scheme`` field. The resolver signature is::

        (scheme_key, mask_paths) -> (camo_name, [(roll_id, palette), …], categories)

    where ``palette`` is a list of 4 RGBA float tuples, ``camo_name``
    is the camouflages.xml entry name (e.g. ``camo_permanent_1``), and
    ``categories`` is the per-camo-category shared-mask dict (see
    :func:`make_skin`'s ``categories`` arg). Categories is shared
    across every roll of the same camo block — same masks, same UVs.

    Returning ``(None, [], {})`` means "no palette data; fall back to
    one skin with mask only" — preserves the legacy behavior. Legacy
    2-tuple resolvers still work (categories defaults to ``{}``).
    """
    # Index `(scheme_key) -> first-seen mask paths` so the resolver can
    # match by mask filename without re-walking materials. The resolver
    # only needs ONE example mask per scheme (the toolkit emits the
    # same scheme files across every material that uses them).
    seen: set[str] = set()
    discovered: list[str] = []
    mask_paths_by_scheme: dict[str, list[str]] = {}
    for mat in materials:
        ts = mat.get("texture_sets") or {}
        for scheme_key, scheme_data in ts.items():
            if scheme_key == "main":
                continue
            if scheme_key.startswith("dead"):
                continue
            if scheme_key not in seen:
                seen.add(scheme_key)
                discovered.append(scheme_key)
            # Stash the first non-empty baseColor.dds_mips for resolver
            # use. Subsequent materials with the same scheme overwrite —
            # they'd resolve to the same camo entry anyway.
            if scheme_key not in mask_paths_by_scheme:
                base = (scheme_data or {}).get("baseColor") or {}
                mips = base.get("dds_mips") or []
                if mips:
                    mask_paths_by_scheme[scheme_key] = mips

    skins: list[dict[str, Any]] = [make_default_skin()]
    for scheme_key in sorted(discovered):
        # Try the palette resolver first — it gives authoritative
        # display names (camouflages.xml entry name) + multi-roll info.
        resolved: list[tuple[str, list[list[float]]]] = []
        camo_name: str | None = None
        categories: dict[str, dict[str, Any]] = {}
        if palette_resolver is not None:
            mask_paths = mask_paths_by_scheme.get(scheme_key, [])
            try:
                result = palette_resolver(scheme_key, mask_paths)
            except Exception:
                # Resolver should never raise — but be defensive in
                # scaffold path. Fall through to legacy behaviour.
                result = (None, [], {})
            # Tolerate legacy 2-tuple resolvers (no categories).
            if isinstance(result, tuple):
                if len(result) == 3:
                    camo_name, resolved, categories = result
                elif len(result) == 2:
                    camo_name, resolved = result
                    categories = {}
                else:
                    camo_name, resolved, categories = None, [], {}
            else:
                camo_name, resolved, categories = None, [], {}

        if resolved:
            # Resolve a human-readable label for the camo entry once per
            # scheme. ``name_resolver`` (when supplied) walks GameParams
            # Exteriors → ``IDS_<name>`` in the WoWS gettext catalogue,
            # turning ``camo_permanent_1`` into ``"Iron Resilience"``
            # etc. See ``tools/shared/wg_localization.py`` and
            # ``tools/shared/wg_camo.display_name_for_camo_entry``.
            # Falls through to the raw entry name when missing or unset.
            human_pattern: str | None = None
            if name_resolver is not None and camo_name:
                try:
                    human_pattern = name_resolver(camo_name)
                except Exception:
                    human_pattern = None
            for roll_id, palette in resolved:
                pattern = camo_name or scheme_key
                skin_id = f"{pattern}__{roll_id}"
                # Display: human label preferred, falls back to the raw
                # entry name. The roll_id suffix disambiguates color
                # variants of the same base pattern (Iowa carries both
                # ``camo_permanent_1`` rolls IJNP03 and IJNP36) so even
                # when the human name is identical, skin_ids stay unique.
                display_base = human_pattern or pattern
                display = f"{display_base} ({roll_id})"
                skins.append(make_skin(
                    skin_id=skin_id,
                    display_name=display,
                    scheme_key=scheme_key,
                    camo_pattern=pattern,
                    color_roll=roll_id,
                    color_scheme={
                        "name":   roll_id,
                        "colors": [list(c) for c in palette],
                    },
                    # Same categories on every roll variant — they
                    # share the same <Textures> + <UV> block.
                    categories=categories or None,
                ))
            continue

        # ── Legacy fallback (no resolver / no palette match) ─────────
        # Same behaviour as before: one skin per scheme key, mask-only.
        camo_pattern: str | None = None
        color_roll: str | None = None
        m = _CAMO_SCHEME_REGEX.match(scheme_key)
        if m:
            base = m.group("base")
            roll = m.group("roll")
            camo_pattern = f"camo_{base}"
            color_roll = roll
        if camo_pattern is not None:
            base = m.group("base")
            display = f"Camo {base}"
            if color_roll:
                display = f"{display} ({color_roll})"
        else:
            display = scheme_key.replace("_", " ").title()

        skins.append(make_skin(
            skin_id=scheme_key,
            display_name=display,
            scheme_key=scheme_key,
            camo_pattern=camo_pattern,
            color_roll=color_roll,
        ))
    return skins


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------

def new_document(
    *,
    pipeline: dict[str, Any],
    ship: dict[str, Any],
    geometry: dict[str, Any] | None = None,
    armor: dict[str, Any] | None = None,
    hitbox: dict[str, Any] | None = None,
    turrets: Iterable[dict[str, Any]] | None = None,
    secondaries: Iterable[dict[str, Any]] | None = None,
    antiair: Iterable[dict[str, Any]] | None = None,
    torpedoes: Iterable[dict[str, Any]] | None = None,
    accessories: Iterable[dict[str, Any]] | None = None,
    ballistics: dict[str, Any] | None = None,
    materials: Iterable[dict[str, Any]] | None = None,
    skins: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a complete v2 sidecar document.

    Sections not passed get minimal well-formed placeholders so the result
    always satisfies the root schema. ``skins`` defaults to ``[default]``;
    ``ballistics`` defaults to an empty section (no shells, no ranges).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline": pipeline,
        "ship": ship,
        "geometry": geometry if geometry is not None else make_geometry(),
        "armor": armor if armor is not None else make_armor(),
        "hitbox": hitbox if hitbox is not None else make_hitbox(),
        "turrets": list(turrets or []),
        "secondaries": list(secondaries or []),
        "antiair": list(antiair or []),
        "torpedoes": list(torpedoes or []),
        "accessories": list(accessories or []),
        "ballistics": ballistics if ballistics is not None else make_ballistics(),
        "materials": list(materials or []),
        "skins": list(skins) if skins is not None else [make_default_skin()],
    }


# ---------------------------------------------------------------------------
# Canonicalisation + I/O
# ---------------------------------------------------------------------------

def _order_dict(
    d: dict[str, Any],
    preferred: tuple[str, ...],
) -> dict[str, Any]:
    """Return a new dict with ``preferred`` keys first (in order), then any
    extra keys alphabetically. Values are left untouched."""
    out: dict[str, Any] = {}
    for k in preferred:
        if k in d:
            out[k] = d[k]
    for k in sorted(d):
        if k not in out:
            out[k] = d[k]
    return out


def _canonicalise(doc: dict[str, Any]) -> dict[str, Any]:
    """Produce a dict with deterministic key order for every section.

    Lists of placements / materials / skins keep input order (the caller
    owns their ordering — typically stable-sorted by instance_id in the
    toolkit's emitter). Dict keys inside those items are reordered per the
    per-section schema.
    """
    out: dict[str, Any] = {}

    for k in _TOP_LEVEL_ORDER:
        if k not in doc:
            continue
        v = doc[k]
        if k == "pipeline" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _PIPELINE_ORDER)
        elif k == "ship" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _SHIP_ORDER)
        elif k == "variants" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _VARIANTS_ORDER)
        elif k == "hulls" and isinstance(v, dict):
            # Dict-of-dicts. Each value is a hull entry (keys ordered per
            # _HULL_ENTRY_ORDER). Outer key order: stock first, then by
            # module_id alphabetical for stable diffs.
            ordered: dict[str, Any] = {}
            entries = list(v.items())
            entries.sort(key=lambda kv: (
                not (isinstance(kv[1], dict) and kv[1].get("is_stock")),
                kv[0],
            ))
            for hull_name, entry in entries:
                if not isinstance(entry, dict):
                    ordered[hull_name] = entry
                    continue
                ordered_entry = _order_dict(_deep_sort_inner(entry), _HULL_ENTRY_ORDER)
                # Order placement lists' inner items per _PLACEMENT_ORDER.
                for sect in PLACEMENT_SECTIONS:
                    items = ordered_entry.get(sect)
                    if isinstance(items, list):
                        ordered_entry[sect] = [
                            _order_dict(_deep_sort_inner(it), _PLACEMENT_ORDER)
                            if isinstance(it, dict) else it
                            for it in items
                        ]
                # Order stats sub-block.
                stats = ordered_entry.get("stats")
                if isinstance(stats, dict):
                    ordered_entry["stats"] = _order_dict(stats, _HULL_STATS_ORDER)
                ordered[hull_name] = ordered_entry
            out[k] = ordered
        elif k == "geometry" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _GEOMETRY_ORDER)
        elif k == "armor" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _ARMOR_ORDER)
        elif k == "hitbox" and isinstance(v, dict):
            out[k] = _order_dict(_deep_sort_inner(v), _HITBOX_ORDER)
        elif k in PLACEMENT_SECTIONS and isinstance(v, list):
            out[k] = [
                _order_dict(_deep_sort_inner(item), _PLACEMENT_ORDER)
                if isinstance(item, dict) else item
                for item in v
            ]
        elif k == "ballistics" and isinstance(v, dict):
            out[k] = _canonicalise_ballistics(v)
        elif k == "materials" and isinstance(v, list):
            out[k] = [
                _order_dict(_deep_sort_inner(item), _MATERIAL_ORDER)
                if isinstance(item, dict) else item
                for item in v
            ]
        elif k == "skins" and isinstance(v, list):
            ordered_skins: list[Any] = []
            for item in v:
                if not isinstance(item, dict):
                    ordered_skins.append(item)
                    continue
                ordered_item = _order_dict(_deep_sort_inner(item), _SKIN_ORDER)
                # Order each ``asset_overrides[<asset_id>]`` block per
                # _ASSET_OVERRIDE_ORDER, with assets keyed alphabetically
                # for stable diffs.
                ao = ordered_item.get("asset_overrides")
                if isinstance(ao, dict):
                    new_ao: dict[str, Any] = {}
                    for aid in sorted(ao):
                        entry = ao[aid]
                        if isinstance(entry, dict):
                            new_ao[aid] = _order_dict(
                                _deep_sort_inner(entry), _ASSET_OVERRIDE_ORDER,
                            )
                        else:
                            new_ao[aid] = entry
                    ordered_item["asset_overrides"] = new_ao
                ordered_skins.append(ordered_item)
            out[k] = ordered_skins
        else:
            out[k] = v

    # Forward-compat: preserve any unknown top-level keys at the end, sorted.
    for k in sorted(doc):
        if k not in out:
            out[k] = doc[k]

    # Special-case: transforms inside placement entries get their own order.
    for section in PLACEMENT_SECTIONS:
        if section not in out:
            continue
        for item in out[section]:
            if isinstance(item, dict) and isinstance(item.get("transform"), dict):
                item["transform"] = _order_dict(
                    item["transform"], _TRANSFORM_ORDER,
                )

    return out


def _deep_sort_inner(obj: Any) -> Any:
    """Recursively alphabetise keys in nested dicts. Lists keep order."""
    if isinstance(obj, dict):
        return {k: _deep_sort_inner(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_deep_sort_inner(v) for v in obj]
    return obj


def _canonicalise_ballistics(b: dict[str, Any]) -> dict[str, Any]:
    """Canonical ordering for the ``ballistics`` section.

    * Top-level keys ordered per :data:`_BALLISTICS_ORDER`.
    * ``source`` ordered alphabetically (no fixed schema yet).
    * ``ranges`` ordered per :data:`_RANGES_ORDER`.
    * ``shells`` keyed by ammo_id, alphabetically sorted; each entry
      ordered per :data:`_SHELL_ORDER` for stable diffs.
    * ``torpedoes`` keyed by ammo_id, alphabetically sorted; each entry
      ordered per :data:`_TORPEDO_PROFILE_ORDER`.
    """
    out: dict[str, Any] = {}
    src = b.get("source")
    if isinstance(src, dict):
        out["source"] = _deep_sort_inner(src)
    rng = b.get("ranges")
    if isinstance(rng, dict):
        out["ranges"] = _order_dict(_deep_sort_inner(rng), _RANGES_ORDER)
    shells = b.get("shells")
    if isinstance(shells, dict):
        ordered_shells: dict[str, Any] = {}
        for ammo_id in sorted(shells):
            entry = shells[ammo_id]
            if isinstance(entry, dict):
                ordered_shells[ammo_id] = _order_dict(
                    _deep_sort_inner(entry), _SHELL_ORDER,
                )
            else:
                ordered_shells[ammo_id] = entry
        out["shells"] = ordered_shells
    torps = b.get("torpedoes")
    if isinstance(torps, dict):
        ordered_torps: dict[str, Any] = {}
        for ammo_id in sorted(torps):
            entry = torps[ammo_id]
            if isinstance(entry, dict):
                ordered_torps[ammo_id] = _order_dict(
                    _deep_sort_inner(entry), _TORPEDO_PROFILE_ORDER,
                )
            else:
                ordered_torps[ammo_id] = entry
        out["torpedoes"] = ordered_torps
    # Forward-compat: surface unknown keys at the end, sorted.
    for k in sorted(b):
        if k not in out:
            out[k] = _deep_sort_inner(b[k])
    return out


def dumps(doc: dict[str, Any]) -> str:
    """Serialise to the canonical on-disk form.

    2-space indent, LF newlines, spec-ordered keys, trailing LF. The output
    is byte-stable for identical input — re-running the pipeline with no
    changes yields the same bytes.
    """
    canon = _canonicalise(doc)
    buf = io.StringIO()
    json.dump(canon, buf, indent=2, ensure_ascii=False, sort_keys=False)
    text = buf.getvalue().replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def write(doc: dict[str, Any], path: str | Path) -> Path:
    """Write a sidecar to disk using canonical formatting.

    Writes atomically via a sibling ``.tmp`` rename. Binary mode keeps
    Windows from sneaking CRLF into the output.
    """
    path = Path(path)
    text = dumps(doc)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(tmp, path)
    return path


def read(path: str | Path) -> dict[str, Any]:
    """Load + validate a v3 sidecar.

    Raises :class:`SidecarSchemaError` for:
      - Missing / non-int ``schema_version``
      - v1 input (``schema_version == 1``). v1 is not auto-migrated; ships
        regenerate through the new pipeline.
      - v2 input (``schema_version == 2``). v2 ships must be
        regenerated to pick up the new ``materials[i].texture_sets``
        scheme-keyed structure + ``skins[].scheme_key`` field. Run
        ``python tools/ship/scaffold_ship.py <Ship> --skip-export
        --skip-armor --skip-ammo`` to upgrade.
      - Any ``schema_version`` other than :data:`SCHEMA_VERSION`.
    """
    path = Path(path)
    with open(path, "rb") as f:
        data = f.read().decode("utf-8")
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise SidecarSchemaError(f"{path}: sidecar root must be an object")
    version = doc.get("schema_version")
    if not isinstance(version, int):
        raise SidecarSchemaError(
            f"{path}: missing or non-int 'schema_version' — not a valid sidecar"
        )
    if version == 1:
        raise SidecarSchemaError(
            f"{path}: schema_version=1 is not supported. v1 ships must be "
            "regenerated through the new toolkit pipeline (see "
            "tools/toolkit_integration/ARCHITECTURE.md); there is no "
            "automatic migration."
        )
    if version == 2:
        raise SidecarSchemaError(
            f"{path}: schema_version=2 is not supported. v2 ships must be "
            "regenerated to pick up the v3 `materials[i].texture_sets` + "
            "`skins[].scheme_key` structure. Re-scaffold with "
            "`python tools/ship/scaffold_ship.py <Ship> --skip-export "
            "--skip-armor --skip-ammo`."
        )
    if version != SCHEMA_VERSION:
        raise SidecarSchemaError(
            f"{path}: schema_version={version} not supported by this "
            f"library (expected {SCHEMA_VERSION})"
        )
    return doc


# ---------------------------------------------------------------------------
# Merge-preserving: the heart of idempotent re-runs.
# ---------------------------------------------------------------------------

#: Which list-of-dict sections merge by which identifier key.
_KEYED_LIST_SECTIONS: dict[str, str] = {
    "turrets": "instance_id",
    "secondaries": "instance_id",
    "antiair": "instance_id",
    "torpedoes": "instance_id",
    "accessories": "instance_id",
    "materials": "material_id",
    "skins": "skin_id",
}


def merge_preserving(
    base: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """Recursive merge that preserves fields in ``base`` not mentioned by
    ``update``.

    Semantics:
      - Dicts merge recursively.
      - For placement arrays (``turrets`` / ``secondaries`` / ``antiair`` /
        ``torpedoes`` / ``accessories``): items match by ``instance_id``.
        The update's fields override; unmentioned fields (including
        hand-authored ``attach_to`` / ``casts_shadow`` / custom
        ``ammo_types``) survive.
      - ``materials[]`` matches by ``material_id``; ``skins[]`` by
        ``skin_id``.
      - For primitive values, ``update`` wins (pass ``None`` explicitly to
        clear a field).
      - Items in ``base`` whose key doesn't appear in ``update`` survive
        unchanged.

    Returns a new dict; input dicts are not mutated.
    """
    out = _deepcopy_jsonish(base)
    _merge_into(out, update)
    return out


def _merge_into(base: dict[str, Any], update: dict[str, Any]) -> None:
    for k, v in update.items():
        if k in _KEYED_LIST_SECTIONS and isinstance(v, list):
            key = _KEYED_LIST_SECTIONS[k]
            base[k] = _merge_keyed_list(base.get(k, []) or [], v, key)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge_into(base[k], v)
        else:
            base[k] = _deepcopy_jsonish(v)


def _merge_keyed_list(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    """Merge two lists of dicts keyed by ``key``.

    - Items in ``new`` with a known key merge into the corresponding ``old``
      entry; unknown keys append at the end (in ``new``'s order).
    - Items in ``old`` whose key isn't mentioned in ``new`` survive
      unchanged and appear after the ``new`` items.
    """
    old_by_key = {
        item[key]: item for item in old
        if isinstance(item, dict) and key in item
    }
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for item in new:
        if not isinstance(item, dict):
            out.append(_deepcopy_jsonish(item))
            continue
        k = item.get(key)
        if k is None:
            out.append(_deepcopy_jsonish(item))
            continue
        base = _deepcopy_jsonish(old_by_key.get(k, {}))
        _merge_into(base, item)
        out.append(base)
        seen.add(k)
    for item in old:
        if not isinstance(item, dict):
            continue
        k = item.get(key)
        if k is not None and k not in seen:
            out.append(_deepcopy_jsonish(item))
    return out


def _deepcopy_jsonish(obj: Any) -> Any:
    """Shallow-enough deep copy for JSON-shaped data. Cheaper than
    ``copy.deepcopy`` because we know the value domain."""
    if isinstance(obj, dict):
        return {k: _deepcopy_jsonish(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deepcopy_jsonish(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Placements-JSON ingestion
# ---------------------------------------------------------------------------

def absorb_placements_json(
    doc: dict[str, Any],
    placements: str | Path | dict[str, Any],
) -> dict[str, Any]:
    """Merge the toolkit's per-ship placements JSON into ``doc``'s typed
    sections, preserving hand-authored extras per ``instance_id``.

    Accepts either a path (str / Path) or the already-parsed dict. The
    toolkit's placements JSON is expected to contain any of the five
    placement section keys (``turrets``, ``secondaries``, ``antiair``,
    ``torpedoes``, ``accessories``); other keys are ignored.

    Normalises CamelCase toolkit-native strings (``species``) to lowercase
    so the sidecar matches the lowercase ``scope`` / ``category`` /
    ``subcategory`` convention already used elsewhere.

    Returns a new doc; ``doc`` is not mutated.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, "rb") as f:
            data = f.read().decode("utf-8")
        placements_dict = json.loads(data)
    else:
        placements_dict = placements

    if not isinstance(placements_dict, dict):
        raise SidecarSchemaError(
            "placements JSON root must be an object with placement sections"
        )

    update: dict[str, Any] = {}
    for section in PLACEMENT_SECTIONS:
        items = placements_dict.get(section)
        if isinstance(items, list):
            update[section] = [_canonicalise_placement_strings(x) for x in items]

    return derive_attach_to(merge_preserving(doc, update))


def apply_variant_asset_swaps(
    doc: dict[str, Any],
    swaps: dict[str, Any],
    *,
    library_root: Path | None = None,
) -> tuple[dict[str, Any], int, set[str]]:
    """Rewrite ``asset_id`` / ``dead_asset_id`` / ``misc_filter`` on every
    placement using a swap table from
    :func:`tools.shared.gameparams.resolve_variant_accessory_swaps`.

    Accepts both shapes the resolver has produced:

      * Current — dict-of-dicts ``{by_asset_id, by_hp_name,
        dead_by_hp_name, misc_filter_by_hp}``. HP-name-keyed swaps take
        precedence over asset-id-keyed ones (more specific). Per-HP
        dead swaps can ADD a ``dead_asset_id`` to a placement that
        didn't have one (Iowa AzurLane: base Iowa has no dead variant;
        the Azur skin adds ``AGM652_..._Azur_dead``). Per-HP
        ``misc_filter`` overrides the vanilla ship's per-HP whitelist
        with the variant-bundled ``MP_*`` IDs from the Exterior's
        ``nodesConfig`` — empty list means "drop every bundled misc on
        this HP under this variant" (common for Azur director HPs).
      * Legacy — flat ``{base_aid: variant_aid}`` dict from the
        peculiarityModels-only era. Treated as ``by_asset_id`` only,
        with no HP-keyed swaps.

    For mesh-swap permoflages whose Exterior carries per-hardpoint
    accessory swaps (ARP gunmounts → JGM57x_Arpeggio, AzurLane Iowa
    main turrets → AGM652_..._Azur, Optimus secondaries →
    AGS533_..._Black, etc.), this rewrites every placement section's
    asset references so downstream consumers (Unity, webview) bind the
    variant library accessories instead of the base ship's gray
    turrets.

    Bone-mismatch correction (when ``library_root`` is provided): WG
    sometimes re-authors the variant's ``.geometry`` pre-flipped 180°
    around Y *and* sets the variant's ``Rotate_Y_BlendBone`` rest pose
    to identity instead of Z-mirror. The toolkit correctly bakes
    ``inverse(source_bone)`` into the placement at base export, but
    that correction is wrong for the variant once we've swapped the
    asset_id. When the source and target meshes have opposite
    forward-Z direction (proxy for ``Rotate_Y_BlendBone.col2.z``
    sign — see :mod:`tools.shared.wg_bone_orientation`), this
    post-multiplies the placement matrix by Ry(180°). Confirmed pairs
    that need this on 2026-05-09: AGM019 → AGM622 (Baltimore Azur
    main), JGS158 → JGS3094 (Azur Shinano secondary). Pairs that
    don't need it (AGM034 → AGM652 for Iowa Azur main) collapse to a
    no-op because both meshes share Z direction. See
    ``memory/project_variant_swap_bone_mismatch.md`` for the
    derivation. Without ``library_root``, the correction is skipped
    (back-compat for callers that don't have the library on disk —
    in that case any bone-mismatched swaps render 180° wrong, same
    as pre-2026-05-09 behaviour).

    No-op if ``swaps`` is empty. Hand-authored ``asset_id`` values that
    don't match any swap key pass through untouched. ``display_name``
    is NOT rewritten — it reflects the gameplay gun's GameParams
    identity (e.g. "JGM025 203mm50 Type E"), not the model variant;
    the base and variant share gameplay stats.

    Returns ``(new_doc, n_swapped, unused)`` where ``n_swapped`` counts
    individual asset_id / dead_asset_id / misc_filter rewrites (multiple
    fields on one placement count separately) and ``unused`` is the set
    of swap keys (from any current sub-dict) that didn't match any
    placement on this ship — surface those to the user; they usually
    mean the Exterior references a hardpoint or base accessory the ship
    no longer carries (patch removal / GameParams drift). ``doc`` is
    not mutated.
    """
    if not swaps:
        return (doc, 0, set())

    # Normalize legacy flat shape → dict-of-dicts shape.
    if all(isinstance(v, str) for v in swaps.values()):
        by_asset_id: dict[str, str] = swaps  # type: ignore[assignment]
        by_hp_name: dict[str, str] = {}
        dead_by_hp_name: dict[str, str] = {}
        misc_filter_by_hp: dict[str, list[str]] = {}
    else:
        by_asset_id = swaps.get("by_asset_id") or {}
        by_hp_name = swaps.get("by_hp_name") or {}
        dead_by_hp_name = swaps.get("dead_by_hp_name") or {}
        misc_filter_by_hp = swaps.get("misc_filter_by_hp") or {}

    if not (by_asset_id or by_hp_name or dead_by_hp_name or misc_filter_by_hp):
        return (doc, 0, set())

    out = dict(doc)
    n_swapped = 0
    used_aid_keys: set[str] = set()
    used_hp_keys: set[str] = set()
    used_dead_hp_keys: set[str] = set()
    used_misc_filter_hp_keys: set[str] = set()

    # Resolve per-asset forward-Z signs lazily — most ships swap only a
    # handful of asset_ids, so caching keeps repeated lookups cheap.
    # Cache keyed on (asset_id, scope, category, subcategory) since the
    # full library path needs all four to find the GLB.
    forward_z_cache: dict[tuple[str, str, str, str], int] = {}
    # Asset_ids whose library GLB couldn't be located on disk. Populated
    # as a side-effect of ``_forward_sign``. Distinguishes the "file
    # missing" zero from the "axially-symmetric" zero so ``_needs_y_flip``
    # can warn loudly about the first case — that one means a real swap
    # is about to silently skip its Ry(180°) correction, typically
    # because scaffold ran before ``build_accessory_library`` had a
    # chance to emit the variant GLB. The "axially symmetric" zero is
    # benign (no flip is correct).
    missing_glb_aids: set[str] = set()
    warned_missing_glb_aids: set[str] = set()

    def _forward_sign(
        asset_id: str | None,
        scope: str | None,
        category: str | None,
        subcategory: str | None,
    ) -> int:
        if not isinstance(asset_id, str) or library_root is None:
            return 0
        # Empty / missing taxonomy → can't locate the GLB. Returning 0
        # (no opinion) is safe — no Y flip will be applied.
        if not (scope and category):
            return 0
        cache_key = (asset_id, scope, category, subcategory or "")
        if cache_key in forward_z_cache:
            return forward_z_cache[cache_key]
        # Lazy-import: keeps the sidecar module importable on hosts that
        # don't have the orientation helper (e.g. minimal CI containers).
        from .bone_orientation import glb_forward_z_sign
        # Library layout: ``<root>/<scope>/<category>[/<subcategory>]/<asset_id>/<asset_id>.glb``.
        parts: list[str] = [scope, category]
        if subcategory:
            parts.extend(subcategory.split("/"))
        parts.extend([asset_id, f"{asset_id}.glb"])
        glb_path = library_root.joinpath(*parts)
        if glb_path.is_file():
            sign = glb_forward_z_sign(glb_path)
        else:
            sign = 0
            missing_glb_aids.add(asset_id)
        forward_z_cache[cache_key] = sign
        return sign

    def _needs_y_flip(
        source_aid: str | None,
        target_aid: str | None,
        scope: str | None,
        category: str | None,
        subcategory: str | None,
    ) -> bool:
        # Bone correction needed iff source and target meshes have
        # confidently-opposite forward-Z direction. Either ``0`` (no
        # opinion / file missing / axially symmetric) → no flip; same
        # sign → no flip.
        if not source_aid or not target_aid or source_aid == target_aid:
            return False
        s_sign = _forward_sign(source_aid, scope, category, subcategory)
        t_sign = _forward_sign(target_aid, scope, category, subcategory)
        if s_sign == 0 or t_sign == 0:
            # Surface the canonical first-ingest race: scaffold called
            # apply_variant_asset_swaps with library_root set, a real
            # swap is queued, but the library GLB isn't on disk yet
            # (typical when build_accessory_library hasn't run for
            # this variant). Without a warning the matrix correction
            # silently no-ops and the variant turrets render 180°
            # off — see `memory/project_variant_swap_bone_mismatch.md`.
            # Dedup per-asset across the loop so multi-HP swaps don't
            # spam.
            for aid in (source_aid, target_aid):
                if aid in missing_glb_aids and aid not in warned_missing_glb_aids:
                    warned_missing_glb_aids.add(aid)
                    print(
                        f"  warn: bone-mismatch check skipped for "
                        f"{aid!r} (swap {source_aid!r} → {target_aid!r}) "
                        f"— library GLB not on disk. Re-run scaffold "
                        f"after build_accessory_library so the Ry(180°) "
                        f"correction can be applied; otherwise the "
                        f"variant turret/mount will render 180° off.",
                        file=sys.stderr,
                    )
            return False
        return s_sign != t_sign

    for section in PLACEMENT_SECTIONS:
        items = out.get(section)
        if not isinstance(items, list):
            continue
        new_items: list[Any] = []
        for p in items:
            if not isinstance(p, dict):
                new_items.append(p)
                continue
            aid = p.get("asset_id")
            daid = p.get("dead_asset_id")
            hp = p.get("hp_name")

            # HP-name-keyed swaps take precedence — they're per-mount
            # overrides. asset-id-keyed is the broader "swap any
            # occurrence of this base asset" pattern.
            new_aid: str | None = None
            if isinstance(hp, str) and hp in by_hp_name:
                new_aid = by_hp_name[hp]
                used_hp_keys.add(hp)
            elif isinstance(aid, str) and aid in by_asset_id:
                new_aid = by_asset_id[aid]
                used_aid_keys.add(aid)

            new_daid: str | None = None
            if isinstance(hp, str) and hp in dead_by_hp_name:
                new_daid = dead_by_hp_name[hp]
                used_dead_hp_keys.add(hp)
            elif isinstance(daid, str) and daid in by_asset_id:
                new_daid = by_asset_id[daid]
                used_aid_keys.add(daid)

            # Per-HP miscFilter override from the Exterior's nodesConfig.
            # WG runtime uses this AS-IS in place of the vanilla ship's
            # per-HP miscFilter when present (verified empirically:
            # PAES438_AZUR_MONTPELIER's nodesConfig.A1_Artillery.HP_AGM_1
            # carries `miscFilter: [MP_AM920_Rangefinder_Azur]` which
            # exactly matches AGM541_Azur's bundled miscs, while the
            # vanilla ship's HP_AGM_1.miscFilter still references the
            # AGM009 vanilla MP_*s the variant doesn't expose). Empty
            # list is meaningful — "drop every bundled misc on this HP
            # under this variant", common for Azur director HPs that
            # discard the entire vanilla decorative set. Apply on every
            # HP listed in misc_filter_by_hp, even ones with no model/
            # deadMesh swap (defensive — the override semantics don't
            # require the model to also be swapped, though in practice
            # they almost always co-occur).
            new_misc_filter: list[str] | None = None
            if isinstance(hp, str) and hp in misc_filter_by_hp:
                new_misc_filter = list(misc_filter_by_hp[hp])
                used_misc_filter_hp_keys.add(hp)

            # Re-run heal detection: when a prior scaffold already
            # rewrote ``asset_id`` to the variant (so ``aid`` is now
            # the swap-target value, not a key in by_asset_id) but the
            # Ry(180°) correction never landed because the variant GLB
            # wasn't on disk yet (typical on a first ingest where
            # build_accessory_library runs after scaffold), the
            # placement's ``attached_y_flip`` flag will be absent. We
            # reverse-lookup the source via ``by_asset_id``'s values
            # so the bone-mismatch gate below can re-check the swap
            # pair and apply the correction now. Gated on
            # ``library_root`` because that's where the bone signs
            # come from. Skipped when the placement is already flagged.
            # This is what lets ``<Ship>_accessories.json`` self-heal
            # on a re-scaffold post-library-build — the webview reads
            # accessories.json directly, so the broken matrix sticks
            # until this heal fires.
            inferred_source_aid: str | None = None
            if (
                new_aid is None
                and library_root is not None
                and isinstance(aid, str)
                and not p.get("attached_y_flip")
            ):
                for base, variant in by_asset_id.items():
                    if variant == aid and base != aid:
                        inferred_source_aid = base
                        break

            if (
                new_aid is None
                and new_daid is None
                and new_misc_filter is None
                and inferred_source_aid is None
            ):
                new_items.append(p)
                continue
            p2 = dict(p)
            if new_aid is not None and p2.get("asset_id") != new_aid:
                p2["asset_id"] = new_aid
                n_swapped += 1
            if new_daid is not None and p2.get("dead_asset_id") != new_daid:
                p2["dead_asset_id"] = new_daid
                n_swapped += 1
            if new_misc_filter is not None and p2.get("misc_filter") != new_misc_filter:
                p2["misc_filter"] = new_misc_filter
                n_swapped += 1

            # Bone-mismatch Y flip — gated on the alive ``aid`` swap,
            # since both alive and dead variants render at the same
            # placement matrix (per the audit doc's "Alive vs dead
            # variant orientation" section). When only ``daid`` swaps
            # (Iowa AzurLane "add a dead variant" pattern), the alive
            # mesh and its placement are already consistent.
            #
            # Two paths feed the gate:
            #   (a) Fresh swap — ``new_aid`` is set and differs from
            #       the original ``aid``. Source = ``aid``,
            #       target = ``new_aid``.
            #   (b) Re-run heal — ``inferred_source_aid`` was resolved
            #       above. Source = inferred base, target = ``aid``
            #       (the variant already baked in).
            scope = p2.get("scope") if isinstance(p2.get("scope"), str) else None
            category = p2.get("category") if isinstance(p2.get("category"), str) else None
            subcategory = p2.get("subcategory") if isinstance(p2.get("subcategory"), str) else None
            flip_source: str | None = None
            flip_target: str | None = None
            if new_aid is not None and aid != new_aid:
                flip_source = aid if isinstance(aid, str) else None
                flip_target = new_aid
            elif inferred_source_aid is not None and isinstance(aid, str):
                flip_source = inferred_source_aid
                flip_target = aid
            if (
                library_root is not None
                and flip_source is not None
                and flip_target is not None
                and _needs_y_flip(flip_source, flip_target, scope, category, subcategory)
            ):
                txfm = p2.get("transform") if isinstance(p2.get("transform"), dict) else None
                matrix = txfm.get("matrix") if txfm else None
                if isinstance(matrix, list) and len(matrix) == 16:
                    from .bone_orientation import post_multiply_ry180
                    new_matrix = post_multiply_ry180(matrix)
                    new_txfm = dict(txfm) if txfm else {}
                    new_txfm["matrix"] = new_matrix
                    # Translation column is unchanged by Ry(180°), so
                    # ``position`` stays valid; preserve it verbatim.
                    p2["transform"] = new_txfm
                    # Stamp the bone-mismatch correction so consumers
                    # also re-rotate the host's attached_accessories
                    # children. The library `<asset>.attached_accessories.json`
                    # sub matrices were emitted by the toolkit with an
                    # unconditional Ry(180°) post-multiply; when the
                    # host's own Ry(180°) is added by this swap, the two
                    # rotations compose so a child whose host-local frame
                    # used to land at world-identity now lands at world-
                    # Ry(180°) — visually 180° off (e.g. Baltimore Azur
                    # AGM019→AGM622, where the rangefinder/boats on top
                    # of the main turret face the wrong way relative to
                    # the corrected turret mesh). Consumer pre-multiplies
                    # each sub matrix by Ry(180°) when this flag is set.
                    p2["attached_y_flip"] = True
                    # Heal path doesn't otherwise reliably increment
                    # n_swapped (no aid change; misc_filter "changed"
                    # may be a no-op equality after a prior swap; etc.)
                    # so the caller's "did anything change?" gate would
                    # miss the matrix rewrite and skip the disk write-
                    # back on accessories.json. Count once for the
                    # heal so it actually persists.
                    if inferred_source_aid is not None:
                        n_swapped += 1

            new_items.append(p2)
        out[section] = new_items

    unused = (
        (set(by_asset_id) - used_aid_keys)
        | (set(by_hp_name) - used_hp_keys)
        | (set(dead_by_hp_name) - used_dead_hp_keys)
        | (set(misc_filter_by_hp) - used_misc_filter_hp_keys)
    )
    return (out, n_swapped, unused)


def derive_attach_to(doc: dict[str, Any]) -> dict[str, Any]:
    """Auto-derive ``attach_to`` for composite-hp_name placements.

    WG names sub-mounts that ride a parent mount with a composite hp_name
    of the form ``<parent_hp>_<child_hp>`` — e.g. ``HP_AGM_3_HP_AGA_4`` is
    a 40 mm Bofors AA mount sitting on top of the ``HP_AGM_3`` main turret.
    The visual frame of the child is the parent's *animated* frame (yaw
    follows turret traverse), so the child's transform must be applied
    relative to the parent at runtime.

    For every placement whose ``hp_name`` contains ``_HP_`` and whose
    ``attach_to`` is not already set, find the longest prefix that matches
    another placement's ``hp_name`` on the same ship and stamp that
    placement's ``instance_id`` as ``attach_to``. Hand-authored
    ``attach_to`` values (including explicitly-set non-None values) are
    preserved.

    Multi-level composites (``HP_A_HP_B_HP_C``) resolve to the longest
    valid prefix — i.e. ``HP_A_HP_B`` if it exists as a placement,
    otherwise ``HP_A``. This lets a sub-sub-mount parent under its
    immediate parent rather than the root.

    Composite hp_names whose prefix doesn't match any placement on the
    ship are left unparented and logged at debug level — they're either
    data bugs or refer to bones outside the placement set (e.g. raw
    skel_ext nodes that aren't gameplay mounts).

    Returns a new doc; ``doc`` is not mutated.
    """
    hp_to_instance: dict[str, str] = {}
    for section in PLACEMENT_SECTIONS:
        for p in doc.get(section, []) or []:
            if not isinstance(p, dict):
                continue
            hp = p.get("hp_name")
            iid = p.get("instance_id")
            if isinstance(hp, str) and isinstance(iid, str):
                hp_to_instance[hp] = iid

    out = dict(doc)
    for section in PLACEMENT_SECTIONS:
        items = out.get(section)
        if not isinstance(items, list):
            continue
        new_items: list[Any] = []
        for p in items:
            if not isinstance(p, dict):
                new_items.append(p)
                continue
            hp = p.get("hp_name")
            attach_value = p.get("attach_to")
            # Sentinel string ``"__suppress__"`` lets a hand-author
            # explicitly opt OUT of auto-derivation for one placement
            # without the next ingest silently re-stamping it. Plain
            # ``null`` continues to mean "derive on this pass" (the
            # canonical writer null-stamps every fresh placement so
            # existing fleet sidecars still re-derive correctly).
            if attach_value == "__suppress__":
                new_items.append(p)
                continue
            if (attach_value is None
                    and isinstance(hp, str)
                    and "_HP_" in hp):
                parent_hp = _longest_parent_hp_match(hp, hp_to_instance)
                if parent_hp is not None:
                    p2 = dict(p)
                    p2["attach_to"] = hp_to_instance[parent_hp]
                    new_items.append(p2)
                    continue
            new_items.append(p)
        out[section] = new_items
    return out


def _longest_parent_hp_match(
    composite_hp: str,
    hp_index: dict[str, str],
) -> str | None:
    """Return the longest ``_HP_``-bounded prefix of ``composite_hp`` that
    exists in ``hp_index``, or None if no prefix matches.

    Walks ``_HP_`` boundaries from the end, longest-first, so a
    triple-composite ``HP_A_HP_B_HP_C`` whose ``HP_A_HP_B`` exists resolves
    to ``HP_A_HP_B`` rather than ``HP_A``.
    """
    candidates: list[str] = []
    pos = len(composite_hp)
    while True:
        idx = composite_hp.rfind("_HP_", 0, pos)
        if idx <= 0:
            break
        candidates.append(composite_hp[:idx])
        pos = idx
    for c in candidates:
        if c in hp_index:
            return c
    return None


# ---------------------------------------------------------------------------
# GameParams-driven autofill (schema v3.1)
# ---------------------------------------------------------------------------
#
# Five independent passes consume a flat GameParams ship dict (returned by
# :func:`tools.shared.gameparams.get_ship`) and merge the derivable fields
# into the sidecar via :func:`merge_preserving`:
#
#   * :func:`absorb_gameparams_ship`     — archetype / peculiarity / paper_ship
#     and the full GameParams entity ID
#   * :func:`absorb_gameparams_variants` — top-level ``variants`` block
#   * :func:`absorb_gameparams_mounts`   — placement gameplay fields
#     (caliber_mm / barrel_count / yaw / elev / reload / sigma / ammo_types
#     / aa_range_km / aa_dps / tube_count / display_name)
#   * :func:`absorb_gameparams_armor`    — per-mount armor + barbettes
#   * :func:`absorb_gameparams_hitbox`   — per-cube classification
#     (``boxes`` + ``hit_locations``)
#
# All five accept the ship_dict directly so the caller controls when to load
# GameParams; that keeps this module bpy-free + import-safe. ``scaffold_ship``
# wires them together via :mod:`tools.shared.gameparams`.

def absorb_gameparams_ship(
    doc: dict[str, Any],
    ship_dict: dict[str, Any],
    *,
    full_ship_id: str | None = None,
) -> dict[str, Any]:
    """Merge ship-level metadata extras (archetype, peculiarity, paper_ship)
    into ``doc.ship``. Returns a new doc; ``doc`` is not mutated.

    ``full_ship_id`` is the GameParams full entity key (``PASB018_Iowa_1944``).
    Passing it stamps ``ship.wg_ship_full_id`` so downstream consumers can
    index the dump without re-resolving.
    """
    if not isinstance(ship_dict, dict):
        return doc
    update_ship: dict[str, Any] = {}
    archetype = ship_dict.get("archetype")
    if isinstance(archetype, str) and archetype and archetype != "Undefined":
        update_ship["archetype"] = archetype
    peculiarity = ship_dict.get("peculiarity")
    if isinstance(peculiarity, str) and peculiarity and peculiarity != "default":
        update_ship["peculiarity"] = peculiarity
    pec_flag = ship_dict.get("peculiarityFlag")
    if isinstance(pec_flag, str) and pec_flag:
        update_ship["peculiarity_flag"] = pec_flag
    if ship_dict.get("isPaperShip") is True:
        update_ship["paper_ship"] = True
    if isinstance(full_ship_id, str) and full_ship_id:
        update_ship["wg_ship_full_id"] = full_ship_id
    if not update_ship:
        return doc
    return merge_preserving(doc, {"ship": update_ship})


def absorb_per_hull_placements(
    doc: dict[str, Any],
    per_hull: dict[str, str | Path | dict[str, Any]],
    *,
    ship_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate ``doc.hulls[<hull_name>]`` from per-hull placements JSONs.

    ``per_hull`` maps the hull entity name (e.g. ``A_Hull``,
    ``B_Hull_1948`` — matching what ``ShipUpgradeInfo._Hull.<entry>.components.hull[0]``
    references) to a placements JSON path or already-parsed dict produced by
    ``wowsunpack export-ship --hull <H> --placements-json <P>``.

    For each hull, build a ``make_hull_entry`` record carrying the per-hull
    mount lists (turrets / secondaries / antiair / torpedoes / accessories).
    When ``ship_dict`` (a GameParams Ship entity) is provided, the
    ``module_id`` (PAUH...), ``is_stock`` flag, and the ``stats`` sub-block
    are also stamped from it. ``is_active`` mirrors ``doc.variants.active_hull``.

    Top-level placement arrays are NOT mutated by this call — they should
    already mirror the active hull (populated by ``absorb_placements_json``
    earlier in the pipeline). To keep the active-hull alias in sync after
    a fresh per-hull pass, call ``alias_active_hull_to_top_level(doc)``.

    Returns a new doc; ``doc`` is not mutated.
    """
    if not per_hull:
        return doc

    # Build a hull_name → module_id index from ShipUpgradeInfo. Each _Hull
    # entry carries components.hull = [hull_name]; we invert that.
    module_for_hull: dict[str, str] = {}
    stock_modules: set[str] = set()
    if isinstance(ship_dict, dict):
        sui = ship_dict.get("ShipUpgradeInfo") or {}
        if isinstance(sui, dict):
            for module_id, mod in sui.items():
                if not isinstance(mod, dict):
                    continue
                if mod.get("ucType") != "_Hull":
                    continue
                comps = mod.get("components") or {}
                hull_list = comps.get("hull") if isinstance(comps, dict) else []
                if isinstance(hull_list, list) and hull_list:
                    hull_name = hull_list[0]
                    module_for_hull[hull_name] = module_id
                    if mod.get("prev") == "":
                        stock_modules.add(module_id)

    active_hull = (doc.get("variants") or {}).get("active_hull")

    new_hulls: dict[str, Any] = dict(doc.get("hulls") or {})
    for hull_name, placements in per_hull.items():
        # Load placements (path or dict).
        if isinstance(placements, (str, Path)):
            with open(placements, "rb") as f:
                placements_dict = json.loads(f.read().decode("utf-8"))
        else:
            placements_dict = dict(placements or {})

        section_lists: dict[str, list[dict[str, Any]]] = {}
        for section in PLACEMENT_SECTIONS:
            items = placements_dict.get(section)
            if isinstance(items, list):
                section_lists[section] = [
                    _canonicalise_placement_strings(x) for x in items
                ]
            else:
                section_lists[section] = []

        # Derive attach_to for composite hp_names within this hull's
        # placement set. Without this, sub-mounts inside hulls.<H>.<section>
        # never resolve their parent (top-level absorb_placements_json's
        # call only covered the active-hull mirror).
        derived = derive_attach_to({s: list(section_lists[s]) for s in PLACEMENT_SECTIONS})
        for s in PLACEMENT_SECTIONS:
            section_lists[s] = derived.get(s, section_lists[s])

        module_id = module_for_hull.get(hull_name)
        is_stock  = module_id in stock_modules if module_id else False
        is_active = (hull_name == active_hull)

        stats: dict[str, Any] = {}
        if isinstance(ship_dict, dict):
            stats = _extract_hull_stats(ship_dict, hull_name)

        new_hulls[hull_name] = make_hull_entry(
            module_id=module_id,
            is_stock=is_stock,
            is_active=is_active,
            stats=stats,
            **section_lists,
        )

    out = dict(doc)
    out["hulls"] = new_hulls
    return out


def _extract_hull_stats(ship_dict: dict[str, Any], hull_name: str) -> dict[str, Any]:
    """Pull the survival- and movement-relevant numbers from ``ship[<hull>]``.

    Hit-zone HPs come from the per-zone subdicts (``Hull`` / ``Bow`` /
    ``Ammo_1`` / ``SS`` / ``SG`` / ``St`` / ``SSC`` / ``Engine`` / etc.) —
    each carries ``maxHP``. Burn-node timing comes from ``burnNodes[i][0]``
    (all four entries share the same float in the cases we've inspected, so
    we record just the first).
    """
    hull = ship_dict.get(hull_name)
    if not isinstance(hull, dict):
        return {}

    health = hull.get("health")
    rudder = hull.get("rudderTime")

    burn_nodes = hull.get("burnNodes")
    burn_first = None
    if isinstance(burn_nodes, list) and burn_nodes:
        first = burn_nodes[0]
        if isinstance(first, list) and first:
            try:
                burn_first = float(first[0])
            except (TypeError, ValueError):
                burn_first = None

    zone_hp: dict[str, float] = {}
    for zone_name, zone in hull.items():
        if not isinstance(zone, dict):
            continue
        max_hp = zone.get("maxHP")
        if max_hp is None:
            continue
        try:
            zone_hp[zone_name] = float(max_hp)
        except (TypeError, ValueError):
            pass

    return make_hull_stats(
        health=float(health) if health is not None else None,
        rudder_time_s=float(rudder) if rudder is not None else None,
        burn_node_time_s=burn_first,
        zone_hp=zone_hp or None,
    )


def alias_active_hull_to_top_level(doc: dict[str, Any]) -> dict[str, Any]:
    """Ensure top-level placement arrays mirror the active hull's mount set.

    Behaviour by section:
      - ``turrets`` / ``secondaries`` / ``antiair`` / ``torpedoes``:
        replaced wholesale with ``doc.hulls[<active>][<section>]``. These
        lists are HP_-bound only on both sides; the per-hull split is
        authoritative for which mounts exist when the active hull is
        equipped.
      - ``accessories``: split into HP_-bound mounts (which differ per
        hull tier — directors, radar, capability mounts) and decoratives
        (hull-agnostic — vents, hatches, smoke gear, fairleads, …).
        The HP_-bound subset is replaced from the active hull; decoratives
        survive untouched. Decoratives are recognised as entries whose
        ``instance_id`` does NOT appear in any per-hull
        ``hulls.<H>.accessories`` list (set membership across all hulls,
        so a mount that only exists on the *non*-active hull also gets
        treated as HP_-bound and dropped from top-level when not active).

    No-op when no ``hulls`` block exists or no entry is ``is_active``.
    Returns a new doc; ``doc`` is not mutated.
    """
    hulls = doc.get("hulls")
    if not isinstance(hulls, dict):
        return doc
    active_entry = None
    for entry in hulls.values():
        if isinstance(entry, dict) and entry.get("is_active"):
            active_entry = entry
            break
    if active_entry is None:
        return doc

    out = dict(doc)

    # Wholesale-replace the strictly-HP_-bound sections.
    for section in ("turrets", "secondaries", "antiair", "torpedoes"):
        items = active_entry.get(section)
        if isinstance(items, list):
            out[section] = [dict(x) if isinstance(x, dict) else x for x in items]

    # Accessories: preserve decoratives, swap the HP_-bound subset.
    hp_bound_ids: set[str] = set()
    for hull_entry in hulls.values():
        if not isinstance(hull_entry, dict):
            continue
        for entry in hull_entry.get("accessories") or []:
            if isinstance(entry, dict):
                iid = entry.get("instance_id")
                if isinstance(iid, str):
                    hp_bound_ids.add(iid)

    existing_acc = doc.get("accessories")
    if isinstance(existing_acc, list):
        decoratives = [
            dict(p) if isinstance(p, dict) else p
            for p in existing_acc
            if not (isinstance(p, dict) and p.get("instance_id") in hp_bound_ids)
        ]
    else:
        decoratives = []

    active_hp_acc = active_entry.get("accessories")
    if isinstance(active_hp_acc, list):
        active_hp_acc = [dict(x) if isinstance(x, dict) else x for x in active_hp_acc]
    else:
        active_hp_acc = []

    out["accessories"] = active_hp_acc + decoratives
    return out


def absorb_gameparams_variants(
    doc: dict[str, Any],
    ship_dict: dict[str, Any] | None = None,
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace ``doc.variants`` with a freshly-built section.

    ``summary`` is the dict returned by
    :func:`tools.shared.gameparams.variants_summary`. When omitted (or
    falsy) the section is left as-is so callers without a loaded
    GameParams dump are no-ops.

    Replacement (not merge) semantics: WG patch-removing a hull / module
    upgrade should drop from the sidecar, not linger. Hand-edits to
    ``variants`` aren't supported anyway.
    """
    if not summary:
        return doc
    out = _deepcopy_jsonish(doc)
    out["variants"] = make_variants(**summary)
    return out


def absorb_gameparams_mounts(
    doc: dict[str, Any],
    autofill_by_hp: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Stamp GameParams-derived gameplay fields onto every placement whose
    ``hp_name`` appears in ``autofill_by_hp``.

    ``autofill_by_hp`` maps hardpoint name → field dict, typically built by
    iterating placements and calling
    :func:`tools.shared.gameparams.autofill_for_hp` per HP. The merge runs
    via :func:`merge_preserving` so per-instance hand-edits in earlier
    rebuilds (e.g. user-tweaked ``display_name``) are overwritten by
    fresh GameParams values. Hand-edits that should survive belong on
    fields GameParams doesn't carry (``attach_to`` / ``casts_shadow``).

    Returns a new doc; ``doc`` is not mutated.
    """
    if not autofill_by_hp:
        return doc
    placement_hps: set[str] = set()
    update: dict[str, list[dict[str, Any]]] = {}
    for section in PLACEMENT_SECTIONS:
        items = doc.get(section)
        if not isinstance(items, list):
            continue
        section_updates: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            iid = entry.get("instance_id")
            hp = entry.get("hp_name")
            if not isinstance(iid, str) or not isinstance(hp, str):
                continue
            placement_hps.add(hp)
            extras = autofill_by_hp.get(hp)
            if not extras:
                continue
            section_updates.append({"instance_id": iid, **extras})
        if section_updates:
            update[section] = section_updates
    # Surface autofill keys that didn't resolve to any placement —
    # typically a stale GameParams scrape (typo'd hp_name, or a mount
    # the patch removed). Silent drift was the prior behaviour.
    stale = sorted(set(autofill_by_hp) - placement_hps)
    if stale:
        preview = ", ".join(stale[:5])
        tail = f" (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        print(
            f"  warn: absorb_gameparams_mounts: {len(stale)} autofill "
            f"key(s) didn't match any placement: {preview}{tail}",
            file=sys.stderr,
        )
    if not update:
        out = doc
    else:
        out = merge_preserving(doc, update)
    # Strip phantom `misc_filter_mode` lingering on placements scaffolded
    # before 2026-05-09. The runtime never read the field; consumers
    # ignored it; emission was dropped. Active strip here lets re-runs
    # of old ships shed the orphan key without a separate migration.
    for section in PLACEMENT_SECTIONS:
        items = out.get(section)
        if not isinstance(items, list):
            continue
        for entry in items:
            if isinstance(entry, dict):
                entry.pop("misc_filter_mode", None)
    return out


def absorb_gameparams_armor(
    doc: dict[str, Any],
    *,
    mount_armor: dict[str, dict[str, float]] | None = None,
    barbettes: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Replace ``doc.armor.mount_armor`` and ``doc.armor.barbettes``
    wholesale from a GameParams scrape.

    Replacement (not merge) semantics: when WG removes a mount in a patch,
    its stale entry in ``mount_armor`` / ``barbettes`` should drop. Other
    ``armor.*`` fields (toolkit-emitted ``materials_table`` / ``zones`` /
    ``hidden_zones``) are untouched. Pass ``None`` for either kwarg to
    leave that subsection alone (typically because GameParams wasn't
    available for this ship). Returns a new doc.
    """
    if mount_armor is None and barbettes is None:
        return doc
    out = _deepcopy_jsonish(doc)
    armor = out.get("armor")
    if not isinstance(armor, dict):
        armor = make_armor()
        out["armor"] = armor
    if mount_armor is not None:
        armor["mount_armor"] = dict(mount_armor)
    if barbettes is not None:
        armor["barbettes"] = dict(barbettes)
    return out


def absorb_gameparams_hitbox(
    doc: dict[str, Any],
    *,
    boxes: dict[str, dict[str, Any]] | None = None,
    hit_locations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replace ``doc.hitbox.boxes`` and ``doc.hitbox.hit_locations``
    wholesale from a GameParams classification.

    Replacement (not merge) semantics for the same reason as
    :func:`absorb_gameparams_armor`: stale entries from removed cubes
    must drop on re-run. The toolkit-emitted ``regions`` / ``region_count``
    / ``source_glb`` fields are untouched. Pass ``None`` for either kwarg
    to skip. Returns a new doc.
    """
    if boxes is None and hit_locations is None:
        return doc
    out = _deepcopy_jsonish(doc)
    hitbox = out.get("hitbox")
    if not isinstance(hitbox, dict):
        hitbox = make_hitbox()
        out["hitbox"] = hitbox
    if boxes is not None:
        hitbox["boxes"] = dict(boxes)
    if hit_locations is not None:
        hitbox["hit_locations"] = dict(hit_locations)
    return out


def absorb_gameparams_torpedoes(
    doc: dict[str, Any],
    torpedo_profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Stamp PAPT* GameParams-derived fields onto every entry in
    ``doc.ballistics.torpedoes``.

    ``torpedo_profiles`` maps ammo_id → field dict (typically built by
    iterating the existing ``ballistics.torpedoes`` keys + calling
    :func:`tools.shared.gameparams.torpedo_profile_extras` per ammo_id).
    Entries not in the dict are left untouched.

    Replacement-on-key semantics for the entry: each torpedo's GameParams
    fields are merged onto the toolkit-emitted core (caliber_mm,
    alpha_damage, max_range_m, ...) via :func:`merge_preserving`. The
    behaviour matches :func:`absorb_gameparams_mounts` — GameParams wins
    on conflict; toolkit-emitted fields stay where GameParams doesn't
    provide them.

    Returns a new doc; ``doc`` is not mutated.
    """
    if not torpedo_profiles:
        return doc
    ballistics = doc.get("ballistics") or {}
    existing = ballistics.get("torpedoes") if isinstance(ballistics, dict) else None
    if not isinstance(existing, dict) or not existing:
        # No torpedo entries to enrich — likely a ship without torps.
        return doc
    update_torps: dict[str, dict[str, Any]] = {}
    for ammo_id, extras in torpedo_profiles.items():
        if not extras or ammo_id not in existing:
            continue
        update_torps[ammo_id] = extras
    if not update_torps:
        return doc
    return merge_preserving(doc, {"ballistics": {"torpedoes": update_torps}})


def absorb_ballistics_json(
    doc: dict[str, Any],
    ballistics: str | Path | dict[str, Any],
) -> dict[str, Any]:
    """Merge the toolkit's per-ship ballistics JSON into ``doc``.

    Accepts either a path (str / Path) or the already-parsed dict produced by
    ``wowsunpack ammo --json <path>``. Pulls:

    * ``shells`` — the per-ammo Projectile profiles, keyed by ammo_id.
    * ``ranges`` — aggregate per-hull battery + detection ranges.
    * ``pipeline.toolkit_version`` — recorded into ``ballistics.source``
      together with a ``generated_at`` ISO8601 timestamp.

    **Ballistics is toolkit-authoritative.** Field-level hand-edits to
    ``shells.<id>.<field>`` are overwritten by every absorb call (the toolkit
    re-reads from GameParams each run). To override values for downstream
    consumers (e.g. balance tweaks), apply them at the consumer layer
    (Unity component, sim) — not in the sidecar. ``scaffold_ship.py``
    additionally strips ``ballistics`` before re-absorbing so shells removed
    in a game patch don't linger as stale entries.

    The merge is still :func:`merge_preserving`-style at the section level,
    so ``doc``'s other sections are untouched.

    Returns a new doc; ``doc`` is not mutated.
    """
    if isinstance(ballistics, (str, Path)):
        with open(ballistics, "rb") as f:
            data = f.read().decode("utf-8")
        ammo = json.loads(data)
    else:
        ammo = ballistics

    if not isinstance(ammo, dict):
        raise SidecarSchemaError(
            "ballistics JSON root must be an object with at least 'shells'"
        )

    source: dict[str, Any] = {"generated_at": _now_iso()}
    pipeline = ammo.get("pipeline")
    if isinstance(pipeline, dict):
        tv = pipeline.get("toolkit_version")
        if isinstance(tv, str) and tv:
            source["toolkit_version"] = tv

    raw_shells = ammo.get("shells")
    ranges = ammo.get("ranges")

    # Bucket the toolkit's flat ``shells`` dict by ``ammo_type``: PAPT*
    # torpedo entries (``ammo_type == "torpedo"``) move to ``torpedoes``;
    # everything else (AP / HE / CS / unknowns) stays in ``shells``. When
    # we move an entry, drop the gun-only fields it can't fill (mass /
    # muzzle velocity / krupp / fuze / ricochet) so the torpedo entry
    # carries only its meaningful fields. The GameParams autofill pass
    # later enriches these with PAPT-only data (speed, depth, …).
    shells_out: dict[str, dict[str, Any]] = {}
    torpedoes_out: dict[str, dict[str, Any]] = {}
    if isinstance(raw_shells, dict):
        for ammo_id, entry in raw_shells.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("ammo_type") == "torpedo":
                torpedoes_out[ammo_id] = make_torpedo_profile(
                    ammo_type="torpedo",
                    caliber_mm=entry.get("caliber_mm"),
                    alpha_damage=entry.get("alpha_damage"),
                    alpha_piercing_he_mm=entry.get("alpha_piercing_he_mm"),
                    max_range_m=entry.get("max_range_m"),
                    burn_probability=entry.get("burn_probability"),
                )
            else:
                shells_out[ammo_id] = dict(entry)

    section = make_ballistics(
        source=source,
        ranges=ranges if isinstance(ranges, dict) else None,
        shells=shells_out or None,
        torpedoes=torpedoes_out or None,
    )

    # Replace-on-key for shells / torpedoes / ranges / source: the toolkit
    # is authoritative for ballistics and a game patch may remove ammo IDs.
    # merge_preserving alone would let stale entries from a previous patch
    # linger forever (the caller-side strip was easy to forget). Strip those
    # specific keys before the merge so other hand-edited fields under
    # ``ballistics`` (if any) survive.
    out = dict(doc)
    bal = dict(out.get("ballistics") or {})
    for k in ("shells", "torpedoes", "ranges", "source"):
        bal.pop(k, None)
    out["ballistics"] = bal
    return merge_preserving(out, {"ballistics": section})


def _canonicalise_placement_strings(entry: Any) -> Any:
    """Lowercase the toolkit-emitted ``species`` on a placement entry (in
    a shallow copy). Values like ``"Main"``, ``"AAircraft"``,
    ``"FireControl"`` become ``"main"``, ``"aaircraft"``, ``"firecontrol"``
    — matching the lowercase run-on pattern used by ``subcategory`` /
    ``scope`` / ``category``."""
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    sp = out.get("species")
    if isinstance(sp, str) and sp:
        out["species"] = sp.lower()
    return out


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def sidecar_path_for(ship_dir: str | Path, ship_name: str) -> Path:
    """Canonical sidecar path: ``<ship_dir>/<ship_name>.meta.json``."""
    return Path(ship_dir) / (ship_name + SIDECAR_SUFFIX)


def build_ship_key(
    nation: str | None,
    cls: str | None,
    ship_name: str,
    suffix: str | None = None,
) -> str:
    """Construct the canonical ``ship_key``.

    Format: ``{NATION}_{ClassLong}_{ShipName}[_{suffix}]``.
    """
    class_long = {
        "CA": "Cruiser", "CL": "Cruiser", "BC": "Cruiser", "CB": "Cruiser",
        "BB": "Battleship",
        "DD": "Destroyer",
        "CV": "Carrier",
        "SS": "Submarine",
        "AUX": "Auxiliary",
    }.get((cls or "").strip().upper(), (cls or "Ship").capitalize())
    nation_u = (nation or "ship").upper()
    parts = [nation_u, class_long, ship_name]
    if suffix:
        parts.append(suffix)
    return "_".join(parts)


# Mapping from toolkit `Species` strings (from `placements.ship.species`) to
# our 2-letter class codes used in `ship.class` and `build_ship_key`.
# If the toolkit emits a species we don't recognize, fall through to the
# capitalized species string (so "Submarine" → "SS", anything novel shows up
# uppercased for manual triage).
_SPECIES_TO_CLASS = {
    "Destroyer":   "DD",
    "Cruiser":     "CA",
    "Battleship":  "BB",
    "AirCarrier":  "CV",
    "Submarine":   "SS",
}


def ship_from_placements(
    placements: str | Path | dict[str, Any],
    *,
    class_override: str | None = None,
    auto_derived_class: str | None = None,
    ship_key_suffix: str | None = None,
) -> dict[str, Any]:
    """Build a sidecar ``ship`` section from a toolkit placements JSON.

    Resolves the toolkit-emitted ``ship`` fields (``model_dir``,
    ``display_name``, ``param_index``, ``nation``, ``species``, ``tier``)
    into the sidecar's canonical shape — lowercased nation + asset_id,
    2-letter class code, ship_key built from the three. Returns a dict
    suitable for passing into :func:`new_document` as ``ship=…``.

    ``placements`` — path to the JSON file or the parsed dict.

    ``class_override`` — explicit 2-letter code that wins over everything
    else. Use this only when the user has hand-typed the right class
    (e.g. ``--class-override BC`` for a battlecruiser that GameParams
    can't disambiguate).

    ``auto_derived_class`` — 2-letter code derived from authoritative
    data (typically caliber-based via
    :func:`tools.shared.gameparams.class_from_caliber`). Wins over the
    species-only mapping, loses to ``class_override``. Pass-through here
    keeps the stdlib-only sidecar invariant — sidecar doesn't import
    gameparams; the caller (scaffold_ship) does.

    Class-resolution precedence: ``class_override`` > ``auto_derived_class``
    > :data:`_SPECIES_TO_CLASS` species lookup > ``species[:2].upper()``.

    ``ship_key_suffix`` — optional trailing segment in the canonical
    ship_key, for ship-variants like ``"Scharnhorst_B"`` that share the
    base name but differ in hull upgrade.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, encoding="utf-8") as f:
            placements = json.load(f)
    if not isinstance(placements, dict):
        raise TypeError(f"ship_from_placements: expected dict or path, got {type(placements).__name__}")
    ship_section = placements.get("ship")
    if not isinstance(ship_section, dict):
        raise SidecarSchemaError("placements JSON missing 'ship' section")

    model_dir    = ship_section.get("model_dir", "")
    display_name = ship_section.get("display_name") or model_dir
    param_index  = ship_section.get("param_index") or None
    nation_raw   = ship_section.get("nation", "") or ""
    species      = ship_section.get("species", "") or ""
    tier         = ship_section.get("tier", 0) or 0

    nation = nation_raw.lower()
    cls = (
        class_override
        or auto_derived_class
        or _SPECIES_TO_CLASS.get(species, (species[:2].upper() if species else None))
    )
    wg_asset_id = model_dir.lower() if model_dir else None
    # display_name is preserved as WG emitted it (may carry the toolkit's
    # disambiguation parenthetical, e.g. "Baltimore (old)" / "U-2501 (old)").
    # ship_key, however, ends up in filesystem paths, Unity asset IDs, and
    # URL fragments — strip the trailing parenthetical + collapse spaces
    # before deriving it. Any explicit `--ship-key-suffix` is appended in
    # `build_ship_key` after this sanitization.
    ship_key_name = _sanitize_for_ship_key(display_name)
    ship_key = build_ship_key(nation or None, cls, ship_key_name, suffix=ship_key_suffix)

    return make_ship(
        ship_key     = ship_key,
        display_name = display_name,
        wg_asset_id  = wg_asset_id,
        wg_ship_id   = param_index,
        nation       = nation or None,
        cls          = cls,
        tier         = int(tier) if tier else None,
    )


_SHIP_KEY_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _sanitize_for_ship_key(name: str) -> str:
    """Strip trailing parentheticals + collapse whitespace so the derived
    ``ship_key`` stays filesystem- and URL-safe.

    Toolkit emits display_names like ``"Baltimore (old)"`` or
    ``"U-2501 (old)"`` when its fuzzy ship-name resolver disambiguates
    multiple candidates. Embedding the parenthetical into ship_key bleeds
    spaces + parens into filesystem paths and Unity asset IDs. Display
    name itself stays as WG emitted it (UIs need the original); only the
    derived key is sanitized.

    Examples:
        "Montana"            -> "Montana"
        "Baltimore (old)"    -> "Baltimore"
        "U-2501 (old)"       -> "U-2501"
        "spaced  name"       -> "spaced_name"
    """
    cleaned = _SHIP_KEY_TRAILING_PAREN.sub("", name).strip()
    return "_".join(cleaned.split()) if cleaned else name


def _load_glb_json_chunk(glb_path: str | Path) -> dict[str, Any]:
    """Read just the JSON chunk of a GLB and return the parsed dict.

    Factored out so :func:`geometry_from_hull_glb` and
    :func:`hitbox_from_hull_glb` can share a single file read when
    callers go through :func:`geometry_and_hitbox_from_hull_glb`.
    """
    import struct
    p = Path(glb_path)
    with open(p, "rb") as f:
        header = f.read(12)
        if len(header) < 12:
            raise ValueError(f"{p}: file too short to be a GLB")
        magic, _ver, _total = struct.unpack("<4sII", header)
        if magic != b"glTF":
            raise ValueError(f"{p}: not a valid GLB (magic={magic!r})")
        chunk_len, chunk_type = struct.unpack("<I4s", f.read(8))
        if chunk_type != b"JSON":
            raise ValueError(f"{p}: expected JSON chunk, got {chunk_type!r}")
        return json.loads(f.read(chunk_len))


def geometry_from_hull_glb(
    glb_path: str | Path,
    *,
    group_name: str = "Hull",
    native_scale_m: float = 1.0,
    waterline_y: float = 0.0,
    keel_y: float | None = None,
    _gltf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute ``geometry.bounds`` (and optionally ``draft_m`` / ``keel_y``)
    by walking the hull GLB and aggregating the AABB of the named
    top-level group.

    The toolkit now emits hull vertices pre-scaled to metres, so
    ``native_scale_m`` defaults to ``1.0`` (pass-through). Set to ``15.0``
    only when reading a pre-bake GLB (pre-2026-04-23 exports) that still
    has native WoWS units (1 u ≈ 15 m). Axis mapping assumes glTF's Y-up
    convention: ``height_m`` is the Y-extent, ``length_m`` is the max of
    the other two axes, ``beam_m`` is the min.

    Only meshes under the ``group_name`` subtree count (defaults to
    ``"Hull"``), so hitbox cubes + armor meshes don't inflate the bounds.
    Node transforms (either ``matrix`` or ``translation``/``rotation``/
    ``scale``) are accumulated; each primitive's accessor ``min``/``max``
    defines a local AABB, and the 8 corners are transformed into scene
    space for a loose-but-correct world AABB.

    Args:
      glb_path:        Path to the hull GLB.
      group_name:      Top-level node to aggregate under. Defaults to ``"Hull"``.
      native_scale_m:  Multiplier applied to raw GLB extents. Defaults to
                       1.0 (new metric GLBs). Use 15.0 for legacy native GLBs.
      waterline_y:     Passed through into ``geometry.hull.waterline_y``.
      keel_y:          If ``None``, computed as the AABB min-Y (metric).
      _gltf:           Pre-parsed glTF dict (private). Passed by
                       :func:`geometry_and_hitbox_from_hull_glb` to avoid
                       re-reading the GLB header + JSON chunk twice.

    Returns:
      A full ``geometry`` section dict with ``bounds`` populated. If the
      GLB has no matching group or mesh data, returns an all-zeros
      geometry (same as :func:`make_geometry` with defaults).
    """
    import math

    gltf = _gltf if _gltf is not None else _load_glb_json_chunk(glb_path)

    nodes = gltf.get("nodes", [])
    accessors = gltf.get("accessors", [])
    meshes = gltf.get("meshes", [])
    scenes = gltf.get("scenes", [])
    if not nodes or not scenes:
        return make_geometry(waterline_y=waterline_y)
    root_nodes = scenes[gltf.get("scene", 0)].get("nodes", [])
    group_idx = next(
        (i for i in root_nodes if nodes[i].get("name") == group_name),
        None,
    )
    if group_idx is None:
        return make_geometry(waterline_y=waterline_y)

    _IDENT = [1.0, 0.0, 0.0, 0.0,
              0.0, 1.0, 0.0, 0.0,
              0.0, 0.0, 1.0, 0.0,
              0.0, 0.0, 0.0, 1.0]

    def _trs_to_matrix(t: list[float], r: list[float], s: list[float]) -> list[float]:
        # glTF: column-major, quaternion (qx,qy,qz,qw), M = T * R * S.
        qx, qy, qz, qw = r
        sx, sy, sz = s
        tx, ty, tz = t
        m = [0.0] * 16
        m[0]  = (1 - 2*(qy*qy + qz*qz)) * sx
        m[1]  = 2*(qx*qy + qz*qw) * sx
        m[2]  = 2*(qx*qz - qy*qw) * sx
        m[4]  = 2*(qx*qy - qz*qw) * sy
        m[5]  = (1 - 2*(qx*qx + qz*qz)) * sy
        m[6]  = 2*(qy*qz + qx*qw) * sy
        m[8]  = 2*(qx*qz + qy*qw) * sz
        m[9]  = 2*(qy*qz - qx*qw) * sz
        m[10] = (1 - 2*(qx*qx + qy*qy)) * sz
        m[12] = tx
        m[13] = ty
        m[14] = tz
        m[15] = 1.0
        return m

    def _node_local_matrix(n: dict) -> list[float]:
        if "matrix" in n:
            return list(n["matrix"])
        if any(k in n for k in ("translation", "rotation", "scale")):
            return _trs_to_matrix(
                list(n.get("translation", [0.0, 0.0, 0.0])),
                list(n.get("rotation",    [0.0, 0.0, 0.0, 1.0])),
                list(n.get("scale",       [1.0, 1.0, 1.0])),
            )
        return _IDENT

    def _mat_mul(a: list[float], b: list[float]) -> list[float]:
        out = [0.0] * 16
        for col in range(4):
            for row in range(4):
                s = 0.0
                for k in range(4):
                    s += a[k*4 + row] * b[col*4 + k]
                out[col*4 + row] = s
        return out

    def _transform_point(m: list[float], x: float, y: float, z: float) -> tuple:
        return (
            m[0]*x + m[4]*y + m[8]*z  + m[12],
            m[1]*x + m[5]*y + m[9]*z  + m[13],
            m[2]*x + m[6]*y + m[10]*z + m[14],
        )

    aabb_min = [math.inf]  * 3
    aabb_max = [-math.inf] * 3

    def _walk(node_idx: int, world: list[float]) -> None:
        n = nodes[node_idx]
        local = _node_local_matrix(n)
        world_here = _mat_mul(world, local) if local is not _IDENT else world
        mi = n.get("mesh")
        if mi is not None and 0 <= mi < len(meshes):
            for prim in meshes[mi].get("primitives", []):
                pos_idx = prim.get("attributes", {}).get("POSITION")
                if pos_idx is None:
                    continue
                acc = accessors[pos_idx]
                lo = acc.get("min")
                hi = acc.get("max")
                if not lo or not hi or len(lo) < 3 or len(hi) < 3:
                    continue
                for cx in (lo[0], hi[0]):
                    for cy in (lo[1], hi[1]):
                        for cz in (lo[2], hi[2]):
                            x, y, z = _transform_point(world_here, cx, cy, cz)
                            if x < aabb_min[0]:
                                aabb_min[0] = x
                            if y < aabb_min[1]:
                                aabb_min[1] = y
                            if z < aabb_min[2]:
                                aabb_min[2] = z
                            if x > aabb_max[0]:
                                aabb_max[0] = x
                            if y > aabb_max[1]:
                                aabb_max[1] = y
                            if z > aabb_max[2]:
                                aabb_max[2] = z
        for c in n.get("children", []):
            _walk(c, world_here)

    _walk(group_idx, _IDENT)

    if aabb_min[0] == math.inf:
        return make_geometry(waterline_y=waterline_y)

    extent = [(aabb_max[i] - aabb_min[i]) * native_scale_m for i in range(3)]
    # Y-up → height is Y; pick length = longer of remaining two axes.
    height_m = extent[1]
    length_m = max(extent[0], extent[2])
    beam_m   = min(extent[0], extent[2])
    keel = (aabb_min[1] * native_scale_m) if keel_y is None else keel_y
    waterline = waterline_y
    draft_m = max(0.0, waterline - keel)

    return make_geometry(
        length_m     = length_m,
        beam_m       = beam_m,
        height_m     = height_m,
        waterline_y  = waterline,
        keel_y       = keel,
        draft_m      = draft_m,
    )


def hitbox_from_hull_glb(
    glb_path: str | Path,
    *,
    source_glb: str | None = None,
    _gltf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract the "Hitboxes" group from a hull GLB and build a sidecar
    ``hitbox`` section.

    The toolkit emits splash-box AABBs as cube meshes under a top-level
    ``Hitboxes`` node (see ``export/gltf_export.rs::export_ship_glb``).
    Each child keeps its raw ``CM_SB_<zone>_<n>[_<m>]`` name. This helper
    walks the scene graph, normalises each name via
    :func:`normalise_hitbox_token`, and returns a dict suitable for
    :func:`make_hitbox`.

    Args:
      glb_path: Path to the hull GLB.
      source_glb: Filename to store in ``hitbox.source_glb`` (defaults to
        the GLB's basename).
      _gltf: Pre-parsed glTF dict (private). Passed by
        :func:`geometry_and_hitbox_from_hull_glb` to avoid re-reading
        the GLB header + JSON chunk twice.

    Returns:
      A ``hitbox`` section dict. Empty regions / region_count=0 if the
      GLB has no Hitboxes group (older hulls that lacked ``.splash``
      files).
    """
    p = Path(glb_path)
    gltf = _gltf if _gltf is not None else _load_glb_json_chunk(p)

    nodes = gltf.get("nodes", [])
    scenes = gltf.get("scenes", [])
    if not nodes or not scenes:
        return make_hitbox(source_glb=source_glb or p.name)
    root_nodes = scenes[gltf.get("scene", 0)].get("nodes", [])
    hitbox_group_idx = next(
        (i for i in root_nodes if nodes[i].get("name") == "Hitboxes"),
        None,
    )
    if hitbox_group_idx is None:
        return make_hitbox(source_glb=source_glb or p.name)

    children = nodes[hitbox_group_idx].get("children", [])
    # Aggregate per canonical zone. Track raw tokens per zone to expose
    # the alias (useful for zones where the raw name is non-obvious, e.g.
    # "cit" → "citadel", "ruder" → "steeringgear").
    regions: dict[str, dict[str, Any]] = {}
    raws_by_zone: dict[str, set[str]] = {}
    total = 0
    for child_idx in children:
        name = nodes[child_idx].get("name", "")
        if not name.startswith("CM_SB_"):
            continue
        stem = name[len("CM_SB_"):]
        # Strip trailing instance suffixes: ``_1``, ``_1_1``, ``_12`` etc.
        raw = stem
        while raw and (raw[-1].isdigit() or raw.endswith("_")):
            raw = raw.rstrip("0123456789").rstrip("_")
        canonical = normalise_hitbox_token(name)
        entry = regions.setdefault(canonical, {"box_count": 0})
        entry["box_count"] += 1
        if raw:
            raws_by_zone.setdefault(canonical, set()).add(raw)
        total += 1

    # Only expose raw alias when it differs from the canonical zone name.
    for zone, raws in raws_by_zone.items():
        non_trivial = sorted(r for r in raws if r != zone)
        if not non_trivial:
            continue
        if len(non_trivial) == 1:
            regions[zone]["raw_name"] = non_trivial[0]
        else:
            regions[zone]["raw_names"] = non_trivial

    return make_hitbox(
        source_glb=source_glb or p.name,
        region_count=total,
        regions=regions,
    )


def geometry_and_hitbox_from_hull_glb(
    glb_path: str | Path,
    *,
    geometry_kwargs: dict[str, Any] | None = None,
    hitbox_source_glb: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk a hull GLB once and return both the ``geometry`` and ``hitbox``
    sections.

    Equivalent to calling :func:`geometry_from_hull_glb` and
    :func:`hitbox_from_hull_glb` separately, but reads + parses the GLB
    file's JSON chunk only once. The two walks inspect disjoint top-level
    groups (``Hull`` and ``Hitboxes``), so they don't interfere.

    Args:
      glb_path:           Path to the hull GLB.
      geometry_kwargs:    Optional kwargs forwarded to
                          :func:`geometry_from_hull_glb` (e.g.
                          ``waterline_y``, ``native_scale_m``).
      hitbox_source_glb:  Forwarded to :func:`hitbox_from_hull_glb` as
                          ``source_glb``.

    Returns:
      ``(geometry, hitbox)`` — two dicts ready to land at
      ``doc["geometry"]`` / ``doc["hitbox"]``.
    """
    gltf = _load_glb_json_chunk(glb_path)
    geometry = geometry_from_hull_glb(
        glb_path, _gltf=gltf, **(geometry_kwargs or {}),
    )
    hitbox = hitbox_from_hull_glb(
        glb_path, source_glb=hitbox_source_glb, _gltf=gltf,
    )
    return geometry, hitbox


def new_document_from_placements(
    placements: str | Path | dict[str, Any],
    *,
    class_override: str | None = None,
    auto_derived_class: str | None = None,
    ship_key_suffix: str | None = None,
    stages_completed: list[int] | None = None,
) -> dict[str, Any]:
    """Build an initial sidecar document from a placements JSON — combines
    :func:`new_document`, :func:`make_pipeline`, :func:`ship_from_placements`,
    and :func:`absorb_placements_json` into one call.

    Typical usage::

        doc = sidecar.new_document_from_placements(
            "Fletcher/models/Fletcher_accessories.json",
        )
        # optional: merge armor table, materials, skins, etc.
        sidecar.write(doc, "Fletcher/Fletcher.meta.json")

    After this call, ``doc`` has a filled-in ``pipeline`` + ``ship`` section
    and every typed placement section populated from the toolkit output.
    Hand-authored overrides should be merged on top via
    :func:`merge_preserving`.

    See :func:`ship_from_placements` for the ``class_override`` /
    ``auto_derived_class`` precedence rules.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, encoding="utf-8") as f:
            placements_data = json.load(f)
    else:
        placements_data = placements

    ship = ship_from_placements(
        placements_data,
        class_override=class_override,
        auto_derived_class=auto_derived_class,
        ship_key_suffix=ship_key_suffix,
    )
    doc = new_document(
        pipeline=make_pipeline(stages_completed=stages_completed or [0, 7]),
        ship=ship,
    )
    # Pass the parsed data dict through so absorb doesn't reopen the file.
    doc = absorb_placements_json(doc, placements_data)
    return doc


def normalise_hitbox_token(token: str) -> str:
    """Return the canonical zone name for a raw splash-box or GameParams
    hitLocations token. Unknown tokens pass through lowercase.

    Accepts ``CM_SB_bow_1``, ``ruder``, ``SteeringGear``, ``ss_3``,
    ``gk_1_1`` (multi-level per-turret-barbette index) etc. Strips the
    ``CM_SB_`` prefix and iteratively peels trailing
    integer/underscore components until a known token appears in
    :data:`HITBOX_TOKEN_MAP` or no further stripping is possible.
    """
    t = token.strip()
    if t.startswith("CM_SB_"):
        t = t[len("CM_SB_"):]
    # Full-token lookup first, then case-insensitive fallback for raw
    # tokens that came through with mixed case (e.g. ``Stearinggear`` from
    # a hand-edited GameParams scrape).
    if t in HITBOX_TOKEN_MAP:
        return HITBOX_TOKEN_MAP[t]
    if t.lower() in HITBOX_TOKEN_MAP:
        return HITBOX_TOKEN_MAP[t.lower()]
    # Strip trailing ``_<n>`` groups iteratively, checking the map at each
    # level. Examples: ``gk_1_1`` → ``gk_1`` → ``gk`` (map hit → citadel);
    # ``ss_3_4`` → ``ss_3`` → ``ss`` (map hit → superstructure).
    stripped = t
    while stripped:
        if stripped in HITBOX_TOKEN_MAP:
            return HITBOX_TOKEN_MAP[stripped]
        low = stripped.lower()
        if low in HITBOX_TOKEN_MAP:
            return HITBOX_TOKEN_MAP[low]
        next_stripped = stripped.rstrip("0123456789").rstrip("_")
        if next_stripped == stripped:
            break
        stripped = next_stripped
    return (stripped or t).lower()


# ---------------------------------------------------------------------------
# Internal normalisers
# ---------------------------------------------------------------------------

def _normalise_transform(t: dict[str, Any] | None) -> dict[str, Any]:
    """Validate + shape a placement transform dict.

    Required: ``matrix`` (16 floats, column-major, metric). Optional but
    recommended: ``position`` (3 floats, convenience readout).
    """
    if not isinstance(t, dict):
        raise ValueError("transform must be an object with a 'matrix' key")
    matrix = t.get("matrix")
    if matrix is None:
        raise ValueError("transform.matrix is required (16 floats, column-major)")
    if len(list(matrix)) != 16:
        raise ValueError("transform.matrix must be 16 floats (column-major)")
    out: dict[str, Any] = {"matrix": [float(v) for v in matrix]}
    pos = t.get("position")
    if pos is not None:
        pos_list = [float(v) for v in pos]
        if len(pos_list) != 3:
            raise ValueError("transform.position must be 3 floats")
        out["position"] = pos_list
    return out


def _normalise_zone_dict(
    zones: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalise armor-zone keys to lowercase canonical form (matching
    ``hitbox.regions``). Toolkit emits CamelCase (``Bow``, ``SteeringGear``,
    ``TorpedoProtection``); we fold through :func:`normalise_hitbox_token`
    so both armor and hitbox sections share vocabulary."""
    out: dict[str, dict[str, Any]] = {}
    for name, info in zones.items():
        canonical = normalise_hitbox_token(name)
        entry: dict[str, Any] = {}
        if "default_thickness_mm" in info:
            entry["default_thickness_mm"] = float(info["default_thickness_mm"])
        if "max_thickness_mm" in info:
            entry["max_thickness_mm"] = float(info["max_thickness_mm"])
        if "plate_count" in info:
            entry["plate_count"] = int(info["plate_count"])
        for k, v in info.items():
            if k not in entry:
                entry[k] = v
        out[canonical] = entry
    return out


def _normalise_materials_table(
    table: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalise per-material ``zones`` to lowercase canonical form
    (matching ``armor.zones`` keys and ``hitbox.regions`` keys)."""
    out: dict[str, dict[str, Any]] = {}
    for mat_id, info in table.items():
        entry: dict[str, Any] = {}
        if "thickness_mm" in info:
            entry["thickness_mm"] = float(info["thickness_mm"])
        if "layers" in info:
            entry["layers"] = [float(v) for v in info["layers"]]
        if "zones" in info:
            entry["zones"] = [normalise_hitbox_token(z) for z in info["zones"]]
        if info.get("hidden"):
            entry["hidden"] = True
        for k, v in info.items():
            if k not in entry:
                entry[k] = v
        out[str(mat_id)] = entry
    return out


def _now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_iso_date() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _default_exporter() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    host = (
        os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or os.environ.get("HOST")
        or "unknown"
    )
    return f"{user}@{host}"


# ---------------------------------------------------------------------------
# Smoke tests live at tools/tests/test_sidecar_smoke.py — run them via:
#     python tools/tests/test_sidecar_smoke.py
# (Extracted 2026-05-07 per static_review_deferred.md item org-1.)
# ---------------------------------------------------------------------------
