"""``absorb_*`` mutating passes + variant-swap + attach-to derivation.

Each ``absorb_*`` takes a sidecar document + a domain payload (toolkit
JSON output, GameParams entity, splash classification) and folds the
payload into the document. All preserve hand-authored fields via
:func:`merge_preserving`.

Also lives here:

- :func:`apply_variant_asset_swaps` — Exterior peculiarityModels
  rewrites + Y-flip bone correction.
- :func:`derive_attach_to` — propagate ``attach_to`` from parent HP
  mounts to attached children.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ._constants import (
    PLACEMENT_SECTIONS,
    SidecarSchemaError,
)
from ._helpers import _now_iso
from ._io import _deepcopy_jsonish, merge_preserving
from ._makers import (
    make_armor,
    make_ballistics,
    make_hitbox,
    make_hull_entry,
    make_hull_stats,
    make_torpedo_profile,
    make_variants,
)

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
    library_root: Path,
    base_aid_by_hp: dict[str, str] | None = None,
) -> tuple[dict[str, Any], int, set[str]]:
    """Rewrite ``asset_id`` / ``dead_asset_id`` / ``misc_filter`` on every
    placement using a swap table from
    :func:`tools.shared.gameparams.resolve_variant_accessory_swaps`.

    ``swaps`` is a dict-of-dicts ``{by_asset_id, by_hp_name,
    dead_by_hp_name, misc_filter_by_hp}``. HP-name-keyed swaps take
    precedence over asset-id-keyed ones (more specific). Per-HP dead
    swaps can ADD a ``dead_asset_id`` to a placement that didn't have
    one (Iowa AzurLane: base Iowa has no dead variant; the Azur skin
    adds ``AGM652_..._Azur_dead``). Per-HP ``misc_filter`` overrides
    the vanilla ship's per-HP whitelist with the variant-bundled
    ``MP_*`` IDs from the Exterior's ``nodesConfig`` — empty list
    means "drop every bundled misc on this HP under this variant"
    (common for Azur director HPs).

    For mesh-swap permoflages whose Exterior carries per-hardpoint
    accessory swaps (ARP gunmounts → JGM57x_Arpeggio, AzurLane Iowa
    main turrets → AGM652_..._Azur, Optimus secondaries →
    AGS533_..._Black, etc.), this rewrites every placement section's
    asset references so downstream consumers bind the variant library
    accessories instead of the base ship's gray turrets.

    Bone-mismatch correction: WG sometimes re-authors the variant's
    ``.geometry`` pre-flipped 180° around Y *and* sets the variant's
    ``Rotate_Y_BlendBone`` rest pose to identity instead of Z-mirror.
    The toolkit correctly bakes ``inverse(source_bone)`` into the
    placement at base export, but that correction is wrong for the
    variant once we've swapped the asset_id. When the source and
    target meshes have opposite forward-Z direction (proxy for
    ``Rotate_Y_BlendBone.col2.z`` sign — see
    :mod:`tools.shared.wg_bone_orientation`), this post-multiplies the
    placement matrix by Ry(180°). Confirmed pairs that need this on
    2026-05-09: AGM019 → AGM622 (Baltimore Azur main), JGS158 →
    JGS3094 (Azur Shinano secondary). Pairs that don't need it
    (AGM034 → AGM652 for Iowa Azur main) collapse to a no-op because
    both meshes share Z direction. See
    ``memory/project_variant_swap_bone_mismatch.md`` for the derivation.

    ``base_aid_by_hp`` is a per-HP map of the BASE vehicle's
    ``hp_name → asset_id`` (i.e. the pre-swap aids that the toolkit
    emits into ``<Ship>_placements.json``). It feeds the re-run heal
    path for variants whose swaps are encoded as ``by_hp_name`` only
    (Azur Lane / ARP / Sabaton nodesConfig pattern — empty
    ``by_asset_id``). On a re-scaffold where ``<Ship>_accessories.json``
    already carries the swapped aid (e.g. ``AGM622``) but is missing
    the ``attached_y_flip`` stamp, the existing heal path's reverse
    lookup over ``by_asset_id.items()`` finds nothing, so the
    Ry(180°) correction silently no-ops and the turret keeps
    rendering 180° off. Passing ``base_aid_by_hp`` lets the heal
    path recover the source aid via the placement's ``hp_name``.

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

    by_asset_id: dict[str, str] = swaps.get("by_asset_id") or {}
    by_hp_name: dict[str, str] = swaps.get("by_hp_name") or {}
    dead_by_hp_name: dict[str, str] = swaps.get("dead_by_hp_name") or {}
    misc_filter_by_hp: dict[str, list[str]] = swaps.get("misc_filter_by_hp") or {}

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
        if not isinstance(asset_id, str):
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
        from ..bone_orientation import glb_forward_z_sign
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
            # the swap-target value) but the Ry(180°) correction never
            # landed because the variant GLB wasn't on disk yet
            # (typical on a first ingest where build_accessory_library
            # runs after scaffold), the placement's ``attached_y_flip``
            # flag will be absent. Recover the source aid so the
            # bone-mismatch gate below can re-check the swap pair and
            # apply the correction now. Skipped when the placement is
            # already flagged. This is what lets
            # ``<Ship>_accessories.json`` self-heal on a re-scaffold
            # post-library-build — the webview reads accessories.json
            # directly, so the broken matrix sticks until this heal
            # fires.
            #
            # Two recovery paths feed ``inferred_source_aid``:
            #
            # (1) ``by_asset_id`` reverse-lookup — for variants whose
            #     Exterior populates peculiarityModels (ARP Blue, Black
            #     Friday, Sabaton, etc.). Source = the by_asset_id key
            #     whose value equals the current (already-swapped) aid.
            #
            # (2) ``by_hp_name`` fallback — for variants whose Exterior
            #     encodes per-HP swaps in nodesConfig WITHOUT populating
            #     peculiarityModels (Azur Lane / ARP nodesConfig-only
            #     pattern; their ``by_asset_id`` is empty, so path 1
            #     finds nothing). Source = ``base_aid_by_hp[hp]`` —
            #     the BASE vehicle's aid at this HP, supplied by the
            #     caller from the toolkit's raw placements JSON.
            #
            # Path 2 also handles the by_hp_name FRESH case where
            # ``aid == new_aid`` (placement was swapped on a prior run,
            # the per-HP lookup above re-resolved ``new_aid`` to the
            # same value, so the ``aid != new_aid`` gate further down
            # doesn't fire). Without it the heal would skip and the
            # matrix would never get Ry(180°)-corrected.
            inferred_source_aid: str | None = None
            if (
                isinstance(aid, str)
                and not p.get("attached_y_flip")
                and (new_aid is None or new_aid == aid)
            ):
                for base, variant in by_asset_id.items():
                    if variant == aid and base != aid:
                        inferred_source_aid = base
                        break
                if (
                    inferred_source_aid is None
                    and base_aid_by_hp is not None
                    and isinstance(hp, str)
                    and hp in by_hp_name
                    and by_hp_name[hp] == aid
                ):
                    base_for_hp = base_aid_by_hp.get(hp)
                    if isinstance(base_for_hp, str) and base_for_hp != aid:
                        inferred_source_aid = base_for_hp

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
                flip_source is not None
                and flip_target is not None
                and _needs_y_flip(flip_source, flip_target, scope, category, subcategory)
            ):
                txfm = p2.get("transform") if isinstance(p2.get("transform"), dict) else None
                matrix = txfm.get("matrix") if txfm else None
                if isinstance(matrix, list) and len(matrix) == 16:
                    from ..bone_orientation import post_multiply_ry180
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
        return doc
    return merge_preserving(doc, update)


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


