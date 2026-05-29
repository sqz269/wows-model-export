"""Unify ship + mesh-swap permoflage variants into one canonical ship.

Step 0 of the *Projected Exteriors on a Canonical Base Ship* design
(see ``docs/SHIP_EXTERIOR_UNIFICATION_HANDOFF.md``). Instead of materialising
each mesh-swap permoflage as a separate ``<Base>__<Variant>`` ship folder, the
base ship's sidecar gains an indexable ``exteriors[]`` array (sibling to
``skins[]``, distinct from ``variants``). Each entry is one WG ``Exterior``
(permoflage) carrying the resolved per-mount mesh-swap delta + a cross-link into
``skins[]`` for its paint scheme — exactly how WG composes
``Vehicle -> permoflages[] of Exteriors``.

This module is **purely additive** and changes no existing behaviour:

* :func:`build_exterior_record` / :func:`project_exterior` / :func:`default_exterior_record`
  / :func:`build_exteriors_block` are PURE (dict-in, dict-out — no GameParams, GLB,
  or filesystem I/O), and are the data-model core validated by
  ``tests/test_exterior_unify.py`` against the real Baltimore base↔Azur delta.
* :func:`collect_exteriors_for_vehicle` is the GameParams-dependent integration
  glue. It reuses the existing resolvers verbatim and is wrapped so it can NEVER
  break the existing emit (worst case: returns ``[default]``). It is the seam the
  scaffold path calls; an end-to-end pipeline run is required to validate it.

Schema stays at version 3 — ``exteriors[]`` is an optional additive key that
strict consumers (Unity ``schema_version == 3`` enforcement; the producer's own
``resolve/sidecar/_io.py`` validator) ignore. Bump to 4 only at cutover.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .sidecar._constants import PLACEMENT_SECTIONS

Placement = dict[str, Any]
PlacementSections = Mapping[str, Sequence[Placement]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _sections_of(source: Mapping[str, Any]) -> dict[str, list[Placement]]:
    """Extract the placement sections from a doc (or a bare sections map)."""
    return {sec: list(source.get(sec) or []) for sec in PLACEMENT_SECTIONS}


def _index_by_hp(placements: Iterable[Placement]) -> dict[str, Placement]:
    """Index placements by ``hp_name`` (the WG swap join key). Last wins on the
    rare duplicate — turret/secondary HPs are unique within a section."""
    out: dict[str, Placement] = {}
    for p in placements:
        hp = p.get("hp_name")
        if hp:
            out[hp] = p
    return out


def _matrix(p: Placement) -> Any:
    return (p.get("transform") or {}).get("matrix")


def _mount_differs(base_p: Placement, var_p: Placement) -> bool:
    """A mount is swapped iff any consumer-visible field differs. ``misc_filter``
    uses 3-state semantics (None=all / []=none / [list]) so ``None`` and ``[]``
    are intentionally distinct."""
    return (
        base_p.get("asset_id") != var_p.get("asset_id")
        or base_p.get("dead_asset_id") != var_p.get("dead_asset_id")
        or _matrix(base_p) != _matrix(var_p)
        or base_p.get("misc_filter") != var_p.get("misc_filter")
    )


# ---------------------------------------------------------------------------
# Data-model core (PURE, validated by tests/test_exterior_unify.py)
# ---------------------------------------------------------------------------

def build_exterior_record(
    exterior_id: str,
    base: PlacementSections,
    variant: PlacementSections,
    *,
    display_name: str | None = None,
    wg_asset_id: str | None = None,
    species: str | None = None,
    peculiarity: str | None = None,
    is_native: bool = False,
    camo_scheme_key: str | None = None,
    hull: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one ``exteriors[]`` record by diffing the base ship's placements
    against the already-swapped *variant* placements (the output of the existing
    ``apply_variant_asset_swaps`` pass, or the legacy variant folder's sidecar).

    The diff captures each swapped mount's VARIANT values **verbatim** —
    ``asset_id`` / ``dead_asset_id`` / the schema_v6 Ry180-baked ``transform`` /
    the ``misc_filter`` override — because none of those is reconstructable from
    the base mount (verified on Baltimore: the base/variant turret matrices differ
    by the Ry180 conjugation, and ``misc_filter`` goes ``[3]`` -> ``[]``).

    Unmatched HPs (present in only one side) are NOT mount swaps — they are
    hull-delta adds/removes and are out of scope here (see ``HullDelta`` in the
    handoff). Only matched-HP differences become ``mounts[]``.
    """
    base_secs = _sections_of(base)
    var_secs = _sections_of(variant)

    mounts: list[dict[str, Any]] = []
    for sec in PLACEMENT_SECTIONS:
        base_by_hp = _index_by_hp(base_secs[sec])
        for var_p in var_secs[sec]:
            hp = var_p.get("hp_name")
            base_p = base_by_hp.get(hp) if hp else None
            if base_p is None or not _mount_differs(base_p, var_p):
                continue
            mounts.append({
                "hp_name": hp,
                "base_asset_id": base_p.get("asset_id"),
                "asset_id": var_p.get("asset_id"),
                "dead_asset_id": var_p.get("dead_asset_id"),
                "transform": copy.deepcopy(var_p.get("transform")),
                "misc_filter": copy.deepcopy(var_p.get("misc_filter")),
                "attached_y_flip": var_p.get("attached_y_flip", False),
            })

    swap_table = _swap_table_from_mounts(mounts)
    swapped_ids = sorted({
        aid
        for m in mounts
        for aid in (m.get("asset_id"), m.get("dead_asset_id"))
        if aid
    })

    return {
        "exterior_id": exterior_id,
        "display_name": display_name,
        "wg_asset_id": wg_asset_id,
        "species": species,
        "peculiarity": peculiarity,
        "is_native": bool(is_native),
        "camo_scheme_key": camo_scheme_key,
        "hull": hull,
        "swap_table": swap_table,
        "mounts": mounts,
        "variant_swapped_asset_ids": swapped_ids,
    }


