"""Ship air operations → the sidecar ``air_ops`` block.

A ship that launches planes has a launchpad, picked by the GameParams
``airArmament`` component of its hull (``launchpadType``):

* ``deck`` — a carrier deck. Parked planes sit on the hull nodes ``HP_Deck_00``,
  ``HP_Deck_01`` … (filled in index order, squadron type by squadron type, up to
  ``deckPlaceCount``; the fill stops at the first missing node). A launch runs
  ``HP_DeckStart`` → ``HP_LiftOffPoint``; a landing runs ``HP_DeckLand`` →
  ``HP_DisappearPoint``.
* ``catapult`` — catapult planes. Each catapult is an ``HP_*`` entry of the
  component (model, rest yaw ``angle``, ``rotateTime``, slide ``dist``, …) whose
  model carries the launch run ``HP_Plane_Start`` → ``HP_Plane_End``.
* ``deckWithCatapult`` — a deck whose launches leave from catapults; parking and
  landing use the deck nodes.
* ``water`` — a water take-off along the ship's heading (``waterLaunchLength``);
  no nodes.
* ``fake`` / empty — no planes: no block.

The node frames come from the toolkit's placements JSON (``air_ops_nodes``), in
the sidecar's placement convention: glTF axes, metres, a node's local −Z is its
forward and +Y its up. Catapult launch frames are the raw hull hardpoint (its
rotation kept) composed with the catapult model's nodes in the authored pose:
the client turns the rotator from the rest yaw back to that pose before a
launch (``Catapult.updateNodeMatrix``: ``lerpAngles(angle, 0, progress)``).

Client logic read from the decompiled ``AirPlanes.Launchpad``,
``DeckLaunchpad``, ``CatapultLaunchpad``, ``DeckWithCatapultLaunchpad``,
``Catapult`` and the client catapult module.
"""
from __future__ import annotations

import re
from typing import Any

from .death_presentation import EMPTY_EFFECTS

#: Launchpad types that launch planes.
AIR_LAUNCHPAD_TYPES: frozenset[str] = frozenset({"deck", "deckWithCatapult", "catapult", "water"})

#: Where launches leave from, per launchpad type.
LAUNCH_FROM: dict[str, str] = {
    "deck": "deck",
    "deckWithCatapult": "catapults",
    "catapult": "catapults",
    "water": "water",
}

#: Launchpad types that park planes on the deck and land them on it.
DECK_TYPES: frozenset[str] = frozenset({"deck", "deckWithCatapult"})

#: BW model units → metres (the geometry domain).
NATIVE_TO_METRES = 15.0

_DECK_SPOT_RE = re.compile(r"^HP_Deck_(\d+)$")

# sidecar key → airArmament field
_LAUNCHPAD_FIELDS: dict[str, str] = {
    "deck_place_count": "deckPlaceCount",
    "parallel_launch": "isParallelLaunch",
    "independent_launchpad": "isIndependentLaunchpad",
    "launch_prepare_time_s": "launchPrepareTime",
    "joint_launch_delay_s": "jointLaunchDelay",
    "joint_launch_plane_count": "jointLaunchPlaneCount",
    "joint_launch_offset": "jointLaunchOffset",
    "planes_reserve_capacity": "planesReserveCapacity",
    "water_launch_length_bw": "waterLaunchLength",
    "bounding_box_margin": "boundingBoxMargin",
    "camera_catapult_node": "turnCameraToCatapultNode",
}


def launchpad_type(air_armament: dict[str, Any] | None) -> str | None:
    """The component's ``launchpadType`` when it launches planes, else ``None``."""
    if not isinstance(air_armament, dict):
        return None
    t = air_armament.get("launchpadType")
    return t if t in AIR_LAUNCHPAD_TYPES else None


def _node(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry.get("name"),
        "parent_section": entry.get("parent_section"),
        "transform": entry.get("transform"),
    }


def _run(nodes: dict[str, dict[str, Any]], start: str, end: str) -> dict[str, Any] | None:
    if start in nodes and end in nodes:
        return {"start": _node(nodes[start]), "end": _node(nodes[end])}
    return None


def _catapult_params(name: str, c: dict[str, Any]) -> dict[str, Any]:
    linked = c.get("linkedGunName")
    dist = c.get("dist")
    effect = c.get("launchEffectName")
    return {
        "hp_name": name,
        "gameparams_name": c.get("name"),
        "model": c.get("model"),
        "rest_yaw_deg": c.get("angle"),
        "rotate_time_s": c.get("rotateTime"),
        "slide_m": round(float(dist) * NATIVE_TO_METRES, 6) if isinstance(dist, (int, float)) else None,
        "slide_delay_s": c.get("translateDelay"),
        "invisible": c.get("isInvisible"),
        "hide_plane": c.get("hidePlane"),
        # Empty_FX is WG's placeholder for "no effect".
        "launch_effect": effect if isinstance(effect, str) and effect not in EMPTY_EFFECTS else None,
        "linked_gun": {
            "hp_name": linked,
            "yaw_map_deg": c.get("linkedGunAngles") or [],
            "rotation_speed_coeff": c.get("linkedRotationSpeedCoeff"),
        } if linked else None,
        "plane_offsets": c.get("planeOffsets") or {},
    }


