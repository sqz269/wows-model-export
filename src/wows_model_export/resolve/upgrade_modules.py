"""Upgrade modules: every module a ship can fit, with the numbers it changes.

A ship's ``Vehicle.ShipUpgradeInfo`` lists its upgrade modules: one entry per
module, each with a ``ucType`` (the slot: ``_Artillery``, ``_Suo``,
``_Engine``, ``_Torpedoes``, the carriers' plane slots, ...), a ``prev`` link
(the research chain) and the components it brings for its slot
(``components.artillery = ["AB2_Artillery"]``). The sidecar's
``variants.modules`` lists the module ids only; a consumer that lets the user
fit another module needs the numbers that module changes and the hulls it
fits. :func:`collect_modules_block` writes them as the sidecar's ``modules``
section (METADATA_SPEC §11.6):

``entries.<module_id>``
    The module: ``slot``, ``display_name`` (the game's name, filled by the
    caller from ``global.mo``), ``prev``, ``is_stock``, ``depth`` (``prev``
    hops to the chain root) and, per hull it fits, ``hulls.<H>``:
    ``component`` (the component id it brings on that hull) and the slot's
    numbers:

    * ``_Artillery``: ``turrets`` (one record per hardpoint: ``hp_name``,
      ``asset_id`` with its library taxonomy ``scope`` / ``category`` /
      ``subcategory``, ``dead_asset_id``, ``ammo_ids``, ``mount_armor`` and
      the placement fields the mount autofill writes: caliber, barrels, arcs,
      rates, ``reload_s``, ``dispersion``, ``hit_location``, ``misc_filter``,
      ``display_name``), ``max_dist_m`` (the battery's ``maxDist``),
      ``effects`` (the mount rows and the munition rows of its ammo) and
      ``unplaced_hp_names`` when some of its hardpoints are ones the hull
      does not place; such a record carries the ``transform`` and
      ``parent_section`` the export writes for a mount there when the caller
      resolves the hardpoint in the hull's model (``hardpoint_placement``),
      else none;
    * ``_Torpedoes``: ``torpedoes`` (launcher records as above, the torpedo
      fields: tubes, reload, arcs, rotation) and ``effects`` (munition rows);
    * ``_Suo``: ``fire_control`` (``max_dist_coef``, ``sigma_count_coef``);
    * ``_Engine``: ``engine`` (``speed_coef``: the top speed is the hull's
      ``maxSpeed`` x (1 + ``speed_coef``); ``hit_location``);
    * the plane slots: ``planes`` (the aircraft ids the module flies).

``hulls.<H>.<slot>``
    ``options`` (the modules of that slot the hull fits, chain order) and
    ``fitted`` (the module the rest of the sidecar already describes for that
    hull, see :func:`_fitted_module`).

A module fits hull ``H`` when its slot component is one ``H``'s ``_Hull``
module lists for that slot (§11.5: the client builds the ship from the
component the selected modules share), or when the hull lists no component
for the slot (fire control and engines usually). The branch weapon modules
of event ships (``_PrimaryWeapons`` / ``_SecondaryWeapons``) ride with their
hull (§11.4) and are not listed here.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import gameparams_autofill as _gp

#: Slot (``ucType``) -> the component key its modules bring.
SLOT_COMPONENT_KEYS: dict[str, str] = {
    "_Artillery": "artillery",
    "_Suo": "fireControl",
    "_Engine": "engine",
    "_Torpedoes": "torpedoes",
    "_Fighter": "fighter",
    "_TorpedoBomber": "torpedoBomber",
    "_DiveBomber": "diveBomber",
    "_SkipBomber": "skipBomber",
}

#: Mount slots -> the placement section their mounts sit in.
MOUNT_SECTIONS: dict[str, str] = {"_Artillery": "turrets", "_Torpedoes": "torpedoes"}

_PLANE_SLOTS: frozenset[str] = frozenset({"_Fighter", "_TorpedoBomber", "_DiveBomber", "_SkipBomber"})


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _hull_modules(sui: dict[str, Any]) -> dict[str, str]:
    """``{hull component name: _Hull module id}`` (``A_Hull`` -> ``PJUH621_...``)."""
    out: dict[str, str] = {}
    for mid in _gp._walk_ucType_chain(sui, "_Hull"):
        comps = (sui.get(mid) or {}).get("components") or {}
        hull = comps.get("hull") if isinstance(comps, dict) else None
        if isinstance(hull, list) and hull and isinstance(hull[0], str) and hull[0] not in out:
            out[hull[0]] = mid
    return out


def _hull_component_name(sui: dict[str, Any], hull_mid: str, fallback: str) -> str:
    """The hull component (``A_Hull``) the ``_Hull`` module ``hull_mid`` brings."""
    mod = sui.get(hull_mid)
    comps = mod.get("components") if isinstance(mod, dict) else None
    for cid in (comps or {}).get("hull") or []:
        if isinstance(cid, str) and cid:
            return cid
    return fallback


def module_component_for_hull(
    sui: dict[str, Any], module_id: str, hull_module_id: str,
) -> str | None:
    """The component ``module_id`` brings on the hull of ``hull_module_id``:
    the first id it lists for its slot that the hull module also lists, the
    first id when the hull lists none for the slot, ``None`` when the hull
    lists the slot but shares no id with the module (the module does not fit)."""
    mod = sui.get(module_id) or {}
    key = SLOT_COMPONENT_KEYS.get(mod.get("ucType") or "")
    if key is None:
        return None
    ids = [c for c in ((mod.get("components") or {}).get(key) or []) if isinstance(c, str)]
    if not ids:
        return None
    hull_comps = (sui.get(hull_module_id) or {}).get("components") or {}
    listed = hull_comps.get(key) if isinstance(hull_comps, dict) else None
    if listed is None:
        return ids[0]
    listed = [c for c in listed if isinstance(c, str)]
    return next((c for c in ids if c in listed), None)


def _chain_depth(sui: dict[str, Any], module_id: str) -> int:
    depth, cur, seen = 0, module_id, set()
    while cur not in seen:
        seen.add(cur)
        prev = (sui.get(cur) or {}).get("prev") or ""
        if not prev or not isinstance(sui.get(prev), dict):
            break
        cur, depth = prev, depth + 1
    return depth


def _mount_items(component: dict[str, Any], key: str, wcs: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """The hardpoint mounts of a weapon component (node-list mounts expanded), in key order."""
    expanded = _gp.expand_node_list_mounts(component, wcs, component_key=key)
    if not isinstance(expanded, dict):
        return []
    return [
        (hp, m) for hp, m in expanded.items()
        if isinstance(hp, str) and hp.startswith("HP_") and isinstance(m, dict)
    ]


def _taxonomy(model_path: Any) -> dict[str, Any]:
    """``scope`` / ``category`` / ``subcategory`` of a mount model from its
    path (``content/gameplay/<scope>/<category>[/<subcategory>]/<asset>/<asset>.model``,
    the accessory library's layout); empty for any other path."""
    if not isinstance(model_path, str):
        return {}
    # Empty segments dropped: game data can spell a folder `missilegun//<asset>`.
    parts = [p for p in model_path.replace("\\", "/").split("/") if p]
    if len(parts) < 5 or parts[0] != "content" or parts[1] != "gameplay":
        return {}
    inner = parts[2:-2]  # <scope>/<category>[/<subcategory>...]
    if len(inner) < 2:
        return {}
    return {
        "scope": inner[0],
        "category": inner[1],
        "subcategory": "/".join(inner[2:]) or None,
    }


def _mount_record(key: str, component: dict[str, Any], hp: str, mount: dict[str, Any]) -> dict[str, Any]:
    """One hardpoint of a mount module: what a consumer writes over the hull's
    placement at ``hp`` when the module is fitted."""
    rec: dict[str, Any] = {"hp_name": hp}
    asset = _gp._path_to_stem(mount.get("model"))
    if asset:
        rec["asset_id"] = asset
        rec.update(_taxonomy(mount.get("model")))
    dead = _gp._path_to_stem(mount.get("deadMesh"))
    if dead:
        rec["dead_asset_id"] = dead
    ammo = [str(a) for a in (mount.get("ammoList") or []) if isinstance(a, str) and a]
    if ammo:
        rec["ammo_ids"] = ammo
    rec.update(_gp.autofill_for_hp({key: {**component, hp: mount}}, hp))
    armor = mount.get("armor")
    if isinstance(armor, dict):
        normalized = {str(k): float(v) for k, v in armor.items() if _num(v) is not None}
        if normalized:
            rec["mount_armor"] = normalized
    return rec


def _mount_effects(ship: dict[str, Any], key: str, component: dict[str, Any], mounts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The module's weapon-bound effect rows: its guns' mount rows (artillery
    only; torpedo launchers have none) and the munition rows of its ammo."""
    placed = {key: {**component, **{hp: m for hp, m in mounts}}}
    ammo: list[str] = []
    for _, m in mounts:
        for a in m.get("ammoList") or []:
            if isinstance(a, str) and a and a not in ammo:
                ammo.append(a)
    rows = _gp.collect_gun_effects(ship, placed) if key == "artillery" else []
    return [*rows, *_gp.collect_munition_effects(ship, placed, ammo_ids=ammo)]


def _slot_numbers(
    ship: dict[str, Any], slot: str, component_id: str, wcs: dict[str, Any] | None,
) -> dict[str, Any]:
    """The numbers module slot ``slot`` changes when component ``component_id`` is fitted."""
    comp = ship.get(component_id)
    if not isinstance(comp, dict):
        return {}
    key = SLOT_COMPONENT_KEYS[slot]
    if slot in MOUNT_SECTIONS:
        mounts = _mount_items(comp, key, wcs)
        out: dict[str, Any] = {MOUNT_SECTIONS[slot]: [_mount_record(key, comp, hp, m) for hp, m in mounts]}
        max_dist = _num(comp.get("maxDist"))
        if slot == "_Artillery" and max_dist is not None and max_dist > 0:
            out["max_dist_m"] = max_dist
        rows = _mount_effects(ship, key, comp, mounts)
        if rows:
            out["effects"] = rows
        return out
    if slot == "_Suo":
        fc = {}
        for src, dst in (("maxDistCoef", "max_dist_coef"), ("sigmaCountCoef", "sigma_count_coef")):
            v = _num(comp.get(src))
            if v is not None:
                fc[dst] = v
        return {"fire_control": fc} if fc else {}
    if slot == "_Engine":
        eng: dict[str, Any] = {}
        v = _num(comp.get("speedCoef"))
        if v is not None:
            eng["speed_coef"] = v
        hl = comp.get("HitLocationEngine")
        if isinstance(hl, dict):
            meta = _gp._hit_location_meta(hl)
            if meta:
                eng["hit_location"] = meta
        return {"engine": eng} if eng else {}
    if slot in _PLANE_SLOTS:
        planes = [p for p in (comp.get("planes") or []) if isinstance(p, str) and p]
        return {"planes": planes} if planes else {}
    return {}


def _placed_component_id(
    ship: dict[str, Any], hull_components: dict[str, Any], key: str,
    placements: list[dict[str, Any]],
) -> str | None:
    """The component the mount autofill read the hull's ``key`` placements
    from (:func:`gameparams_autofill.placed_group`): the hull's resolved
    component when it holds the placed model, else the listed component that
    does. Read at the first placement whose hardpoint the slot's component
    holds; ``None`` when no placement is the slot's."""
    resolved_id = (hull_components.get("__component_ids__") or {}).get(key)
    sui = ship.get("ShipUpgradeInfo") or {}
    wcs = _gp._ship_wcs(ship)
    for item in placements:
        hp = item.get("hp_name") if isinstance(item, dict) else None
        if not isinstance(hp, str):
            continue
        group_key, group = _gp.placed_group(ship, hull_components, hp, item.get("asset_id"))
        if group_key != key or not isinstance(group, dict):
            continue
        if group is hull_components.get(key):
            return resolved_id
        # Another listed component holds the placed model: find its id.
        for name in sorted(sui):
            comps = sui[name].get("components") if isinstance(sui[name], dict) else None
            for cid in (comps or {}).get(key) or []:
                if not isinstance(cid, str):
                    continue
                alt = _gp.expand_node_list_mounts(ship.get(cid), wcs, component_key=key)
                if alt is group or (isinstance(alt, dict) and alt.get(hp) == group.get(hp)):
                    return cid
        return resolved_id
    return None


def _fitted_module(
    slot: str,
    options: list[str],
    component_of: dict[str, str],
    numbers: dict[str, dict[str, Any]],
    placed_component: str | None,
) -> str | None:
    """The module of ``slot`` the rest of the sidecar describes for a hull.

    * mount slots: the option whose component the hull's placement fields
      were read from (passes 3 / 3b);
    * ``_Suo``: the option with ``max_dist_coef`` 1 (the sidecar's ranges and
      each mount's ``dispersion.maxDist`` are the artillery's own, with no
      fire-control coefficient);
    * ``_Engine``: the option with ``speed_coef`` 0 (the hull's ``maxSpeed`` as
      ``hulls.<H>.stats`` carries it);
    * the plane slots: the chain root (the sidecar carries no plane numbers).

    Falls back to the chain root (the first option)."""
    if not options:
        return None
    if slot in MOUNT_SECTIONS and placed_component:
        hit = next((m for m in options if component_of.get(m) == placed_component), None)
        if hit:
            return hit
    if slot == "_Suo":
        hit = next((m for m in options
                    if (numbers.get(m, {}).get("fire_control") or {}).get("max_dist_coef") == 1.0), None)
        if hit:
            return hit
    if slot == "_Engine":
        hit = next((m for m in options
                    if (numbers.get(m, {}).get("engine") or {}).get("speed_coef") == 0.0), None)
        if hit:
            return hit
    return options[0]


def collect_modules_block(
    ship: dict[str, Any],
    doc: dict[str, Any],
    *,
    hull_components: Callable[[str], dict[str, Any]],
    display_name: Callable[[str], str | None] | None = None,
    hardpoint_placement: Callable[[str, str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """The sidecar's ``modules`` section for ``ship`` (a GameParams Vehicle).

    ``doc`` is the sidecar (its ``hulls`` snapshots name the hulls and carry
    each hull's placements); ``hull_components(H)`` resolves hull ``H``'s
    components the way the mount passes do; ``display_name(module_id)`` gives
    the game's name of a module (``None`` when it has none). Empty when the
    ship lists no module of a known slot or the sidecar has no hulls.

    ``hardpoint_placement(hull_component, hp_name, model_path)`` gives the
    placement (``{"transform", "parent_section"}``) of mount model
    ``model_path`` (its GameParams ``.model`` path) at a hardpoint of the
    hull component's model, or ``None`` when the model holds no such node.
    It is asked only for a module's hardpoints the hull does not place, and
    its answer is merged into that hardpoint's record; without it those
    records carry no transform.
    """
    sui = ship.get("ShipUpgradeInfo") if isinstance(ship, dict) else None
    hulls = doc.get("hulls") if isinstance(doc, dict) else None
    if not isinstance(sui, dict) or not isinstance(hulls, dict) or not hulls:
        return {}
    hull_module_of = _hull_modules(sui)
    wcs = _gp._ship_wcs(ship)
    chains = {slot: _gp._walk_ucType_chain(sui, slot) for slot in SLOT_COMPONENT_KEYS}
    chains = {slot: chain for slot, chain in chains.items() if chain}
    if not chains:
        return {}

    entries: dict[str, dict[str, Any]] = {}
    by_hull: dict[str, dict[str, Any]] = {}
    for hull_name in hulls:
        entry = hulls.get(hull_name)
        if not isinstance(entry, dict):
            continue
        hull_mid = entry.get("module_id") or hull_module_of.get(hull_name)
        if not isinstance(hull_mid, str) or hull_mid not in sui:
            continue
        comps_h = None
        slots: dict[str, Any] = {}
        for slot, chain in chains.items():
            options: list[str] = []
            component_of: dict[str, str] = {}
            numbers: dict[str, dict[str, Any]] = {}
            section = MOUNT_SECTIONS.get(slot)
            placed_hps = {
                p.get("hp_name") for p in entry.get(section) or [] if isinstance(p, dict)
            } if section else set()
            for mid in chain:
                cid = module_component_for_hull(sui, mid, hull_mid)
                if cid is None or not isinstance(ship.get(cid), dict):
                    continue
                options.append(mid)
                component_of[mid] = cid
                nums = _slot_numbers(ship, slot, cid, wcs)
                if section:
                    # A mount at a hardpoint the hull does not place has no
                    # transform in the sidecar: list those hardpoints.
                    unplaced = [r["hp_name"] for r in nums.get(section) or [] if r["hp_name"] not in placed_hps]
                    if unplaced:
                        nums["unplaced_hp_names"] = unplaced
                        if hardpoint_placement is not None:
                            # The game mounts a module's models at the module's
                            # own hardpoints: give each the export's transform.
                            hull_comp = _hull_component_name(sui, hull_mid, hull_name)
                            models = {
                                hp: m.get("model")
                                for hp, m in _mount_items(ship[cid], SLOT_COMPONENT_KEYS[slot], wcs)
                            }
                            for r in nums.get(section) or []:
                                model = models.get(r["hp_name"])
                                if r["hp_name"] not in unplaced or not isinstance(model, str):
                                    continue
                                got = hardpoint_placement(hull_comp, r["hp_name"], model)
                                if got:
                                    r.update(got)
                numbers[mid] = nums
                rec = entries.get(mid)
                if rec is None:
                    mod = sui[mid]
                    rec = {
                        "slot": slot,
                        "prev": mod.get("prev") or "",
                        "is_stock": not (mod.get("prev") or ""),
                        "depth": _chain_depth(sui, mid),
                        "hulls": {},
                    }
                    name = display_name(mid) if display_name else None
                    if name:
                        rec["display_name"] = name
                    entries[mid] = rec
                rec["hulls"][hull_name] = {"component": cid, **nums}
            if not options:
                continue
            placed = None
            if slot in MOUNT_SECTIONS:
                if comps_h is None:
                    comps_h = hull_components(hull_name)
                placed = _placed_component_id(
                    ship, comps_h, SLOT_COMPONENT_KEYS[slot],
                    [p for p in entry.get(MOUNT_SECTIONS[slot]) or [] if isinstance(p, dict)],
                )
            slots[slot] = {
                "fitted": _fitted_module(slot, options, component_of, numbers, placed),
                "options": options,
            }
        if slots:
            by_hull[hull_name] = slots
    if not entries:
        return {}
    ordered = {mid: entries[mid] for slot in chains for mid in chains[slot] if mid in entries}
    return {"entries": ordered, "hulls": by_hull}


__all__ = [
    "MOUNT_SECTIONS",
    "SLOT_COMPONENT_KEYS",
    "collect_modules_block",
    "module_component_for_hull",
]
