"""Resolve native exterior hull groups and shooter path substitutions.

ShipHull replaces the complete hull table with Exterior.effects[hullKey];
Peculiarities.getEffect substitutes shooter paths once, not hull group paths.
The emitted list is complete; consumers must never merge it with the base list.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..read.gameparams import get_projectile
from .gameparams_autofill import resolve_hull_effect_positions

_SHOOTER_GROUPS = {"shotEffect", "lensEffect", "purgingEffect", "reloadBoostEffect"}
# Mount sources whose shooter passes these groups through Peculiarities.getEffect:
# ClientShooterEffects (guns) and WaveGunShooter (wave emitters, shotEffect
# only). Laser charge / targeting / beam effects and the missile launch
# effect are played directly (build 13533378), so they are never substituted.
_SHOOTER_SOURCES = {"artillery", "atba", "airDefense"}
_WAVE_SHOOTER_GROUPS = {"shotEffect"}
# Ammo attributes the client passes through Peculiarities.getEffect: every
# createEffect pick (impacts, incl. the depth-charge waterHitEffect) and the
# shell tracer. Known divergence: the torpedo client (ClientTorpedoes.Torpedo,
# build 13533378) plays tracerEffect / ownTracerEffect / underwaterTracerEffect
# without substitution, but the rows carry no species to tell torpedo tracers
# from shell tracers.
_MUNITION_GROUPS = {
    "tracerEffect", "ownTracerEffect", "dropToTheGroundEffect", "hitEffect",
    "enemyHitSurfaceEffect", "enemyHitUnderwaterEffect", "waterHitEffect", "blowUpEffect",
    "water", "ground", "groundHorizontal", "groundVertical",
    "projDestroyedEffect", "projDestroyedEffectHorizontal", "projDestroyedEffectVertical",
    "torpedoProjDestroyedEffect",
    *(f"{prefix}{axis}" for prefix in (
        "projHitCitadelEffect", "projHitNoPenetrationEffect", "projOverPenetrationEffect",
        "projOverPenetrationExitEffect", "projRicochetEffect",
    ) for axis in ("Horizontal", "Vertical")),
}
# Ammo species whose effects the client plays without Peculiarities.getEffect
# (build 13533378): laser hits (createLaserHitEffect) and missile hits and
# flight (ClientMissile). Their munition rows keep the base paths even when an
# exterior substitutes the same path for shells.
_UNSUBSTITUTED_AMMO_SPECIES = frozenset({"Laser", "Missile"})


def resolve_exterior_effects(
    base_attachments: list[dict[str, Any]], exterior: dict[str, Any],
    hull_key: str, *, exterior_id: str = "", themed_enabled: bool = True,
    variant_hull: bool = False, skel_ext_json: Path | None = None,
    base_by_hull: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """None inherits base; an emitted attachments list replaces it in full.

    With ``base_by_hull`` (``effects.by_hull`` as hull -> rows) and weapon
    substitutions, the payload also carries ``by_hull``: each hull's weapon
    rows with the substitutions applied. Without substitutions consumers use
    ``effects.by_hull`` unchanged.

    Unresolved EP nodes remain name-only, never guessed at the ship origin.
    A swapped hull cannot reuse positions from the original model.
    """
    if not themed_enabled:
        return None
    tables = exterior.get("effects") or {}
    replace_hull = isinstance(tables, dict) and hull_key in tables
    substitutions = exterior.get("peculiarityEffects") or {}
    if not isinstance(substitutions, dict):
        substitutions = {}
    if not replace_hull and not substitutions and not variant_hull:
        return None
    rows = copy.deepcopy(base_attachments)
    if replace_hull:
        # Preserve authored event names, including exterior-only groups not in
        # the legacy base-ship collector's allowlist.
        table = tables[hull_key]
        hull = []
        if isinstance(table, dict):
            for group, entries in sorted(table.items()):
                if not isinstance(entries, (list, tuple)):
                    continue
                for entry in entries:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    node, path = entry[:2]
                    if isinstance(node, str) and node and isinstance(path, str) and path:
                        hull.append(dict(source="hull", group=group, node=node, particle_path=path))
        rows = hull + [r for r in rows if r.get("source", "hull") != "hull"]
    base_by_node = {r.get("node"): r for r in base_attachments
                    if r.get("source", "hull") == "hull"}
    for row in rows:
        if row.get("source", "hull") == "hull":
            if variant_hull:
                for key in ("position", "rotation", "matrix"):
                    row.pop(key, None)
            elif replace_hull:
                original = base_by_node.get(row.get("node"), {})
                for key in ("position", "rotation", "matrix"):
                    if key in original:
                        row[key] = copy.deepcopy(original[key])
        elif _is_substituted(row):
            path = row.get("particle_path")
            row["particle_path"] = substitutions.get(path, path)
    rows = [r for r in rows if r.get("particle_path")
            and r["particle_path"] != "particles/Empty_FX.xml"]
    resolve_hull_effect_positions(rows, skel_ext_json)
    payload: dict[str, Any] = {
        "source": {"exterior_id": exterior_id, "hull_key": hull_key,
                   "peculiarity": exterior.get("peculiarity"),
                   "requires_peculiarity_enabled": True,
                   "hull_table_replaced": replace_hull},
        "attachments": rows,
    }
    if substitutions and base_by_hull:
        payload["by_hull"] = {
            name: {"attachments": substitute_weapon_rows(hull_rows, substitutions)}
            for name, hull_rows in base_by_hull.items()
        }
    return payload


def _ammo_species(ammo_id: Any) -> str | None:
    """The GameParams ``typeinfo.species`` of a munition row's ammo
    (``Artillery``, ``Torpedo``, ``Laser``, ``Missile``, ...); ``None`` when
    the ammo is not in the cache."""
    proj = get_projectile(ammo_id) if isinstance(ammo_id, str) and ammo_id else None
    species = ((proj or {}).get("typeinfo") or {}).get("species")
    return species if isinstance(species, str) else None


def _is_substituted(row: dict[str, Any]) -> bool:
    """True for a row whose path the client passes through
    ``Peculiarities.getEffect`` (the firing ship's skin override). Munition
    rows of laser and missile ammo never are (:data:`_UNSUBSTITUTED_AMMO_SPECIES`)."""
    source, group = row.get("source"), row.get("group")
    if source == "munition":
        return (group in _MUNITION_GROUPS
                and _ammo_species(row.get("source_id")) not in _UNSUBSTITUTED_AMMO_SPECIES)
    return ((source in _SHOOTER_SOURCES and group in _SHOOTER_GROUPS)
            or (source == "waves" and group in _WAVE_SHOOTER_GROUPS))


def substitute_weapon_rows(
    rows: list[dict[str, Any]], substitutions: dict[str, Any],
) -> list[dict[str, Any]]:
    """Copies of weapon-bound ``rows`` with the exterior's
    ``peculiarityEffects`` applied once to the substituted slots; a row
    whose path becomes ``Empty_FX.xml`` is dropped."""
    out = []
    for row in copy.deepcopy(rows):
        if _is_substituted(row):
            path = row.get("particle_path")
            row["particle_path"] = substitutions.get(path, path)
        if row.get("particle_path") and row["particle_path"] != "particles/Empty_FX.xml":
            out.append(row)
    return out


def exterior_weapon_effects(
    payload: dict[str, Any], exterior: dict[str, Any],
    base_attachments: list[dict[str, Any]],
    base_by_hull: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """An existing exterior ``effects`` payload with its weapon-bound rows
    re-derived from ``base_attachments`` (and ``by_hull`` from
    ``base_by_hull``), the exterior's substitutions applied; its other rows
    (hull groups with their resolved positions) are kept."""
    from .gameparams_autofill import WEAPON_EFFECT_SOURCES, replace_weapon_effect_rows

    substitutions = exterior.get("peculiarityEffects") or {}
    if not isinstance(substitutions, dict):
        substitutions = {}
    weapon = [r for r in base_attachments
              if r.get("source", "hull") in WEAPON_EFFECT_SOURCES]
    out = {k: v for k, v in payload.items() if k != "by_hull"}
    out["attachments"] = replace_weapon_effect_rows(
        payload.get("attachments") or [], substitute_weapon_rows(weapon, substitutions),
    )
    if substitutions and base_by_hull:
        out["by_hull"] = {
            name: {"attachments": substitute_weapon_rows(rows, substitutions)}
            for name, rows in base_by_hull.items()
        }
    return out
