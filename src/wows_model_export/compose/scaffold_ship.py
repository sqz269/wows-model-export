"""Compose `scaffold_ship` — one-shot ship working-directory scaffolder.

Lifted from ``tools/ship/scaffold_ship.py`` on the I:-side warships repo.
This is the Layer 4 orchestrator that bundles the RUNBOOK §1 steps into a
single callable:

  1. ``wowsunpack export-ship`` -> ``<Ship>_hull.glb`` +
     ``<Ship>_placements.json`` + ``<Ship>_skel_ext.json`` +
     ``<Ship>_material_mappings.json`` + raw DDS textures.  Accessory
     meshes are excluded; the shared accessory library carries those.
  2. ``wowsunpack armor`` -> ``<Ship>_armor.json``.
  3. ``wowsunpack ammo``  -> ``<Ship>_ballistics.json``.
  4. Build ``<Ship>.meta.json`` sidecar.  Idempotent — existing sidecars
     are merged so hand-authored fields survive a re-run.

Mesh-swap permoflage routing: after the base export, the Vehicle's
``nativePermoflage`` is checked for a full hull mesh swap (ARP Takao,
Azur Lane, Sabaton, ...).  When detected, a second ``export-ship`` pass
runs against the variant model_dir to overwrite the hull GLB +
skel_ext + textures; the placements JSON (HP_ mounts from the base
Vehicle's GameParams) is preserved.  The per-mount accessory swap from
``Exterior.peculiarityModels`` / ``Exterior.nodesConfig`` is applied
right before the sidecar write.

Refactor notes vs the I:-side ``scaffold(ship, *, out_root=, ...)``:

* ``out_root`` -> ``workspace``; defaults to ``config.workspace``.
* ``game_dir`` + ``wowsunpack_path`` -> resolved via :class:`PipelineConfig`.
* Return type: typed :class:`ScaffoldResult` instead of a free-form dict.
* Progress callback: ``on_event=OnEvent`` emits :class:`StepEvent` at
  step boundaries (``resolve_identity``, ``export_hull``,
  ``export_armor``, ``export_ammo``, ``gameparams_autofill``,
  ``materials_skins``, ``geometry_hitbox``, ``emit_sidecar``).
* Errors: each step's exception is wrapped in
  :class:`StepError(step=...)` via ``raise ... from e``.
* Non-fatal failures inside the GameParams / camo / permoflage passes
  are appended to the result's ``warnings`` tuple via the local
  :func:`_warn` helper. ``StepEvent`` notifications are emitted at
  step boundaries (``started`` / ``completed`` / ``failed``).

Native-permoflage auto-ingest routes through
:mod:`wows_model_export.compose.skin_pack` — the skin-pack composer is
called directly when the Vehicle's ``nativePermoflage`` declares a
non-default ``peculiarity`` (Arpeggio / Azur Lane / Sabaton /
Kobayashi).

Emissive-DDS synthesis (ARP / Azur Lane / Sabaton crossover skins)
routes through :mod:`wows_model_export.resolve.synth_emission` — called
inside the ``export_hull`` step on a best-effort basis. Failures
degrade to warnings; the sidecar's stem classifier picks up any
``*_emissive.dd?`` / ``*_emissive.dds`` files that landed.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp_read
from ..read import localization as _localization
from ..resolve import camo as wg_camo
from ..resolve import gameparams_autofill as _gp_autofill
from ..resolve import sidecar
from ..resolve import synth_emission as _synth_emission
from ..toolkit import ammo_json as _toolkit_ammo_json
from ..toolkit import armor_json as _toolkit_armor_json
from ..toolkit import export_ship as _toolkit_export_ship
from ..toolkit import ingest_ship_bundle as _toolkit_ingest_ship_bundle
from ..toolkit import ingest_ship_supported as _toolkit_ingest_ship_supported
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from ..types import OnEvent, ScaffoldResult
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# Module-local caches + constants (lifted verbatim)
# ---------------------------------------------------------------------------

# Cached camouflages.xml DB shared across multiple scaffold calls in a
# single Python session (e.g. when ingest_ship runs scaffold + sidecar
# refresh sequentially). XML parse is ~1s and the lookup is read-only.
_CAMO_DB_CACHE: wg_camo.CamouflageDb | None = None

# Cached accessory-library index.json per library root, keyed on the
# resolved absolute path string. The index lists every built asset and
# (when applicable) the relative path to that asset's
# ``<asset>.attached_accessories.json``. Loaded once per scaffold to
# back the variant-swap bespoke-children extension below.
_ACCESSORY_INDEX_BY_ROOT: dict[str, dict[str, Any]] = {}

# Cached (library_root → asset_id → set of attached-child asset_ids)
# pulled from ``<asset>.attached_accessories.json``. Populated lazily
# by :func:`_attached_child_ids_for_asset`.
_ATTACHED_CHILDREN_BY_ASSET: dict[tuple[str, str], frozenset[str]] = {}


def _accessory_index_for_root(library_root: Path) -> dict[str, Any]:
    """Return the parsed ``index.json`` for an accessory library root,
    cached per resolved root path. Empty dict on missing or malformed.
    """
    key = str(library_root.resolve())
    cached = _ACCESSORY_INDEX_BY_ROOT.get(key)
    if cached is not None:
        return cached
    path = library_root / "index.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    _ACCESSORY_INDEX_BY_ROOT[key] = raw
    return raw


def _attached_child_ids_for_asset(
    library_root: Path,
    asset_id: str,
) -> frozenset[str]:
    """Return the set of child ``asset_id``s referenced by an asset's
    ``<asset>.attached_accessories.json`` (live + dead union).

    Empty set when the library index has no entry, no attached file is
    listed, the file is missing on disk, or the JSON is malformed. The
    asset itself is excluded — only its children are returned.
    """
    root_key = str(library_root.resolve())
    cache_key = (root_key, asset_id)
    cached = _ATTACHED_CHILDREN_BY_ASSET.get(cache_key)
    if cached is not None:
        return cached

    index = _accessory_index_for_root(library_root)
    assets = index.get("assets") if isinstance(index, dict) else None
    entry = assets.get(asset_id) if isinstance(assets, dict) else None
    rel = entry.get("attached_accessories") if isinstance(entry, dict) else None
    if not isinstance(rel, str) or not rel:
        _ATTACHED_CHILDREN_BY_ASSET[cache_key] = frozenset()
        return frozenset()

    doc_path = library_root / rel
    try:
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ATTACHED_CHILDREN_BY_ASSET[cache_key] = frozenset()
        return frozenset()

    out: set[str] = set()
    for section in ("attachments_live", "attachments_dead"):
        items = doc.get(section) if isinstance(doc, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            aid = item.get("asset_id")
            if isinstance(aid, str) and aid and aid != asset_id:
                out.add(aid)
    frozen = frozenset(out)
    _ATTACHED_CHILDREN_BY_ASSET[cache_key] = frozen
    return frozen


def _bespoke_attached_children_for_swap(
    library_root: Path,
    base_asset_id: str,
    variant_asset_id: str,
) -> set[str]:
    """Return the bespoke variant-only attached children for a swap pair.

    ``variant.attachments - base.attachments`` over the union of live +
    dead children. These are the variant-themed assets bundled inside
    the variant's ``.skel_ext`` that the base parent does not carry —
    e.g. ``AM6068_Cartridges_Hoshino`` shows up under
    ``AGM3019_16in50_Mk7_Hoshino`` but not under the base
    ``AGM034_16in50_Mk7`` it replaces.

    Returns an empty set when either parent's attached doc is missing
    or empty (defensive — a fresh library may not yet carry the variant
    on first ingest; the base counterpart is always built first).
    """
    if not base_asset_id or not variant_asset_id:
        return set()
    if base_asset_id == variant_asset_id:
        return set()
    base = _attached_child_ids_for_asset(library_root, base_asset_id)
    variant = _attached_child_ids_for_asset(library_root, variant_asset_id)
    if not variant:
        return set()
    return set(variant) - set(base)


# ---------------------------------------------------------------------------
# Engine-side camo opt-out: material-id _9 suffix detector
# ---------------------------------------------------------------------------

# WG runtime gates per-mesh camo binding via a static material-name →
# part_index lookup table at exe ``0x140071a20`` (verified 2026-05-16 via
# Ghidra; see ``reference/topics/camo/camo_part_index_table.md``). The 4
# lookup groups (Hull-family / Gun-family / Misc-family / Plane-family)
# enumerate names like ``Hull``, ``DeckHouse``, ``Bulge``, ``Gun``,
# ``Gun1``, ``Director``, ``Misc``, ``Misc1``, ``Misc_skinned``, etc.
# ``Misc9`` / ``Gun9`` / ``Catapult9_skinned`` / ``Misc_9`` are **NOT**
# in any table; meshes carrying those material identifiers never enter
# the camo dispatch list and render with their natural diffuse.
#
# Empirically the ``9`` digit marks themed / skin-exclusive decorative
# geometry (Hoshino bow whale, Azur Lane secondaries, Ayane turret
# barrels, snowman ornaments, …). 220 materials across the live corpus
# match the pattern.
#
# The 8 RE-confirmed skip tokens — every name shape that is BOTH absent
# from the part_index groups AND occurs in shipped material IDs — are:
#   Misc, Gun, Catapult, Director, Plane, Hull, DeckHouse, Bulge
#
# note: dropped Float (no engine binding — Float materials are
# placement-side only, never carry a bindable material in any of the 4
# part_index groups) and Wire (engine slot 9 = paint, not skip — Wire9
# IS in the Misc-family part_index table, where slot 9 is a wire-overlay
# paint binding rather than a skip marker; the earlier regex was
# conflating these). Audit pass 2026-05-17.
#
# Mirror the engine rule by collecting placement ``asset_id``s whose
# library entry carries at least one ``_9``-suffix material. Emitted as
# ``ship.camo_skip_asset_ids`` and unioned with
# ``variant_swapped_asset_ids`` in the consumer's ``variantOptOut`` gate.
_CAMO_SKIP_RE = re.compile(
    r"_(Misc|Gun|Catapult|Director|Plane|Hull|DeckHouse|Bulge)"
    r"_?9(Dead)?(_skinned)?$",
)


def _material_id_is_camo_skip(material_id: str) -> bool:
    """True if ``material_id`` matches WG's ``_9``-suffix no-camo marker."""
    return bool(_CAMO_SKIP_RE.search(material_id))


def _camo_skip_asset_ids_from_doc(
    library_root: Path,
    doc: dict[str, Any],
) -> set[str]:
    """Return placement ``asset_id``s whose library material set carries
    at least one ``_9``-suffix material identifier (engine's "skip camo"
    marker; see module-level note).

    Conservative rule: ANY ``_9`` material on the asset → full opt-out.
    Mixed-material assets where some renderers are ``_9`` and others
    aren't (~4/1020 in the live corpus, e.g. JGS3019 Ayane gun's
    ``Gun_skinned`` + ``Gun9_skinned``) get the whole asset opted out;
    proper per-renderer suppression would require plumbing material-id
    through to the consumer entry-level, which we haven't done yet.
    """
    index = _accessory_index_for_root(library_root)
    assets = index.get("assets") if isinstance(index, dict) else None
    if not isinstance(assets, dict):
        return set()

    # Seed with every asset_id appearing in a top-level placement.
    seen_aids: set[str] = set()
    for section in sidecar.PLACEMENT_SECTIONS:
        for entry in doc.get(section) or []:
            if not isinstance(entry, dict):
                continue
            for key in ("asset_id", "dead_asset_id"):
                aid = entry.get(key)
                if isinstance(aid, str) and aid:
                    seen_aids.add(aid)

    # Also walk attached children — themed accessories like
    # AM6068_Cartridges_Hoshino are bundled under parent turrets and
    # never appear as top-level placements. The swap-children-diff path
    # (variant_swapped_asset_ids) catches them WHEN their parent is a
    # swap target; this walker catches them for the vanilla-parent case
    # too (e.g. a `_9` decoration attached to an unswapped mount).
    for aid in list(seen_aids):
        seen_aids |= _attached_child_ids_for_asset(library_root, aid)

    skip: set[str] = set()
    for aid in seen_aids:
        info = assets.get(aid)
        if not isinstance(info, dict):
            continue
        mats = info.get("materials")
        if not isinstance(mats, list):
            continue
        for m in mats:
            if not isinstance(m, dict):
                continue
            mid = m.get("material_id")
            if isinstance(mid, str) and _material_id_is_camo_skip(mid):
                skip.add(aid)
                break
    return skip


