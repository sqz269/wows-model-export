"""``make_*`` constructors — every section + placement builder.

Each constructor returns a plain ``dict`` shaped to the canonical key
order defined in :mod:`._constants`. All return dicts; no class
hierarchy. Builders are pure: no disk I/O, no module-level state
mutation. The only mutating constructor is :func:`make_pipeline`,
which captures wall-clock time + user/host into the ``pipeline`` block.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ._constants import VALID_SHIP_CLASSES
from ._helpers import (
    _default_exporter,
    _normalise_materials_table,
    _normalise_transform,
    _normalise_zone_dict,
    _now_iso,
    _today_iso_date,
    normalise_hitbox_token,
)

# ---------------------------------------------------------------------------
# Factory functions — each returns a plain dict in spec shape.
# ---------------------------------------------------------------------------


def make_pipeline(
    *,
    version: str | None = None,
    stages_completed: Iterable[int] = (),
    dcc_version: str = "",
    toolkit_version: str = "",
    tool_commits: dict[str, str] | None = None,
    exported_by: str | None = None,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build a ``pipeline`` section."""
    return {
        "version": version or _today_iso_date(),
        "exported_at": exported_at or _now_iso(),
        "exported_by": exported_by or _default_exporter(),
        "dcc_version": dcc_version,
        "toolkit_version": toolkit_version,
        "stages_completed": sorted({int(s) for s in stages_completed}),
        "tool_commits": dict(tool_commits or {}),
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
    ``{thickness_mm, layers, zones[, hidden]}``. The downstream consumer
    resolves ``RaycastHit.triangleIndex`` → per-vertex ``_MATERIAL_ID`` →
    this table at runtime.

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
    dispersion: Mapping[str, Any] | None,
    yaw_range_deg: Iterable[float] | None,
    elev_range_deg: Iterable[float] | None,
    yaw_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    pitch_dead_zones_deg: Iterable[Iterable[float]] | None = None,
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
    if dispersion:
        out["dispersion"] = dict(dispersion)
    if yaw_range_deg is not None:
        out["yaw_range_deg"] = [float(v) for v in yaw_range_deg]
    if elev_range_deg is not None:
        out["elev_range_deg"] = [float(v) for v in elev_range_deg]
    if yaw_dead_zones_deg is not None:
        out["yaw_dead_zones_deg"] = [[float(x) for x in pair] for pair in yaw_dead_zones_deg]
    if pitch_dead_zones_deg is not None:
        out["pitch_dead_zones_deg"] = [[float(x) for x in pair] for pair in pitch_dead_zones_deg]
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
    dispersion: Mapping[str, Any] | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    elev_range_deg: Iterable[float] | None = None,
    yaw_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    pitch_dead_zones_deg: Iterable[Iterable[float]] | None = None,
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
        barrel_count=barrel_count, ammo_types=ammo_types,
        dispersion=dispersion,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        yaw_dead_zones_deg=yaw_dead_zones_deg, pitch_dead_zones_deg=pitch_dead_zones_deg,
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
    dispersion: Mapping[str, Any] | None = None,
    yaw_range_deg: Iterable[float] | None = None,
    elev_range_deg: Iterable[float] | None = None,
    yaw_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    pitch_dead_zones_deg: Iterable[Iterable[float]] | None = None,
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
        barrel_count=barrel_count, ammo_types=ammo_types,
        dispersion=dispersion,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        yaw_dead_zones_deg=yaw_dead_zones_deg, pitch_dead_zones_deg=pitch_dead_zones_deg,
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
    yaw_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    pitch_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    traverse_rate: float | None = None,
    elev_rate: float | None = None,
    aa_range_km: float | None = None,
    aa_dps: float | None = None,
    attach_to: str | None = None,
    casts_shadow: bool = True,
) -> dict[str, Any]:
    """Build an entry for ``antiair[]`` (AA mount).

    AA mounts add ``aa_range_km`` + ``aa_dps`` for aura math. Standard
    gun-gameplay fields (``ammo_types``, ``reload_s``, ``dispersion``) are
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
        ammo_types=None, dispersion=None,
        yaw_range_deg=yaw_range_deg, elev_range_deg=elev_range_deg,
        yaw_dead_zones_deg=yaw_dead_zones_deg, pitch_dead_zones_deg=pitch_dead_zones_deg,
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
    yaw_dead_zones_deg: Iterable[Iterable[float]] | None = None,
    shoot_sector_deg: Iterable[float] | None = None,
    additional_aim_sector_deg: Iterable[float] | None = None,
    torpedo_angles_deg: Iterable[float] | None = None,
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
    if yaw_dead_zones_deg is not None:
        out["yaw_dead_zones_deg"] = [[float(x) for x in pair] for pair in yaw_dead_zones_deg]
    if shoot_sector_deg is not None:
        out["shoot_sector_deg"] = [float(v) for v in shoot_sector_deg]
    if additional_aim_sector_deg is not None:
        out["additional_aim_sector_deg"] = [float(v) for v in additional_aim_sector_deg]
    if torpedo_angles_deg is not None:
        out["torpedo_angles_deg"] = [float(v) for v in torpedo_angles_deg]
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
    accessory under a turret's Yaw transform at consumer import time
    (Phase 2 feature) — always preserved across merges, never auto-set.
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
    block to sample when a skin is backed by per-material texture sets.
    Official WG camos can instead carry all paint data in ``categories`` /
    ``mat_textures`` from GameParams + ``camouflages.xml``; for those skins
    ``scheme_key`` is provenance and consumers fall back to ``main`` for base
    PBR slots.

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
    transforms from ``camouflages.xml``. Shape::

        {
          "tile":  {"mask": {"dds_mips": [...]}, "uv": {"scale": [...], "offset": [...]}},
          "gun":   {"mask": {"dds_mips": [...]}, "uv": {"scale": [...], "offset": [...]}},
          "plane": {"mask": {...}, "uv": {...}},
          "float": {...},
          ...
        }

    Empty / missing means "no per-category overrides for this skin"; the
    consumer renders meshes with base albedo or any active per-material
    scheme. Modern WG extraction includes hull-side categories here when the
    XML entry authored them, avoiding DDS filename discovery for hull camos.

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
    # decision to consumers — typical consumers derive the flip from
    # `source` (loose mods → off, vanilla / VFS extracts → on).
    # Setting ``True`` /
    # ``False`` explicitly forces the convention regardless of source
    # — useful when a loose mod is authored top-down (rare but
    # possible if the artist used a tool that preserves DDS row
    # order) or when a VFS variant happens to ship bottom-up. The
    # field round-trips through ``merge_preserving`` so a hand-edit
    # of the sidecar JSON survives re-runs of the ingester.
    if flip_v is not None:
        out["flip_v"] = bool(flip_v)
    return out