def absorb_gameparams_effects(
    doc: dict[str, Any],
    *,
    attachments: list[dict[str, Any]],
    particles: dict[str, Any],
) -> dict[str, Any]:
    """Stamp per-ship particle effect attachments + their resolved effect
    data into ``doc.effects``.

    ``attachments`` is a list of ``{group, node, particle_path}`` dicts
    derived from the active hull's ``effects`` table in GameParams.
    ``particles`` is a dict keyed by particle path (e.g.
    ``"particles/vehicles/Fire_big_2.xml"``) whose values are the
    parsed Effect-blob records (see
    :class:`wows_model_export.read.particles.ParticleStore`).

    Both inputs are typically produced by the
    :mod:`wows_model_export.compose.particles_library` builder. Empty
    inputs short-circuit — no ``effects`` section is created.

    **Replace-by-section semantics.** Re-running the absorb overwrites
    the previous ``effects`` block. This matches ballistics — the
    particle data is fully toolkit/assets.bin-derived, so hand-edits
    don't belong here.
    """
    if not attachments and not particles:
        return doc
    effects_block: dict[str, Any] = {
        "source": {"generated_at": _now_iso()},
        "attachments": attachments,
        "particles": particles,
    }
    # Strip any existing effects block first so removed entries (e.g.
    # ship that no longer ships fire4) don't linger.
    new_doc = _deepcopy_jsonish(doc)
    if "effects" in new_doc:
        del new_doc["effects"]
    new_doc["effects"] = effects_block
    return new_doc


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
    (downstream component, sim) — not in the sidecar. ``scaffold_ship.py``
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