def catapult_components(air_armament: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The component's catapults: ``HP_*`` sub-dicts that name a catapult model."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in air_armament.items():
        if (isinstance(k, str) and k.startswith("HP_") and isinstance(v, dict)
                and isinstance(v.get("model"), str)):
            out[k] = v
    return out


def collect_air_ops_block(
    air_armament: dict[str, Any] | None,
    air_ops_nodes: dict[str, Any] | None,
    *,
    hull_component: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build the ``air_ops`` block, or ``None``.

    ``air_armament`` is the rendered hull's GameParams ``airArmament``
    component and ``hull_component`` that hull's component name (``A_Hull``),
    kept as ``launchpad.hull``; ``air_ops_nodes`` the toolkit placements
    JSON's section of the same name.

    ``None`` for a ship that launches no planes, and for an export made before
    the toolkit wrote ``air_ops_nodes`` (a warning then asks for a re-export):
    a block always carries the frames the export has.
    """
    lp_type = launchpad_type(air_armament)
    if lp_type is None:
        return None
    assert air_armament is not None
    warn = warnings if warnings is not None else []
    if not isinstance(air_ops_nodes, dict):
        warn.append(
            "air_ops: the placements JSON has no air_ops_nodes (exported before the "
            "toolkit wrote them; re-export with a toolkit that lists "
            "export_ship.placements_json.air_ops_nodes); no air_ops block"
        )
        return None
    nodes_doc = air_ops_nodes

    launchpad: dict[str, Any] = {"type": lp_type}
    for key, field in _LAUNCHPAD_FIELDS.items():
        if field in air_armament:
            launchpad[key] = air_armament[field]
    if hull_component:
        launchpad["hull"] = hull_component

    hull_nodes: dict[str, dict[str, Any]] = {}
    for e in nodes_doc.get("hull") or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            hull_nodes[e["name"]] = e

    # Parked-plane spots, in index order; `used` marks the ones the client
    # fills (from 00 up to deck_place_count, stopping at the first gap).
    spots = sorted(
        ((int(m.group(1)), name) for name in hull_nodes if (m := _DECK_SPOT_RE.match(name))),
    )
    place_count = air_armament.get("deckPlaceCount")
    place_count = int(place_count) if isinstance(place_count, (int, float)) else 0
    present = {i for i, _ in spots}
    filled = 0
    while filled < place_count and filled in present:
        filled += 1
    deck_spots = [
        {"index": i, **_node(hull_nodes[name]), "used": i < filled}
        for i, name in spots
    ]

    block: dict[str, Any] = {
        "launchpad": launchpad,
        "launch_from": LAUNCH_FROM[lp_type],
        "deck_spots": deck_spots,
        "launch_run": _run(hull_nodes, "HP_DeckStart", "HP_LiftOffPoint"),
        "landing_run": _run(hull_nodes, "HP_DeckLand", "HP_DisappearPoint"),
        "catapults": [],
    }

    # Catapults: GameParams parameters joined to the toolkit's launch nodes by
    # hardpoint name, sorted by hardpoint name. (The client numbers its
    # catapults in the component's dict order and launches plane i from
    # catapult i % count; that order is not recorded.)
    tk_cats: dict[str, dict[str, Any]] = {}
    for e in nodes_doc.get("catapults") or []:
        if isinstance(e, dict) and isinstance(e.get("hp_name"), str):
            tk_cats.setdefault(e["hp_name"], e)
    for hp in sorted(catapult_components(air_armament)):
        entry = _catapult_params(hp, air_armament[hp])
        tk = tk_cats.get(hp)
        nodes = (tk or {}).get("nodes") or {}
        pivot_name = next((n for n in ("Rotate_Y", "Translate_Z") if n in nodes), None)
        entry.update({
            "instance_id": (tk or {}).get("instance_id"),
            "asset_id": (tk or {}).get("asset_id"),
            "parent_section": (tk or {}).get("parent_section"),
            "hardpoint": (tk or {}).get("hardpoint"),
            "launch_start": nodes.get("HP_Plane_Start"),
            "launch_end": nodes.get("HP_Plane_End"),
            "pivot": {"node": pivot_name, "transform": nodes[pivot_name]} if pivot_name else None,
        })
        if not tk:
            warn.append(f"air_ops: catapult {hp} has no launch nodes in the export")
        block["catapults"].append(dict(sorted(entry.items())))

    if lp_type in DECK_TYPES:
        if not deck_spots:
            warn.append("air_ops: deck launchpad without HP_Deck_* nodes")
        if block["landing_run"] is None:
            warn.append("air_ops: deck launchpad without HP_DeckLand / HP_DisappearPoint")
    if lp_type == "deck" and block["launch_run"] is None:
        warn.append("air_ops: deck launchpad without HP_DeckStart / HP_LiftOffPoint")
    if lp_type in ("catapult", "deckWithCatapult") and not block["catapults"]:
        warn.append(f"air_ops: {lp_type} launchpad without catapults")
    return block


__all__ = [
    "AIR_LAUNCHPAD_TYPES",
    "LAUNCH_FROM",
    "DECK_TYPES",
    "launchpad_type",
    "catapult_components",
    "collect_air_ops_block",
]
