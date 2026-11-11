"""Ship anti-aircraft defence → the sidecar ``air_defense`` block.

WoWS models a ship's AA as **auras**: range bands around the ship that damage
every hostile squadron inside them. The client's ``AirDefense`` component
builds them from three weapon components of the hull, in this order:

* ``artillery`` (main-battery dual-purpose guns, e.g. ``A_Artillery``),
* ``atba`` (secondaries, e.g. ``B_ATBA``),
* ``airDefense`` (the AA mounts, e.g. ``B_AirDefense``),

and every sub-dict of those components that carries ``isJoint`` is one aura
(``AuraNear``, ``AuraMedium1``, ``Far_1_Bubbles``, ``Near1`` …; the key names
vary, the ``isJoint`` field does not). Each aura has:

* a band ``type`` (``near`` / ``medium`` / ``far``: short / medium / long
  range) and a ring ``minDistance`` → ``maxDistance`` in game metres (the
  distance domain, BW × 30 — the same metres as the ballistics ranges);
* continuous damage: ``areaDamage`` every ``areaDamagePeriod`` seconds
  (2/7 s, two server ticks; the server runs ``TICKS_PER_SECOND`` = 7), with
  ``hitChance``. The client's port figure for an aura is
  ``round(areaDamage / areaDamagePeriod) × hitChance``
  (``ModifiersApply.calculateAverageAuraDPS``);
* flak (exploding clouds, "bubbles"): ``bubbleDamage`` with
  ``innerBubbleCount`` + ``outerBubbleCount`` clouds per salvo. In GameParams
  flak lives on its own ``far`` aura (``AuraFar_Bubbles``, ring 3.5 km →
  the long-range edge) whose ``areaDamage`` is 0. The client shows one
  explosion's damage as ``bubbleDamage × TICKS_PER_SECOND``;
* ``guns``: the hardpoints whose survival scales the aura (its strength is the
  surviving fraction of them).

Of the auras with flak, the client picks one "main" aura (the one barrage and
extra-cloud modifiers act on): the largest
``(bubbleDamage × cloud count, maxDistance, largest gun barrelDiameter)``
(``AirDefense.__initMostImportantBubbleAura``).

Read from the decompiled ``Components.AirDefense`` (``AirDefense``,
``AirDefenseAura``), ``Modifiers.ModifiersApply``, ``AirPlanes.AntiAirBubble``
and the TTX preprocessor ``PreprocessedAirDefense``; ``TICKS_PER_SECOND`` from
the constants module's bytecode. The per-tick damage code itself is
server-side and not in the client.

This replaces the per-mount ``aa_range_km`` / ``aa_dps`` fields: those come
from the mounts' legacy ``antiAirAuraDistance`` (BW units, so 0.067 "km" is
the 2.0 km band) and ``antiAirAuraStrength`` (a pre-rework coefficient), which
the current client does not read.
"""
from __future__ import annotations

from typing import Any

#: Server ticks per second (the constants module's ``TICKS_PER_SECOND``).
TICKS_PER_SECOND = 7

#: The weapon components the client reads auras from, in its order:
#: (sidecar ``weapon`` name, resolve_components slot).
WEAPON_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("artillery", "artillery"),
    ("atba", "atba"),
    ("air_defense", "airDefense"),
)

#: ``targetType`` → name (``AirDefenseAuraTargetType``).
TARGET_TYPES: dict[int, str] = {0: "squadron", 1: "missile"}


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _int(v: Any) -> int:
    n = _num(v)
    return int(n) if n is not None else 0


def _pair(v: Any) -> list[float] | None:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        a, b = _num(v[0]), _num(v[1])
        if a is not None and b is not None:
            return [a, b]
    return None


def is_aura(entry: Any) -> bool:
    """True for a component sub-dict the client builds an aura from."""
    return isinstance(entry, dict) and "isJoint" in entry


def _flak(aura: dict[str, Any]) -> dict[str, Any] | None:
    damage = _num(aura.get("bubbleDamage")) or 0.0
    inner = _int(aura.get("innerBubbleCount"))
    outer = _int(aura.get("outerBubbleCount"))
    if damage <= 0 and inner + outer <= 0:
        return None
    lo = _num(aura.get("minBubbleActivationDelay"))
    hi = _num(aura.get("maxBubbleActivationDelay"))
    return {
        "damage": damage,
        "explosion_damage": round(damage * TICKS_PER_SECOND, 4),
        "inner_count": inner,
        "outer_count": outer,
        "salvo_count": inner + outer,
        "explosion_count": _int(aura.get("explosionCount")),
        "radius_bw": _num(aura.get("bubbleRadius")),
        "duration_s": _num(aura.get("bubbleDuration")),
        "activation_delay_s": [lo, hi] if lo is not None and hi is not None else None,
        "inner_zone": _num(aura.get("innerBubbleZone")),
        "half_outer_zone": _num(aura.get("halfOuterBubbleZone")),
        "inner_spawn_time_s": _pair(aura.get("innerBubbleSpawnTimeRange")),
        "outer_spawn_time_s": _pair(aura.get("outerBubbleSpawnTimeRange")),
        "main": False,
    }