_COLOR_SCHEME_PREFIX = "colorScheme"

_PER_HULL_DIRNAME = "per_hull"


def _warn(warnings: list[str] | None, msg: str) -> None:
    """Surface a non-fatal failure.

    When ``warnings`` is provided, append the message so it lands on
    :attr:`ScaffoldResult.warnings` (visible to library callers + the
    StepRunner consumers). When ``None`` -- direct calls into the
    private helpers -- fall back to stderr so the dev still sees the
    message at the terminal.
    """
    if warnings is not None:
        warnings.append(msg)
    else:
        print(f"[scaffold_ship] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Permoflage discovery + camo skin emission (lifted)
# ---------------------------------------------------------------------------


def _build_palette_resolver(
    config: PipelineConfig | None,
    *,
    ship_name_hint: str | None = None,
    warnings: list[str] | None = None,
) -> Callable[[str, list[str]], tuple[str | None, list, dict]] | None:
    """Build a closure resolving ``(scheme_key, mask_paths)`` to
    ``(camo_name, rolls, categories)`` for the sidecar emit.

    Returns ``None`` on camo-DB extraction failure — sidecar emission
    then falls back to mask-only skins.
    """
    global _CAMO_DB_CACHE
    if _CAMO_DB_CACHE is None:
        try:
            _CAMO_DB_CACHE = wg_camo.CamouflageDb.load(config=config)
        except Exception as e:
            _warn(warnings, f"wg_camo unavailable: {e}")
            return None

    db = _CAMO_DB_CACHE

    def resolver(scheme_key: str, mask_paths: list[str]):
        entry, palettes = wg_camo.palette_for_mask_paths(
            db, mask_paths, ship_name_hint=ship_name_hint,
        )
        if entry is None or not palettes:
            return None, [], {}
        rolls: list[tuple[str, list[tuple[float, float, float, float]]]] = []
        for cs in palettes:
            rolls.append((_strip_color_scheme_prefix(cs.name), list(cs.colors)))
        try:
            wg_camo.ensure_camo_masks_for_entries([entry], config=config)
            mip_index = wg_camo.list_extracted_mips()
            categories = wg_camo.categories_for_entry(entry, mip_index)
        except Exception as e:
            _warn(warnings, f"wg_camo categories: {e}")
            categories = {}
        return entry.name, rolls, categories

    return resolver


def _humanize_camo_name(
    name: str,
    *,
    drop_prefixes: tuple[str, ...] = ("mat_", "camo_"),
) -> str:
    """Convert a WG camo entry name into a human-readable label."""
    if not name:
        return name
    for pfx in drop_prefixes:
        if name.startswith(pfx):
            name = name[len(pfx):]
            break
    return name.replace("_", " ")


def _strip_color_scheme_prefix(roll_name: str) -> str:
    """Drop the ``colorScheme`` prefix WG uses on entry names."""
    if roll_name.startswith(_COLOR_SCHEME_PREFIX):
        return roll_name[len(_COLOR_SCHEME_PREFIX):]
    return roll_name


def _merged_path_a_b_categories(
    entry: wg_camo.CamoEntry,
    masks_mip_index: dict[str, list[str]],
    mat_mip_index: dict[str, list[str]],
) -> dict[str, dict]:
    """Build category records by walking Path B (``entry.mgn_textures``)
    then Path A (``entry.textures``), with Path B winning per category.

    Mirrors the engine's per-part selector at ``+0x188 + part*0xc0`` in
    ``makeCamoMaterial``: when both ``<X>`` and ``<X_mgn>`` exist, the
    engine picks Path B. Pure-Path-B parts (e.g. ``<Director_mgn>``
    with no matching ``<Director>``) come from the mgn walk; pure-Path-A
    parts come from the textures walk; hybrid parts get Path B fields
    attached via :func:`categories_for_entry`'s ``mat_extracted_mips``
    mechanism (so ``setdefault`` here only resolves the pure-Path-A
    additions — hybrid parts have already landed via the Path B pass).

    The Path A walk excludes ``Hull``/``DeckHouse``/``Bulge`` (covered
    by Phase A's per-stem ``texture_sets`` cascade). The Path B walk
    includes hull because shared Path B hull textures (e.g.
    ``mat_250_NAVY_mgn``) don't have an equivalent per-stem cascade.
    """
    categories = wg_camo.path_b_categories_for_entry(
        entry, mat_mip_index, include_hull=True,
    )
    path_a_cats = wg_camo.categories_for_entry(
        entry, masks_mip_index,
        include_hull=False,
        mat_extracted_mips=mat_mip_index,
    )
    for cat, record in path_a_cats.items():
        categories.setdefault(cat, record)
    return categories


def _resolve_skin_display_base(
    exterior_id: str,
    camo_name: str,
    *,
    warnings: list[str] | None = None,
) -> str:
    """Pick the best human-readable display name for a permoflage skin."""
    try:
        loc = wg_camo.display_name_for_exterior(exterior_id, humanize_fallback=False)
        if loc:
            return loc
    except Exception as exc:
        _warn(warnings, f"display-name lookup failed: {exc}")
    return _humanize_camo_name(camo_name) or camo_name


def _emit_permoflage_skins(
    candidates: list[tuple[str, str, str, wg_camo.CamoEntry]],
    *,
    db: wg_camo.CamouflageDb,
    config: PipelineConfig | None,
    warnings: list[str] | None = None,
) -> list[dict]:
    """Common emission backend shared by the per-ship and universal walkers.

    Two top-level skin shapes:

    - ``kind="mat_albedo"`` — untinted ``mat_*`` entries with no palette
      (or no hull). Carries ``mat_textures`` only, no ``categories``.
    - ``kind="palette"`` (default) — anything with a palette + hull/tile.
      Per color roll: emits ``categories`` (per-part Path A masks with
      Path B fields attached where present) plus optional ``mat_textures``
      for hybrid mat_palette entries.

    The engine selects Path A vs Path B per-part (selector at
    ``+0x188 + part*0xc0`` in ``makeCamoMaterial``). Within the palette
    branch, three sub-shapes pick different category builders:

    - ``is_mat`` (mat_palette hybrid): Path A masks excl. mat_camo entries
      + ``mat_textures_from_palette_entry`` for the Path B atlases.
    - ``has_tile`` (tile broadcast): one ``<Tile>`` mask broadcasts to
      many categories via ``tile_categories_for_entry``.
    - else (hull_palette): per-part Path A + Path B union via
      :func:`_merged_path_a_b_categories`.

    Malformed entries (no recognized topology tags) are surfaced via
    stderr and skipped — empirically dead after the May 2026
    ``is_mat``-escape fix, but kept defensively.
    """
    untinted_mat: list[tuple[str, str, str, wg_camo.CamoEntry]] = []
    mat_palette: list[tuple[str, str, str, wg_camo.CamoEntry]] = []
    tile_broadcast: list[tuple[str, str, str, wg_camo.CamoEntry]] = []
    hull_palette: list[tuple[str, str, str, wg_camo.CamoEntry]] = []

    for exterior_id, camo_name, peculiarity, entry in candidates:
        is_mat = entry.name.startswith("mat_")
        has_palette = bool(entry.color_schemes)
        keys = set(entry.textures)
        has_hull = bool(keys & {"Hull", "DeckHouse", "Bulge"})
        has_tile = "Tile" in keys
        bucket: list | None
        if is_mat and (not has_palette or not has_hull):
            # mat_* with accessory-only tags OR mat_* + hull but no
            # palette → kind="mat_albedo" emit (no categories, just
            # mat_textures). ~39 mat_Azur_* entries authored for
            # not-yet-released ships land here via the accessory-only
            # case.
            bucket = untinted_mat
        elif is_mat and has_palette and has_hull:
            bucket = mat_palette
        elif has_palette and has_tile:
            bucket = tile_broadcast
        elif has_palette and has_hull:
            bucket = hull_palette
        else:
            print(
                f"[scaffold_ship] camo entry {entry.name} has no "
                f"recognized topology tags; skipping",
                file=sys.stderr,
            )
            continue
        bucket.append((exterior_id, camo_name, peculiarity, entry))

    skins: list[dict] = []

    # ── Untinted mat_albedo (no palette) ─────────────────────────────
    if untinted_mat:
        try:
            wg_camo.ensure_mat_camo_textures(
                [e for _, _, _, e in untinted_mat], config=config,
            )
        except Exception as e:
            _warn(warnings, f"mat_albedo extract failed ({e})")
        mat_mip_index = wg_camo.list_extracted_mips(wg_camo._mat_dir(config))
        for exterior_id, camo_name, peculiarity, entry in untinted_mat:
            mat_textures = wg_camo.mat_textures_for_entry(entry, mat_mip_index)
            if not mat_textures:
                continue
            display = _resolve_skin_display_base(
                exterior_id, camo_name, warnings=warnings,
            )
            skins.append(sidecar.make_skin(
                skin_id=camo_name,
                display_name=display,
                scheme_key=camo_name,
                camo_pattern=camo_name,
                kind="mat_albedo",
                exterior_id=exterior_id,
                peculiarity=peculiarity or None,
                mat_textures=mat_textures,
            ))

    # ── Batched palette-branch extracts ──────────────────────────────
    # Three sub-shapes share extract phases but call different
    # category builders during emit. Run extracts up front so the mip
    # indices below are populated once.
    if mat_palette:
        try:
            wg_camo.ensure_camo_masks_for_entries(
                [e for _, _, _, e in mat_palette],
                include_hull=True,
                skip_mat_camo=True,
                config=config,
            )
        except Exception as e:
            _warn(warnings, f"mat_palette mask extract failed ({e})")
        try:
            wg_camo.ensure_mat_camo_textures(
                [e for _, _, _, e in mat_palette],
                only_mat_camo=True,
                config=config,
            )
        except Exception as e:
            _warn(warnings, f"mat_palette atlas extract failed ({e})")

    if tile_broadcast:
        try:
            wg_camo.ensure_camo_masks_for_entries(
                [e for _, _, _, e in tile_broadcast],
                include_hull=True,
                config=config,
            )
        except Exception as e:
            _warn(warnings, f"tile_broadcast mask extract failed ({e})")
        tile_with_mgn = [
            e for _, _, _, e in tile_broadcast
            if e.mgn_textures or e.anim_maps
        ]
        if tile_with_mgn:
            try:
                wg_camo.ensure_mat_camo_textures(tile_with_mgn, config=config)
            except Exception as e:
                _warn(warnings, f"tile_broadcast mgn extract failed ({e})")

    if hull_palette:
        try:
            wg_camo.ensure_mat_camo_textures(
                [e for _, _, _, e in hull_palette],
                config=config,
            )
        except Exception as e:
            _warn(warnings, f"hull_palette mgn extract failed ({e})")
        try:
            wg_camo.ensure_camo_masks_for_entries(
                [e for _, _, _, e in hull_palette],
                include_hull=False,
                config=config,
            )
        except Exception as e:
            _warn(warnings, f"hull_palette accessory-mask extract failed ({e})")

    if mat_palette or tile_broadcast or hull_palette:
        masks_mip_index = wg_camo.list_extracted_mips(wg_camo._masks_dir(config))
        mat_mip_index = wg_camo.list_extracted_mips(wg_camo._mat_dir(config))
    else:
        masks_mip_index = {}
        mat_mip_index = {}

    # ── Tinted mat_palette emit (mask + palette + mat_camo atlas) ────
    for exterior_id, camo_name, peculiarity, entry in mat_palette:
        categories = wg_camo.categories_for_entry(
            entry, masks_mip_index,
            include_hull=True, skip_mat_camo=True,
            mat_extracted_mips=mat_mip_index,
        )
        mat_textures = wg_camo.mat_textures_from_palette_entry(
            entry, mat_mip_index,
        )
        if not categories and not mat_textures:
            continue
        skins.extend(_emit_palette_skins(
            entry, categories, db,
            exterior_id=exterior_id,
            camo_name=camo_name,
            peculiarity=peculiarity,
            display_base=_resolve_skin_display_base(
                exterior_id, camo_name, warnings=warnings,
            ),
            mat_textures=mat_textures or None,
        ))

    # ── Tile broadcast emit ──────────────────────────────────────────
    # `include_hull=True` because plain camo_*_tile entries have NO
    # per-stem hull cascade — the toolkit only bakes per-stem variant
    # textures for `mat_camo_schemes` (mat_* style camos), not for
    # plain tile camos. Without this, the hull falls through to
    # `entry.maskTextureByScheme` which is empty for the tile-camo
    # scheme key → `camoEnable = 0` → hull renders unpainted while
    # accessories show the tile pattern. The docstring on
    # `tile_categories_for_entry` warns of double-tinting from
    # include_hull=True, but that only applies to variant-routed
    # bespoke crossovers (Iowa_AzurLane etc.) — those bespokes live in
    # `texture_sets["main"]`, not in the tile scheme, so no overlap.
    for exterior_id, camo_name, peculiarity, entry in tile_broadcast:
        categories = wg_camo.tile_categories_for_entry(
            entry, masks_mip_index,
            include_hull=True,
            mat_extracted_mips=mat_mip_index,
        )
        if not categories:
            continue
        skins.extend(_emit_palette_skins(
            entry, categories, db,
            exterior_id=exterior_id,
            camo_name=camo_name,
            peculiarity=peculiarity,
            display_base=_resolve_skin_display_base(
                exterior_id, camo_name, warnings=warnings,
            ),
        ))

    # ── hull_palette emit (per-part Path A + Path B merge) ───────────
    # Hybrid Phase A + Path B entries. Pure Path A hull_palette entries
    # (~932 in corpus, none currently applied to any Vehicle) also
    # reach here — for those _merged_path_a_b_categories returns the
    # Path A accessory cats (hull stems covered by per-stem Phase A
    # cascade in materials/texture_sets).
    for exterior_id, camo_name, peculiarity, entry in hull_palette:
        categories = _merged_path_a_b_categories(
            entry, masks_mip_index, mat_mip_index,
        )
        if not categories:
            continue
        skins.extend(_emit_palette_skins(
            entry, categories, db,
            exterior_id=exterior_id,
            camo_name=camo_name,
            peculiarity=peculiarity,
            display_base=_resolve_skin_display_base(
                exterior_id, camo_name, warnings=warnings,
            ),
        ))

    return skins


def _emit_palette_skins(
    entry: wg_camo.CamoEntry,
    categories: dict[str, dict],
    db: wg_camo.CamouflageDb,
    *,
    exterior_id: str,
    camo_name: str,
    peculiarity: str,
    display_base: str,
    mat_textures: dict[str, dict] | None = None,
) -> list[dict]:
    """Emit one skin per color roll for a mask+palette entry."""
    palettes = db.resolve_palettes(entry)
    if not palettes:
        return []
    multi_roll = len(palettes) > 1
    out: list[dict] = []
    for cs in palettes:
        roll_id = _strip_color_scheme_prefix(cs.name)
        if multi_roll:
            skin_id = f"{camo_name}__{roll_id}"
            display = f"{display_base} ({roll_id})"
        else:
            skin_id = camo_name
            display = display_base
        out.append(sidecar.make_skin(
            skin_id=skin_id,
            display_name=display,
            scheme_key=camo_name,
            camo_pattern=camo_name,
            color_roll=roll_id if multi_roll else None,
            exterior_id=exterior_id,
            peculiarity=peculiarity or None,
            color_scheme={
                "name":   roll_id,
                "colors": [list(c) for c in cs.colors],
            },
            categories=categories or None,
            mat_textures=mat_textures,
        ))
    return out


def _resolve_full_ship_id(
    ship_id: str,
    *,
    config: PipelineConfig | None,
    log_label: str,
    warnings: list[str] | None = None,
) -> str | None:
    """Resolve a sidecar wg_ship_id prefix to the full GameParams entity key."""
    try:
        _ensure_gameparams_dump(config=config)
    except Exception as e:
        _warn(warnings, f"{log_label} skip: gameparams.json unavailable ({e})")
        return None
    if "_" in ship_id:
        return ship_id
    resolved = _gp_read.resolve_ship_id(ship_id)
    if resolved is None:
        _warn(warnings, f"{log_label} skip: ship_id {ship_id!r} not in cache")
    return resolved


def _ensure_camo_db(
    config: PipelineConfig | None,
    *,
    log_label: str,
    warnings: list[str] | None = None,
) -> wg_camo.CamouflageDb | None:
    """Lazily load + cache the camouflages.xml DB."""
    global _CAMO_DB_CACHE
    if _CAMO_DB_CACHE is None:
        try:
            _CAMO_DB_CACHE = wg_camo.CamouflageDb.load(config=config)
        except Exception as e:
            _warn(warnings, f"{log_label} skip: wg_camo unavailable ({e})")
            return None
    return _CAMO_DB_CACHE


def _discover_permoflage_skins(
    ship_id: str,
    *,
    config: PipelineConfig | None,
    ship_name_hint: str | None = None,
    known_camo_patterns: set[str] | None = None,
    warnings: list[str] | None = None,
) -> list[dict]:
    """Discover non-Phase-A permoflages applicable to ``ship_id``."""
    db = _ensure_camo_db(config, log_label="permoflages", warnings=warnings)
    if db is None:
        return []
    full_ship_id = _resolve_full_ship_id(
        ship_id, config=config, log_label="permoflages", warnings=warnings,
    )
    if full_ship_id is None:
        return []
    try:
        permos = wg_camo.read_vehicle_permoflages(full_ship_id)
    except Exception as e:
        _warn(warnings, f"permoflages skip: read failed ({e})")
        return []
    if not permos:
        return []

    known: set[str] = set(known_camo_patterns or ())
    candidates: list[tuple[str, str, str, wg_camo.CamoEntry]] = []
    for exterior_id, camo_name, peculiarity in permos:
        if not camo_name or camo_name in known:
            continue
        entry = db.find_entry_by_name(camo_name, ship_index=full_ship_id)
        if entry is None:
            continue
        if entry.name in known:
            continue
        candidates.append((exterior_id, camo_name, peculiarity, entry))
        known.add(entry.name)
    if not candidates:
        return []

    return _emit_permoflage_skins(
        candidates, db=db, config=config, warnings=warnings,
    )


def _discover_universal_skins(
    ship_id: str,
    *,
    config: PipelineConfig | None,
    ship_name_hint: str | None = None,
    known_camo_patterns: set[str] | None = None,
    warnings: list[str] | None = None,
) -> list[dict]:
    """Discover universal (``PCEC*``) camos and return them as Skin entries."""
    db = _ensure_camo_db(config, log_label="universal-PCEC", warnings=warnings)
    if db is None:
        return []
    full_ship_id = _resolve_full_ship_id(
        ship_id, config=config, log_label="universal-PCEC", warnings=warnings,
    )
    if full_ship_id is None:
        return []
    try:
        pcec = wg_camo.read_universal_exteriors()
    except Exception as e:
        _warn(warnings, f"universal-PCEC skip: read failed ({e})")
        return []
    if not pcec:
        return []

    known: set[str] = set(known_camo_patterns or ())
    candidates: list[tuple[str, str, str, wg_camo.CamoEntry]] = []
    for exterior_id, camo_name, peculiarity in pcec:
        if not camo_name or camo_name in known:
            continue
        entry = db.find_entry_by_name(camo_name, ship_index=full_ship_id)
        if entry is None:
            continue
        if entry.name in known:
            continue
        candidates.append((exterior_id, camo_name, peculiarity, entry))
        known.add(entry.name)
    if not candidates:
        return []

    return _emit_permoflage_skins(
        candidates, db=db, config=config, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Per-hull placements + ship-id override (lifted)
# ---------------------------------------------------------------------------


def _enumerate_hull_names(ship_dict: dict) -> list[tuple[str, str]]:
    """Return ``[(hull_name, module_id), ...]`` from ``ShipUpgradeInfo``."""
    sui = ship_dict.get("ShipUpgradeInfo") or {}
    if not isinstance(sui, dict):
        return []
    hull_entries: dict[str, dict] = {
        k: v for k, v in sui.items()
        if isinstance(v, dict) and v.get("ucType") == "_Hull"
    }
    if not hull_entries:
        return []
    ordered_module_ids: list[str] = []
    seen: set[str] = set()
    cursor = next(
        (mid for mid, m in hull_entries.items() if m.get("prev") == ""), None,
    )
    while cursor and cursor not in seen:
        ordered_module_ids.append(cursor)
        seen.add(cursor)
        nxt = next(
            (mid for mid, m in hull_entries.items() if m.get("prev") == cursor),
            None,
        )
        cursor = nxt
    for mid in hull_entries:
        if mid not in seen:
            ordered_module_ids.append(mid)

    out: list[tuple[str, str]] = []
    for mid in ordered_module_ids:
        comps = hull_entries[mid].get("components") or {}
        hull_list = comps.get("hull") if isinstance(comps, dict) else []
        if isinstance(hull_list, list) and hull_list:
            out.append((hull_list[0], mid))
    return out


def _load_placements_with_id_override(
    placements_path: Path,
    gameparams_ship_id: str | None,
    config: PipelineConfig | None,
) -> dict[str, Any] | None:
    """Read placements + override ``placements.ship`` for a different Vehicle.

    Returns the parsed-and-corrected dict, or ``None`` when no override
    is needed.
    """
    if not gameparams_ship_id:
        return None
    try:
        with open(placements_path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    pl_ship = doc.get("ship") or {}
    if not isinstance(pl_ship, dict):
        return None
    pl_param_index = pl_ship.get("param_index")
    try:
        _ensure_gameparams_dump(config=config)
    except Exception:
        return None
    try:
        target = _gp_read.get_ship(gameparams_ship_id)
    except Exception:
        return None
    if not isinstance(target, dict):
        return None
    target_index = target.get("index")
    if not target_index:
        return None
    if pl_param_index and target_index == pl_param_index:
        return None  # toolkit already picked the right Vehicle

    ti = target.get("typeinfo") or {}
    new_tier = target.get("level")
    new_nation = ti.get("nation") or pl_ship.get("nation") or ""
    new_species = ti.get("species") or pl_ship.get("species") or ""

    new_display_name: str | None = None
    try:
        db = _localization.load()
        candidate = db.get(f"IDS_{target_index.upper()}")
        if candidate:
            new_display_name = candidate
    except Exception:
        pass
    if not new_display_name:
        full_name = target.get("name") or ""
        if "_" in full_name:
            new_display_name = full_name.split("_", 1)[1]
        else:
            new_display_name = full_name or pl_ship.get("display_name", "")

    new_ship = dict(pl_ship)
    new_ship["param_index"] = target_index
    new_ship["tier"] = new_tier
    new_ship["nation"] = new_nation
    new_ship["species"] = new_species
    new_ship["display_name"] = new_display_name
    doc["ship"] = new_ship
    return doc


def _export_per_hull_placements(
    *,
    toolkit_name: str,
    gm3d_dir: Path,
    gameparams_ship_id: str | None,
    doc_wg_ship_id: str | None,
    config: PipelineConfig | None,
) -> dict[str, Path]:
    """Run ``wowsunpack export-ship --hull <H>`` for every hull tier."""
    try:
        _ensure_gameparams_dump(config=config)
    except Exception:
        return {}

    ship_id = gameparams_ship_id or doc_wg_ship_id
    if not ship_id:
        return {}
    try:
        ship_dict = _gp_read.get_ship(ship_id)
    except Exception:
        return {}
    if not isinstance(ship_dict, dict):
        return {}

    hulls = _enumerate_hull_names(ship_dict)
    if len(hulls) <= 1:
        return {}

    out_dir = gm3d_dir / _PER_HULL_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    import re
    import tempfile
    _HULL_LETTER_RE = re.compile(r"^([A-Z])_Hull")
    out: dict[str, Path] = {}
    for hull_name, _module_id in hulls:
        placements_path = out_dir / f"{hull_name}.placements.json"
        if placements_path.is_file() and placements_path.stat().st_size > 0:
            out[hull_name] = placements_path
            continue
        m = _HULL_LETTER_RE.match(hull_name)
        if not m:
            continue
        hull_selector = m.group(1)
        with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as tmp_glb:
            tmp_glb_path = Path(tmp_glb.name)
        try:
            _toolkit_export_ship(
                toolkit_name, tmp_glb_path,
                hull=hull_selector,
                accessories="exclude",
                placements_json=placements_path,
                no_textures=True,
                config=config,
            )
            out[hull_name] = placements_path
        finally:
            try:
                tmp_glb_path.unlink(missing_ok=True)
            except Exception:
                pass
    return out


def _collect_per_hull_placements(gm3d_dir: Path) -> dict[str, Path]:
    """Discover per-hull placements files from disk."""
    out_dir = gm3d_dir / _PER_HULL_DIRNAME
    out: dict[str, Path] = {}
    if not out_dir.is_dir():
        return out
    for f in sorted(out_dir.glob("*.placements.json")):
        hull_name = f.name[: -len(".placements.json")]
        if hull_name:
            out[hull_name] = f
    return out


def _detect_rendered_hull(doc: dict[str, Any], active_hull: str | None) -> str | None:
    """Return the hull whose per-hull mount set best matches the rendered
    top-level placements.

    The toolkit's default ``export-ship`` (and mesh-swap variant routing)
    can render a hull tier other than the gameplay-active one — typically
    the *stock* hull, whose secondary / AA loadout differs from the upgraded
    hull. Stamping upgraded-hull arcs onto a stock-rendered model leaves the
    mismatched mounts wrong (empty, or aliased to a gun that isn't in the
    GLB). Matching by ``(section, hp_name, asset_id)`` recovers the hull
    actually in the GLB so gameplay + the top-level alias can follow it.

    Returns ``active_hull`` (no override) when it already matches at least
    as well as any other hull — the normal case, so non-variant ships and
    ships rendered at their active hull are unaffected. Returns ``None``
    when there's no ``hulls`` block to compare against.

    Must be called BEFORE :func:`sidecar.alias_active_hull_to_top_level`,
    while the top-level arrays still hold the toolkit's rendered placements.
    """
    hulls = doc.get("hulls")
    if not isinstance(hulls, dict) or len(hulls) < 2:
        return None

    def keyset(container: dict[str, Any]) -> set[tuple[str, str, str]]:
        out: set[tuple[str, str, str]] = set()
        for sec in ("turrets", "secondaries", "antiair", "torpedoes"):
            for e in container.get(sec) or []:
                if not isinstance(e, dict):
                    continue
                hp, aid = e.get("hp_name"), e.get("asset_id")
                if isinstance(hp, str) and isinstance(aid, str):
                    out.add((sec, hp, aid))
        return out

    top = keyset(doc)
    if not top:
        return None
    scores = {
        name: len(top & keyset(he))
        for name, he in hulls.items()
        if isinstance(he, dict)
    }
    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    active_score = scores.get(active_hull, -1) if active_hull else -1
    if best != active_hull and scores[best] > active_score:
        return best
    return active_hull


# ---------------------------------------------------------------------------
# GameParams autofill passes (lifted)
# ---------------------------------------------------------------------------


def _absorb_gameparams_passes(
    doc: dict,
    *,
    ship: str,
    config: PipelineConfig | None,
    toolkit_armor_data: dict | None,
    gameparams_ship_id: str | None = None,
    gm3d_dir: Path | None = None,
    active_placements_json: Path | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Run the GameParams-driven autofill passes (schema v3.1)."""
    ship_id = gameparams_ship_id or (doc.get("ship") or {}).get("wg_ship_id")
    if not ship_id:
        return doc

    try:
        _ensure_gameparams_dump(config=config)
    except Exception as e:
        _warn(warnings, f"gameparams autofill skipped — cache unavailable ({e})")
        return doc

    try:
        full_id = _gp_read.resolve_ship_id(ship_id)
        ship_dict = _gp_read.get_ship(ship_id)
    except Exception as e:
        _warn(warnings, f"gameparams autofill skipped — load failed ({e})")
        return doc
    if ship_dict is None:
        _warn(warnings, f"gameparams autofill: ship {ship_id!r} not in cache")
        return doc

    components = _gp_autofill.resolve_components(ship_dict, hull_choice="upgraded")

    # Pass 1: ship metadata
    try:
        doc = sidecar.absorb_gameparams_ship(doc, ship_dict, full_ship_id=full_id)
    except Exception as e:
        _warn(warnings, f"gameparams ship-extras failed ({e})")

    # Pass 2: variants summary
    try:
        summary = _gp_autofill.variants_summary(ship_dict)
        if summary:
            doc = sidecar.absorb_gameparams_variants(doc, ship_dict, summary=summary)
    except Exception as e:
        _warn(warnings, f"gameparams variants failed ({e})")

    # Pass 2b: per-hull placement snapshots (schema v3.2)
    try:
        per_hull_files: dict = {}
        if gm3d_dir is not None:
            per_hull_files = dict(_collect_per_hull_placements(gm3d_dir))
        active_hull = (doc.get("variants") or {}).get("active_hull")
        if (active_hull and active_placements_json is not None
                and Path(active_placements_json).is_file()
                and active_hull not in per_hull_files):
            per_hull_files[active_hull] = Path(active_placements_json)
        if per_hull_files:
            doc = sidecar.absorb_per_hull_placements(
                doc, per_hull_files, ship_dict=ship_dict,
            )
            # The toolkit may render a hull tier other than the gameplay-
            # active one (mesh-swap variants render the stock hull, whose
            # secondary / AA guns differ from the upgraded hull). Detect the
            # hull actually in the GLB and resolve weapon arcs + alias the
            # top-level from it, so per-mount fields and asset_ids match the
            # rendered meshes instead of an unequipped hull tier.
            rendered_hull = _detect_rendered_hull(doc, active_hull)
            if rendered_hull and rendered_hull != active_hull:
                stock_hull = (doc.get("variants") or {}).get("stock_hull")
                hull_choice = (
                    "stock" if rendered_hull == stock_hull else rendered_hull
                )
                components = _gp_autofill.resolve_components(
                    ship_dict, hull_choice=hull_choice,
                )
                doc = sidecar.alias_active_hull_to_top_level(
                    doc, hull_name=rendered_hull,
                )
                _warn(
                    warnings,
                    f"rendered hull {rendered_hull!r} != gameplay-active "
                    f"{active_hull!r}; resolving weapon arcs + top-level mounts "
                    f"from the rendered hull",
                )
            else:
                doc = sidecar.alias_active_hull_to_top_level(doc)
    except Exception as e:
        _warn(warnings, f"per-hull mount index failed ({e})")

    # Pass 3: per-placement gameplay autofill
    try:
        autofill_by_hp: dict[str, dict] = {}
        for section in sidecar.PLACEMENT_SECTIONS:
            for entry in doc.get(section) or []:
                if not isinstance(entry, dict):
                    continue
                hp = entry.get("hp_name")
                if not isinstance(hp, str) or hp in autofill_by_hp:
                    continue
                fields = _gp_autofill.autofill_for_hp(components, hp)
                if fields:
                    autofill_by_hp[hp] = fields
        if autofill_by_hp:
            doc = sidecar.absorb_gameparams_mounts(doc, autofill_by_hp)
    except Exception as e:
        _warn(warnings, f"gameparams mount autofill failed ({e})")

    # Pass 4: per-mount armor + barbettes
    try:
        mount_armor = _gp_autofill.collect_mount_armor(components)
        hull = components.get("hull") or {}
        barbettes = _gp_autofill.collect_barbettes(hull)
        if mount_armor or barbettes:
            doc = sidecar.absorb_gameparams_armor(
                doc, mount_armor=mount_armor, barbettes=barbettes,
            )
        if toolkit_armor_data and isinstance(toolkit_armor_data, dict):
            gp_armor = (hull.get("armor") if isinstance(hull, dict) else None) or {}
            # ``armor_diffs`` is the cross-validate report (not the
            # outer-scope ``warnings`` accumulator — kept distinct for
            # clarity).
            armor_diffs = _gp_autofill.cross_validate_armor(
                toolkit_armor_data.get("materials_table", {}),
                gp_armor,
            )
            for line in armor_diffs[:5]:
                _warn(warnings, f"armor cross-validate: {line}")
    except Exception as e:
        _warn(warnings, f"gameparams armor failed ({e})")

    # Pass 5: per-cube hitbox classification
    try:
        classification = _gp_autofill.classify_splash_boxes(ship_dict, components)
        if classification.get("boxes") or classification.get("hit_locations"):
            doc = sidecar.absorb_gameparams_hitbox(
                doc,
                boxes=classification.get("boxes"),
                hit_locations=classification.get("hit_locations"),
            )
    except Exception as e:
        _warn(warnings, f"gameparams hitbox failed ({e})")

    # Pass 6: per-torpedo PAPT* enrichment
    try:
        ballistics_section = doc.get("ballistics") or {}
        torps = (
            ballistics_section.get("torpedoes")
            if isinstance(ballistics_section, dict)
            else None
        )
        if isinstance(torps, dict) and torps:
            extras_by_id: dict[str, dict] = {}
            for ammo_id in torps:
                extras = _gp_autofill.torpedo_profile_extras(ammo_id)
                if extras:
                    extras_by_id[ammo_id] = extras
            if extras_by_id:
                doc = sidecar.absorb_gameparams_torpedoes(doc, extras_by_id)
    except Exception as e:
        _warn(warnings, f"gameparams torpedo profiles failed ({e})")

    # Pass 6b: per-ship camera trajectory ellipses (the GameParams ``Cameras``
    # component → ``doc.camera``, schema wg_cameras_v1). Drives the consumer's
    # WG-faithful 3rd-person / observe / binocular orbit
    # (reference/engine/wg_battle_camera.md). Pure pass-through; best-effort.
    try:
        cameras = ship_dict.get("Cameras") if isinstance(ship_dict, dict) else None
        if isinstance(cameras, dict):
            doc = sidecar.absorb_gameparams_camera(doc, cameras)
    except Exception as e:
        _warn(warnings, f"gameparams camera failed ({e})")

    # Pass 7: particle effect attachments + resolved Effect blob data
    # (schema v3.x). Drives the webview's particle renderer and the
    # Unity-side ParticleSystem authoring. Best-effort: a missing
    # assets.bin / unresolved entry degrades to a warning, never blocks
    # the rest of the sidecar build.
    try:
        # Collect every particle scope the consumer can preview: hull
        # anchored EP_* effects (fire / flood / death / smoke / wake),
        # per-gun muzzle + damage + purge + reload + lens, per-AA-aura
        # flak burst + detonation + miss puff, and per-Munition impact
        # / projectile-destroyed / tracer XML refs. Each row carries a
        # ``source`` field the webview pivots on for tab bucketing.
        attachments = _gp_autofill.collect_all_particle_attachments(
            ship_dict, components,
        )
        if attachments:
            from ..read import particles as _particles
            from ..toolkit import assets_bin as _assets_bin
            try:
                assets_bin_path = _assets_bin.ensure_dump(config=config)
            except Exception as e:
                _warn(warnings, f"gameparams effects: assets.bin unavailable ({e})")
                assets_bin_path = None
            if assets_bin_path is not None:
                with _particles.ParticleStore.open(assets_bin_path) as store:
                    paths = _gp_autofill.collect_unique_particle_paths(attachments)
                    resolved: dict[str, dict] = {}
                    unresolved: list[str] = []
                    for p in paths:
                        eff = store.get(p)
                        if eff is None:
                            unresolved.append(p)
                        else:
                            resolved[p] = eff
                    if unresolved:
                        _warn(
                            warnings,
                            f"gameparams effects: {len(unresolved)} of "
                            f"{len(paths)} particle paths unresolved "
                            f"(e.g. {unresolved[0]!r})",
                        )
                    # Extract every DDS referenced by Renderer.textureName*
                    # / Animation.motionVectorsTexture into the workspace
                    # cache so the webview can fetch them. Stamps each
                    # renderer block with a ``texture_url_*`` field used
                    # by the JS-side ParticleScene.
                    try:
                        from . import effects_textures as _eff_tex
                        tex_paths = _eff_tex.collect_texture_paths(resolved)
                        if tex_paths:
                            resolved_urls, missing_tex = _eff_tex.ensure_textures_on_disk(
                                tex_paths, config=config,
                            )
                            stamped = _eff_tex.stamp_texture_urls(resolved, resolved_urls)
                            if missing_tex:
                                _warn(
                                    warnings,
                                    f"effects textures: {len(missing_tex)} of "
                                    f"{len(tex_paths)} referenced textures not "
                                    f"extractable (e.g. {sorted(missing_tex)[0]!r})",
                                )
                            print(
                                f"  effects: extracted {len(resolved_urls)} "
                                f"texture(s) ({stamped} field(s) stamped)",
                            )
                    except Exception as e:
                        _warn(warnings, f"effects textures failed ({e})")
                    doc = sidecar.absorb_gameparams_effects(
                        doc, attachments=attachments, particles=resolved,
                    )
    except Exception as e:
        _warn(warnings, f"gameparams effects failed ({e})")

    return doc


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def scaffold_ship(
    ship: str,
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    class_override: str | None = None,
    ship_key_suffix: str | None = None,
    toolkit_ship: str | None = None,
    gameparams_ship_id: str | None = None,
    skip_export: bool = False,
    skip_armor: bool = False,
    skip_ammo: bool = False,
    skip_sidecar: bool = False,
    skip_native_skin: bool = False,
    skip_gameparams_autofill: bool = False,
    skip_materials_skins: bool = False,
    skip_geometry_hitbox: bool = False,
    variant_permoflage: str | None = "auto",
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> ScaffoldResult:
    """Scaffold a fresh ship directory: hull GLB + sidecar + side files.

    ``ship`` is the filesystem label; it drives folder names, file prefixes,
    and the sidecar's ``ship_key`` derivation.  ``toolkit_ship`` overrides
    what gets passed to ``wowsunpack``; defaults to ``ship``.

    ``workspace`` defaults to ``config.workspace`` (which itself defaults
    to ``cwd``).  Per-ship working dir is ``<workspace>/ships/<ship>``.

    ``variant_permoflage`` controls mesh-swap permoflage routing:
        * ``"auto"`` (default): after the toolkit's name resolution gives
          us a ``param_index``, look up ``Vehicle.nativePermoflage``.  If
          it has a full hull mesh swap, run a second ``export-ship``
          pass against the variant model_dir.
        * ``<exterior_id>``: explicitly route to that Exterior's variant.
        * ``"none"`` / empty string: disable variant routing.

    ``on_event`` is an optional callback invoked at each step boundary
    with a :class:`StepEvent`.  Canonical step names:
    ``resolve_identity`` / ``export_hull`` / ``export_armor`` /
    ``export_ammo`` / ``gameparams_autofill`` / ``materials_skins`` /
    ``geometry_hitbox`` / ``emit_sidecar``.

    ``cancel`` is an optional :class:`threading.Event` for cooperative
    cancellation; when set, the next step boundary raises
    :class:`wows_model_export.errors.CancelledError`.  The parallel
    export block (hull + armor + ammo) checks the flag too — but a
    long-running wowsunpack subprocess won't be torn down mid-call;
    cancel takes effect at the next subprocess boundary.
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)

    toolkit_name = toolkit_ship or ship
    if variant_permoflage in ("none", ""):
        variant_permoflage = None

    timer = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []

    # ── Step: resolve_identity ─────────────────────────────────────────
    timer.start("resolve_identity", detail=ship)
    try:
        ship_dir = (workspace / "ships" / ship).resolve()
        gm3d_dir = ship_dir / sidecar.MODELS_SUBDIR
        gm3d_dir.mkdir(parents=True, exist_ok=True)

        hull_glb = gm3d_dir / f"{ship}_hull.glb"
        placements_json = gm3d_dir / f"{ship}_placements.json"
        skel_ext_candidates_json = gm3d_dir / f"{ship}_skel_ext.json"
        material_mappings_json = gm3d_dir / f"{ship}_material_mappings.json"
        armor_json_path = ship_dir / f"{ship}_armor.json"
        ballistics_json = ship_dir / f"{ship}_ballistics.json"
        sidecar_path = sidecar.sidecar_path_for(ship_dir, ship)
        textures_dir = gm3d_dir / "textures"
        textures_dds_dir = gm3d_dir / "textures_dds"
    except Exception as e:
        timer.emit("resolve_identity", "failed")
        raise StepError(
            step="resolve_identity",
            underlying=e,
            detail=f"failed to set up paths under {workspace!r}",
        ) from e
    timer.complete()

    variant_routed: bool = False
    variant_exterior_id: str | None = None

    # ── Parallel block: export_hull + (export_armor → export_ammo) ───
    # All three wowsunpack subprocesses are independent, but each Rust
    # process loads its own ~200 MB GameParams copy on startup. To keep
    # peak memory bounded we cap parallelism at 2: hull runs in one
    # worker, armor + ammo run sequentially in a second worker. Since
    # armor + ammo (~12s + 12s) together still fit inside hull's ~64s
    # window, the wall-clock is the same as a full 3-way fan-out
    # (max(64, 12+12) = 64s) but peak wowsunpack processes drops from
    # 3 to 2.
    #
    # Each task emits its own start/complete/fail StepEvents under a
    # lock so on_event listeners see one full line per event, and
    # records its own timing into ``timer.step_timings_ms``. We bypass
    # StepRunner's single-active-step state machine because two
    # concurrent tasks can't share it.
    #
    # Variant-permoflage routing and emissive synthesis ride inside the
    # hull task because they read the placements JSON + textures dir
    # that the initial subprocess produces; only the initial subprocess
    # actually overlaps with armor/ammo.
    _emit_lock = threading.Lock()

    def _emit_locked(step: str, state: str, **kw: Any) -> None:
        with _emit_lock:
            timer.emit(step, state, **kw)

    def _record_timing(step: str, ms: float) -> None:
        with _emit_lock:
            timer.step_timings_ms[step] = ms

    def _variant_and_emissive(out: dict[str, Any]) -> None:
        """Post base-export work shared by the hull + bundle tasks.

        (1) Mesh-swap permoflage routing — a 2nd GLB-only ``export-ship``
        for the resolved variant model_dir, re-stamping the placements
        JSON. (2) Emissive DDS synthesis. Both read the placements JSON +
        raw-DDS dir the base export produced, so they run AFTER it (in the
        same worker), never overlapping armor/ammo.
        """
        # Mesh-swap permoflage routing.
        if variant_permoflage is not None:
            try:
                with open(placements_json, "rb") as f:
                    _pl_doc = json.loads(f.read().decode("utf-8"))
                param_index = ((_pl_doc.get("ship") or {}).get("param_index"))
            except Exception:
                param_index = None

            base_vehicle_id = gameparams_ship_id or param_index
            variant_dir, exterior_id = (None, None)
            if base_vehicle_id:
                variant_dir, exterior_id = (
                    _gp_autofill.resolve_variant_model_dir(
                        base_vehicle_id,
                        permoflage_id=(
                            variant_permoflage
                            if variant_permoflage != "auto" else None
                        ),
                    )
                )
            elif variant_permoflage is not None and variant_permoflage != "auto":
                out["task_warnings"].append(
                    f"--variant-permoflage={variant_permoflage!r} requested "
                    f"but no GameParams Vehicle ID resolvable; base ship used"
                )

            if variant_dir:
                _toolkit_export_ship(
                    variant_dir, hull_glb,
                    accessories="exclude",
                    all_render_sets=True,
                    placements_json=None,
                    skel_ext_candidates_json=skel_ext_candidates_json,
                    material_mappings_json=material_mappings_json,
                    no_textures=True,
                    raw_dds_dir=textures_dds_dir,
                    config=cfg,
                )
                _pl_doc.setdefault("ship", {})["model_dir"] = variant_dir
                _pl_doc["ship"]["variant_permoflage"] = exterior_id
                with open(placements_json, "wb") as f:
                    f.write(
                        json.dumps(_pl_doc, indent=2, ensure_ascii=False)
                        .encode("utf-8")
                    )
                out["variant_routed"] = True
                out["variant_exterior_id"] = exterior_id

        # Emissive synthesis — discovers ``*_emissive.mfm`` files in the
        # VFS, then synthesizes per-stem emissive DDS files next to the
        # diffuse so the sidecar's stem classifier can route them into
        # ``texture_sets[<scheme>]["emissive"]``. Best-effort: synth
        # failures don't abort the scaffold, just surface as warnings.
        # No-op for non-emissive ships (no ``*_emissive.mfm`` matches).
        try:
            synth_paths = _synth_emission.synthesize_emissive_textures(
                textures_dds_dir,
                config=cfg,
                label=ship,
                material_mappings_json=material_mappings_json,
            )
            if synth_paths:
                _emit_locked(
                    "export_hull", "progress",
                    detail=f"synthesised {len(synth_paths)} emissive DDS file(s)",
                    data={"emissive_files": [str(p) for p in synth_paths]},
                )
        except Exception as e:
            out["task_warnings"].append(
                f"emissive synthesis skipped ({type(e).__name__}: {e})"
            )

    def _hull_task() -> dict[str, Any]:
        out: dict[str, Any] = {
            "variant_routed":       False,
            "variant_exterior_id":  None,
            "task_warnings":        [],
        }
        detail = f"{toolkit_name} -> {hull_glb.name}"
        _emit_locked("export_hull", "started", detail=detail)
        t0 = time.perf_counter()
        try:
            _toolkit_export_ship(
                toolkit_name, hull_glb,
                accessories="exclude",
                all_render_sets=True,
                placements_json=placements_json,
                skel_ext_candidates_json=skel_ext_candidates_json,
                material_mappings_json=material_mappings_json,
                no_textures=True,
                raw_dds_dir=textures_dds_dir,
                config=cfg,
            )
            _variant_and_emissive(out)
        except StepError:
            step_ms = (time.perf_counter() - t0) * 1000.0
            _record_timing("export_hull", step_ms)
            _emit_locked("export_hull", "failed", step_ms=step_ms)
            raise
        except Exception as e:
            step_ms = (time.perf_counter() - t0) * 1000.0
            _record_timing("export_hull", step_ms)
            _emit_locked("export_hull", "failed", step_ms=step_ms)
            raise StepError(
                step="export_hull",
                underlying=e,
                detail=f"export-ship {toolkit_name!r} failed",
            ) from e
        step_ms = (time.perf_counter() - t0) * 1000.0
        _record_timing("export_hull", step_ms)
        _emit_locked("export_hull", "completed", detail=detail, step_ms=step_ms)
        return out

    def _armor_task() -> None:
        detail = str(armor_json_path.name)
        _emit_locked("export_armor", "started", detail=detail)
        t0 = time.perf_counter()
        try:
            _toolkit_armor_json(toolkit_name, armor_json_path, config=cfg)
        except Exception as e:
            step_ms = (time.perf_counter() - t0) * 1000.0
            _record_timing("export_armor", step_ms)
            _emit_locked("export_armor", "failed", step_ms=step_ms)
            raise StepError(
                step="export_armor",
                underlying=e,
                detail=f"armor {toolkit_name!r} failed",
            ) from e
        step_ms = (time.perf_counter() - t0) * 1000.0
        _record_timing("export_armor", step_ms)
        _emit_locked("export_armor", "completed", detail=detail, step_ms=step_ms)

    def _ammo_task() -> None:
        detail = str(ballistics_json.name)
        _emit_locked("export_ammo", "started", detail=detail)
        t0 = time.perf_counter()
        try:
            _toolkit_ammo_json(toolkit_name, ballistics_json, config=cfg)
        except Exception as e:
            step_ms = (time.perf_counter() - t0) * 1000.0
            _record_timing("export_ammo", step_ms)
            _emit_locked("export_ammo", "failed", step_ms=step_ms)
            raise StepError(
                step="export_ammo",
                underlying=e,
                detail=f"ammo {toolkit_name!r} failed",
            ) from e
        step_ms = (time.perf_counter() - t0) * 1000.0
        _record_timing("export_ammo", step_ms)
        _emit_locked("export_ammo", "completed", detail=detail, step_ms=step_ms)

    # Emit skip events for any disabled step.
    if skip_export:
        timer.skip("export_hull")
        # Pre-flight check for skip_export + variant_permoflage mismatch.
        if (variant_permoflage is not None
                and variant_permoflage != "auto"
                and placements_json.is_file()):
            try:
                with open(placements_json, "rb") as f:
                    _pl_check = json.loads(f.read().decode("utf-8"))
                stamped = (_pl_check.get("ship") or {}).get("variant_permoflage")
            except Exception:
                stamped = None
            if stamped and stamped != variant_permoflage:
                raise StepError(
                    step="export_hull",
                    underlying=ValueError(
                        f"--skip-export: --variant-permoflage={variant_permoflage!r} "
                        f"disagrees with stamped {stamped!r} in {placements_json.name}"
                    ),
                    detail="variant_permoflage mismatch on skip-export path",
                )
    if skip_armor:
        timer.skip("export_armor")
    if skip_ammo:
        timer.skip("export_ammo")

    # Bundle armor + ammo into one worker so the parallel block uses
    # at most 2 concurrent wowsunpack processes. Order matches the
    # original sequential chain (armor before ammo) — if armor raises,
    # ammo is skipped, same as the pre-parallel behavior.
    def _aux_task() -> None:
        if not skip_armor:
            _armor_task()
        if not skip_ammo:
            _ammo_task()

    def _bundle_task() -> dict[str, Any]:
        """Full-pass fast path: ONE ``ingest-ship`` process emits the GLB +
        placements + skel_ext + material-mappings + raw DDS + armor JSON +
        ammo JSON from a single VFS + GameParams parse, replacing the
        hull‖(armor+ammo) trio (each of which re-parsed assets.bin +
        GameParams.data independently). armor/ammo JSON are byte-identical
        to the standalone subcommands; the GLB comes from the identical
        export-ship code path. Variant routing + emissive synth run after,
        exactly as in _hull_task.
        """
        out: dict[str, Any] = {
            "variant_routed":       False,
            "variant_exterior_id":  None,
            "task_warnings":        [],
        }
        detail = f"{toolkit_name} -> {hull_glb.name} (+armor+ammo, one parse)"
        _emit_locked("export_hull", "started", detail=detail)
        _emit_locked("export_armor", "started", detail=str(armor_json_path.name))
        _emit_locked("export_ammo", "started", detail=str(ballistics_json.name))
        t0 = time.perf_counter()
        try:
            _toolkit_ingest_ship_bundle(
                toolkit_name, hull_glb,
                accessories="exclude",
                all_render_sets=True,
                placements_json=placements_json,
                skel_ext_candidates_json=skel_ext_candidates_json,
                material_mappings_json=material_mappings_json,
                no_textures=True,
                raw_dds_dir=textures_dds_dir,
                armor_json=armor_json_path,
                ammo_json=ballistics_json,
                config=cfg,
            )
            _variant_and_emissive(out)
        except Exception as e:
            step_ms = (time.perf_counter() - t0) * 1000.0
            _record_timing("export_hull", step_ms)
            _emit_locked("export_hull", "failed", step_ms=step_ms)
            _emit_locked("export_armor", "failed")
            _emit_locked("export_ammo", "failed")
            if isinstance(e, StepError):
                raise
            raise StepError(
                step="export_hull",
                underlying=e,
                detail=f"ingest-ship {toolkit_name!r} failed",
            ) from e
        step_ms = (time.perf_counter() - t0) * 1000.0
        _record_timing("export_hull", step_ms)
        _emit_locked("export_hull", "completed", detail=detail, step_ms=step_ms)
        # armor + ammo were produced inside the same process; surface them
        # as completed (timing folded into export_hull) so the step set is
        # unchanged for progress consumers that track all three.
        _record_timing("export_armor", 0.0)
        _record_timing("export_ammo", 0.0)
        _emit_locked("export_armor", "completed",
                     detail="(folded into ingest-ship)", step_ms=0.0)
        _emit_locked("export_ammo", "completed",
                     detail="(folded into ingest-ship)", step_ms=0.0)
        return out

    # Gather + run live tasks. Priority list determines which error
    # surfaces first when both tasks fail in the same parallel batch
    # (hull > aux, matching the original sequential order). The aux
    # task already carries the right inner step name (export_armor or
    # export_ammo) on its raised StepError.
    #
    # Fast path: when the full trio is wanted (the common initial ingest
    # pass) AND the toolkit binary has the `ingest-ship` subcommand, fuse
    # export+armor+ammo into ONE process. Any skip combination (e.g. the
    # skip-everything _refresh_sidecar pass, or a CLI --skip-ammo), or an
    # older wowsunpack without the subcommand, falls back to the separate
    # hull‖aux path (ingest-ship can't express a skip — it always
    # re-exports the GLB — and an old binary has no ingest-ship at all).
    use_bundle = (
        (not skip_export) and (not skip_armor) and (not skip_ammo)
        and _toolkit_ingest_ship_supported(cfg)
    )
    tasks: list[tuple[str, Callable[[], Any]]] = []
    if use_bundle:
        tasks.append(("export_hull", _bundle_task))
    else:
        if not skip_export:
            tasks.append(("export_hull", _hull_task))
        if not skip_armor or not skip_ammo:
            tasks.append(("export_aux", _aux_task))

    hull_result: dict[str, Any] | None = None
    if tasks:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(tasks),
            thread_name_prefix="scaffold-toolkit",
        ) as ex:
            futures: dict[str, concurrent.futures.Future[Any]] = {
                name: ex.submit(fn) for name, fn in tasks
            }
            concurrent.futures.wait(futures.values())

        for name, _fn in tasks:
            exc = futures[name].exception()
            if exc is not None:
                # Tasks wrap their own exceptions in StepError; surface
                # the first one in priority order so the most-
                # significant failure wins.
                raise exc
            if name == "export_hull":
                hull_result = futures[name].result()

    if hull_result is not None:
        warnings.extend(hull_result["task_warnings"])
        variant_routed = hull_result["variant_routed"]
        variant_exterior_id = hull_result["variant_exterior_id"]

    # ── Sidecar pipeline ──────────────────────────────────────────────
    if skip_sidecar:
        return ScaffoldResult(
            ship_id=ship,
            workspace_dir=ship_dir,
            hull_glb=hull_glb if hull_glb.is_file() else None,
            placements_json=placements_json if placements_json.is_file() else None,
            skel_ext_json=(
                skel_ext_candidates_json if skel_ext_candidates_json.is_file() else None
            ),
            material_mappings_json=(
                material_mappings_json if material_mappings_json.is_file() else None
            ),
            armor_json=armor_json_path if armor_json_path.is_file() else None,
            ammo_json=ballistics_json if ballistics_json.is_file() else None,
            sidecar_path=sidecar_path if sidecar_path.is_file() else None,
            textures_dds_dir=textures_dds_dir if textures_dds_dir.is_dir() else None,
            variant_routed=variant_routed,
            variant_permoflage=variant_exterior_id,
            warnings=tuple(warnings),
            step_timings_ms=dict(timer.step_timings_ms),
        )

    if not placements_json.is_file():
        raise StepError(
            step="emit_sidecar",
            underlying=FileNotFoundError(
                f"placements JSON missing: {placements_json} (rerun without skip_export)"
            ),
            detail="placements JSON missing",
        )

    # Single placements_json load shared by every downstream consumer in
    # this function (per-hull export, ship-id override, variant swap,
    # variant-routed retroactive flag, _fold_variant_overlay_into_default).
    try:
        with open(placements_json, "rb") as _f:
            pl_doc = json.loads(_f.read().decode("utf-8"))
        if not isinstance(pl_doc, dict):
            pl_doc = {}
    except Exception:
        pl_doc = {}
    pl_ship_block = pl_doc.get("ship") if isinstance(pl_doc.get("ship"), dict) else {}

    # Per-hull placements export (still runs under --skip-export so legacy
    # ships scaffolded before v3.2 land on v3.2).
    toolkit_param_index = pl_ship_block.get("param_index")
    try:
        _export_per_hull_placements(
            toolkit_name=toolkit_name,
            gm3d_dir=gm3d_dir,
            gameparams_ship_id=gameparams_ship_id,
            doc_wg_ship_id=toolkit_param_index,
            config=cfg,
        )
    except Exception as e:
        warnings.append(
            f"per-hull placements export failed ({e}); "
            f"sidecar's hulls block will only carry the active hull"
        )

    # Warn if the merged accessories.json is stale.
    accessories_json = gm3d_dir / f"{ship}_accessories.json"
    if accessories_json.is_file() and placements_json.is_file():
        if accessories_json.stat().st_mtime < placements_json.stat().st_mtime:
            warnings.append(
                f"{accessories_json.name} is older than {placements_json.name}; "
                f"re-run skel_ext_resolve to refresh"
            )

    sidecar_source: Path | dict[str, Any] = (
        accessories_json if accessories_json.is_file() else placements_json
    )

    # Caliber-based class derivation.
    auto_derived_class: str | None = None
    if not class_override:
        try:
            auto_derived_class = _gp_autofill.derive_class_from_placements(sidecar_source)
        except Exception:
            pass

    # Auto-derive ship-id override for mesh-swap variants.
    if gameparams_ship_id is None:
        _variant_perm_for_id = pl_ship_block.get("variant_permoflage")
        if isinstance(_variant_perm_for_id, str) and _variant_perm_for_id:
            try:
                _ensure_gameparams_dump(config=cfg)
                derived = _gp_autofill.find_vehicle_by_native_permoflage(
                    _variant_perm_for_id,
                )
            except Exception:
                derived = None
            if derived:
                _pl_existing_index = pl_ship_block.get("param_index")
                if derived != _pl_existing_index and not (
                    isinstance(_pl_existing_index, str)
                    and derived.startswith(_pl_existing_index + "_")
                ):
                    gameparams_ship_id = derived

    placements_dict = _load_placements_with_id_override(
        sidecar_source if isinstance(sidecar_source, Path) else placements_json,
        gameparams_ship_id,
        cfg,
    )
    if placements_dict is not None:
        sidecar_source = placements_dict
        if not class_override:
            try:
                auto_derived_class = _gp_autofill.derive_class_from_placements(
                    placements_dict,
                )
            except Exception:
                pass

    doc = sidecar.new_document_from_placements(
        sidecar_source,
        class_override=class_override,
        auto_derived_class=auto_derived_class,
        ship_key_suffix=ship_key_suffix,
    )

    armor_data: dict[str, Any] | None = None
    # NOTE: this read is load-bearing even on a _refresh_sidecar pass
    # (skip_armor=True). new_document_from_placements stubs an EMPTY armor
    # block (source_glb=None, hidden_zones=[]) which then wins in the
    # final merge; re-reading armor.json here is what repopulates the real
    # source_glb + hidden_zones. (Empirically verified 2026-05-29: gating
    # this on `not skip_armor` dropped armor.hidden_zones + source_glb from
    # the refreshed sidecar.) Do NOT gate it on skip_armor.
    if armor_json_path.is_file():
        armor_data = json.loads(armor_json_path.read_text(encoding="utf-8"))
        doc = sidecar.merge_preserving(doc, {
            "armor": sidecar.make_armor(
                source_glb=hull_glb.name,
                materials_table=armor_data.get("materials_table", {}),
                zones=armor_data.get("zones", {}),
                hidden_zones=armor_data.get("hidden_zones", []),
            ),
        })

    # Skip-flag safety: any of skip_*_after_sidecar is only safe when an
    # existing sidecar is on disk to carry the corresponding section.
    if (skip_gameparams_autofill or skip_materials_skins or skip_geometry_hitbox) \
            and not sidecar_path.is_file():
        warnings.append(
            f"skip_gameparams_autofill / skip_materials_skins / skip_geometry_hitbox "
            f"requested but no existing sidecar at {sidecar_path.name}; "
            f"falling back to full regeneration"
        )
        skip_gameparams_autofill = False
        skip_materials_skins = False
        skip_geometry_hitbox = False

    # Ballistics absorb.
    if ballistics_json.is_file() and not skip_ammo:
        doc = sidecar.absorb_ballistics_json(doc, ballistics_json)

    # ── Step: geometry_hitbox ─────────────────────────────────────────
    if hull_glb.is_file() and not skip_geometry_hitbox:
        timer.start("geometry_hitbox", detail=hull_glb.name)
        try:
            geom, hitbox = sidecar.geometry_and_hitbox_from_hull_glb(hull_glb)
            doc = sidecar.merge_preserving(doc, {"geometry": geom})
            doc["hitbox"] = hitbox
        except Exception as e:
            timer.emit("geometry_hitbox", "failed")
            raise StepError(
                step="geometry_hitbox",
                underlying=e,
                detail="GLB walker failed",
            ) from e
        timer.complete()
    else:
        timer.skip("geometry_hitbox")

    # ── Step: gameparams_autofill ─────────────────────────────────────
    if not skip_gameparams_autofill:
        timer.start("gameparams_autofill")
        try:
            doc = _absorb_gameparams_passes(
                doc,
                ship=ship,
                config=cfg,
                toolkit_armor_data=armor_data,
                gameparams_ship_id=gameparams_ship_id,
                gm3d_dir=gm3d_dir,
                active_placements_json=placements_json,
                warnings=warnings,
            )
        except Exception as e:
            timer.emit("gameparams_autofill", "failed")
            raise StepError(
                step="gameparams_autofill",
                underlying=e,
                detail="autofill passes failed",
            ) from e
        timer.complete()
    else:
        timer.skip("gameparams_autofill")

    # ── Step: materials_skins ─────────────────────────────────────────
    if hull_glb.is_file() and not skip_materials_skins:
        timer.start("materials_skins")
        try:
            mats = sidecar.materials_from_glb(
                hull_glb,
                textures_dir=textures_dir if textures_dir.is_dir() else None,
                textures_dds_dir=textures_dds_dir if textures_dds_dir.is_dir() else None,
                material_mappings_json=material_mappings_json,
            )
            if mats:
                doc["materials"] = mats
                palette_resolver = _build_palette_resolver(
                    cfg, ship_name_hint=ship, warnings=warnings,
                )
                name_resolver: Callable[[str], str | None] | None
                try:
                    def name_resolver(pat: str) -> str | None:
                        return wg_camo.display_name_for_camo_entry(pat)
                except Exception as exc:
                    _warn(warnings, f"wg_localization unavailable: {exc}")
                    name_resolver = None
                doc["skins"] = sidecar.discover_skins_from_materials(
                    mats,
                    palette_resolver=palette_resolver,
                    name_resolver=name_resolver,
                )
                ship_id_for_perm = (
                    gameparams_ship_id
                    or (doc.get("ship") or {}).get("wg_ship_id")
                )
                if ship_id_for_perm:
                    known_patterns: set[str] = {
                        s.get("camo_pattern") for s in doc["skins"]
                        if s.get("camo_pattern")
                    }
                    permo_skins = _discover_permoflage_skins(
                        ship_id_for_perm,
                        config=cfg,
                        ship_name_hint=ship,
                        known_camo_patterns=known_patterns,
                        warnings=warnings,
                    )
                    if permo_skins:
                        doc["skins"].extend(permo_skins)
                        known_patterns.update(
                            s.get("camo_pattern") for s in permo_skins
                            if s.get("camo_pattern")
                        )

                    universal_skins = _discover_universal_skins(
                        ship_id_for_perm,
                        config=cfg,
                        ship_name_hint=ship,
                        known_camo_patterns=known_patterns,
                        warnings=warnings,
                    )
                    if universal_skins:
                        doc["skins"].extend(universal_skins)
        except Exception as e:
            timer.emit("materials_skins", "failed")
            raise StepError(
                step="materials_skins",
                underlying=e,
                detail="materials/skins discovery failed",
            ) from e
        timer.complete()
    else:
        timer.skip("materials_skins")

    # ── Preserve hand-authored edits on re-runs ───────────────────────
    if sidecar_path.is_file():
        try:
            existing = sidecar.read(sidecar_path)
            if not skip_materials_skins:
                existing["materials"] = []
                existing.pop("skins", None)
            existing.pop("texture_sets", None)
            if not skip_geometry_hitbox:
                existing.pop("hitbox", None)
            if not skip_ammo:
                existing.pop("ballistics", None)
            if not skip_gameparams_autofill:
                existing.pop("variants", None)
                existing.pop("hulls", None)
            new_inst_ids: dict[str, set[str]] = {
                sec: {
                    p.get("instance_id")
                    for p in (doc.get(sec) or [])
                    if isinstance(p, dict)
                }
                for sec in sidecar.PLACEMENT_SECTIONS
            }
            for sec in sidecar.PLACEMENT_SECTIONS:
                ex_items = existing.get(sec)
                if not isinstance(ex_items, list):
                    continue
                kept: list = []
                for p in ex_items:
                    if (
                        isinstance(p, dict)
                        and p.get("instance_id") in new_inst_ids[sec]
                    ):
                        kept.append(p)
                existing[sec] = kept
            if not skip_gameparams_autofill:
                ex_armor = existing.get("armor")
                if isinstance(ex_armor, dict):
                    ex_armor.pop("mount_armor", None)
                    ex_armor.pop("barbettes", None)

            old_ship_id = (existing.get("ship") or {}).get("wg_ship_id")
            new_ship_id = (doc.get("ship") or {}).get("wg_ship_id")
            if old_ship_id and new_ship_id and old_ship_id != new_ship_id:
                warnings.append(
                    f"Vehicle changed in this scaffold ({old_ship_id} -> "
                    f"{new_ship_id}); dropping prior placement arrays to "
                    f"prevent merge-stacking"
                )
                for sec in sidecar.PLACEMENT_SECTIONS:
                    existing.pop(sec, None)

            doc = sidecar.merge_preserving(existing, doc)
        except sidecar.SidecarSchemaError as e:
            warnings.append(f"existing sidecar not reusable ({e}); overwriting")

    # ── Per-mount accessory swap for mesh-swap permoflages ────────────
    variant_perm_for_swap = pl_ship_block.get("variant_permoflage")
    if not isinstance(variant_perm_for_swap, str):
        variant_perm_for_swap = None
    base_vehicle_for_swap = (
        gameparams_ship_id
        or (doc.get("ship") or {}).get("wg_ship_id")
    )
    if variant_perm_for_swap and base_vehicle_for_swap:
        try:
            swaps = _gp_autofill.resolve_variant_accessory_swaps(
                base_vehicle_for_swap,
                permoflage_id=variant_perm_for_swap,
            )
        except Exception as e:
            warnings.append(
                f"variant accessory-swap resolution failed ({e}); "
                f"base asset_ids retained"
            )
            swaps = {}
        has_swaps = any(
            (swaps or {}).get(k)
            for k in (
                "by_asset_id",
                "by_hp_name",
                "dead_by_hp_name",
                "misc_filter_by_hp",
            )
        )
        if has_swaps:
            # Library root: under workspace/libraries/accessories.
            _accessory_lib_root = workspace / "libraries" / "accessories"
            # Build the BASE vehicle's ``hp_name → asset_id`` map from the
            # raw placements_json (pre-swap aids). Feeds the by_hp_name
            # heal fallback in apply_variant_asset_swaps so a re-scaffold
            # against an already-swapped accessories.json can still
            # recover the source aid and land the Ry(180°) correction
            # for Azur/ARP nodesConfig-only variants (whose swap table
            # has empty by_asset_id). Without this map, ships like
            # Baltimore_Azur silently render their turrets 180° off on
            # re-scaffold.
            _base_aid_by_hp: dict[str, str] = {}
            for _section in sidecar.PLACEMENT_SECTIONS:
                for _entry in pl_doc.get(_section) or []:
                    if not isinstance(_entry, dict):
                        continue
                    _hp = _entry.get("hp_name")
                    _aid = _entry.get("asset_id")
                    if isinstance(_hp, str) and isinstance(_aid, str):
                        _base_aid_by_hp[_hp] = _aid
            doc, n_swapped, unused_keys = sidecar.apply_variant_asset_swaps(
                doc, swaps,
                library_root=_accessory_lib_root,
                base_aid_by_hp=_base_aid_by_hp,
            )
            variant_asset_set: set[str] = set()
            for _swap_key in ("by_asset_id", "by_hp_name", "dead_by_hp_name"):
                for vv in (swaps.get(_swap_key) or {}).values():
                    if vv:
                        variant_asset_set.add(vv)
            # Extend the opt-out set with bespoke attached children of
            # each variant-swapped parent. These are the variant-themed
            # decorative assets (AM6068_Cartridges_Hoshino,
            # AM6072_Rangefinder_Hoshino, Azur AM920_Rangefinder, etc.)
            # bundled inside the variant turret's `.skel_ext`. Without
            # this extension, _fold_variant_overlay_into_default lands
            # the variant's `mat_textures` onto the default skin and
            # consumers paint them over with the generic atlas, masking
            # the bespoke albedo. Identification by set-diff against the
            # base parent's attached children — robust against the
            # generic accessories (boats, ladders, ammo boxes) that the
            # variant SHOULD still inherit the camo wash on.
            for base_aid, variant_aid in (swaps.get("by_asset_id") or {}).items():
                if not isinstance(base_aid, str) or not isinstance(variant_aid, str):
                    continue
                variant_asset_set |= _bespoke_attached_children_for_swap(
                    _accessory_lib_root, base_aid, variant_aid,
                )
            for hp_name, variant_aid in (swaps.get("by_hp_name") or {}).items():
                if not isinstance(variant_aid, str):
                    continue
                base_aid = _base_aid_by_hp.get(hp_name) if isinstance(hp_name, str) else None
                if not isinstance(base_aid, str):
                    continue
                variant_asset_set |= _bespoke_attached_children_for_swap(
                    _accessory_lib_root, base_aid, variant_aid,
                )
            variant_asset_list = sorted(variant_asset_set)
            doc.setdefault("ship", {})["variant_swapped_asset_ids"] = variant_asset_list

            if unused_keys:
                warnings.append(
                    f"{len(unused_keys)} swap key(s) didn't match any placement"
                )

            # Also rewrite the merged accessories.json on disk.
            try:
                acc_path = gm3d_dir / f"{ship}_accessories.json"
                if acc_path.is_file():
                    with open(acc_path, "rb") as _af:
                        acc_doc = json.loads(_af.read().decode("utf-8"))
                    swapped_acc, n_acc_swapped, _unused_acc = (
                        sidecar.apply_variant_asset_swaps(
                            acc_doc, swaps,
                            library_root=_accessory_lib_root,
                            base_aid_by_hp=_base_aid_by_hp,
                        )
                    )
                    if n_acc_swapped:
                        with open(acc_path, "wb") as _af:
                            _af.write(
                                json.dumps(swapped_acc, indent=2, ensure_ascii=False)
                                .encode("utf-8")
                            )
            except Exception as e:
                warnings.append(
                    f"variant swap on accessories.json failed ({e}); "
                    f"webview may render base asset_ids"
                )

    # Fold variant overlay into default skin.
    _fold_variant_overlay_into_default(doc, placements_doc=pl_doc)

    # Strip phantom `misc_filter_mode` fields.
    for _section in sidecar.PLACEMENT_SECTIONS:
        for _entry in doc.get(_section) or []:
            if isinstance(_entry, dict):
                _entry.pop("misc_filter_mode", None)

    # Stamp engine-side camo opt-out set. Runs unconditionally so even
    # non-variant ships (no variant_swapped_asset_ids) record their
    # ``_9``-suffix themed accessories — those still ride along when a
    # camo / tile permoflage tries to paint the misc/gun category.
    try:
        _camo_skip = _camo_skip_asset_ids_from_doc(
            workspace / "libraries" / "accessories",
            doc,
        )
        doc.setdefault("ship", {})["camo_skip_asset_ids"] = sorted(_camo_skip)
    except Exception as e:
        warnings.append(f"camo_skip_asset_ids scan failed ({e})")

    # ── Step: emit_sidecar ────────────────────────────────────────────
    timer.start("emit_sidecar", detail=sidecar_path.name)
    try:
        sidecar.write(doc, sidecar_path)
    except Exception as e:
        timer.emit("emit_sidecar", "failed")
        raise StepError(
            step="emit_sidecar",
            underlying=e,
            detail=f"sidecar write to {sidecar_path} failed",
        ) from e
    timer.complete()

    # ── Native-permoflage auto-ingest ─────────────────────────────────
    # Compute variant_routed across both fresh-export and skip-export passes.
    if not variant_routed:
        _stamped = pl_ship_block.get("variant_permoflage")
        if isinstance(_stamped, str) and _stamped:
            variant_routed = True
            if variant_exterior_id is None:
                variant_exterior_id = _stamped
    if not skip_native_skin and not variant_routed:
        # Auto-ingest the Vehicle's ``nativePermoflage`` when peculiarity
        # is non-default (Arpeggio / Azur Lane / Sabaton / Kobayashi /
        # haunted / decorative). The toolkit's export-ship above sees
        # the base ship's A_Hull, so without this step the sidecar's
        # ``main`` scheme renders the wrong paint.
        #
        # Idempotent: skin_pack.ingest_skin_pack replaces a prior auto-
        # skin in place if re-run. Best-effort: any failure surfaces as
        # a warning and the scaffold continues with the bare-hull main
        # scheme intact.
        try:
            ship_id_native = (
                gameparams_ship_id
                or (doc.get("ship") or {}).get("wg_ship_id")
            )
            if ship_id_native:
                ship_dict_native = _gp_read.get_ship(ship_id_native)
                if ship_dict_native is not None:
                    native = ship_dict_native.get("nativePermoflage")
                    peculiarity = (
                        ship_dict_native.get("peculiarity") or "default"
                    )
                    if (
                        isinstance(native, str)
                        and native
                        and peculiarity != "default"
                    ):
                        from . import skin_pack as _skin_pack
                        try:
                            _skin_pack.ingest_skin_pack(
                                native,
                                ship_id=ship,
                                workspace=workspace,
                                config=cfg,
                                source_kind="vfs_variant",
                                on_event=on_event,
                            )
                        except Exception as exc:
                            warnings.append(
                                f"native-permoflage {native!r} ingest "
                                f"failed ({type(exc).__name__}: {exc}); "
                                f"sidecar carries the bare-hull main "
                                f"scheme. Retry via "
                                f"compose.skin_pack.ingest_skin_pack."
                            )
        except Exception:
            # Quiet — native-permoflage detection itself is best-effort.
            pass

    # Camo-coverage warning.
    camo_warn = _check_camo_coverage_gap(textures_dds_dir, doc)
    if camo_warn:
        warnings.append(camo_warn)

    # Vehicle-collision warning.
    coll_warn = _check_vehicle_collision(doc, config=cfg)
    if coll_warn:
        warnings.append(coll_warn)

    # PBR-coverage warning.
    untextured = _check_untextured_pbr_materials(doc)
    if untextured:
        detail = "; ".join(
            f"{r['material_id']} (intent={r['shader_intent']}, bound={r['bound']})"
            for r in untextured
        )
        warnings.append(
            f"{len(untextured)} material(s) render untextured "
            f"(opaque_pbr/cutout w/o baseColor in texture_sets[\"main\"]): "
            f"{detail}"
        )

    return ScaffoldResult(
        ship_id=ship,
        workspace_dir=ship_dir,
        hull_glb=hull_glb if hull_glb.is_file() else None,
        placements_json=placements_json if placements_json.is_file() else None,
        skel_ext_json=(
            skel_ext_candidates_json if skel_ext_candidates_json.is_file() else None
        ),
        material_mappings_json=(
            material_mappings_json if material_mappings_json.is_file() else None
        ),
        armor_json=armor_json_path if armor_json_path.is_file() else None,
        ammo_json=ballistics_json if ballistics_json.is_file() else None,
        sidecar_path=sidecar_path if sidecar_path.is_file() else None,
        textures_dds_dir=textures_dds_dir if textures_dds_dir.is_dir() else None,
        variant_routed=variant_routed,
        variant_permoflage=variant_exterior_id,
        warnings=tuple(warnings),
        step_timings_ms=dict(timer.step_timings_ms),
    )


# ---------------------------------------------------------------------------
# Variant-overlay fold (lifted)
# ---------------------------------------------------------------------------


def _fold_variant_overlay_into_default(
    doc: dict,
    *,
    placements_doc: dict[str, Any],
) -> None:
    """Copy the active variant permoflage's overlay onto the default skin."""
    skins = doc.get("skins")
    if not isinstance(skins, list) or not skins:
        return
    default = next((s for s in skins if s.get("skin_id") == "default"), None)
    if default is None:
        return
    if default.get("categories") or default.get("mat_textures"):
        return

    pl_ship = placements_doc.get("ship") if isinstance(placements_doc.get("ship"), dict) else {}
    variant_id = pl_ship.get("variant_permoflage")
    if not isinstance(variant_id, str) or not variant_id:
        return

    match = next((s for s in skins if s.get("exterior_id") == variant_id), None)
    if match is None:
        return

    cats = match.get("categories")
    mat = match.get("mat_textures")
    palette = match.get("color_scheme")
    if not cats and not mat:
        return

    if cats:
        default["categories"] = cats
    if mat:
        default["mat_textures"] = mat
    if palette:
        default["color_scheme"] = palette
    default["overlay_source"] = match.get("skin_id")


# ---------------------------------------------------------------------------
# Coverage / sanity checks (lifted; return warning strings rather than print)
# ---------------------------------------------------------------------------


def _check_camo_coverage_gap(textures_dds_dir: Path, doc: dict) -> str | None:
    """Return a warning string when camo DDS files extracted but no camo skin."""
    if not textures_dds_dir.is_dir():
        return None
    camo_files = sorted(
        f.name for f in textures_dds_dir.iterdir()
        if (f.is_file()
            and (f.name.endswith(".dd0") or f.name.endswith(".dds"))
            and "_camo_" in f.name.lower())
    )
    if not camo_files:
        return None
    skins = doc.get("skins") or []
    has_camo_skin = any(
        (s.get("skin_id") or "").startswith("camo_") for s in skins
    )
    if has_camo_skin:
        return None
    sample = ", ".join(camo_files[:5]) + ("..." if len(camo_files) > 5 else "")
    return (
        f"{len(camo_files)} _camo_* DDS file(s) in {textures_dds_dir.name}/ "
        f"but no camo skin in sidecar; likely a stem-classifier gap. "
        f"Files: {sample}"
    )


def _check_untextured_pbr_materials(doc: dict) -> list[dict]:
    """Return any opaque_pbr/cutout material missing baseColor."""
    out: list[dict] = []
    for mat in doc.get("materials", []):
        intent = mat.get("shader_intent")
        if intent not in ("opaque_pbr", "cutout"):
            continue
        main = (mat.get("texture_sets") or {}).get("main") or {}
        bound = sorted(main.keys())
        if "baseColor" in bound:
            continue
        out.append({
            "material_id": mat.get("material_id"),
            "shader_intent": intent,
            "bound": bound,
        })
    return out


def _check_vehicle_collision(doc: dict, *, config: PipelineConfig | None) -> str | None:
    """Warn if multiple Vehicles reference the same model_dir.

    Walks the (cached) GameParams dump via the streaming key reader to
    avoid the flat-load cost.  Returns a multi-line warning string when
    a tier collision is detected; ``None`` otherwise.
    """
    pipeline = doc.get("pipeline") or {}
    model_dir = pipeline.get("hull_glb_model_dir") or pipeline.get("model_dir")
    if not model_dir:
        wg_id = (doc.get("ship") or {}).get("wg_asset_id") or ""
        model_dir_lower = wg_id
    else:
        model_dir_lower = model_dir.lower()
    if not model_dir_lower:
        return None

    try:
        flat = _gp_read.load_full()
    except Exception:
        return None
    if not isinstance(flat, dict) or not flat:
        return None

    matching: list[dict] = []
    for _top_key, v in flat.items():
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo") or {}
        if ti.get("type") != "Ship":
            continue
        m = _model_dir_from_vehicle(v) or ""
        if m.lower() != model_dir_lower:
            continue
        matching.append({
            "param_index": str(v.get("index") or ""),
            "tier": v.get("level"),
            "permoflages": len(v.get("permoflages") or []),
            "is_premium": bool(v.get("isPremium", False)),
            "is_in_test": bool(v.get("isInTest", False)),
        })

    if len(matching) <= 1:
        return None
    tiers = {e["tier"] for e in matching if e["tier"] is not None}
    if len(tiers) <= 1:
        return None
    matching.sort(key=lambda e: (e["tier"] or 0, e["param_index"]))
    listing = "; ".join(
        f"{e['param_index']} tier {e['tier']}"
        for e in matching
    )
    return (
        f"{len(matching)} Vehicle GameParams share this model_dir at "
        f"different tiers ({sorted(tiers)}): {listing}. "
        f"Re-run with toolkit_ship=<param_index> to choose explicitly."
    )


def _model_dir_from_vehicle(v: dict[str, Any]) -> str | None:
    """Extract the hull model_dir from a Vehicle GameParam record."""
    components = _gp_autofill.resolve_components(v, hull_choice="upgraded")
    hull = components.get("hull") if isinstance(components, dict) else None
    if not isinstance(hull, dict):
        return None
    mp = hull.get("model")
    if not isinstance(mp, str):
        return None
    parts = mp.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[-1].endswith(".model"):
        return parts[-2]
    return None


__all__ = ["scaffold_ship"]
