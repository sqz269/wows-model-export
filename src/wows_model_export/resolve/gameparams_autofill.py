"""GameParams entity transforms — sidecar autofill payloads.

Lifted from ``tools/shared/gameparams.py`` — every function here is a
pure transform over an already-loaded GameParams entity dict (or the
flat-load cache, via the :mod:`wows_model_export.read.gameparams`
slicers). No subprocess, no file writes, no in-memory caching of its
own — the only state is the read-side flat-load cache that backs
:func:`get_entity` etc.

What ends up here:

* Mesh-swap permoflage resolution
  (:func:`resolve_variant_model_dir`,
  :func:`find_vehicle_by_native_permoflage`,
  :func:`resolve_variant_accessory_swaps`).

* ShipUpgradeInfo navigation (:func:`resolve_components`,
  :func:`variants_summary`).

* Per-ship metadata extras (:func:`ship_metadata_extras`).

* Per-mount autofill (:func:`autofill_for_hp` plus the gun / AA /
  torpedo field fillers).

* Per-projectile profile + visual + effects extras
  (:func:`torpedo_profile_extras`, :func:`shell_visual_extras`,
  :func:`shell_effects_extras`, :func:`torpedo_visual_extras`,
  :func:`torpedo_effects_extras`).

* Hitbox classification (:func:`classify_splash_boxes`).

* Armor extraction + cross-validation (:func:`collect_mount_armor`,
  :func:`collect_barbettes`, :func:`cross_validate_armor`).

* Class derivation (:func:`class_from_caliber`,
  :func:`derive_class_from_placements`).

Layer 3 resolve module: depends on the Layer 1 read module
(:mod:`wows_model_export.read.gameparams`) for entity access, never
on the toolkit-side dump itself.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..read.gameparams import (
    get_exterior,
    get_projectile,
    get_ship,
    load_full,
    resolve_ship_id,
)

# ---------------------------------------------------------------------------
# Mesh-swap permoflage resolution
# ---------------------------------------------------------------------------


def _model_path_to_dir(model_path: str) -> str | None:
    """Strip ``content/.../<dir>/<dir>.model`` -> ``<dir>``.

    Returns the basename of the parent directory (which equals the
    model stem for WG content layout). Returns ``None`` if the path
    doesn't fit the expected ``.model`` shape.
    """
    if not isinstance(model_path, str) or not model_path.endswith(".model"):
        return None
    parts = model_path.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    return parts[-2]   # parent directory name


def _is_ship_hull_path(path: str) -> bool:
    """True if ``path`` looks like a top-level ship hull ``.model``.

    Top-level ship hull paths follow the shape
    ``content/gameplay/<nation>/ship/<class>/<DIR>/<DIR>.model`` with
    NO ``_Bow`` / ``_MidFront`` / ``_MidBack`` / ``_Stern`` segment
    suffix. This is what we want to swap for a hull mesh-swap; sub-
    section variants are exercised at runtime through the same model.
    """
    if not isinstance(path, str) or "/ship/" not in path or not path.endswith(".model"):
        return False
    leaf = path.rsplit("/", 1)[-1]
    stem = leaf[:-len(".model")]
    for suffix in ("_Bow", "_MidFront", "_MidBack", "_Stern",
                   "_Bow_ep", "_MidFront_ep", "_MidBack_ep", "_Stern_ep",
                   "_Stern_dock"):
        if stem.endswith(suffix):
            return False
    return True


def resolve_variant_model_dir(
    vehicle_id: str,
    *,
    permoflage_id: str | None = None,
    refresh: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve a Vehicle's mesh-swap variant model_dir.

    Returns ``(variant_model_dir, exterior_id)`` where:

    - ``variant_model_dir`` is the swap-target hull directory (e.g.
      ``"JSC507_Takao_1944_Arpeggio"``) or ``None`` if the Vehicle's
      selected permoflage doesn't carry a hull mesh swap.
    - ``exterior_id`` is the Exterior GameParams id we walked. Useful
      for surfacing in sidecar metadata and for downstream nodesConfig
      / peculiarityModels consumers (per-mount accessory swaps).

    Resolution order, mirroring the runtime:

    1. If ``permoflage_id`` is given, use that Exterior. Otherwise fall
       back to ``Vehicle.nativePermoflage`` (the default permoflage
       loaded when the ship spawns without a player-selected skin).
    2. Inspect ``Exterior.hullConfig.{A,B}_Hull.model``. If non-empty,
       extract the directory portion of that path. WG ships set both
       hulls to the same variant for permoflages — A_Hull wins.
    3. Inspect ``Exterior.peculiarityModels`` for an entry whose KEY
       is a top-level ship hull ``.model`` path (no
       ``_Bow``/``_MidFront``/etc. suffix). The VALUE is the variant
       hull's ``.model`` path; extract its directory.
    4. If neither path is set (texture-only camo), return
       ``(None, exterior_id)``.

    See ``tools/reference/ships/mesh_swap_permoflages.md`` for the
    full encoding and the ARP Takao worked example.
    """
    ship = get_ship(vehicle_id, refresh=refresh)
    if not ship:
        return (None, None)

    chosen_id = permoflage_id or ship.get("nativePermoflage")
    if not chosen_id:
        return (None, None)

    ext = get_exterior(chosen_id, refresh=refresh)
    if not ext:
        return (None, chosen_id)

    # Path 1: hullConfig (per-hull-tier full hull swap)
    hull_config = ext.get("hullConfig") or {}
    if isinstance(hull_config, dict):
        for hull_key in ("A_Hull", "B_Hull"):
            entry = hull_config.get(hull_key)
            if isinstance(entry, dict):
                model = entry.get("model")
                if isinstance(model, str) and model:
                    d = _model_path_to_dir(model)
                    if d:
                        return (d, chosen_id)

    # Path 2: peculiarityModels keyed by top-level ship hull path
    pm = ext.get("peculiarityModels") or {}
    if isinstance(pm, dict):
        for src, dst in pm.items():
            if not _is_ship_hull_path(src):
                continue
            # Value can be either a string path or a dict with a "model" key
            if isinstance(dst, dict):
                dst = dst.get("model")
            if not isinstance(dst, str):
                continue
            d = _model_path_to_dir(dst)
            if d:
                return (d, chosen_id)

    return (None, chosen_id)


#: Per-process index ``{nativePermoflage → vehicle_key}`` built lazily on
#: first :func:`find_vehicle_by_native_permoflage` call. Keyed by the
#: ``id(flat)`` of the cached GameParams dict so a refresh
#: (load_full(refresh=True)) invalidates automatically — the new dict has
#: a different id() and we rebuild.
_NATIVE_PERMOFLAGE_INDEX: tuple[int, dict[str, str]] | None = None


def find_vehicle_by_native_permoflage(
    exterior_id: str,
    *,
    refresh: bool = False,
) -> str | None:
    """Return the full GameParams entity key (e.g. ``"PASB820_BA_Montana"``)
    of the Vehicle whose ``nativePermoflage`` equals ``exterior_id``, or
    ``None`` if no such Vehicle exists in the dump.

    Used by scaffold_ship to recover the canonical variant Vehicle ID
    when placements.json's ``param_index`` was set by the toolkit's
    name-resolution to the BASE Vehicle (e.g. PASB017_Montana_1945 for
    the Hoshino BA_Montana model_dir), but the actual gameplay Vehicle
    that owns this variant's permoflage list / peculiarity / etc. is a
    distinct entity (PASB820_BA_Montana). Without this reverse lookup,
    re-scaffolds of variant ships load the base Vehicle and miss the
    variant's per-ship ``mat_*`` permoflage skin (e.g. mat_Montana_Hoshino)
    that should fold into the default skin's overlay.

    Builds a per-process ``{nativePermoflage → vehicle_key}`` index on
    first call so repeated lookups in a batch run amortise the walk. The
    fleet has ~1 Vehicle per ``nativePermoflage`` (mesh-swap variants
    are 1:1 with their permoflage); first occurrence wins.
    """
    global _NATIVE_PERMOFLAGE_INDEX
    if not isinstance(exterior_id, str) or not exterior_id:
        return None
    flat = load_full(refresh=refresh) if refresh else load_full()
    cache = _NATIVE_PERMOFLAGE_INDEX
    if cache is None or cache[0] != id(flat):
        index: dict[str, str] = {}
        for k, v in flat.items():
            if not isinstance(v, dict):
                continue
            ti = v.get("typeinfo")
            if not isinstance(ti, dict) or ti.get("type") != "Ship":
                continue
            np = v.get("nativePermoflage")
            if isinstance(np, str) and np and np not in index:
                index[np] = k
        _NATIVE_PERMOFLAGE_INDEX = (id(flat), index)
        cache = _NATIVE_PERMOFLAGE_INDEX
    return cache[1].get(exterior_id)