def _aura_record(
    name: str,
    aura: dict[str, Any],
    *,
    component_id: str | None,
    weapon: str,
) -> dict[str, Any]:
    damage = _num(aura.get("areaDamage")) or 0.0
    period = _num(aura.get("areaDamagePeriod"))
    target = aura.get("targetType")
    target_name = TARGET_TYPES.get(int(target)) if _num(target) is not None else None
    guns = aura.get("guns")
    band = aura.get("type")
    return {
        "name": name,
        "component": component_id,
        "weapon": weapon,
        "band": band if isinstance(band, str) else None,
        "target": target_name if target_name is not None else target,
        "min_range_m": _num(aura.get("minDistance")),
        "max_range_m": _num(aura.get("maxDistance")),
        "hit_chance": _num(aura.get("hitChance")),
        "damage": damage,
        "period_s": period,
        "dps": round(damage / period, 4) if period else None,
        "barrage": bool(aura.get("enableBarrage", False)),
        "shot_travel_time_s": _num(aura.get("shotTravelTime")),
        "guns": [g for g in guns if isinstance(g, str)] if isinstance(guns, list) else [],
        "flak": _flak(aura),
    }


def _max_caliber(component: dict[str, Any], guns: list[str]) -> float:
    best = 0.0
    for g in guns:
        mount = component.get(g)
        if isinstance(mount, dict):
            d = _num(mount.get("barrelDiameter"))
            if d is not None and d > best:
                best = d
    return best


def _mark_main_flak(auras: list[dict[str, Any]], comps: list[dict[str, Any] | None]) -> None:
    """Flag the aura the client treats as the main flak aura."""
    scored = []
    for rec, comp in zip(auras, comps, strict=True):
        flak = rec.get("flak")
        if not flak or flak["salvo_count"] <= 0:
            continue
        key = (
            flak["damage"] * flak["salvo_count"],
            rec.get("max_range_m") or 0.0,
            _max_caliber(comp or {}, rec["guns"]),
        )
        scored.append((key, rec))
    if scored:
        # Stable sort: among equal keys the last in our order wins, as the
        # client's sort-then-take-last does over its own order.
        scored.sort(key=lambda kv: kv[0])
        scored[-1][1]["flak"]["main"] = True


def collect_auras(components: dict[str, Any] | None) -> dict[str, Any]:
    """The auras of one resolved hull (:func:`resolve_components` output).

    Returns ``{"components": {weapon: component key | None}, "auras": [...]}``.
    Auras are ordered by weapon (artillery, atba, air_defense), then by name.
    """
    components = components or {}
    ids = components.get("__component_ids__") or {}
    comp_ids: dict[str, str | None] = {}
    auras: list[dict[str, Any]] = []
    owners: list[dict[str, Any] | None] = []
    for weapon, slot in WEAPON_COMPONENTS:
        comp = components.get(slot)
        comp_ids[weapon] = ids.get(slot) if isinstance(comp, dict) else None
        if not isinstance(comp, dict):
            continue
        for name in sorted(k for k, v in comp.items() if isinstance(k, str) and is_aura(v)):
            auras.append(_aura_record(name, comp[name], component_id=comp_ids[weapon], weapon=weapon))
            owners.append(comp)
    _mark_main_flak(auras, owners)
    return {"components": comp_ids, "auras": auras}


def hull_names(ship: dict[str, Any] | None) -> list[str]:
    """Every hull component the ship's ``_Hull`` modules name, sorted."""
    sui = (ship or {}).get("ShipUpgradeInfo") or {}
    names: set[str] = set()
    if isinstance(sui, dict):
        for mod in sui.values():
            if not isinstance(mod, dict) or mod.get("ucType") != "_Hull":
                continue
            hulls = (mod.get("components") or {}).get("hull")
            if isinstance(hulls, list) and hulls and isinstance(hulls[0], str):
                names.add(hulls[0])
    return sorted(names)


def collect_air_defense_block(
    ship: dict[str, Any] | None,
    components: dict[str, Any] | None,
    *,
    resolve_hull: Any = None,
) -> dict[str, Any]:
    """Build the ``air_defense`` block (without its ``source`` stamp).

    ``components`` is the rendered hull's :func:`resolve_components` output:
    its auras are the block's top level, next to the ``hull`` component key.
    ``resolve_hull(name)`` returns the components of a named hull; when given,
    ``by_hull`` holds every hull of the ship resolved that way.
    """
    top = collect_auras(components)
    block: dict[str, Any] = {
        "hull": (components or {}).get("__active_hull_id__"),
        "components": top["components"],
        "auras": top["auras"],
    }
    if resolve_hull is not None:
        by_hull: dict[str, Any] = {}
        for name in hull_names(ship):
            by_hull[name] = collect_auras(resolve_hull(name))
        block["by_hull"] = by_hull
    return block


__all__ = [
    "TICKS_PER_SECOND",
    "WEAPON_COMPONENTS",
    "TARGET_TYPES",
    "is_aura",
    "collect_auras",
    "hull_names",
    "collect_air_defense_block",
]