def _swap_table_from_mounts(mounts: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """Derive the four-way swap table (the ``resolve_variant_accessory_swaps``
    shape) from the resolved ``mounts[]`` — kept on the record for consumers /
    opt-out gates that key off it."""
    by_asset_id: dict[str, str] = {}
    by_hp_name: dict[str, str] = {}
    dead_by_hp_name: dict[str, str] = {}
    misc_filter_by_hp: dict[str, list] = {}
    for m in mounts:
        hp = m.get("hp_name")
        if hp and m.get("asset_id"):
            by_hp_name[hp] = m["asset_id"]
        if m.get("base_asset_id") and m.get("asset_id"):
            by_asset_id[m["base_asset_id"]] = m["asset_id"]
        if hp and m.get("dead_asset_id"):
            dead_by_hp_name[hp] = m["dead_asset_id"]
        if hp and m.get("misc_filter") is not None:
            misc_filter_by_hp[hp] = list(m["misc_filter"])
    return {
        "by_asset_id": by_asset_id,
        "by_hp_name": by_hp_name,
        "dead_by_hp_name": dead_by_hp_name,
        "misc_filter_by_hp": misc_filter_by_hp,
    }


def project_exterior(
    base: PlacementSections, record: Mapping[str, Any],
) -> dict[str, list[Placement]]:
    """Apply an ``exteriors[]`` record back onto the base placements — the
    inverse of :func:`build_exterior_record`, and the regression gate's core:
    ``project_exterior(base, build_exterior_record(base, variant)) == variant``
    for every swapped HP (non-swapped placements are returned unchanged).

    Mounts are HP-name keyed; HP-name precedence over asset-id matches the
    existing ``apply_variant_asset_swaps`` convention. Returns a deep copy — the
    base is never mutated.
    """
    out = {sec: copy.deepcopy(list(base.get(sec) or [])) for sec in PLACEMENT_SECTIONS}
    mounts_by_hp = {m["hp_name"]: m for m in record.get("mounts", []) if m.get("hp_name")}
    for sec in PLACEMENT_SECTIONS:
        for p in out[sec]:
            m = mounts_by_hp.get(p.get("hp_name"))
            if m is None:
                continue
            p["asset_id"] = m.get("asset_id")
            p["dead_asset_id"] = m.get("dead_asset_id")
            if m.get("transform") is not None:
                p["transform"] = copy.deepcopy(m["transform"])
            p["misc_filter"] = copy.deepcopy(m.get("misc_filter"))
    return out


def default_exterior_record() -> dict[str, Any]:
    """Synthesised index-0 ``default`` exterior (the vanilla composition), mirroring
    how ``skins[]`` always carries a synthesised ``default``."""
    return {
        "exterior_id": "default",
        "display_name": "Standard",
        "wg_asset_id": None,
        "species": "default",
        "peculiarity": "default",
        "is_native": True,
        "camo_scheme_key": "main",
        "hull": None,
        "swap_table": {
            "by_asset_id": {}, "by_hp_name": {},
            "dead_by_hp_name": {}, "misc_filter_by_hp": {},
        },
        "mounts": [],
        "variant_swapped_asset_ids": [],
    }


def build_exteriors_block(
    base: PlacementSections,
    variant_records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble the full ``exteriors[]`` list: a synthesised ``default`` (index 0)
    followed by one :func:`build_exterior_record` per mesh-swap permoflage.

    PURE. ``variant_records`` is a sequence of dicts
    ``{exterior_id, variant_placements, display_name?, wg_asset_id?, species?,
    peculiarity?, is_native?, camo_scheme_key?, hull?}`` — the GameParams + GLB
    resolution that produces ``variant_placements`` is done by the existing
    pipeline functions and injected here (see :func:`collect_exteriors_for_vehicle`).
    Records whose diff yields no ``mounts`` and no ``hull`` are dropped (a
    texture-only camo is a ``skins[]`` entry, not an exterior).
    """
    out = [default_exterior_record()]
    for vr in variant_records:
        rec = build_exterior_record(
            vr["exterior_id"],
            base,
            vr.get("variant_placements") or {},
            display_name=vr.get("display_name"),
            wg_asset_id=vr.get("wg_asset_id"),
            species=vr.get("species"),
            peculiarity=vr.get("peculiarity"),
            is_native=vr.get("is_native", False),
            camo_scheme_key=vr.get("camo_scheme_key"),
            hull=vr.get("hull"),
        )
        if rec["mounts"] or rec["hull"]:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# GameParams integration glue (GUARDED — reuses existing resolvers).
# NOT validated by the pure tests; needs an end-to-end pipeline run. Wired so it
# can never break the existing emit (worst case: returns just the default).
# ---------------------------------------------------------------------------

def collect_exteriors_for_vehicle(
    vehicle_id: str,
    base: PlacementSections,
    *,
    get_ship: Callable[[str], Mapping[str, Any] | None],
    resolve_swaps: Callable[..., Mapping[str, Any]],
    apply_swaps: Callable[..., dict[str, list[Placement]]],
    get_exterior: Callable[[str], Mapping[str, Any] | None] | None = None,
    camo_scheme_for: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Enumerate a Vehicle's ``permoflages[]`` and build the ``exteriors[]`` block,
    reusing the existing resolvers (dependency-injected so this stays testable and
    has no hard import cycle):

    * ``get_ship(vehicle_id)`` -> the Vehicle GameParams dict (``permoflages`` /
      ``nativePermoflage``) — e.g. ``gameparams_autofill.get_ship``.
    * ``resolve_swaps(vehicle_id, permoflage_id=...)`` ->
      ``gameparams_autofill.resolve_variant_accessory_swaps``.
    * ``apply_swaps(base_placements, swap_table, ...)`` -> the existing
      ``resolve.sidecar._absorb.apply_variant_asset_swaps`` (produces the
      Ry180-baked variant placements). This is the step that needs the variant
      mount GLB on disk and a full pipeline run to validate.
    * ``get_exterior(id)`` -> Exterior dict (for ``species`` / ``peculiarity`` /
      ``wg_asset_id`` / ``camouflage``).
    * ``camo_scheme_for(exterior_id)`` -> the matching ``skins[].scheme_key``.

    Any exception is swallowed and logged-as-skip so the additive emit can never
    regress the existing pipeline.
    """
    try:
        ship = get_ship(vehicle_id) or {}
        permos: list[str] = list(ship.get("permoflages") or [])
        native = ship.get("nativePermoflage")
        variant_records: list[dict[str, Any]] = []
        for ext_id in permos:
            swaps = resolve_swaps(vehicle_id, permoflage_id=ext_id) or {}
            if not _has_mesh_swap(swaps):
                continue  # texture-only camo -> skins[], not an exterior row
            variant_placements = apply_swaps(base, swaps)
            ext = (get_exterior(ext_id) if get_exterior else None) or {}
            variant_records.append({
                "exterior_id": ext_id,
                "variant_placements": variant_placements,
                "display_name": ext.get("title") or ext.get("name"),
                "wg_asset_id": ext.get("id") or ext.get("name"),
                "species": (ext.get("typeinfo") or {}).get("species"),
                "peculiarity": ext.get("peculiarity"),
                "is_native": ext_id == native,
                "camo_scheme_key": camo_scheme_for(ext_id) if camo_scheme_for else None,
                "hull": None,  # HullDelta resolution is a later step
            })
        return build_exteriors_block(base, variant_records)
    except Exception as exc:  # pragma: no cover - defensive, must not break emit
        import logging
        logging.getLogger(__name__).warning(
            "exterior_unify: skipping exteriors[] for %s (%s)", vehicle_id, exc,
        )
        return [default_exterior_record()]


def _has_mesh_swap(swaps: Mapping[str, Any]) -> bool:
    """True iff a resolved swap table carries any mesh swap (vs texture-only)."""
    return any(swaps.get(k) for k in ("by_asset_id", "by_hp_name", "dead_by_hp_name"))
