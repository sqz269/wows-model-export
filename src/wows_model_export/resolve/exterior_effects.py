"""Resolve native exterior hull groups and shooter path substitutions.

ShipHull replaces the complete hull table with Exterior.effects[hullKey];
Peculiarities.getEffect substitutes shooter paths once, not hull group paths.
The emitted list is complete; consumers must never merge it with the base list.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .gameparams_autofill import resolve_hull_effect_positions

_SHOOTER_GROUPS = {"shotEffect", "lensEffect", "purgingEffect", "reloadBoostEffect"}
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


def resolve_exterior_effects(
    base_attachments: list[dict[str, Any]], exterior: dict[str, Any],
    hull_key: str, *, exterior_id: str = "", themed_enabled: bool = True,
    variant_hull: bool = False, skel_ext_json: Path | None = None,
) -> dict[str, Any] | None:
    """None inherits base; an emitted attachments list replaces it in full.

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
        elif ((row.get("source") in {"artillery", "atba", "airDefense"}
               and row.get("group") in _SHOOTER_GROUPS)
              or (row.get("source") == "munition" and row.get("group") in _MUNITION_GROUPS)):
            path = row.get("particle_path")
            row["particle_path"] = substitutions.get(path, path)
    rows = [r for r in rows if r.get("particle_path")
            and r["particle_path"] != "particles/Empty_FX.xml"]
    resolve_hull_effect_positions(rows, skel_ext_json)
    return {
        "source": {"exterior_id": exterior_id, "hull_key": hull_key,
                   "peculiarity": exterior.get("peculiarity"),
                   "requires_peculiarity_enabled": True,
                   "hull_table_replaced": replace_hull},
        "attachments": rows,
    }