def _path_to_stem(model_path: str) -> str | None:
    """Strip ``content/.../<DIR>/<STEM>.model`` -> ``<STEM>``.

    Differs from :func:`_model_path_to_dir` (which returns the parent
    directory name) in that it returns the bare ``.model`` filename
    stem. WG content layout means the two normally agree
    (``JGM024_.../JGM024_.model``), but ``_dead`` variants sit in the
    same dir as their live sibling so the stems diverge
    (``JGM024_.../JGM024_..._dead.model`` -> stem ``JGM024_..._dead``,
    parent dir ``JGM024_...``). For per-mount accessory swaps we need
    the stem because that's what ``asset_id`` / ``dead_asset_id`` carry
    in the sidecar.
    """
    if not isinstance(model_path, str) or not model_path.endswith(".model"):
        return None
    leaf = model_path.replace("\\", "/").rsplit("/", 1)[-1]
    return leaf[:-len(".model")] or None


def resolve_variant_accessory_swaps(
    vehicle_id: str,
    *,
    permoflage_id: str | None = None,
    refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Resolve a mesh-swap permoflage's per-mount accessory swaps from
    BOTH encodings WG uses across the variant corpus.

    Pattern A — ``Exterior.peculiarityModels`` (asset-path keyed):
        ``{base_model_path -> variant_model_path}``. Used by ARP Blue
        (PJES477_ARP_TAKAO), Black Friday, Sabaton, etc. — typical for
        single-shot swap-everything variants.

    Pattern B — ``Exterior.nodesConfig.<section>.<HP_name>.{model,
    deadMesh}`` (hardpoint-name keyed): a per-HP override carrying
    both alive and dead model paths. Used by Azur Lane (PAES329 New
    Jersey on Iowa, etc.), Transformers / Optimus Prime, and mixed in
    with ``peculiarityModels`` on some variants — ARP Takao Red
    (PJES478_RED_TAKAO) carries 5 turret swaps + 2 director swaps in
    ``nodesConfig`` that the peculiarityModels-only walker misses.
    Iowa AzurLane (PAES329) has empty peculiarityModels and ALL its
    accessory swaps in nodesConfig.

    Returns the union of both patterns as a dict-of-dicts::

        {
          "by_asset_id":          {base_asset_id: variant_asset_id},
          "by_hp_name":           {hp_name: variant_asset_id},
          "dead_by_hp_name":      {hp_name: variant_dead_asset_id},
          "misc_filter_by_hp":    {hp_name: [MP_<variant_aid>, ...]},
        }

    All four sub-dicts are empty for ships without an Exterior,
    without a permoflage selected, or where the Exterior is texture-
    only. The hull entry in peculiarityModels is excluded —
    :func:`resolve_variant_model_dir` handles the hull swap.

    ``misc_filter_by_hp`` carries the per-HP whitelist override that
    WG's runtime applies when the variant's bundled miscs differ from
    the vanilla's. Each Azur/ARP/Sabaton variant turret has its OWN
    ``.skel_ext`` with its OWN bundled MP_*s — sometimes a strict
    subset of vanilla, sometimes a mix of vanilla + variant-themed.
    The Exterior's ``nodesConfig.<sec>.<HP>.miscFilter`` lists exactly
    the bundled MP_*s that render on the variant under THAT HP, so it
    must override the vanilla's per-HP miscFilter (sourced from the
    ship's ``<Component>.<HP>.miscFilter`` and stored in our sidecar's
    per-mount ``misc_filter`` field). Empty list = drop everything
    bundled on this HP under this variant (the most common state for
    Azur director HPs, where the entire vanilla decorative set is
    discarded). Without applying the override, our sidecar carries the
    vanilla MP_* IDs that the variant's ``.skel_ext`` doesn't expose,
    and consumers drop every bundled misc — leaving e.g. Azur Cleveland
    secondary directors radar-bare, vanilla cranes / boats / ladders
    missing from Azur Montpelier's main turrets, etc.

    Aircraft swaps (e.g. ARP's JAF017 -> JAF506_Takao) come through on
    ``by_asset_id``; whether downstream consumers render them is a
    separate concern (the catapult fighter isn't currently emitted as
    an accessory by export-ship, so the entry is harmless).

    Apply via :func:`tools.ship.sidecar.apply_variant_asset_swaps`
    after ``new_document_from_placements``. HP-name-keyed swaps take
    precedence over asset-id-keyed ones (more specific).
    """
    empty = {
        "by_asset_id":       {},
        "by_hp_name":        {},
        "dead_by_hp_name":   {},
        "misc_filter_by_hp": {},
    }
    ship = get_ship(vehicle_id, refresh=refresh)
    if not ship:
        return empty
    chosen_id = permoflage_id or ship.get("nativePermoflage")
    if not chosen_id:
        return empty
    ext = get_exterior(chosen_id, refresh=refresh)
    if not ext:
        return empty

    by_asset_id: dict[str, str] = {}
    by_hp_name: dict[str, str] = {}
    dead_by_hp_name: dict[str, str] = {}
    misc_filter_by_hp: dict[str, list[str]] = {}

    # Pattern A — peculiarityModels (asset-path keyed).
    pm = ext.get("peculiarityModels") or {}
    if isinstance(pm, dict):
        for src, dst in pm.items():
            if _is_ship_hull_path(src):
                continue  # hull swap — resolve_variant_model_dir handles it
            if isinstance(dst, dict):
                dst = dst.get("model")
            if not isinstance(dst, str):
                continue
            bs = _path_to_stem(src)
            vs = _path_to_stem(dst)
            if bs and vs and bs != vs:
                by_asset_id[bs] = vs

    # Pattern B — nodesConfig per-HP (hardpoint-name keyed). Carries
    # model + deadMesh swaps AND a per-HP miscFilter override that
    # references the variant's bundled MP_*s (which differ from the
    # vanilla ship's per-HP miscFilter when the variant's .skel_ext
    # carries different placement_ids — almost always, for Azur Lane
    # mesh-swap variants).
    nc = ext.get("nodesConfig") or {}
    if isinstance(nc, dict):
        for sec in nc.values():
            if not isinstance(sec, dict):
                continue
            for hp, body in sec.items():
                if not isinstance(body, dict) or not isinstance(hp, str):
                    continue
                m = body.get("model")
                if isinstance(m, str):
                    stem = _path_to_stem(m)
                    if stem:
                        by_hp_name[hp] = stem
                d = body.get("deadMesh")
                if isinstance(d, str):
                    stem = _path_to_stem(d)
                    if stem:
                        dead_by_hp_name[hp] = stem
                # Per-HP miscFilter override. WG runtime treats this as
                # the authoritative whitelist when present (overrides the
                # vanilla ship's <Component>.<HP>.miscFilter). Empty list
                # is a meaningful state — "drop every bundled misc on
                # this HP under this variant". Last write wins on HP-name
                # collision across components (rare; same HP under
                # multiple sections would be a GameParams data bug).
                mf = body.get("miscFilter")
                if isinstance(mf, list):
                    misc_filter_by_hp[hp] = [str(s) for s in mf]

    return {
        "by_asset_id":       by_asset_id,
        "by_hp_name":        by_hp_name,
        "dead_by_hp_name":   dead_by_hp_name,
        "misc_filter_by_hp": misc_filter_by_hp,
    }


# ---------------------------------------------------------------------------
# ShipUpgradeInfo navigation
# ---------------------------------------------------------------------------


def _walk_ucType_chain(
    sui: dict[str, Any],
    uc_type: str,
) -> list[str]:
    """Return upgrade entries of one ``ucType`` ordered stock -> upgraded.

    Stock = the entry with ``prev == ""``. Each subsequent entry's ``prev``
    points at the previous step. WG always lays this out as a linear chain
    per ucType (one "true" upgrade path); branch-style upgrades aren't used
    in the current data.
    """
    entries = {
        k: v for k, v in sui.items()
        if isinstance(v, dict) and v.get("ucType") == uc_type
    }
    if not entries:
        return []
    # Find roots (prev empty / null / pointing at a non-existent entry).
    roots = [k for k, v in entries.items() if not (v.get("prev") or "").strip()]
    if not roots:
        # Fallback: pick whichever entry has no other entry pointing at it
        # via prev. Defensive — WG data hasn't been seen without a clear
        # ``prev=""`` root, but don't crash if it ever ships.
        pointed_at = {v.get("prev") for v in entries.values() if v.get("prev")}
        roots = [k for k in entries if k not in pointed_at]
        if not roots:
            return sorted(entries.keys())  # last-ditch deterministic order
    # Walk forward from each root via children-of-prev. With a single linear
    # chain there's exactly one root; multi-root case (rare) emits each chain
    # in turn for stable output.
    out: list[str] = []
    for root in sorted(roots):
        cur: str | None = root
        seen: set[str] = set()
        while cur and cur in entries and cur not in seen:
            out.append(cur)
            seen.add(cur)
            # Find the child whose prev == cur.
            nxt = next(
                (k for k, v in entries.items()
                 if v.get("prev") == cur and k not in seen),
                None,
            )
            cur = nxt
    return out


# WG components dict keys we care about, in document order. Module slots WG
# defines but the sidecar doesn't currently consume (``pinger``,
# ``depthCharges``) are still surfaced — empty lists don't hurt.
_COMPONENT_KEYS: tuple[str, ...] = (
    "hull",
    "artillery",
    "atba",
    "airDefense",
    "torpedoes",
    "directors",
    "finders",
    "radars",
    "airArmament",
    "airSupport",
    "fireControl",
    "engine",
    "depthCharges",
    "pinger",
)

# (ucType, slot) pairs where the override is the *intended* design: _Hull
# carries stock defaults for these slots and the dedicated ucType chain is
# meant to replace them. The override warning skips these to avoid drowning
# real anomalies (e.g. a future patch shipping _Suo with an `artillery`
# slot) in thousands of benign lines.
_EXPECTED_OVERRIDES: frozenset[tuple[str, str]] = frozenset({
    ("_Artillery", "artillery"),
    ("_Torpedoes", "torpedoes"),
    ("_Engine", "engine"),
    ("_Suo", "fireControl"),
})


def resolve_components(
    ship: dict[str, Any],
    *,
    hull_choice: str = "upgraded",
) -> dict[str, Any]:
    """Walk ``ShipUpgradeInfo`` and return the active component dicts.

    Returns ``{component_key: ship[component_id_or_dict]}``. When a slot has
    multiple options (Iowa's engine: AB1_Engine vs AB2_Engine; fireControl:
    AB1_FireControl vs AB2_FireControl), the picked entry is the chain-end
    (``hull_choice='upgraded'``) or chain-root (``hull_choice='stock'``).

    Empty slots (Iowa: ``torpedoes: []``) yield ``{"hull": ..., ..., "torpedoes": None}``.
    Slots whose component_id doesn't exist on the ship dict yield ``None``
    so callers can tell "WG didn't ship this" from "we got it wrong".

    Returns:
      A dict keyed by every key in :data:`_COMPONENT_KEYS`, plus
      ``__active_hull_id__`` / ``__stock_hull_id__`` recording the picked
      ``A_Hull`` / ``B_Hull`` slot name (so callers building a ``variants``
      summary don't have to re-walk).
    """
    if hull_choice not in ("upgraded", "stock"):
        raise ValueError(
            f"hull_choice must be 'upgraded' or 'stock', got {hull_choice!r}"
        )
    sui = ship.get("ShipUpgradeInfo") or {}
    if not isinstance(sui, dict):
        sui = {}

    # Determine the active entry per ucType: chain end (upgraded) or root
    # (stock). _Hull carries the bulk of the components dict; _Engine /
    # _Suo / _Torpedoes / _Artillery contribute their own slot keys
    # (``engine``, ``fireControl``, etc.) that the _Hull entry's
    # components dict does NOT cover. We merge across all ucTypes to get a
    # complete view.
    chain_by_uctype: dict[str, list[str]] = {}
    for uc_type in ("_Hull", "_Artillery", "_Engine", "_Suo", "_Torpedoes"):
        chain = _walk_ucType_chain(sui, uc_type)
        if chain:
            chain_by_uctype[uc_type] = chain

    def pick_chain(uc_type: str) -> str | None:
        chain = chain_by_uctype.get(uc_type) or []
        if not chain:
            return None
        return chain[-1] if hull_choice == "upgraded" else chain[0]

    # Resolved upgraded-hull entry isn't read directly (we walk the merged
    # components below to pick the active hull component_id), but the
    # call has side-effects via the `pick_chain` closure's chain memoisation
    # in some callers — preserve it as a discarded read for parity with
    # the original module.
    _active_hull_entry = pick_chain("_Hull")
    stock_hull_entry = chain_by_uctype.get("_Hull", [None])[0] if chain_by_uctype.get("_Hull") else None

    # Union the components dicts from each ucType's picked entry. Later
    # entries override earlier ones on the (rare) overlap; ucType iteration
    # order is deterministic so the merged result is stable.
    components_src: dict[str, Any] = {}
    components_origin: dict[str, str] = {}
    for uc_type in ("_Hull", "_Artillery", "_Engine", "_Suo", "_Torpedoes"):
        pick = pick_chain(uc_type)
        if not pick or not isinstance(sui.get(pick), dict):
            continue
        comps = sui[pick].get("components")
        if not isinstance(comps, dict):
            continue
        for k, v in comps.items():
            if v:
                if (
                    k in components_src
                    and (uc_type, k) not in _EXPECTED_OVERRIDES
                ):
                    # Surface only *unexpected* overrides. The expected
                    # pairings (see _EXPECTED_OVERRIDES) are the design;
                    # this branch catches a future WG patch shipping e.g.
                    # `_Suo` with an `artillery` slot, which would silently
                    # clobber `_Hull`'s artillery without this signal.
                    print(
                        f"  warn: resolve_components: {uc_type} entry "
                        f"{pick!r} overrides existing {k!r} from "
                        f"{components_origin[k]!r}",
                        file=sys.stderr,
                    )
                components_src[k] = v
                components_origin[k] = uc_type

    out: dict[str, Any] = {}
    active_hull_component_id: str | None = None
    stock_hull_component_id: str | None = None
    for key in _COMPONENT_KEYS:
        ids = components_src.get(key) or []
        if not isinstance(ids, list) or not ids:
            out[key] = None
            continue
        component_id = ids[0]
        if key == "hull":
            active_hull_component_id = component_id
            stock_components = (
                sui.get(stock_hull_entry, {}).get("components", {})
                if stock_hull_entry
                else {}
            )
            stock_hull_ids = stock_components.get("hull") or []
            stock_hull_component_id = (
                stock_hull_ids[0] if stock_hull_ids else None
            )
        out[key] = ship.get(component_id) if isinstance(component_id, str) else None

    out["__active_hull_id__"] = active_hull_component_id
    out["__stock_hull_id__"] = stock_hull_component_id
    return out


def variants_summary(ship: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-friendly summary of the ship's upgrade tree.

    Shape::

        {
          "active_hull": "B_Hull",
          "stock_hull":  "A_Hull",
          "research_path": ["PAUH831_Iowa_1943", "PAUH832_Iowa_1944"],
          "next_ships":    ["PASB017_Montana_1945"],
          "modules": {
            "_Engine":      ["AB1_Engine", "AB2_Engine"],
            "_FireControl": ["AB1_FireControl", "AB2_FireControl"],
            ...
          }
        }
    """
    sui = ship.get("ShipUpgradeInfo") or {}
    if not isinstance(sui, dict):
        return {}

    components = resolve_components(ship, hull_choice="upgraded")
    active_hull_id = components.get("__active_hull_id__")
    stock_hull_id = components.get("__stock_hull_id__")

    research_path = _walk_ucType_chain(sui, "_Hull")
    # Pick `nextShips` from the chain-end hull entry (closest to "research
    # this hull, then this destination is unlocked").
    next_ships: list[str] = []
    if research_path:
        end_entry = sui.get(research_path[-1]) or {}
        next_ships = list(end_entry.get("nextShips") or [])

    modules: dict[str, list[str]] = {}
    for uc_type in (
        "_Hull", "_Artillery", "_Engine", "_Suo", "_Torpedoes",
    ):
        chain = _walk_ucType_chain(sui, uc_type)
        if chain:
            modules[uc_type] = chain

    return {
        "active_hull": active_hull_id,
        "stock_hull":  stock_hull_id,
        "research_path": research_path,
        "next_ships":    next_ships,
        "modules":       modules,
    }


# ---------------------------------------------------------------------------
# Ship metadata extras
# ---------------------------------------------------------------------------


def ship_metadata_extras(ship: dict[str, Any]) -> dict[str, Any]:
    """Return ship-level metadata not currently captured by the toolkit's
    placements JSON. Suitable for merging into ``sidecar.ship``.

    Only fields with non-empty / non-default values are included so a
    plain Vehicle without exotic flags doesn't pollute the sidecar with
    empty strings.
    """
    out: dict[str, Any] = {}
    archetype = ship.get("archetype")
    if isinstance(archetype, str) and archetype and archetype != "Undefined":
        out["archetype"] = archetype
    peculiarity = ship.get("peculiarity")
    if isinstance(peculiarity, str) and peculiarity and peculiarity != "default":
        out["peculiarity"] = peculiarity
    pec_flag = ship.get("peculiarityFlag")
    if isinstance(pec_flag, str) and pec_flag:
        out["peculiarity_flag"] = pec_flag
    if ship.get("isPaperShip") is True:
        out["paper_ship"] = True
    return out


# ---------------------------------------------------------------------------
# Mount autofill
# ---------------------------------------------------------------------------


def _name_to_display(name: str) -> str:
    """Convert a WG asset name (``PAGM034_16in50_Mk7``) to a readable label
    (``"AGM034 16in50 Mk7"``).

    Strips just the leading ``P`` constant prefix — keeps the nation letter
    + type code so the result aligns with the toolkit's asset_id. Hand-edits
    to ``display_name`` survive merges, so callers can substitute friendlier
    labels (``"Main Battery — Forward Superfire"``) downstream.
    """
    if not name:
        return ""
    label = name
    # Position 0 is always 'P' (constant prefix in WG's GameParams naming).
    # Position 1 is the nation/scope letter — keep it (it lines up with the
    # toolkit-emitted asset_id, which preserves the same letters).
    if label.startswith("P") and len(label) > 1 and label[1].isalpha():
        label = label[1:]
    return label.replace("_", " ").strip()


def _safe_int(v: Any) -> int | None:
    if isinstance(v, bool):  # bool is an int subclass — exclude
        return None
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return None
    return None


def _safe_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _ammo_types_for(ammo_ids: Iterable[str], *, refresh: bool = False) -> list[str]:
    """Resolve a list of Projectile IDs to their ammo-type strings.

    Ammo IDs not found in GameParams are skipped (rare; would indicate
    stale ``ammoList`` data). The returned list preserves the order of
    ``ammo_ids`` and de-duplicates while preserving first-seen position.
    """
    seen: set[str] = set()
    out: list[str] = []
    for ammo_id in ammo_ids:
        if not isinstance(ammo_id, str):
            continue
        proj = get_projectile(ammo_id, refresh=refresh)
        if proj is None:
            continue
        ammo_type = proj.get("ammoType")
        if not isinstance(ammo_type, str) or not ammo_type:
            continue
        if ammo_type not in seen:
            seen.add(ammo_type)
            out.append(ammo_type)
    return out


def autofill_for_hp(
    components: dict[str, Any],
    hp_name: str,
) -> dict[str, Any]:
    """Return a dict of sidecar-shape gameplay fields for one hardpoint.

    Walks every group in ``components`` looking for ``hp_name``; returns
    ``{}`` if the hardpoint isn't an HP_-bound mount (e.g. a decorative
    placement) or isn't carried by any active group.

    The returned dict uses sidecar field names (``caliber_mm``, etc.); a
    caller passes it straight through ``merge_preserving``.
    """
    if not hp_name or not isinstance(hp_name, str):
        return {}

    # Each group dict (Artillery / ATBA / AirDefense / Torpedoes / Directors /
    # Finders / Radars / AirArmament) holds either HP_-keyed mount dicts plus
    # group-level scalars (for guns; e.g. AB1_Artillery.maxDist), or a flat
    # mount-only dict (Directors). Walk both shapes.
    for group_key in (
        "artillery", "atba", "airDefense", "torpedoes",
        "directors", "finders", "radars", "airArmament",
    ):
        group = components.get(group_key)
        if not isinstance(group, dict):
            continue
        mount = group.get(hp_name)
        if not isinstance(mount, dict):
            continue
        out: dict[str, Any] = {}
        # display_name from the asset name. The toolkit's placements JSON
        # already carries the asset_id; this gives a human label that
        # survives independent of the mesh.
        name = mount.get("name")
        if isinstance(name, str) and name:
            out["display_name"] = _name_to_display(name)

        # Per-HP miscFilter — universal across all groups. Drives
        # composition-time selection of accessory-attached miscs (the
        # rangefinder + periscope bundled into a main turret are picked
        # at HP_AGM_1 on Iowa, etc.). The WG runtime treats it as a
        # WHITELIST — `keep iff placement.isStyle OR placement.name in
        # miscFilter OR miscName in customMiscs` (verified 2026-05-08
        # via decompile of `MiscsController._getMiscsForLoading`).
        # Empty `miscFilter: []` therefore drops every non-isStyle
        # placement (e.g. base Shinano HP_JD_4 gates out the H2017
        # Zuikaku alt-director skin from JD124's bundled attachments).
        # Preserve `[]` distinct from absent so consumers can tell
        # "empty whitelist = drop all" apart from "no filter info =
        # render all (legacy)". The companion `miscFilterMode` field
        # is phantom — never read by runtime, dropped 2026-05-09.
        # See `project_misc_filter_whitelist_inverted.md` and
        # `tools/reference/investigations/misc_filter_per_mount_handoff.md`.
        mf = mount.get("miscFilter")
        if isinstance(mf, list):
            out["misc_filter"] = [str(s) for s in mf]

        if group_key in ("artillery", "atba"):
            _fill_gun_fields(mount, group, out)
        elif group_key == "airDefense":
            _fill_aa_fields(mount, out)
        elif group_key == "torpedoes":
            _fill_torpedo_fields(mount, out)
        # directors / finders / radars / airArmament: display_name + miscFilter only.
        return out
    return {}


def _fill_gun_fields(
    mount: dict[str, Any],
    group: dict[str, Any],
    out: dict[str, Any],
) -> None:
    """Main / secondary battery fields. ``group`` carries shared dispersion
    + range data (``sigmaCount``, ``maxDist``); ``mount`` carries per-mount
    kinematics (yaw/elev arcs, traverse rate, reload)."""
    barrel_d = _safe_float(mount.get("barrelDiameter"))
    if barrel_d is not None and barrel_d > 0:
        # GameParams stores barrel diameter in metres (0.406 = 406 mm).
        out["caliber_mm"] = round(barrel_d * 1000.0, 2)
    barrels = _safe_int(mount.get("numBarrels"))
    if barrels is not None:
        out["barrel_count"] = barrels
    horiz = mount.get("horizSector")
    if isinstance(horiz, list) and len(horiz) == 2:
        out["yaw_range_deg"] = [float(horiz[0]), float(horiz[1])]
    vert = mount.get("vertSector")
    if isinstance(vert, list) and len(vert) == 2:
        out["elev_range_deg"] = [float(vert[0]), float(vert[1])]
    rot = mount.get("rotationSpeed")
    if isinstance(rot, list) and len(rot) >= 1:
        out["traverse_rate"] = float(rot[0])
        if len(rot) >= 2:
            out["elev_rate"] = float(rot[1])
    reload = _safe_float(mount.get("shotDelay"))
    if reload is not None and reload > 0:
        out["reload_s"] = reload
    sigma = _safe_float(group.get("sigmaCount"))
    if sigma is not None:
        out["sigma"] = sigma
    ammo_list = mount.get("ammoList") or []
    if isinstance(ammo_list, list):
        types = _ammo_types_for(ammo_list)
        if types:
            out["ammo_types"] = types


def _fill_aa_fields(mount: dict[str, Any], out: dict[str, Any]) -> None:
    """AA mount fields. ``aa_range_km`` and ``aa_dps`` are the aura
    coefficients — consumers scale them to per-frame DPS at ``AAMount``
    instantiation. Yaw / elev arcs are also exposed where present."""
    barrel_d = _safe_float(mount.get("barrelDiameter"))
    if barrel_d is not None and barrel_d > 0:
        out["caliber_mm"] = round(barrel_d * 1000.0, 2)
    barrels = _safe_int(mount.get("numBarrels"))
    if barrels is not None:
        out["barrel_count"] = barrels
    horiz = mount.get("horizSector")
    if isinstance(horiz, list) and len(horiz) == 2:
        out["yaw_range_deg"] = [float(horiz[0]), float(horiz[1])]
    vert = mount.get("vertSector")
    if isinstance(vert, list) and len(vert) == 2:
        out["elev_range_deg"] = [float(vert[0]), float(vert[1])]
    rot = mount.get("rotationSpeed")
    if isinstance(rot, list) and len(rot) >= 1:
        out["traverse_rate"] = float(rot[0])
        if len(rot) >= 2:
            out["elev_rate"] = float(rot[1])
    aa_dist = _safe_float(mount.get("antiAirAuraDistance"))
    if aa_dist is not None and aa_dist > 0:
        # GameParams emits range in metres; sidecar exposes km.
        out["aa_range_km"] = round(aa_dist / 1000.0, 3)
    aa_str = _safe_float(mount.get("antiAirAuraStrength"))
    if aa_str is not None and aa_str > 0:
        out["aa_dps"] = aa_str  # coefficient, not absolute DPS


def _fill_torpedo_fields(mount: dict[str, Any], out: dict[str, Any]) -> None:
    """Torpedo tube fields. WG calls each barrel a ``numBarrels`` count
    that's actually the number of tubes in the mount."""
    tubes = _safe_int(mount.get("numBarrels"))
    if tubes is not None:
        out["tube_count"] = tubes
    reload = _safe_float(mount.get("shotDelay"))
    if reload is not None and reload > 0:
        out["reload_s"] = reload
    horiz = mount.get("horizSector")
    if isinstance(horiz, list) and len(horiz) == 2:
        out["yaw_range_deg"] = [float(horiz[0]), float(horiz[1])]
    rot = mount.get("rotationSpeed")
    if isinstance(rot, list) and len(rot) >= 1:
        out["traverse_rate"] = float(rot[0])
    ammo_list = mount.get("ammoList") or []
    if isinstance(ammo_list, list):
        types = _ammo_types_for(ammo_list)
        if types:
            out["ammo_types"] = types


# ---------------------------------------------------------------------------
# Torpedo profile extras (schema v3.1 ballistics split)
# ---------------------------------------------------------------------------

# WoWS native unit -> metres conversion. The toolkit pre-scales geometry at
# emit time (since 2026-04-23), but ``Projectile.depth`` / ``alertDist`` are
# loaded straight out of GameParams and stay in native units.
_NATIVE_TO_METRES = 15.0


def torpedo_profile_extras(ammo_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Resolve a PAPT* torpedo's GameParams entry and return the
    sidecar-shape ``ballistics.torpedoes[<ammo_id>]`` extras dict.

    Empty dict when:
    * The ammo_id doesn't resolve to a Projectile (typo / patch removal).
    * The Projectile's ``ammoType`` isn't ``"torpedo"`` (caller passed a
      shell ID by mistake).

    See :func:`tools.ship.sidecar.make_torpedo_profile` for the field
    schema. Source mappings:

    * ``speed_kts``       <- ``speed`` (knots — already in source units).
    * ``running_depth_m`` <- ``depth * 15`` (native -> metres).
    * ``arming_time_s``   <- ``armingTime`` (seconds).
    * ``flood_capable``   <- ``floodGeneration`` (bool).
    * ``is_deep_water``   <- ``isDeepWater`` (bool).
    * ``with_parachute``  <- ``withParachute`` (bool — air-dropped torps).
    * ``visibility_factor`` <- ``visibilityFactor`` (detection-range coefficient).
    * ``splash_armor_coeff`` <- ``splashArmorCoeff`` (secondary blast vs armor).
    * ``alert_distance_m``   <- ``alertDist * 15`` (best-effort — see note).
    * ``affected_by_ptz``    <- ``affectedByPTZ`` (bool — torpedo defense system applies).
    * ``burn_probability``   <- ``burnProb`` (always 0.0 for torps; preserved for schema parity).

    ``splash_radius_m`` is NOT emitted here — ``splashCubeSize`` semantics
    are ambiguous (might already be metres post-2026-04-23 toolkit bake or
    might still be native; values like ``1.2`` are plausible either way).
    """
    proj = get_projectile(ammo_id, refresh=refresh)
    if proj is None:
        return {}
    if proj.get("ammoType") != "torpedo":
        return {}
    out: dict[str, Any] = {}
    speed = _safe_float(proj.get("speed"))
    if speed is not None and speed > 0:
        out["speed_kts"] = speed
    depth = _safe_float(proj.get("depth"))
    if depth is not None and depth > 0:
        out["running_depth_m"] = round(depth * _NATIVE_TO_METRES, 3)
    arming = _safe_float(proj.get("armingTime"))
    if arming is not None and arming >= 0:
        out["arming_time_s"] = arming
    if "floodGeneration" in proj:
        out["flood_capable"] = bool(proj["floodGeneration"])
    if "isDeepWater" in proj:
        out["is_deep_water"] = bool(proj["isDeepWater"])
    if "withParachute" in proj:
        out["with_parachute"] = bool(proj["withParachute"])
    vis = _safe_float(proj.get("visibilityFactor"))
    if vis is not None and vis > 0:
        out["visibility_factor"] = vis
    sp_armor = _safe_float(proj.get("splashArmorCoeff"))
    if sp_armor is not None:
        out["splash_armor_coeff"] = sp_armor
    alert = _safe_float(proj.get("alertDist"))
    if alert is not None and alert > 0:
        out["alert_distance_m"] = alert * _NATIVE_TO_METRES
    if "affectedByPTZ" in proj:
        out["affected_by_ptz"] = bool(proj["affectedByPTZ"])
    burn = _safe_float(proj.get("burnProb"))
    if burn is not None:
        out["burn_probability"] = burn
    return out


# ---------------------------------------------------------------------------
# Per-projectile visual + effects extras (schema v3.2)
#
# Pulls render-pipeline hints out of GameParams Projectile entities so
# downstream consumers can tint shells per-ammo (HE orange / AP pale-blue),
# drive tracer length/thickness/opacity from the data, and route impact-
# effect particle scripts by hit category. Source XML / DDS paths are kept
# as VFS strings —
# no extraction here; consumers resolve via VFS at use time.
# ---------------------------------------------------------------------------


def _str_or_none(v: Any) -> str | None:
    """Coerce ``v`` to a non-empty string, treating WG's "/" placeholder
    as absent. Returns ``None`` for non-strings, empty strings, or "/".
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s == "/":
        return None
    return s


def _color3(v: Any) -> list[float] | None:
    """Coerce a GameParams ``[r, g, b]`` list to ``list[float]`` of length 3.
    Returns ``None`` if the input isn't a length->=3 list of numbers.
    """
    if not isinstance(v, list) or len(v) < 3:
        return None
    out: list[float] = []
    for x in v[:3]:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        out.append(float(x))
    return out


def shell_visual_extras(ammo_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Resolve a Projectile entity and return the shell-style render
    fields — model sizing, tracer overlay, smoke trail, UV scroll, water
    impact intensity. Empty dict on resolution failure or when none of
    the fields are present (e.g. pure-VFX species like Laser / Wave or
    torpedo-style entities that don't carry shell mesh sizing).

    All artillery / bomb / rocket projectiles share the
    ``CPA001_Shell_Main`` mesh — the per-projectile fields here are how
    WG customises that single mesh per shell type (HE orange tracer +
    bright glow vs AP pale-blue + long thin tracer, etc.). The block
    has two nested sub-dicts: ``tracer`` (the friction/frictionHead
    overlay parameters) and ``smoke`` (the trailing smoke trail).
    Particle XML refs are emitted by :func:`shell_effects_extras`.

    No species filter — fields-only extraction. Pair with
    :func:`torpedo_visual_extras` to cover both render families;
    output blocks have disjoint keys so the caller can merge.
    """
    proj = get_projectile(ammo_id, refresh=refresh)
    if proj is None:
        return {}
    # Skip torpedo-style entities (Torpedo / DepthCharge / Laser / Wave /
    # PlaneTracer) — they don't carry shell mesh sizing, and falling
    # through would otherwise duplicate the torpedo helper's tracer XML
    # refs into a meaningless ``tracer`` sub-dict.
    if "shellModelScale" not in proj and "shellLength" not in proj:
        return {}
    out: dict[str, Any] = {}

    # Shell mesh sizing (the 1 native unit = 15 m model gets shrunk by
    # ``model_scale`` at render time; ``model_length_m`` matches the runtime
    # tracer length). ``tint`` and ``glow`` drive the per-shell colour.
    v = _safe_float(proj.get("shellModelScale"))
    if v is not None:
        out["model_scale"] = v
    v = _safe_float(proj.get("shellLength"))
    if v is not None:
        out["model_length_m"] = v
    tint = _color3(proj.get("shellTint"))
    if tint is not None:
        out["tint"] = tint
    v = _safe_float(proj.get("shellGlow"))
    if v is not None:
        out["glow"] = v

    # Tracer overlay — the friction / frictionHead render sets on
    # CPA001_Shell_Main are sized + textured + tinted per these.
    tracer: dict[str, Any] = {}
    for src, dst in (
        ("tracerLength", "length_m"),
        ("tracerThickness", "thickness_m"),
        ("tracerOpacity", "opacity"),
    ):
        x = _safe_float(proj.get(src))
        if x is not None:
            tracer[dst] = x
    for src, dst in (
        ("tracerTexture", "texture"),
        ("hatTracerTexture", "hat_texture"),
        ("ownTracerTexture", "own_texture"),
        ("ownHatTracerTexture", "own_hat_texture"),
        ("shellDistortTexture", "distort_texture"),
        ("tracerEffect", "effect"),
        ("ownTracerEffect", "own_effect"),
    ):
        s = _str_or_none(proj.get(src))
        if s is not None:
            tracer[dst] = s
    if tracer:
        out["tracer"] = tracer

    # Trailing smoke (HE / AP / SAP variants tint differently; WG
    # ships separate ``Trail_Smoke_HE.dds`` / ``Trail_Smoke_AP.dds`` etc.).
    smoke: dict[str, Any] = {}
    for src, dst in (
        ("smokeOpacity", "opacity"),
        ("smokeThickness", "thickness"),
        ("smokeTileLength", "tile_length"),
        ("smokeAlphaFalloff", "alpha_falloff"),
    ):
        x = _safe_float(proj.get(src))
        if x is not None:
            smoke[dst] = x
    for src, dst in (
        ("smokeTexture", "texture"),
        ("smokeDistortTexture", "distort_texture"),
    ):
        s = _str_or_none(proj.get(src))
        if s is not None:
            smoke[dst] = s
    smoke_tint = _color3(proj.get("smokeTint"))
    if smoke_tint is not None:
        smoke["tint"] = smoke_tint
    if smoke:
        out["smoke"] = smoke

    # UV / distortion (scroll speed for the tracer texture, distortion
    # tiling for the heat-shimmer effect).
    v = _safe_float(proj.get("uvSpeed"))
    if v is not None:
        out["uv_speed"] = v
    v = _safe_float(proj.get("distTile"))
    if v is not None:
        out["dist_tile"] = v
    dp = proj.get("distParams")
    if isinstance(dp, list) and dp:
        coerced = []
        for x in dp:
            f = _safe_float(x)
            if f is None:
                coerced = None
                break
            coerced.append(f)
        if coerced:
            out["dist_params"] = coerced

    # Water impact intensity range (drives the splash particle scale on
    # over-water hits / near misses).
    v = _safe_float(proj.get("waterEffectMinIntensity"))
    if v is not None:
        out["water_effect_min_intensity"] = v
    v = _safe_float(proj.get("waterEffectMaxIntensity"))
    if v is not None:
        out["water_effect_max_intensity"] = v

    return out


def shell_effects_extras(ammo_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Resolve a Projectile entity and return the shell-style impact-FX
    block — particle XML refs keyed by impact category. Empty dict on
    resolution failure or when none of the projHit*Effect{H,V} fields
    are present (torpedoes / depth charges use the simpler torpedo-style
    flat block — see :func:`torpedo_effects_extras`).

    Format per category that has horizontal/vertical splits:
    ``{"h": [paths], "v": [paths]}``. Flat lists/strings for the rest.
    Source XML paths are kept as-is; consumers resolve via VFS or copy
    out at extraction time.
    """
    proj = get_projectile(ammo_id, refresh=refresh)
    if proj is None:
        return {}
    # Same shell-render guard as :func:`shell_visual_extras`. Without it
    # the helper would extract ``blowUpEffect`` / ``shipDestroyEffect``
    # from torpedo / depth charge entries (which carry those flat fields
    # too), duplicating them into both this block and the torpedo-style
    # output.
    if "shellModelScale" not in proj and "shellLength" not in proj:
        return {}
    out: dict[str, Any] = {}

    # Per-impact (horizontal / vertical splits — WG branches on hit-angle
    # type so flat-vertical-armour vs deck-shot use different particle
    # scripts).
    for src_h, src_v, dst in (
        ("projDestroyedEffectHorizontal", "projDestroyedEffectVertical", "destroyed"),
        ("projHitCitadelEffectHorizontal", "projHitCitadelEffectVertical", "citadel"),
        ("projHitNoPenetrationEffectHorizontal", "projHitNoPenetrationEffectVertical", "no_penetration"),
        ("projOverPenetrationEffectHorizontal", "projOverPenetrationEffectVertical", "over_penetration"),
        ("projOverPenetrationExitEffectHorizontal", "projOverPenetrationExitEffectVertical", "over_penetration_exit"),
        ("projRicochetEffectHorizontal", "projRicochetEffectVertical", "ricochet"),
    ):
        h = proj.get(src_h)
        v = proj.get(src_v)
        block: dict[str, list[str]] = {}
        if isinstance(h, list) and h:
            block["h"] = [s for s in (_str_or_none(x) for x in h) if s is not None]
            if not block["h"]:
                block.pop("h")
        if isinstance(v, list) and v:
            block["v"] = [s for s in (_str_or_none(x) for x in v) if s is not None]
            if not block["v"]:
                block.pop("v")
        if block:
            out[dst] = block

    # Flat fields (no horizontal/vertical split).
    bl = proj.get("blowUpEffect")
    if isinstance(bl, list) and bl:
        coerced = [s for s in (_str_or_none(x) for x in bl) if s is not None]
        if coerced:
            out["blow_up"] = coerced
    elif isinstance(bl, str):
        s = _str_or_none(bl)
        if s is not None:
            out["blow_up"] = s

    sd = _str_or_none(proj.get("shipDestroyEffect"))
    if sd is not None:
        out["ship_destroy"] = sd

    return out


def torpedo_visual_extras(ammo_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Resolve a Projectile entity and return torpedo-style render fields
    — tracer-effect XML refs (above-water / underwater / falling) plus
    air-drop parachute model + timing. Empty dict on resolution failure
    or when none of the fields are present.

    Smaller surface than :func:`shell_visual_extras` since torpedoes
    have their own per-asset GLB so model sizing/tint live in the
    geometry; the runtime hints are mainly the dropping animation +
    underwater wake.

    Pairs with :func:`shell_visual_extras`: airdrop fields fire on bombs
    + torpedoes (which both fall from aircraft); tracer XML refs fire on
    torpedoes + depth charges. Disjoint keys with shell_visual_extras so
    callers can merge both outputs into one ``visual`` block.

    Note: ``with_parachute``, ``visibility_factor``, ``alert_distance_m``
    etc. are emitted by :func:`torpedo_profile_extras` (physics, not
    visual) — don't duplicate them here.
    """
    proj = get_projectile(ammo_id, refresh=refresh)
    if proj is None:
        return {}
    out: dict[str, Any] = {}

    for src, dst in (
        ("tracerEffect", "tracer_effect"),
        ("ownTracerEffect", "own_tracer_effect"),
        ("underwaterTracerEffect", "underwater_tracer_effect"),
        ("fallingTracerEffect", "falling_tracer_effect"),
    ):
        s = _str_or_none(proj.get(src))
        if s is not None:
            out[dst] = s

    s = _str_or_none(proj.get("parachuteModel"))
    if s is not None:
        out["parachute_model"] = s
    v = _safe_float(proj.get("parachuteHeightCoeff"))
    if v is not None:
        out["parachute_height_coeff"] = v
    v = _safe_float(proj.get("parachuteTimeCoeff"))
    if v is not None:
        out["parachute_time_coeff"] = v
    v = _safe_float(proj.get("fallDistance"))
    if v is not None:
        out["fall_distance_m"] = v

    return out


def torpedo_effects_extras(ammo_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """Resolve a Projectile entity and return the torpedo-style impact-FX
    block — flat detonation / drop-to-ground particle XML refs. Empty
    dict on resolution failure or when none of the fields are present.

    Pairs with :func:`shell_effects_extras`: shell-style entries split
    impact effects by horizontal/vertical hit-angle (six split categories
    + flat blow_up); torpedo-style entries only emit flat fields
    (blow_up / destroyed / ship_destroy / drop_to_ground /
    torpedo_destroyed). Disjoint keys, safe to merge.
    """
    proj = get_projectile(ammo_id, refresh=refresh)
    if proj is None:
        return {}
    # Skip shell-style entities — their impact effects are already emitted
    # by :func:`shell_effects_extras` in the authoritative H/V-split form.
    # WG ships duplicate flat ``projDestroyedEffect`` alongside the split
    # for shells; running both helpers without this guard would clone the
    # destroyed/blow_up/ship_destroy fields into the output.
    if (
        "projDestroyedEffectHorizontal" in proj
        or "projDestroyedEffectVertical" in proj
    ):
        return {}
    out: dict[str, Any] = {}

    bl = proj.get("blowUpEffect")
    if isinstance(bl, str):
        s = _str_or_none(bl)
        if s is not None:
            out["blow_up"] = s
    elif isinstance(bl, list) and bl:
        coerced = [s for s in (_str_or_none(x) for x in bl) if s is not None]
        if coerced:
            out["blow_up"] = coerced

    pde = proj.get("projDestroyedEffect")
    if isinstance(pde, list) and pde:
        coerced = [s for s in (_str_or_none(x) for x in pde) if s is not None]
        if coerced:
            out["destroyed"] = coerced
    elif isinstance(pde, str):
        s = _str_or_none(pde)
        if s is not None:
            out["destroyed"] = s

    tpde = proj.get("torpedoProjDestroyedEffect")
    if isinstance(tpde, list) and tpde:
        coerced = [s for s in (_str_or_none(x) for x in tpde) if s is not None]
        if coerced:
            out["torpedo_destroyed"] = coerced

    s = _str_or_none(proj.get("dropToTheGroundEffect"))
    if s is not None:
        out["drop_to_ground"] = s

    s = _str_or_none(proj.get("shipDestroyEffect"))
    if s is not None:
        out["ship_destroy"] = s

    return out


# ---------------------------------------------------------------------------
# Hitbox classification
# ---------------------------------------------------------------------------

# A_Hull section keys the survey verified as carrying ``splashBoxes`` lists.
# Order matters only for stable iteration over the result; classification
# itself is keyed by box name.
#
# WG ships ammo blocks under either `Ammo_<N>` (underscored — Iowa, Montana,
# PASC108_Baltimore) or `Ammo<N>` (concatenated — Atago, PASC017_Baltimore).
# Both spellings are listed so the classifier picks up cubes regardless of
# which convention the per-ship hull authoring used. SS variants `SSC` (Cas
# variant on Baltimore) likewise listed for completeness.
_HULL_HITLOC_KEYS: tuple[str, ...] = (
    "Bow", "Cit", "SS", "SSC", "St", "Cas", "SG", "Hull",
    "Ammo_1", "Ammo_2",
    "Ammo1", "Ammo2",
    # CV-specific (Essex/Shinano per the existing scoping doc)
    "FlightDeck", "Hangar", "AuxRoom",
    # SS-specific (U-2501)
    "OvCit", "Sonar",
)

# HitLocation* sub-blocks per HP_ mount — survey: ``HitLocationArtillery``,
# ``HitLocationTorpedo``, ``HitLocationEngine``, ``HitLocationSuo``. CV / SS /
# DC mounts use parallel names.
_HP_HITLOC_KEYS: tuple[str, ...] = (
    "HitLocationArtillery",
    "HitLocationSecondary",
    "HitLocationAA",
    "HitLocationTorpedo",
    "HitLocationDC",
    "HitLocationDirector",
    "HitLocationFinder",
    "HitLocationRadar",
    "HitLocationCatapult",
    "HitLocationEngine",
    "HitLocationSuo",
    "HitLocation",
)


def classify_splash_boxes(
    ship: dict[str, Any],
    components: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk ``A_Hull`` + every active gun group's HP_ mounts to classify
    each ``CM_SB_*`` cube name into ``{section, hl_type, parent_hl
    [, owner_hp]}``.

    Returns ``{boxes: dict, hit_locations: dict}`` ready to merge into
    ``sidecar.hitbox``. Ships without splash data return empty dicts.
    """
    if components is None:
        components = resolve_components(ship)

    hull = components.get("hull") if components else None
    if not isinstance(hull, dict):
        # Fall back to A_Hull/B_Hull on the ship dict directly.
        hull = ship.get("B_Hull") or ship.get("A_Hull") or {}
    if not isinstance(hull, dict):
        hull = {}

    boxes: dict[str, dict[str, Any]] = {}
    hit_locations: dict[str, dict[str, Any]] = {}

    # --- per-section hit-locations (Bow / Cit / SS / SG / etc.)
    for sec_key in _HULL_HITLOC_KEYS:
        sec = hull.get(sec_key)
        if not isinstance(sec, dict):
            continue
        if "splashBoxes" not in sec and "hlType" not in sec:
            continue
        meta = _hit_location_meta(sec)
        if meta:
            hit_locations[sec_key] = meta
        for box_name in sec.get("splashBoxes") or []:
            if not isinstance(box_name, str) or not box_name:
                continue
            entry = {"section": sec_key}
            if isinstance(sec.get("hlType"), str):
                entry["hl_type"] = sec["hlType"]
            if isinstance(sec.get("parentHL"), str) and sec["parentHL"]:
                entry["parent_hl"] = sec["parentHL"]
            boxes[box_name] = entry

    # --- per-mount hit-locations (turret barbettes etc.)
    for group_key in (
        "artillery", "atba", "airDefense", "torpedoes",
        "directors", "finders", "radars", "airArmament",
    ):
        group = components.get(group_key) if components else None
        if not isinstance(group, dict):
            continue
        for hp_name, mount in group.items():
            if not isinstance(hp_name, str) or not hp_name.startswith("HP_"):
                continue
            if not isinstance(mount, dict):
                continue
            for hl_key in _HP_HITLOC_KEYS:
                hl = mount.get(hl_key)
                if not isinstance(hl, dict):
                    continue
                for box_name in hl.get("splashBoxes") or []:
                    if not isinstance(box_name, str) or not box_name:
                        continue
                    entry: dict[str, Any] = {"owner_hp": hp_name}
                    if isinstance(hl.get("hlType"), str):
                        entry["hl_type"] = hl["hlType"]
                    if isinstance(hl.get("parentHL"), str) and hl["parentHL"]:
                        entry["parent_hl"] = hl["parentHL"]
                    # `section` for per-mount cubes is conventionally the
                    # owning HitLocation* family (artillery/secondary/...).
                    # Use the toolkit's lowercase convention to match the
                    # placement `species` field.
                    entry["section"] = group_key
                    boxes[box_name] = entry

    # --- module-level hit-locations carried directly on the group dict
    # (engine + fireControl don't have HP_-namespaced sub-mounts; their
    # ``HitLocationEngine`` / ``HitLocationSuo`` sit at the group root).
    for group_key in ("engine", "fireControl"):
        group = components.get(group_key) if components else None
        if not isinstance(group, dict):
            continue
        for hl_key in _HP_HITLOC_KEYS:
            hl = group.get(hl_key)
            if not isinstance(hl, dict):
                continue
            meta = _hit_location_meta(hl)
            if meta:
                # Use the lowercase module key as the section identifier
                # — matches the per-mount convention above.
                hit_locations[group_key] = meta
            for box_name in hl.get("splashBoxes") or []:
                if not isinstance(box_name, str) or not box_name:
                    continue
                entry = {"section": group_key}
                if isinstance(hl.get("hlType"), str):
                    entry["hl_type"] = hl["hlType"]
                if isinstance(hl.get("parentHL"), str) and hl["parentHL"]:
                    entry["parent_hl"] = hl["parentHL"]
                boxes[box_name] = entry

    # Surface unrecognised hull-level sections that carry splashBoxes —
    # future ship classes (Wave-prefix subs, missile cruisers, ...) may
    # introduce section names we haven't encoded in _HULL_HITLOC_KEYS,
    # and the silent-skip behaviour drops their boxes from the sidecar.
    known = set(_HULL_HITLOC_KEYS)
    unknown_sections = []
    for k, v in hull.items():
        if k in known:
            continue
        if isinstance(v, dict) and "splashBoxes" in v:
            unknown_sections.append(k)
    if unknown_sections:
        print(
            f"  warn: classify_splash_boxes: unrecognised hull section(s) "
            f"with splashBoxes (boxes dropped — extend "
            f"_HULL_HITLOC_KEYS): {', '.join(sorted(unknown_sections))}",
            file=sys.stderr,
        )

    return {"boxes": boxes, "hit_locations": hit_locations}


def _hit_location_meta(sec: dict[str, Any]) -> dict[str, Any]:
    """Extract per-section damage-state numbers from a HitLocation* block."""
    out: dict[str, Any] = {}
    if isinstance(sec.get("hlType"), str) and sec["hlType"]:
        out["hl_type"] = sec["hlType"]
    if isinstance(sec.get("parentHL"), str) and sec["parentHL"]:
        out["parent_hl"] = sec["parentHL"]
    max_hp = _safe_float(sec.get("maxHP"))
    if max_hp is not None:
        out["max_hp"] = max_hp
    regen = _safe_float(sec.get("regeneratedHPPart"))
    if regen is not None:
        out["regen_part"] = regen
    enhanced = _safe_float(sec.get("enhancedRegeneratedHPPart"))
    if enhanced is not None and enhanced > 0:
        out["enhanced_regen_part"] = enhanced
    repair = _safe_float(sec.get("autoRepairTime"))
    if repair is not None:
        out["auto_repair_s"] = repair
    broken = _safe_float(sec.get("brokenRepairTime"))
    if broken is not None:
        out["broken_repair_s"] = broken
    return out


# ---------------------------------------------------------------------------
# Armor: per-mount + barbettes + cross-validation
# ---------------------------------------------------------------------------


def collect_mount_armor(
    components: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """Walk every gun group and return ``{hp_name: {material_id: thickness_mm}}``.

    Only non-empty armor dicts are emitted. Material IDs are stringified
    to match the existing ``materials_table`` convention.
    """
    out: dict[str, dict[str, float]] = {}
    for group_key in ("artillery", "atba", "airDefense", "torpedoes"):
        group = components.get(group_key) if components else None
        if not isinstance(group, dict):
            continue
        for hp_name, mount in group.items():
            if not isinstance(hp_name, str) or not hp_name.startswith("HP_"):
                continue
            if not isinstance(mount, dict):
                continue
            armor = mount.get("armor")
            if not isinstance(armor, dict) or not armor:
                continue
            normalized: dict[str, float] = {}
            for mid, thickness in armor.items():
                t = _safe_float(thickness)
                if t is None:
                    continue
                normalized[str(mid)] = t
            if normalized:
                out[hp_name] = normalized
    return out


def collect_barbettes(hull: dict[str, Any]) -> dict[str, list[str]]:
    """Return ``A_Hull.barbettes`` with stringified material IDs.

    Iowa shape: ``{HP_AGM_1: [65670, 131206, 65710, 65750]}``. Returned
    dict has the same shape with strings: ``{HP_AGM_1: ["65670", ...]}``.
    """
    if not isinstance(hull, dict):
        return {}
    barbettes = hull.get("barbettes")
    if not isinstance(barbettes, dict):
        return {}
    out: dict[str, list[str]] = {}
    for hp_name, mids in barbettes.items():
        if not isinstance(hp_name, str) or not isinstance(mids, list):
            continue
        out[hp_name] = [str(m) for m in mids]
    return out


def cross_validate_armor(
    toolkit_materials_table: dict[str, Any],
    gp_hull_armor: dict[str, Any],
    *,
    tolerance_mm: float = 0.5,
) -> list[str]:
    """Compare toolkit-emitted ``materials_table`` against GameParams
    ``A_Hull.armor`` and return a list of warning strings for any
    overlapping material IDs whose thickness differs.

    Empty list = clean. Useful as a stale-armor-json detector after a
    game patch.
    """
    warnings: list[str] = []
    if not isinstance(toolkit_materials_table, dict):
        return warnings
    if not isinstance(gp_hull_armor, dict):
        return warnings
    for mid_str, gp_thickness in gp_hull_armor.items():
        gp_t = _safe_float(gp_thickness)
        if gp_t is None:
            continue
        entry = toolkit_materials_table.get(str(mid_str))
        if not isinstance(entry, dict):
            continue
        toolkit_t = _safe_float(entry.get("thickness_mm"))
        if toolkit_t is None:
            continue
        if abs(toolkit_t - gp_t) > tolerance_mm:
            warnings.append(
                f"material_id={mid_str}: toolkit={toolkit_t:g}mm, "
                f"gameparams={gp_t:g}mm (Δ={toolkit_t - gp_t:+g}mm)"
            )
    return warnings


# ---------------------------------------------------------------------------
# Class derivation from primary armament caliber
# ---------------------------------------------------------------------------

# Cruiser caliber -> 2-letter class code. WG's GameParams Vehicle ``species``
# only carries five values (``Cruiser`` for everything CL/CA/CB), so the
# only deterministic discriminator is the primary battery. Boundaries
# follow the standard naval convention: <=155 mm = light cruiser (London
# Treaty), 155-250 mm = heavy cruiser, >250 mm = "large cruiser" / battle-
# cruiser substitute (Alaska 305 mm, Stalingrad 305 mm, Kronshtadt 305 mm).
# Manual override stays the escape valve for edge cases (Genova, hybrid
# CL/CA refits, paper ships).
_CRUISER_CL_MAX_CALIBER_MM = 155.0
_CRUISER_CA_MAX_CALIBER_MM = 250.0


def _primary_caliber_mm(components: dict[str, Any] | None) -> float | None:
    """Return the primary HP_AGM_* mount's barrel diameter in mm, or ``None``.

    Reads ``components.artillery``'s first ``HP_AGM_*`` entry. Returns
    ``None`` when the ship has no main battery (rare; happens on
    submarines + missile cruisers).
    """
    if not isinstance(components, dict):
        return None
    art = components.get("artillery")
    if not isinstance(art, dict):
        return None
    main_hp = next(
        (k for k in art if isinstance(k, str) and k.startswith("HP_AGM_")),
        None,
    )
    if main_hp is None:
        return None
    mount = art.get(main_hp)
    if not isinstance(mount, dict):
        return None
    bd = _safe_float(mount.get("barrelDiameter"))
    if bd is None or bd <= 0:
        return None
    return bd * 1000.0


def class_from_caliber(
    species: str | None,
    components: dict[str, Any] | None = None,
) -> str | None:
    """Derive a 2-letter ship-class code from species + primary caliber.

    Returns one of ``DD`` / ``CL`` / ``CA`` / ``CB`` / ``BB`` / ``CV`` /
    ``SS``, or ``None`` when the species is unrecognised. The ``CL`` /
    ``CA`` / ``CB`` distinction is the only one that needs ``components``;
    the other species map deterministically.

    Cruiser thresholds (primary battery caliber):

    * caliber <= 155 mm -> ``CL`` — Cleveland (152), Worcester (152),
      Atlanta (127, anti-air cruiser).
    * 155 < caliber <= 250 mm -> ``CA`` — Baltimore (203), Pensacola (203),
      Des Moines (203), Hipper (203).
    * caliber > 250 mm -> ``CB`` — Alaska (305), Stalingrad (305),
      Kronshtadt (305), Yoshino (310).
    * No artillery / unknown caliber -> ``CA`` (matches the existing
      flat-mapping default).

    GameParams does NOT carry a ``BB`` / ``BC`` distinction (Hood,
    Repulse, Renown, Kongo all show ``species='Battleship'`` with
    ``archetype='Undefined'`` — same shape as Iowa/Yamato), so battle-
    cruisers must be set via ``--class-override``.
    """
    if not species:
        return None
    species_low = species.lower()
    if species_low == "destroyer":
        return "DD"
    if species_low == "battleship":
        return "BB"
    if species_low == "aircarrier":
        return "CV"
    if species_low == "submarine":
        return "SS"
    if species_low != "cruiser":
        # Unknown species — let the caller fall back to its default mapping.
        return None
    caliber = _primary_caliber_mm(components)
    if caliber is None:
        return "CA"
    if caliber <= _CRUISER_CL_MAX_CALIBER_MM:
        return "CL"
    if caliber <= _CRUISER_CA_MAX_CALIBER_MM:
        return "CA"
    return "CB"


def derive_class_from_placements(
    placements: dict[str, Any] | str | Path,
    *,
    refresh: bool = False,
) -> str | None:
    """Convenience wrapper: load the GameParams Ship for the placements
    JSON's ``ship.param_index`` and derive the 2-letter class via
    :func:`class_from_caliber`.

    Returns ``None`` when the param_index doesn't resolve (stale cache,
    test-server ship not in the live dump) so callers can fall back to
    the species-only mapping. Best-effort — exceptions during the
    GameParams load propagate so the caller can decide whether to log
    or swallow.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, encoding="utf-8") as f:
            placements = json.load(f)
    if not isinstance(placements, dict):
        return None
    ship_section = placements.get("ship") or {}
    species = (ship_section.get("species") or "").strip() or None
    param_index = (ship_section.get("param_index") or "").strip()
    if not species:
        return None
    if not param_index:
        return class_from_caliber(species, None)
    full_id = resolve_ship_id(param_index)
    if not full_id:
        return class_from_caliber(species, None)
    ship = get_ship(full_id, refresh=refresh)
    if ship is None:
        return class_from_caliber(species, None)
    components = resolve_components(ship)
    return class_from_caliber(species, components)


__all__ = [
    "resolve_variant_model_dir",
    "find_vehicle_by_native_permoflage",
    "resolve_variant_accessory_swaps",
    "resolve_components",
    "variants_summary",
    "ship_metadata_extras",
    "autofill_for_hp",
    "torpedo_profile_extras",
    "shell_visual_extras",
    "shell_effects_extras",
    "torpedo_visual_extras",
    "torpedo_effects_extras",
    "classify_splash_boxes",
    "collect_mount_armor",
    "collect_barbettes",
    "cross_validate_armor",
    "class_from_caliber",
    "derive_class_from_placements",
]
