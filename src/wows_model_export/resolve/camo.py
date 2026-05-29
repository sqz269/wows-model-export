"""WG camouflage pipeline transforms.

Lifted from ``tools/shared/wg_camo.py`` — the producer-side WG camo
pipeline.  Pure transforms over ``camouflages.xml`` + GameParams data
that synthesize the sidecar payload (per-camo categories, mat_albedo
textures, Path B ``<*_mgn>`` overrides, …).

What ends up here:

* The ``CamouflageDb`` parser — three primary indices (entries by
  camouflage name, color-scheme palette table, ship-group membership)
  plus a reverse mask-filename index for per-ship sidecar emit.
* The schema dataclasses — :class:`ColorScheme`, :class:`UvTransform`,
  :class:`MgnParams`, :class:`CamoEntry`.
* The classifier + tag normaliser
  (:func:`classify_part_category`, :func:`_tag_to_category`).
* Sidecar adapters (:func:`categories_for_entry`,
  :func:`mat_textures_for_entry`, :func:`tile_categories_for_entry`,
  :func:`path_b_categories_for_entry`,
  :func:`mat_textures_from_palette_entry`,
  :func:`palette_for_mask_paths`,
  :func:`mgn_params_to_json`).
* GameParams + VFS helpers (:func:`ensure_camouflages_xml`,
  :func:`read_vehicle_permoflages`, :func:`read_universal_exteriors`,
  :func:`display_name_for_camo_entry`,
  :func:`display_name_for_exterior`,
  :func:`ensure_camo_masks_for_entries`,
  :func:`ensure_mat_camo_textures`, :func:`list_extracted_mips`).

Layer 3 fit: the file-extracting helpers (``ensure_*``) cross into
Layer 2 by way of :func:`wows_model_export.toolkit.extract`; they're
the necessary bridge for getting masks + atlases on disk before the
adapter functions index them.  Everything else is a strict transform
over already-loaded data.

Open consumer-side work (NOT in scope here): the producer side of the
hybrid Path A + Path B path (596/3533 entries, ~17% of the camo
corpus) is shipped + visually validated; the matching full Path B
shader render in the downstream consumers still needs a ``catMgnMap``
uniform + per-channel mix.  See the user's memory
``project_camo_hybrid_path_ab`` for the consumer-side checklist.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PipelineConfig
from ..read import gameparams as _gp
from ..read import localization as _loc
from ..toolkit import extract as _toolkit_extract

# ---------------------------------------------------------------------------
# Cache + library locations
# ---------------------------------------------------------------------------

# Shared accessory camo masks live alongside the accessory library —
# fleet-wide, regenerated per game patch, referenced from sidecar
# ``Skin.categories[<cat>].mask.dds_mips`` paths.  In short, WG ships
# per-category shared tile/dazzle masks (``Plane_tile_camo_R.dds``,
# ``Dazzle_tile_camo_03.dds``, …) that the toolkit's stem-prefix lookup
# never sees, so we extract them ourselves into one canonical location.
#
# These are workspace-relative POSIX paths used inside the sidecar
# payload; the on-disk dir is resolved from ``PipelineConfig.workspace``
# in :func:`_masks_dir` / :func:`_mat_dir` below.
MASKS_BASE_DIR = "libraries/camo_masks"

# Mat-camo full-ship albedo atlases (``mat_<name>.dds``,
# ``mat_<name>_mgn.dds``, etc.) live in their own library dir for the
# same reason as ``camo_masks/``: they're shared across the fleet, not
# tied to any single ship.
MAT_BASE_DIR = "libraries/camo_mat"


def _cache_dir(config: PipelineConfig | None) -> Path:
    cfg = config or PipelineConfig.load()
    return cfg.require_cache_dir()


def _workspace(config: PipelineConfig | None) -> Path:
    cfg = config or PipelineConfig.load()
    return cfg.workspace


def _camo_xml_path(config: PipelineConfig | None) -> Path:
    return _cache_dir(config) / "camouflages.xml"


def _masks_dir(config: PipelineConfig | None) -> Path:
    return _workspace(config) / MASKS_BASE_DIR


def _mat_dir(config: PipelineConfig | None) -> Path:
    return _workspace(config) / MAT_BASE_DIR


# ---------------------------------------------------------------------------
# camouflages.xml cache
# ---------------------------------------------------------------------------

def ensure_camouflages_xml(
    *,
    refresh: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Ensure ``camouflages.xml`` is extracted to the workspace cache.

    Idempotent.  Pass ``refresh=True`` after a game patch to re-extract.
    The cache file lives at ``<config.cache_dir>/camouflages.xml`` (default
    ``<workspace>/.cache/camouflages.xml``).
    """
    cfg = config or PipelineConfig.load()
    cache_dir = cfg.require_cache_dir()
    camo_xml = cache_dir / "camouflages.xml"
    if camo_xml.is_file() and not refresh:
        return camo_xml
    print(
        f"[wg_camo] extracting camouflages.xml -> {camo_xml} (~9s)...",
        file=sys.stderr,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Glob extract; wowsunpack writes to <out_dir>/<full vfs path>, but
    # camouflages.xml is at the VFS root so the output lands at
    # <out_dir>/camouflages.xml directly.
    _toolkit_extract(["**/camouflages.xml"], out_dir=cache_dir, config=cfg)
    if not camo_xml.is_file():
        # Toolkit wrote it under the matched glob path — locate + move.
        for found in cache_dir.rglob("camouflages.xml"):
            if found != camo_xml:
                found.replace(camo_xml)
            break
    if not camo_xml.is_file():
        raise RuntimeError(
            f"wg_camo: camouflages.xml not found after extract under {cache_dir}"
        )
    return camo_xml


# ---------------------------------------------------------------------------
# GameParams-driven permoflage discovery
# ---------------------------------------------------------------------------

def read_vehicle_permoflages(
    ship_id: str,
    *,
    gameparams_path: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``Vehicle.permoflages`` for a ship, each resolved to camo metadata.

    ``ship_id`` may be the full GameParams entity key
    (``"PASB018_Iowa_1944"``) or just the param_index prefix
    (``"PASB018"``); :func:`wows_model_export.read.gameparams.resolve_ship_id`
    is used to expand the prefix when needed.  Returns a list of
    ``(exterior_id, camouflage_name, peculiarity)`` tuples, one per
    permoflage entry.  Empty list if the ship has no permoflages or
    isn't in GameParams.  ``camouflage_name`` may be empty (some
    Exteriors have no camo bound — e.g. flag-only or mesh-swap entries).

    Reads the per-process flat cache from
    :func:`wows_model_export.read.gameparams.load_full`; the first call
    in a process pays the parse cost (~30 s), every subsequent call is
    a dict access.
    """
    flat = _gp.load_full(path=gameparams_path) if gameparams_path else _gp.load_full()

    full_id = ship_id
    if "_" not in ship_id:
        # We already hold the parsed dict — resolve the prefix against its
        # top-level keys directly instead of round-tripping through
        # resolve_ship_id (which would re-open the cache / re-stream the
        # file). Same first-top-level-match predicate.
        needle = ship_id + "_"
        full_id = next(
            (k for k in flat if k == ship_id or k.startswith(needle)), None
        )
        if full_id is None:
            return []

    vehicle = flat.get(full_id)
    if not isinstance(vehicle, dict):
        return []
    permo_ids = vehicle.get("permoflages") or []
    if not isinstance(permo_ids, list):
        return []

    out: list[tuple[str, str, str]] = []
    for pid in permo_ids:
        if not isinstance(pid, str):
            continue
        ext = flat.get(pid)
        if isinstance(ext, dict):
            camo = ext.get("camouflage", "") or ""
            pec = ext.get("peculiarity", "") or ""
        else:
            camo = ""
            pec = ""
        out.append((pid, camo, pec))
    return out


def read_universal_exteriors(
    *,
    gameparams_path: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Walk every ``PCEC*``-prefixed ``Exterior`` in ``gameparams.json``
    and return ``(exterior_id, camouflage_name, peculiarity)`` tuples.

    ``PCEC*`` Exteriors are universal/fleet-wide camos — legendary
    anniversary patterns, holiday camos, ``Black_friday`` style entries,
    etc. — that any qualifying ship can equip.  Mirrors the toolkit's
    ``discover_universal_camo_schemes`` (``ship.rs:658``).

    Pair the result with :meth:`CamouflageDb.entries_for_ship` (or an
    explicit ``target_ships`` / ``ship_groups`` membership check) to
    filter to entries that actually target a specific ship; many
    universal exteriors apply only to a subset of nations / classes /
    tiers via ``<shipGroups>`` in ``camouflages.xml``.

    Returns ``[]`` if the cache isn't populated.  Reuses the per-process
    flat cache from
    :func:`wows_model_export.read.gameparams.load_full`, so the first
    call pays the parse cost and subsequent calls are dict scans.
    """
    flat = _gp.load_full(path=gameparams_path) if gameparams_path else _gp.load_full()
    out: list[tuple[str, str, str]] = []
    for k, v in flat.items():
        if not isinstance(k, str) or not k.startswith("PCEC"):
            continue
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Exterior":
            continue
        camo = v.get("camouflage", "") or ""
        pec = v.get("peculiarity", "") or ""
        out.append((k, camo, pec))
    return out


# ---------------------------------------------------------------------------
# Display-name resolution (camo entry → human-readable label)
# ---------------------------------------------------------------------------

# Preference order when multiple Exteriors reference the same
# `<camouflage>` block. PCEC = universal tinted camos (Patches /
# Stripes / …); PAES = per-ship paint-only Skin variants (Azur Lane,
# crossover skins); PCEM = mat_albedo permoflage Skins (Infernal,
# New Year, …); P*EM = nation-locked MSkin variants; P*EP = legacy
# permoflages (mixed catalogue coverage). Within a tier we walk
# in catalogue order, so the lowest-numbered Exterior wins ties —
# matches WG's UI which orders by sortOrder / id.
_EXT_PREFIX_PREFERENCE = (
    "PCEC",  # Camouflage (universal)
    "PAES", "PBES", "PFES", "PGES", "PHES", "PIES", "PJES",
    "PRES", "PSES", "PUES", "PVES", "PWES", "PXES", "PZES",
    "PCEM",  # MSkin (universal mat_*)
    "PAEM", "PBEM", "PGEM", "PJEM", "PREM", "PWEM", "PZEM",
    "PCED",  # ShipDestruction
    "PAEP", "PBEP", "PCEP", "PFEP", "PGEP", "PIEP", "PJEP",
    "PREP", "PSEP", "PUEP", "PVEP", "PWEP", "PZEP",  # Permoflage (legacy)
)


def _exterior_pref_rank(exterior_name: str) -> int:
    """Return a sort key: lower = preferred. Unknown prefixes sort last."""
    for i, p in enumerate(_EXT_PREFIX_PREFERENCE):
        if exterior_name.startswith(p):
            return i
    return len(_EXT_PREFIX_PREFERENCE)


# Cache: gameparams_path → {camouflage_name: [exterior_id, ...]} reverse
# index, sorted by preference + id. Built lazily on first display-name
# lookup, reused across the process. Invalidated implicitly when the
# underlying ``gameparams.load_full`` cache is invalidated.
_camo_to_exteriors_cache: dict[str, list[str]] | None = None


def _build_camo_to_exteriors_index(
    *, gameparams_path: Path | None = None,
) -> dict[str, list[str]]:
    """Walk every Exterior in GameParams and bucket them by their
    referenced camouflage entry name.

    Returns ``{camouflage_name: [exterior_id, ...]}`` with each list
    sorted by preference (most user-facing first).
    """
    flat = _gp.load_full(path=gameparams_path) if gameparams_path else _gp.load_full()
    out: dict[str, list[str]] = {}
    for k, v in flat.items():
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo")
        if not isinstance(ti, dict) or ti.get("type") != "Exterior":
            continue
        camo = v.get("camouflage")
        if not isinstance(camo, str) or not camo:
            continue
        out.setdefault(camo, []).append(k)
    for lst in out.values():
        lst.sort(key=lambda eid: (_exterior_pref_rank(eid), eid))
    return out


def display_name_for_camo_entry(
    camo_entry_name: str,
    *,
    lang: str = "en",
    fallback: str | None = None,
    gameparams_path: Path | None = None,
    config: PipelineConfig | None = None,
) -> str | None:
    """Resolve a ``<camouflage>.<name>`` to a localized display name.

    Walks ``camouflage_name → Exterior(s) → IDS_<exterior.upper()>`` in
    the gettext catalogue.  When multiple Exteriors reference the same
    camouflage, picks per :data:`_EXT_PREFIX_PREFERENCE` (universal
    Camouflage > per-ship Skin > MSkin > legacy Permoflage > others)
    with ID-order tiebreak.

    Returns ``fallback`` (default ``None``) on miss.  Failures are
    silent — missing GameParams cache, missing ``.mo`` file, or
    Exterior-with-no-IDS-key all degrade gracefully.
    """
    global _camo_to_exteriors_cache
    if not camo_entry_name:
        return fallback

    # Lazy-build the reverse index on first call.
    if _camo_to_exteriors_cache is None:
        try:
            _camo_to_exteriors_cache = _build_camo_to_exteriors_index(
                gameparams_path=gameparams_path,
            )
        except Exception as exc:
            print(f"[wg_camo] display-name index build failed: {exc}",
                  file=sys.stderr)
            _camo_to_exteriors_cache = {}

    exterior_ids = _camo_to_exteriors_cache.get(camo_entry_name) or []
    if not exterior_ids:
        return fallback

    try:
        cfg = config or PipelineConfig.load()
        game_dir = cfg.game_dir
        db = _loc.load(game_dir=game_dir, lang=lang)
    except Exception as exc:
        print(f"[wg_camo] localization load failed: {exc}", file=sys.stderr)
        return fallback

    for eid in exterior_ids:
        name = db.exterior_display_name(eid)
        if name:
            return name
    return fallback


def display_name_for_exterior(
    exterior_name: str,
    *,
    lang: str = "en",
    humanize_fallback: bool = True,
    config: PipelineConfig | None = None,
) -> str | None:
    """Look up an Exterior's localized display name directly.

    Thin convenience wrapper around
    :meth:`wows_model_export.read.localization.LocalizationDb.exterior_display_name`.
    With ``humanize_fallback=True`` (default), returns
    :func:`wows_model_export.read.localization.humanize_exterior_name`
    when the catalogue lookup misses; with ``humanize_fallback=False``
    returns ``None``.
    """
    if not exterior_name:
        return None
    try:
        cfg = config or PipelineConfig.load()
        game_dir = cfg.game_dir
        db = _loc.load(game_dir=game_dir, lang=lang)
    except Exception:
        return _loc.humanize_exterior_name(exterior_name) if humanize_fallback else None
    name = db.exterior_display_name(exterior_name)
    if name:
        return name
    return _loc.humanize_exterior_name(exterior_name) if humanize_fallback else None


# ---------------------------------------------------------------------------
# Camo-mask + mat_camo extraction
# ---------------------------------------------------------------------------

_MIP_SUFFIXES = (".dd0", ".dd1", ".dd2", ".dds")
_MIP_ORDER = {"dd0": 0, "dd1": 1, "dd2": 2, "dds": 3}
# Conservative chunk size for `wowsunpack extract` invocations. Each
# pattern is ~80-100 chars; Windows command-line cap is 8191 chars.
# 50 patterns × ~100 chars + flags + paths leaves comfortable headroom.
_EXTRACT_CHUNK = 50


def ensure_camo_masks_for_entries(
    entries: Iterable[CamoEntry],
    *,
    refresh: bool = False,
    include_hull: bool = False,
    skip_mat_camo: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Extract every mask DDS referenced by the given ``<camouflage>``
    entries to ``<workspace>/libraries/camo_masks/``, with full available
    mip chain.

    Idempotent: when ``refresh=False``, masks whose ``.dds`` already
    exists on disk are skipped.  Pass ``refresh=True`` after a game patch
    to re-extract everything.

    The extractor always asks for ``.dd0/.dd1/.dd2/.dds`` per mask;
    the toolkit silently skips suffixes the VFS doesn't have (most
    shared tile masks ship only ``.dds``, while per-asset masks like
    AGM034 carry the full chain).  ``flatten=True`` so files land
    directly under ``libraries/camo_masks/<basename>`` regardless of
    their VFS subdirectory.

    Hull / deckhouse / bulge masks are filtered out by default — the
    standard Phase A path covers them via per-ship ``_camo_NN.dd0``
    extraction into ``<Ship>/models/textures_dds/``.  ``include_hull=True``
    lifts the filter for tinted mat_* permoflages whose
    ``<Hull>``/``<DeckHouse>``/``<Bulge>`` reference shared textures
    (e.g. ``Black_gun_camo_01.dds``) that aren't shipped per-ship.
    """
    cfg = config or PipelineConfig.load()
    masks_dir = _masks_dir(cfg)
    masks_dir.mkdir(parents=True, exist_ok=True)

    paths: set[str] = set()
    for entry in entries:
        for tag, path in entry.textures.items():
            if not path:
                continue
            if not include_hull and _tag_to_category(tag) in HULL_CATEGORIES:
                continue
            if skip_mat_camo and _is_mat_camo_path(path):
                continue
            paths.add(path)
    if not paths:
        return masks_dir

    if not refresh:
        # Skip masks whose `.dds` is already cached. We treat the `.dds`
        # presence as the cache marker — if a mask has any extra mips,
        # they'd have been extracted in the same prior pass.
        already: set[str] = set()
        for path in paths:
            basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            stem = basename.rsplit(".", 1)[0]
            if (masks_dir / f"{stem}.dds").is_file():
                already.add(path)
        paths -= already
    if not paths:
        return masks_dir

    # Toolkit's `wowsunpack extract` only matches glob patterns rooted at
    # `**/<basename>` — full-path patterns and partial-globbed paths
    # silently match nothing (verified empirically). One pattern per
    # mask via `**/<stem>.dd*` covers the full mip chain in a single
    # match (.dd0/.dd1/.dd2/.dds in one go); the toolkit silently skips
    # suffixes that don't exist.
    patterns: list[str] = []
    for path in sorted(paths):
        basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = basename.rsplit(".", 1)[0]
        patterns.append(f"**/{stem}.dd*")

    print(
        f"[wg_camo] extracting {len(paths)} shared camo mask(s) -> {masks_dir}",
        file=sys.stderr,
    )
    for i in range(0, len(patterns), _EXTRACT_CHUNK):
        chunk = patterns[i:i + _EXTRACT_CHUNK]
        _toolkit_extract(chunk, out_dir=masks_dir, flatten=True, config=cfg)
    return masks_dir


def ensure_mat_camo_textures(
    entries: Iterable[CamoEntry],
    *,
    refresh: bool = False,
    only_mat_camo: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Extract every mat_* texture (and ``_mgn`` companion if present)
    referenced by the given ``<camouflage>`` entries to
    ``<workspace>/libraries/camo_mat/``, with full available mip chain.

    Mirrors :func:`ensure_camo_masks_for_entries` but doesn't filter out
    hull/deckhouse/bulge — for mat_*, those categories ARE the primary
    targets (they're full-ship albedo replacements, not accessory
    overlays).  Idempotent: skip if ``.dds`` already on disk unless
    ``refresh=True``.

    ``only_mat_camo=True`` restricts extraction to texture paths under
    ``content/.../mat_camo/`` (uniform-colour atlases / full-paint
    overlays).  Used by the hybrid mat_palette branch — the same entry
    can carry a real hull mask (``Black_gun_camo_01.dds`` belongs in
    ``camo_masks/``) plus a mat_camo accessory atlas (belongs here);
    routing them by path keeps each library dir semantically pure.

    Extracts ``_a`` (albedo), ``_mgn`` (MGN override), and ``_animmap``
    (emission timeline) textures together — all three share the same
    library dir since they're co-bound per camo.  The MGN textures and
    animmaps are sourced from ``entry.mgn_textures`` and
    ``entry.anim_maps`` respectively (populated by the parser from
    ``<Part_mgn>`` and ``<Part_animmap>`` sub-blocks under
    ``<Textures>``).
    """
    cfg = config or PipelineConfig.load()
    mat_dir = _mat_dir(cfg)
    mat_dir.mkdir(parents=True, exist_ok=True)

    paths: set[str] = set()
    for entry in entries:
        for path in entry.textures.values():
            if not path:
                continue
            if only_mat_camo and not _is_mat_camo_path(path):
                continue
            paths.add(path)
        # Path B companions: MGN + animmap textures referenced via
        # ``<Part_mgn>`` and ``<Part_animmap>`` sub-blocks. Same library
        # dir. NOTE: MGN/animmap textures are always extracted regardless
        # of ``only_mat_camo`` — they're full-paint Path B overrides that
        # always belong in ``camo_mat/``, even when the same camo's Path A
        # textures are in non-mat_camo paths (e.g. hybrid entries like
        # ``Ernst_Gaede_Pirate`` whose ``<Hull_mgn>`` references
        # ``content/gameplay/common/camouflage/textures/mat_Black_01_mgn.dds``).
        # The ``only_mat_camo`` filter only applies to Path A above, where
        # it routes regular hull masks to ``camo_masks/`` and atlases to
        # ``camo_mat/``.
        for path in list(entry.mgn_textures.values()) + list(entry.anim_maps.values()):
            if not path:
                continue
            paths.add(path)
    if not paths:
        return mat_dir

    if not refresh:
        already: set[str] = set()
        for path in paths:
            basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            stem = basename.rsplit(".", 1)[0]
            if (mat_dir / f"{stem}.dds").is_file():
                already.add(path)
        paths -= already
    if not paths:
        return mat_dir

    patterns: list[str] = []
    for path in sorted(paths):
        basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = basename.rsplit(".", 1)[0]
        patterns.append(f"**/{stem}.dd*")

    print(
        f"[wg_camo] extracting {len(paths)} mat_* texture(s) -> {mat_dir}",
        file=sys.stderr,
    )
    for i in range(0, len(patterns), _EXTRACT_CHUNK):
        chunk = patterns[i:i + _EXTRACT_CHUNK]
        _toolkit_extract(chunk, out_dir=mat_dir, flatten=True, config=cfg)
    return mat_dir


def list_extracted_mips(masks_dir: Path | None = None) -> dict[str, list[str]]:
    """Index ``masks_dir`` by stem → list of available mip filenames
    (``.dd0`` first, ``.dds`` last).  Pure read of the filesystem.

    Used by :func:`categories_for_entry` to emit only paths that are
    actually present on disk, so the webview's ``loadDdsMipChain``
    doesn't waste fetches on URLs that 404.

    Note: callers typically pass an explicit directory (the path
    returned by :func:`ensure_camo_masks_for_entries` /
    :func:`ensure_mat_camo_textures`).  The default ``None`` is kept
    for backwards-compat with the legacy zero-arg call style; it
    resolves to ``<cwd>/libraries/camo_masks``, which only matches the
    workspace layout when the cwd is the workspace root.
    """
    base = masks_dir if masks_dir is not None else Path(MASKS_BASE_DIR)
    out: dict[str, list[str]] = {}
    if not base.is_dir():
        return out
    for f in base.iterdir():
        if not f.is_file():
            continue
        name = f.name
        # Find the mip suffix; reject anything that isn't .dd0/.dd1/.dd2/.dds.
        stem, _, ext = name.rpartition(".")
        if not stem or ext not in _MIP_ORDER:
            continue
        out.setdefault(stem, []).append(name)
    for files in out.values():
        files.sort(key=lambda n: _MIP_ORDER.get(n.rsplit(".", 1)[1], 99))
    return out


# ---------------------------------------------------------------------------
# Classifier (port of toolkit's classify_part_category)
# ---------------------------------------------------------------------------

# Hull-side categories the per-stem `materials[i].texture_sets[<scheme>]`
# cascade already handles via §1ter Layer 1 (per-ship `_camo_NN.dds`
# files). The `Skin.categories` mechanism only fills the gap for
# accessory categories — secondaries, AAs, directors, planes, etc.
HULL_CATEGORIES = frozenset({"tile", "deckhouse", "bulge"})


def _is_mat_camo_path(path: str) -> bool:
    """True iff a VFS texture path points into the ``content/.../mat_camo/``
    subdirectory.

    These are pre-baked full-paint atlases (uniform colour or full art)
    that the WG runtime samples as **albedo** — multiplied or replaced
    over the base ship colour, NOT mask + palette composited.  Files
    outside ``mat_camo/`` (e.g. ``Black_gun_camo_01.dds``,
    ``Plane_tile_camo_R.dds``) ARE masks: the runtime samples them as
    R/G/B-zoned masks and looks up the corresponding palette colour
    per zone (see ``ship.ts::updateCamoUniforms`` step 1).

    The pipeline routes mat_camo/ paths to ``Skin.mat_textures``
    (consumed via the atlas-overlay shader chunk) and non-mat paths to
    ``Skin.categories`` (consumed via the mask + palette chunk).  One
    ``<camouflage>`` block can mix both — e.g. ``mat_Baltimore_Azur``
    uses ``Black_gun_camo_01.dds`` for ``<Hull>`` (mask) plus
    ``mat_camo/mat_Baltimore_Azur.dds`` for ``<Gun>``/``<Director>``/etc.
    (uniform light-cream atlas painted over base albedo).
    """
    return "/mat_camo/" in path.replace("\\", "/").lower()


def _tag_to_category(tag: str) -> str:
    """Normalize an XML tag name from ``<Textures>`` or ``<UV>`` to a
    canonical category (the same vocabulary
    :func:`classify_part_category` returns).

    Tiled vs non-tiled camos use slightly different vocabulary for the
    hull mask:

    * ``<Tile>`` — tiled-camo universal mask           → ``tile``
    * ``<Hull>`` — non-tiled camo hull-specific mask   → ``tile``

    All other tags are already canonical when lowercased
    (``DeckHouse`` → ``deckhouse``, ``Gun`` → ``gun``, …).
    """
    lc = tag.lower()
    return "tile" if lc == "hull" else lc


def classify_part_category(stem: str) -> str:
    """Classify an MFM stem (or asset_id) into a camouflage part category.

    Direct port of the toolkit's classifier at
    ``J:/PROG/test/wows-toolkit/.../camouflage.rs:59``.  Used to look up
    the per-mesh camo mask in a ``<camouflage><Textures>`` block::

        category = classify_part_category(asset_id)
        mask     = camo.textures.get(category.capitalize())  # XML uses CamelCase
        uv_xform = camo.uv_transforms.get(category, UvTransform())

    Categories returned (lowercase):
    ``tile`` (= hull), ``deckhouse``, ``bulge``, ``gun``, ``director``,
    ``misc``.  Falls back to ``tile`` when no rule matches — the runtime
    treats that as "use the hull mask," same default WG uses.

    Asset prefix conventions (2-letter nation + 2-letter category code +
    serial number, e.g. ``AGM034`` = US main gun #034):

    * ``?GM*`` / ``?GS*`` / ``?GA*`` — main / secondary / AA gun → ``gun``
    * ``?D0*`` / ``?D1*`` — directors                         → ``director``
    * ``?F0*`` / ``?F1*`` — rangefinders / fire-control       → ``director``
    * ``?RS*``           — radars / sensors                   → ``misc``

    No ``plane`` / ``float`` rules: floatplanes use the hull's
    ``<Plane>`` / ``<Float>`` masks via different mesh-name conventions
    (the toolkit's Rust classifier doesn't surface plane/float either —
    those categories appear in ``<Textures>`` but the runtime's
    mesh-classification path hits them via a separate code branch we
    haven't reverse-engineered yet).  For our purposes, treat
    catapult-launched aircraft as accessories that the consumer assigns
    ``plane`` / ``float`` via placement metadata, not via this classifier.
    """
    lower = stem.lower()
    if lower.endswith("_hull") or lower.endswith("_hull_wire"):
        return "tile"
    if lower.endswith("_deckhouse"):
        return "deckhouse"
    if "_bulge" in lower:
        return "bulge"
    if len(stem) >= 4 and stem[0].isupper():
        cat_code = stem[1:3]
        if cat_code in ("GM", "GS", "GA"):
            return "gun"
        if cat_code in ("D0", "D1", "F0", "F1"):
            return "director"
        if cat_code == "RS":
            return "misc"
        # Single-letter `M*` covers ~74% of the accessory library:
        # decorative meshes that the engine routes to part_index 6
        # (Misc-family). 269 AM*, 269 JM*, 96 RM*, plus BM/CM/FM/GM/etc.
        # Mostly hit at the per-placement category lookup (which uses
        # the VFS directory), but the classifier surface is exported as
        # public API so cover the fallback path too.
        if stem[1] == "M" and len(stem) >= 3 and stem[2].isdigit():
            return "misc"
        # Catapult equipment (`?C0*` / `?C1*`): engine has a distinct
        # `Catapult` part_index 4 entry; we collapse to `gun` (same
        # group, similar paint behaviour) until a catapult-specific
        # category is added downstream.
        if cat_code in ("C0", "C1"):
            return "gun"
        # `?GT*` torpedo gear → misc. Same group reasoning.
        if cat_code == "GT":
            return "misc"
    if "_hull" in lower:
        return "tile"
    return "tile"


# ---------------------------------------------------------------------------
# MGN-block helpers (used by the parser; defined ahead of CamoEntry so
# the dataclass type annotation can reference MgnParams).
# ---------------------------------------------------------------------------


def _parse_float(text: str | None, default: float) -> float:
    if not text:
        return default
    try:
        return float(text.strip())
    except ValueError:
        return default


def _parse_int(text: str | None, default: int) -> int:
    if not text:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default


def _parse_bool(text: str | None, default: bool) -> bool:
    if not text:
        return default
    return text.strip().lower() in ("true", "1")


def _parse_floats(text: str | None, count: int, default: tuple) -> tuple:
    if not text:
        return default
    parts = text.strip().split()
    out = []
    for i in range(count):
        try:
            out.append(float(parts[i]) if i < len(parts) else default[i])
        except (ValueError, IndexError):
            out.append(default[i])
    return tuple(out)


def _parse_mgn_params(node) -> MgnParams | None:
    """Extract Path B shader params from a ``<Part_mgn>`` XML node.

    Returns ``None`` when the node has no recognised child params (e.g.
    a bare ``<Hull_mgn>texture.dds</Hull_mgn>`` with no shader-param
    children).  Otherwise returns ``MgnParams`` with each missing field
    falling back to the dataclass default — matching WG's runtime
    behaviour where unspecified params keep the shader's compiled-in
    defaults.

    WG XML quirk: ``<camoAnimScale>`` and ``<camoMaskSpeed>`` are
    declared with 4 floats but the shader only consumes the first 3
    (``cb0[20].xyz``); we drop the 4th.  ``<camoMaskColor1>`` is 4
    floats in XML but shader-consumed as vec3 (alpha masked off);
    ``<camoMaskColor2>`` is full vec4.
    """
    # Quick sanity: do any of the recognised children exist? If not,
    # this _mgn block is likely just a texture pointer with no params.
    has_any = False
    for known_tag in (
        "Influence_n", "Influence_g", "Influence_m", "Influence_ao",
        "useCamoMaskGlobal", "camoMode",
        "camoEmissionAnimationMode", "camoEmissionColorMode",
        "camoEmissionBasePower", "camoEmissionAnimationMaxPower",
        "camoMaskSmooth", "camoAnimScale", "camoMaskSpeed",
        "camoMaskColor1", "camoMaskColor2",
    ):
        if node.find(known_tag) is not None:
            has_any = True
            break
    if not has_any:
        return None

    return MgnParams(
        influence_n=                       _parse_float(node.findtext("Influence_n"), 1.0),
        influence_g=                       _parse_float(node.findtext("Influence_g"), 1.0),
        influence_m=                       _parse_float(node.findtext("Influence_m"), 1.0),
        influence_ao=                      _parse_float(node.findtext("Influence_ao"), 0.0),
        use_camo_mask_global=              _parse_bool(node.findtext("useCamoMaskGlobal"), False),
        camo_mode=                         _parse_int(node.findtext("camoMode"), -1),
        camo_emission_animation_mode=      _parse_int(node.findtext("camoEmissionAnimationMode"), 0),
        camo_emission_color_mode=          _parse_int(node.findtext("camoEmissionColorMode"), 0),
        camo_emission_base_power=          _parse_float(node.findtext("camoEmissionBasePower"), 0.0),
        camo_emission_animation_max_power= _parse_float(node.findtext("camoEmissionAnimationMaxPower"), 0.0),
        camo_mask_smooth=                  _parse_float(node.findtext("camoMaskSmooth"), 1.0),
        camo_anim_scale=                   _parse_floats(node.findtext("camoAnimScale"), 3, (1.0, 1.0, 1.0)),
        camo_mask_speed=                   _parse_floats(node.findtext("camoMaskSpeed"), 3, (0.1, 0.1, 0.5)),
        camo_mask_color1=                  _parse_floats(node.findtext("camoMaskColor1"), 3, (1.0, 0.0, 0.0)),
        camo_mask_color2=                  _parse_floats(node.findtext("camoMaskColor2"), 4, (1.0, 1.0, 0.0, 1.0)),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColorScheme:
    """4-color RGBA palette (linear space) parsed from a ``<colorScheme>``
    block.  Floats; alpha may be < 1.0 for partial-transparency hints.
    """
    name:   str
    colors: tuple[tuple[float, float, float, float],
                  tuple[float, float, float, float],
                  tuple[float, float, float, float],
                  tuple[float, float, float, float]]


@dataclass(frozen=True)
class UvTransform:
    """Per-category UV transform from a ``<camouflage>``'s ``<UV>`` block.
    ``scale`` multiplies the base UV; ``offset`` shifts after scale.
    Defaults to identity ``([1,1], [0,0])`` when a category is missing
    from the XML.  Mirrors the toolkit's ``UvTransform`` at
    ``J:/PROG/test/wows-toolkit/.../camouflage.rs:26``.
    """
    scale:  tuple[float, float] = (1.0, 1.0)
    offset: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class MgnParams:
    """Path B (``ship_camo_mgn_material.fx``) per-part shader parameters
    parsed from a ``<Part_mgn>`` sub-block.  Mirrors the camo CB fields
    read by ``makeCamoMaterial`` and consumed by the MGN-variant pixel
    shader.

    See ``reference/topics/camo/wg_camo_shader_reference.md`` Path B
    for the full blend equation.  Defaults match the shader's "no
    override" state: ``camo_mode = -1`` is the runtime DEFAULT.id-style
    "no camo" path that the C++ side substitutes when a part isn't in
    ``params.parts``.
    """
    influence_n:                       float = 1.0
    influence_g:                       float = 1.0
    influence_m:                       float = 1.0
    influence_ao:                      float = 0.0
    use_camo_mask_global:              bool  = False
    camo_mode:                         int   = -1
    camo_emission_animation_mode:      int   = 0
    camo_emission_color_mode:          int   = 0
    camo_emission_base_power:          float = 0.0
    camo_emission_animation_max_power: float = 0.0
    camo_mask_smooth:                  float = 1.0
    # 3-vec (x,y,z); WG XML has 4 floats but only the first 3 are used
    # by the shader (cb0[20].xyz). We drop the 4th per shader reflection.
    camo_anim_scale:                   tuple[float, float, float] = (1.0, 1.0, 1.0)
    camo_mask_speed:                   tuple[float, float, float] = (0.1, 0.1, 0.5)
    camo_mask_color1:                  tuple[float, float, float] = (1.0, 0.0, 0.0)
    camo_mask_color2:                  tuple[float, float, float, float] = (1.0, 1.0, 0.0, 1.0)


@dataclass
class CamoEntry:
    """One ``<camouflage>`` block.  Multiple entries may share a name (the
    XML keys ship-group variants under the same name); we keep them all
    in a list keyed by name (see ``CamouflageDb.entries``).
    """
    name:           str
    tiled:          bool
    # Direct ship-index references via ``<targetShip>`` siblings (one tag
    # per ship). Used by per-ship permoflages like ``camo_permanent_1``.
    # 28% of camo blocks use this form (verified empirically). Mutually
    # disambiguating with ``ship_groups`` — most blocks use one or the
    # other; ~5 use both as a redundant double-association.
    target_ships:   list[str] = field(default_factory=list)
    # Indirect ship-set references via ``<shipGroups>`` text (single tag,
    # space-separated group names like ``USN_group_2 GER_group_2``).
    # Used by fleet-wide ``mat_*`` schemes that apply across many
    # ships. 62% of camo blocks use this form. Resolution requires
    # cross-referencing ``CamouflageDb.ship_groups``.
    ship_groups:    list[str] = field(default_factory=list)
    color_schemes:  list[str] = field(default_factory=list)   # roll references in order
    # Maps part-category tag → mask VFS path. Tags are XML-original case
    # (Hull, DeckHouse, Gun, …) so we keep callers unsurprised; consumers
    # match by basename, not category.
    textures:       dict[str, str] = field(default_factory=dict)
    # Maps part-category tag (lowercased to match
    # `classify_part_category` output) → UvTransform from ``<UV>``
    # block. WG runtime samples ``mask`` at ``vMapUv * scale + offset``
    # per category — critical for tiled-pattern accessories where the
    # `Plane_tile_camo_R.dds`-style file repeats at the per-camo scale.
    # Categories absent from the XML are NOT inserted; consumers fall
    # back to identity ``UvTransform()`` themselves.
    uv_transforms:  dict[str, UvTransform] = field(default_factory=dict)
    # Path B per-part overrides. Populated from ``<Part_mgn>`` sub-blocks
    # under ``<Textures>``. The bare text content of ``<Part_mgn>`` is the
    # MGN texture VFS path; the child elements (``<Influence_*>``,
    # ``<camoMode>``, ``<useCamoMaskGlobal>``, etc.) populate ``mgn_params``.
    # Empty for Path A camos (palette + zoned mask) — consumers should
    # check ``mgn_params`` presence to discriminate. Tags are XML-original
    # case minus the ``_mgn`` suffix (so ``Hull_mgn`` → ``mgn_textures["Hull"]``).
    mgn_textures:   dict[str, str] = field(default_factory=dict)
    mgn_params:     dict[str, MgnParams] = field(default_factory=dict)
    # Path B per-part emission-animation timeline texture. Bound to the
    # shader's ``camoAnimMap`` slot when the camo enables emissive
    # animation (``camoEmissionAnimationMode != 0``). Tags are XML-original
    # case minus the ``_animmap`` suffix.
    anim_maps:      dict[str, str] = field(default_factory=dict)


class CamouflageDb:
    """Parsed ``camouflages.xml``.  Three primary indices:

    * ``entries``       — name → list of ``CamoEntry`` (one per ship-group variant)
    * ``color_schemes`` — name → ``ColorScheme`` (palette definitions)
    * ``ship_groups``   — group name → set of ship-index strings (e.g.
      ``"USN_group_2"`` → ``{"PASD002_Sampson_1917", ...}``).  Built from
      the top-level ``<shipgroups.xml>`` block; consumed when resolving
      ``<shipGroups>``-style camo references (see :meth:`entries_for_ship`).

    Plus derived indices:

    * ``by_mask_filename`` — mask basename → list of camo entries that
      reference it.  Used by per-ship sidecar emit (reverse-lookup which
      camo references this ship's mask file, sidesteps GameParams
      pickle parsing).
    """

    def __init__(self) -> None:
        self.entries: dict[str, list[CamoEntry]] = {}
        self.color_schemes: dict[str, ColorScheme] = {}
        self.ship_groups: dict[str, set[str]] = {}
        self.by_mask_filename: dict[str, list[CamoEntry]] = {}

    # -- Construction ------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        refresh: bool = False,
        config: PipelineConfig | None = None,
    ) -> CamouflageDb:
        xml_path = path or ensure_camouflages_xml(refresh=refresh, config=config)
        return cls.parse(xml_path)

    @classmethod
    def parse(cls, xml_path: Path) -> CamouflageDb:
        # ElementTree handles 5-15 MB XML in ~1s. We load once per
        # scaffold run — re-load is cached at the caller level.
        root = ET.parse(xml_path).getroot()
        db = cls()

        # 0. Ship groups — top-level <shipgroups.xml> block defining
        #    named clubs of ships used by mat_* camo schemes that span
        #    the fleet. Each group is a child element whose tag is the
        #    group name (e.g. <USN_group_2>) containing one <ships>
        #    sub-element with whitespace-separated ship-index strings.
        #
        #    ElementTree localname-only lookup: the tag is
        #    `shipgroups.xml` (note the dot in the tag name; legal in
        #    XML) so we walk root's direct children to find it.
        sg_root = next(
            (c for c in root if c.tag == "shipgroups.xml"),
            None,
        )
        if sg_root is not None:
            for group_node in sg_root:
                group_name = group_node.tag
                if not group_name:
                    continue
                ships_text = (group_node.findtext("ships") or "").strip()
                if not ships_text:
                    continue
                db.ship_groups[group_name] = set(ships_text.split())

        # 1. ColorSchemes — top-level <colorScheme> blocks. Each has
        #    <name>, <color0>..<color3> as space-separated RGBA floats.
        for cs_node in root.iter("colorScheme"):
            name = (cs_node.findtext("name") or "").strip()
            if not name:
                continue
            colors = []
            for i in range(4):
                txt = (cs_node.findtext(f"color{i}") or "").strip()
                parts = txt.split()
                if len(parts) >= 4:
                    try:
                        colors.append((
                            float(parts[0]), float(parts[1]),
                            float(parts[2]), float(parts[3]),
                        ))
                    except ValueError:
                        colors.append((0.0, 0.0, 0.0, 1.0))
                else:
                    colors.append((0.0, 0.0, 0.0, 1.0))
            db.color_schemes[name] = ColorScheme(
                name=name,
                colors=(colors[0], colors[1], colors[2], colors[3]),
            )

        # 2. Camouflage entries — <camouflage> blocks anywhere in the
        #    document. Same name may appear multiple times; we collect.
        for camo_node in root.iter("camouflage"):
            name = (camo_node.findtext("name") or "").strip()
            if not name:
                continue
            tiled = (camo_node.findtext("tiled") or "").strip().lower() == "true"

            # <Textures> child — one element per part category. Three
            # naming conventions cohabit:
            #   <Part>            — albedo / mask texture (Path A or B)
            #   <Part_mgn>        — Path B MGN override + shader params
            #                       (text = MGN texture path, children =
            #                        <Influence_*>, <camoMode>, etc.)
            #   <Part_animmap>    — Path B emission-animation timeline
            textures:     dict[str, str] = {}
            mgn_textures: dict[str, str] = {}
            mgn_params:   dict[str, MgnParams] = {}
            anim_maps:    dict[str, str] = {}
            tex_root = camo_node.find("Textures")
            if tex_root is not None:
                for child in tex_root:
                    tag = child.tag
                    if tag.endswith("_mgn"):
                        part_tag = tag[:-len("_mgn")]
                        # Bare text is the MGN texture path (before any
                        # child elements). ElementTree's .text covers
                        # only leading text — exactly what we want.
                        path = (child.text or "").strip()
                        if path:
                            mgn_textures[part_tag] = path
                        params = _parse_mgn_params(child)
                        if params is not None:
                            mgn_params[part_tag] = params
                        continue
                    if tag.endswith("_animmap"):
                        part_tag = tag[:-len("_animmap")]
                        path = (child.text or "").strip()
                        if path:
                            anim_maps[part_tag] = path
                        continue
                    text = (child.text or "").strip()
                    if text:
                        textures[tag] = text

            # <UV> child — per-category scale/offset transforms. Same
            # category vocabulary as <Textures>, lowercased to match the
            # output of `classify_part_category`. Per-camo data; same
            # shape on every color-roll variant of the same camo block.
            uv_transforms: dict[str, UvTransform] = {}
            uv_root = camo_node.find("UV")
            if uv_root is not None:
                for child in uv_root:
                    tag = child.tag.lower()
                    scale_text = (child.findtext("scale") or "").strip()
                    offset_text = (child.findtext("offset") or "").strip()
                    scale_parts = scale_text.split()
                    offset_parts = offset_text.split()
                    try:
                        sx = float(scale_parts[0]) if len(scale_parts) >= 1 else 1.0
                        sy = float(scale_parts[1]) if len(scale_parts) >= 2 else 1.0
                    except ValueError:
                        sx = sy = 1.0
                    try:
                        ox = float(offset_parts[0]) if len(offset_parts) >= 1 else 0.0
                        oy = float(offset_parts[1]) if len(offset_parts) >= 2 else 0.0
                    except ValueError:
                        ox = oy = 0.0
                    uv_transforms[tag] = UvTransform(scale=(sx, sy), offset=(ox, oy))

            # <colorSchemes> — multiple sibling blocks, one per roll. The
            # toolkit's Rust parser at camouflage.rs:191-196 takes only
            # the first via split_whitespace().next(), losing alt rolls;
            # we collect ALL siblings here. The scheme name is text
            # before the inner <colorUI> child (XML quirk: name is bare
            # text-content of the parent, sibling to <colorUI>).
            color_schemes: list[str] = []
            for cs_ref in camo_node.findall("colorSchemes"):
                # The text content of <colorSchemes> includes everything
                # not inside child elements — strip + split, take first
                # word (the scheme name).
                text_content = (cs_ref.text or "").strip()
                if text_content:
                    first_token = text_content.split()[0]
                    color_schemes.append(first_token)

            # <targetShip> — multiple sibling blocks listing ship param
            # names (e.g. PASB017_Montana_1945).
            targets: list[str] = []
            for ts in camo_node.findall("targetShip"):
                txt = (ts.text or "").strip()
                if txt:
                    targets.append(txt)

            # <shipGroups> — single tag, whitespace-separated group names
            # like "USN_group_2 GER_group_2". Resolution against
            # `db.ship_groups` happens at lookup time
            # (:meth:`entries_for_ship`) — keep the raw list here so
            # consumers can also enumerate which groups a camo applies to.
            sg_text = (camo_node.findtext("shipGroups") or "").strip()
            sg_list = sg_text.split() if sg_text else []

            entry = CamoEntry(
                name=name,
                tiled=tiled,
                target_ships=targets,
                ship_groups=sg_list,
                color_schemes=color_schemes,
                textures=textures,
                uv_transforms=uv_transforms,
                mgn_textures=mgn_textures,
                mgn_params=mgn_params,
                anim_maps=anim_maps,
            )
            db.entries.setdefault(name, []).append(entry)

            # 3. Reverse mask-filename index. For each texture path,
            #    bucket by basename so we can resolve a ship's mask file
            #    back to its camo entry without walking Vehicle.permoflages.
            for tex_path in textures.values():
                fname = tex_path.rsplit("/", 1)[-1]
                db.by_mask_filename.setdefault(fname, []).append(entry)

        return db

    # -- Lookup -----------------------------------------------------------

    def color_scheme(self, name: str) -> ColorScheme | None:
        return self.color_schemes.get(name)

    def find_entry_by_mask_filename(
        self, filename: str, *, ship_name_hint: str | None = None,
    ) -> CamoEntry | None:
        """Find a camo entry that references the given mask filename.

        Filename is the basename (e.g. ``ASB017_Montana_camo_01.dds``);
        path prefix doesn't matter.

        Multiple `<camouflage>` blocks may reference the same mask file
        (e.g. Iowa's mask is shared between the base Iowa permoflage and
        the Pan-Asian "Xuan_Wu" Iowa skin, each with its own
        `<colorSchemes>`).  When ``ship_name_hint`` is provided we prefer
        the entry whose ``target_ships`` substring-match the hint
        (case-insensitive), falling back to the first entry that has a
        non-empty ``color_schemes`` list, then to the literal first hit.
        """
        hits = self.by_mask_filename.get(filename)
        if not hits:
            return None
        if ship_name_hint:
            needle = ship_name_hint.lower()
            for h in hits:
                if any(needle in t.lower() for t in h.target_ships):
                    return h
        for h in hits:
            if h.color_schemes:
                return h
        return hits[0]

    def find_entry_by_name(
        self,
        name: str,
        *,
        ship_index: str | None = None,
    ) -> CamoEntry | None:
        """Look up a ``<camouflage>`` block by its ``<name>`` text.

        Multiple blocks may share the same name when WG ships per-ship-
        group variants (e.g. ``mat_250_NAVY`` has 11 blocks, one per
        atlas-size class).  When ``ship_index`` is provided, prefer the
        variant whose ``shipGroups`` cover the ship; otherwise fall back
        to the **catch-all** variant (one with empty ``target_ships``
        AND empty ``ship_groups``), and only as a last resort return the
        first parsed block.  Direct port of the toolkit's
        ``CamouflageDb::get`` (camouflage.rs:266).

        Most PCEC*-targeted multi-block entries (Navy_day, Black_friday,
        anniversary patterns) author one block per ship-group bracket
        plus a catch-all block at the start; the catch-all carries
        "default" UV/textures the runtime applies to ships that don't
        match any explicit group.
        """
        hits = self.entries.get(name)
        if not hits:
            return None
        # Defensive: even single-hit entries must respect targetShip /
        # shipGroups filtering. A camo block authored exclusively for
        # ship X (target_ships={X} or ship_groups={GX}) shouldn't apply
        # to ship Y just because no other block named the camo. Today
        # the live corpus never authors a single targeted block without
        # a sibling catch-all so this branch is defensive; the moment a
        # future patch ships a PCEC*-only camo block, the OLD early-
        # return would silently mis-paint every non-matching ship.
        if len(hits) == 1:
            h = hits[0]
            if ship_index and (h.target_ships or h.ship_groups):
                if ship_index in h.target_ships:
                    return h
                for grp in h.ship_groups:
                    members = self.ship_groups.get(grp)
                    if members and ship_index in members:
                        return h
                return None
            return h
        if ship_index:
            for h in hits:
                if ship_index in h.target_ships:
                    return h
                for grp in h.ship_groups:
                    members = self.ship_groups.get(grp)
                    if members and ship_index in members:
                        return h
        # Fallback: prefer the catch-all (empty target_ships AND
        # empty ship_groups) so ships not in any explicit group still
        # render with the camo's "default" authoring; otherwise first.
        for h in hits:
            if not h.target_ships and not h.ship_groups:
                return h
        return hits[0]

    def entries_for_ship(self, ship_index: str) -> list[CamoEntry]:
        """Return every ``CamoEntry`` that applies to a given ship index
        (e.g. ``"PASB017_Montana_1945"``), resolving both association
        forms:

        1. ``entry.target_ships`` — direct membership (per-ship permoflages)
        2. ``entry.ship_groups`` — indirect via group definitions
           (fleet-wide ``mat_*`` schemes)

        Order: targetShip-only matches first (more-specific), then
        shipGroups matches.  Within each, original parse order is
        preserved (which is XML document order).

        Empirical distribution of camo blocks (camouflages.xml as of
        2026-04-29):

        * 28% use ``<targetShip>`` only (per-ship permoflages)
        * 62% use ``<shipGroups>`` only (mat_* fleet-wide)
        *  0% use both (only ~5 blocks)
        * 10% use neither (tier-default tile camos with no XML
          association — applied via runtime tier matching, not via this XML)
        """
        out: list[CamoEntry] = []
        seen: set[int] = set()
        # Pass 1: direct targetShip matches.
        for entries in self.entries.values():
            for e in entries:
                if id(e) in seen:
                    continue
                if ship_index in e.target_ships:
                    out.append(e)
                    seen.add(id(e))
        # Pass 2: indirect shipGroups matches.
        for entries in self.entries.values():
            for e in entries:
                if id(e) in seen:
                    continue
                for group_name in e.ship_groups:
                    members = self.ship_groups.get(group_name)
                    if members and ship_index in members:
                        out.append(e)
                        seen.add(id(e))
                        break
        return out

    def resolve_palettes(self, entry: CamoEntry) -> list[ColorScheme]:
        """Resolve every ``<colorSchemes>`` reference in an entry to its
        full palette.  Skips refs whose name doesn't match a defined
        ``<colorScheme>`` block (rare; would indicate XML inconsistency).
        """
        out: list[ColorScheme] = []
        for cs_name in entry.color_schemes:
            cs = self.color_schemes.get(cs_name)
            if cs is not None:
                out.append(cs)
        return out


# ---------------------------------------------------------------------------
# Sidecar adapter
# ---------------------------------------------------------------------------

def categories_for_entry(
    entry: CamoEntry,
    extracted_mips: dict[str, list[str]],
    *,
    masks_base_dir: str = MASKS_BASE_DIR,
    include_hull: bool = False,
    skip_mat_camo: bool = False,
    mat_extracted_mips: dict[str, list[str]] | None = None,
    mat_base_dir: str = MAT_BASE_DIR,
) -> dict[str, dict]:
    """Project a ``CamoEntry``'s ``<Textures>`` + ``<UV>`` blocks into
    the shape consumed by sidecar ``Skin.categories``::

        {
          "<category>": {
            "mask":     { "dds_mips": ["libraries/camo_masks/<file>.dd0", ...] },
            "uv":       { "scale": [sx, sy], "offset": [ox, oy] },
            "mgn":      { "dds_mips": [...] },           # optional, Path B
            "anim_map": { "dds_mips": [...] },           # optional, Path B
            "params":   { ...MgnParams as JSON... }      # optional, Path B
          }
        }

    When ``mat_extracted_mips`` is provided, Path B fields (``mgn`` /
    ``anim_map`` / ``params``) are attached to category records when the
    entry has matching ``<Part_mgn>`` / ``<Part_animmap>`` blocks.  This
    captures hybrid Path A + Path B entries (~17% of the camo corpus —
    see ``project_camo_hybrid_path_ab.md``) where the engine selects
    Path B per-part when ``_mgn`` exists for that part.  Consumers should
    prefer ``mgn`` over ``mask`` when both are present, matching the
    engine's per-part selector test at ``+0x188 + part*0xc0`` in
    ``makeCamoMaterial``.

    Without ``mat_extracted_mips``, only the Path A ``mask`` + ``uv``
    fields are emitted (legacy behaviour).

    By default skips hull / deckhouse / bulge categories — those are
    emitted via the per-stem ``materials[i].texture_sets[<scheme>]``
    cascade in §1ter Layer 1, not via ``Skin.categories``.  Only
    "accessory" categories the layer-1 path misses appear in the result:
    ``gun``, ``director``, ``plane``, ``float``, ``misc``, plus ``tile``
    when a tiled-camo entry uses ``<Tile>`` for the universal mask.

    ``include_hull=True`` lifts the filter so tinted mat_* permoflages
    can express their full per-category mask set (Hull/DeckHouse/Bulge
    plus the accessory categories) in one ``Skin.categories`` block.
    The webview's existing category-mask cascade picks them up without
    shader changes — see ``ship.ts::updateCamoUniforms`` step 1.

    ``skip_mat_camo=True`` filters out any category whose texture path
    points into ``content/.../mat_camo/`` — those atlases are albedo
    overlays (sampled and multiplied/replaced over base, NOT mask +
    palette composited).  They belong in ``Skin.mat_textures`` instead;
    use :func:`mat_textures_from_palette_entry` to build that block.
    Hybrid mat_palette entries (e.g. ``mat_Baltimore_Azur`` =
    Black_gun_camo_01 hull mask + mat_camo accessory atlas) call
    this with ``skip_mat_camo=True`` so the categories block carries
    only the real masks.

    ``extracted_mips`` should come from
    :func:`list_extracted_mips` after
    :func:`ensure_camo_masks_for_entries` populated the dir (call with
    matching ``include_hull`` flag).  Categories whose mask isn't
    present on disk (extractor failure or mask missing in VFS) are
    dropped — sidecar consumers rely on the dict containing only
    ready-to-fetch paths.
    """
    out: dict[str, dict] = {}
    for tag, vfs_path in entry.textures.items():
        cat = _tag_to_category(tag)
        if not include_hull and cat in HULL_CATEGORIES:
            continue
        if skip_mat_camo and _is_mat_camo_path(vfs_path):
            continue
        basename = vfs_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        stem = basename.rsplit(".", 1)[0]
        mip_files = extracted_mips.get(stem)
        if not mip_files:
            continue
        mips = [f"{masks_base_dir}/{f}" for f in mip_files]
        uv = entry.uv_transforms.get(cat, UvTransform())
        record: dict = {
            "mask": {"dds_mips": mips},
            "uv":   {
                "scale":  list(uv.scale),
                "offset": list(uv.offset),
            },
        }
        # Path B attachment — surfaced when the caller has extracted
        # mat_camo/ textures and provided their mip index. The XML tag
        # (e.g. "Hull") keys ``mgn_textures`` / ``anim_maps`` / ``mgn_params``
        # on the parsed entry.
        if mat_extracted_mips is not None:
            mgn_path = entry.mgn_textures.get(tag)
            if mgn_path:
                mgn_mips = _resolve_mips(mgn_path, mat_extracted_mips, mat_base_dir)
                if mgn_mips:
                    record["mgn"] = {"dds_mips": mgn_mips}
            anim_path = entry.anim_maps.get(tag)
            if anim_path:
                anim_mips = _resolve_mips(anim_path, mat_extracted_mips, mat_base_dir)
                if anim_mips:
                    record["anim_map"] = {"dds_mips": anim_mips}
            params = entry.mgn_params.get(tag)
            if params is not None:
                record["params"] = mgn_params_to_json(params)
        out[cat] = record
    return out


def mat_textures_from_palette_entry(
    entry: CamoEntry,
    extracted_mips: dict[str, list[str]],
    *,
    mat_base_dir: str = MAT_BASE_DIR,
) -> dict[str, dict]:
    """Companion to :func:`categories_for_entry` for hybrid mat_palette
    entries: project ONLY the ``mat_camo/`` atlas paths into a
    ``Skin.mat_textures`` block.

    Same emission shape as :func:`mat_textures_for_entry`
    (``albedo`` slot per category) but filters to ``mat_camo/`` paths
    only — leaves the real-mask paths (e.g. ``Black_gun_camo_01.dds``
    for hull, ``Plane_tile_camo_R.dds`` for plane) to the categories
    block where palette compositing is correct.

    Used by ``_emit_permoflage_skins``'s ``mat_palette`` branch to
    split a hybrid entry into two blocks::

        skin.categories    = {tile/deckhouse/...}     # masks
        skin.mat_textures  = {gun/director/.../bulge} # mat_camo atlases

    Without this split, ``mat_Baltimore_Azur``'s 4×4 uniform-cream
    mat_camo atlas would have been bound as a "mask" with USNP17
    palette compositing — producing a wrong tinted result instead of
    the white overlay WG actually renders.
    """
    out: dict[str, dict] = {}
    for tag, vfs_path in entry.textures.items():
        if tag.endswith("_mgn") or tag.endswith("_animmap"):
            continue
        if not _is_mat_camo_path(vfs_path):
            continue
        cat = _tag_to_category(tag)
        mips = _resolve_mips(vfs_path, extracted_mips, mat_base_dir)
        if not mips:
            continue
        uv = entry.uv_transforms.get(cat, UvTransform())
        record: dict = {
            "albedo": {"dds_mips": mips},
            "uv":     {
                "scale":  list(uv.scale),
                "offset": list(uv.offset),
            },
        }
        # Path B extras (rare on hybrid mat_palette but possible).
        mgn_path = entry.mgn_textures.get(tag)
        if mgn_path:
            mgn_mips = _resolve_mips(mgn_path, extracted_mips, mat_base_dir)
            if mgn_mips:
                record["mgn"] = {"dds_mips": mgn_mips}
        anim_path = entry.anim_maps.get(tag)
        if anim_path:
            anim_mips = _resolve_mips(anim_path, extracted_mips, mat_base_dir)
            if anim_mips:
                record["anim_map"] = {"dds_mips": anim_mips}
        params = entry.mgn_params.get(tag)
        if params is not None:
            record["params"] = mgn_params_to_json(params)
        out[cat] = record
    return out


# Default vocabulary of part categories tile permoflages get broadcast
# across. Matches the webview's classifier output (`classifyPartCategory`
# + `classifyPlacementCategory`) — every category a mesh can be tagged
# as in the renderer. Tile permoflages broadcast a single shared mask
# across all of these so any classified mesh picks up the paint, with
# per-category UV from the camo's ``<UV>`` block (identity when absent).
#
# ``wire`` covers WG's per-mesh ``SHIPWIRE_PBS_*`` material family
# (antennas / rigging / jib lines / fightin' lights) — distinct from the
# accessory's gameplay category. Engine renders these via the
# ``Wire*`` part_index 9 entries in the runtime lookup table. Our
# consumers currently classify accessories per-asset (not per-material)
# so the wire branch only fires when an asset's gameplay-category itself
# is ``wire``; emitting the broadcast entry now keeps the producer
# output engine-faithful ahead of a per-material consumer split.
TILE_BROADCAST_CATEGORIES: tuple[str, ...] = (
    "tile", "deckhouse", "bulge",
    "gun", "director", "plane", "float", "misc", "wire",
)


def path_b_categories_for_entry(
    entry: CamoEntry,
    mat_extracted_mips: dict[str, list[str]],
    *,
    mat_base_dir: str = MAT_BASE_DIR,
    include_hull: bool = True,
) -> dict[str, dict]:
    """Project a ``CamoEntry``'s Path B blocks ONLY (no Path A mask) into
    the shape consumed by sidecar ``Skin.categories``::

        {
          "<category>": {
            "mgn":      { "dds_mips": [...] },           # required
            "uv":       { "scale": [sx, sy], "offset": [ox, oy] },
            "anim_map": { "dds_mips": [...] },           # optional
            "params":   { ...MgnParams as JSON... }      # optional
          }
        }

    Iterates ``entry.mgn_textures`` (NOT ``entry.textures``), so it
    captures pure-Path-B parts (e.g. ``Ernst_Gaede_Pirate``'s
    ``<Director_mgn>``/``<Float_mgn>``/etc., where there's no
    matching ``<Director>``/``<Float>``).  Used by the
    hull_palette emit branch in ``scaffold_ship.py`` to
    surface Path B data for non-mat_* hybrid entries — Phase A's
    per-stem ``materials[i].texture_sets[<scheme>]`` cascade
    independently covers Path A on hull stems, so these Skin records
    deliberately omit ``mask``.

    ``include_hull=True`` by default (the opposite of
    :func:`categories_for_entry`) — for non-mat_* hull_palette
    entries, Phase A only handles the per-ship hull albedos; the
    shared Path B hull texture (e.g. ``mat_250_NAVY_mgn.dds``)
    needs to flow through here too.  Set ``include_hull=False`` to
    restrict to accessory parts.

    Records without a resolvable MGN texture (extractor failure or
    VFS gap) are silently dropped.  Records with no MGN params and
    no animmap fall through with just ``mgn`` + ``uv`` — that's
    the "shader runs Path B with default uniforms" case.
    """
    out: dict[str, dict] = {}
    for tag, vfs_path in entry.mgn_textures.items():
        cat = _tag_to_category(tag)
        if not include_hull and cat in HULL_CATEGORIES:
            continue
        mgn_mips = _resolve_mips(vfs_path, mat_extracted_mips, mat_base_dir)
        if not mgn_mips:
            continue
        uv = entry.uv_transforms.get(cat, UvTransform())
        record: dict = {
            "mgn": {"dds_mips": mgn_mips},
            "uv": {
                "scale":  list(uv.scale),
                "offset": list(uv.offset),
            },
        }
        anim_path = entry.anim_maps.get(tag)
        if anim_path:
            anim_mips = _resolve_mips(anim_path, mat_extracted_mips, mat_base_dir)
            if anim_mips:
                record["anim_map"] = {"dds_mips": anim_mips}
        params = entry.mgn_params.get(tag)
        if params is not None:
            record["params"] = mgn_params_to_json(params)
        out[cat] = record
    return out


def tile_categories_for_entry(
    entry: CamoEntry,
    extracted_mips: dict[str, list[str]],
    *,
    masks_base_dir: str = MASKS_BASE_DIR,
    broadcast_categories: tuple[str, ...] = TILE_BROADCAST_CATEGORIES,
    include_hull: bool = False,
    mat_extracted_mips: dict[str, list[str]] | None = None,
    mat_base_dir: str = MAT_BASE_DIR,
) -> dict[str, dict]:
    """Project a tile-permoflage ``CamoEntry`` (single ``<Tile>`` mask
    plus per-category ``<UV>``) into a sidecar ``Skin.categories`` dict
    that broadcasts the same tile mask across every part category.

    Differs from :func:`categories_for_entry`: the source has ONE mask
    file (``<Tile>``) plus a ``<UV>`` block listing per-category
    transforms.  We emit one ``Skin.categories`` entry per broadcast
    category, all sharing the same ``mask.dds_mips`` but each with its
    own UV from ``<UV>`` (or identity when the category isn't listed).

    Hull-side categories (``tile``/``deckhouse``/``bulge``) are skipped
    by default — the per-stem ``materials[i].texture_sets[<scheme>]``
    cascade (Layer 1) already covers the hull, both for fleet-wide tile
    camos (toolkit emits per-stem variant materials for every hull
    stem) and for variant-routed bespoke crossovers (the bespoke
    ``ASB077_Iowa_AzurLane`` style albedo lives in
    ``texture_sets["main"]``).  Re-emitting them here would double-tint
    the hull: the consumer's category-mask path runs at higher priority
    than the per-stem cascade (``ship.ts::updateCamoUniforms`` step 1
    vs step 2), so a categories block claiming the hull stem clobbers
    its bespoke variant texture with a generic ``R_gun_camo_01`` overlay.
    Pass ``include_hull=True`` to lift the filter for entries where the
    tile mask IS the only hull mask (no per-stem cascade exists).

    Mirrors the toolkit's ``bake_tiled_camo_png()`` recipe (texture.rs:131)
    where the tile mask is sampled per-stem with the stem's classified
    category UV.

    Falls back to ``<Hull>`` if no ``<Tile>`` is present (some entries
    use the Hull tag for the universal mask).  Returns ``{}`` when neither
    is present, or when the mask file isn't on disk in
    ``libraries/camo_masks/`` (extractor failure or VFS gap).

    Webview consumes the result through the existing
    ``Skin.categories`` cascade in ``ship.ts::updateCamoUniforms`` —
    every classified mesh hits step 1 (category match) and binds the
    tile mask at the right UV.  No shader changes needed.
    """
    universal_path = entry.textures.get("Tile") or entry.textures.get("Hull")
    if not universal_path:
        return {}
    basename = universal_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    mip_files = extracted_mips.get(stem)
    if not mip_files:
        return {}
    mips = [f"{masks_base_dir}/{f}" for f in mip_files]

    # Build a category-to-XML-tag reverse index from the entry's mgn/
    # anim/params dicts so we can attach Path B per broadcast category.
    # Tags in those dicts are XML-original case (e.g. "DeckHouse"); the
    # broadcast categories are lowercase (`_tag_to_category` output).
    mgn_by_cat: dict[str, str] = {}
    anim_by_cat: dict[str, str] = {}
    params_by_cat: dict[str, MgnParams] = {}
    if mat_extracted_mips is not None:
        for tag, path in entry.mgn_textures.items():
            mgn_by_cat[_tag_to_category(tag)] = path
        for tag, path in entry.anim_maps.items():
            anim_by_cat[_tag_to_category(tag)] = path
        for tag, p in entry.mgn_params.items():
            params_by_cat[_tag_to_category(tag)] = p

    out: dict[str, dict] = {}
    for cat in broadcast_categories:
        if not include_hull and cat in HULL_CATEGORIES:
            continue
        uv = entry.uv_transforms.get(cat, UvTransform())
        record: dict = {
            "mask": {"dds_mips": mips},
            "uv":   {
                "scale":  list(uv.scale),
                "offset": list(uv.offset),
            },
        }
        if mat_extracted_mips is not None:
            mgn_path = mgn_by_cat.get(cat)
            if mgn_path:
                mgn_mips = _resolve_mips(mgn_path, mat_extracted_mips, mat_base_dir)
                if mgn_mips:
                    record["mgn"] = {"dds_mips": mgn_mips}
            anim_path = anim_by_cat.get(cat)
            if anim_path:
                anim_mips = _resolve_mips(anim_path, mat_extracted_mips, mat_base_dir)
                if anim_mips:
                    record["anim_map"] = {"dds_mips": anim_mips}
            p = params_by_cat.get(cat)
            if p is not None:
                record["params"] = mgn_params_to_json(p)
        out[cat] = record
    return out


def mgn_params_to_json(p: MgnParams) -> dict:
    """Serialise ``MgnParams`` to a sidecar-friendly JSON dict.

    Field names use the snake_case form already canonicalised on the
    dataclass; consumers can dispatch on ``params.camo_mode`` and feed
    the rest verbatim into the
    ``ship_camo_mgn_material.fx`` constant buffer (see
    ``reference/topics/camo/wg_camo_shader_reference.md`` Path B for
    the full mapping).

    The ``mgn_influence`` shader uniform is a vec3 packing
    ``(influence_m, influence_g, influence_n)``. The DXBC RE of
    `ship_camo_mgn_material.fx` (see
    `reference/topics/camo/camo_path_b_makecamomaterial_re.md §1`)
    confirmed cb0[17].w is read by no PS chunk — the 4th slot is dead
    and dropped from the contract.
    """
    return {
        "camo_mode":              p.camo_mode,
        "use_camo_mask_global":   p.use_camo_mask_global,
        "mgn_influence":          [p.influence_m, p.influence_g, p.influence_n],
        "ao_influence":           p.influence_ao,
        "emission_anim_mode":     p.camo_emission_animation_mode,
        "emission_color_mode":    p.camo_emission_color_mode,
        "emission_base_power":    p.camo_emission_base_power,
        "emission_anim_max_power": p.camo_emission_animation_max_power,
        "mask_smooth":            p.camo_mask_smooth,
        "anim_scale":             list(p.camo_anim_scale),
        "mask_speed":             list(p.camo_mask_speed),
        "mask_color1":            list(p.camo_mask_color1),
        "mask_color2":            list(p.camo_mask_color2),
    }


def _resolve_mips(vfs_path: str, extracted_mips: dict[str, list[str]],
                  base_dir: str) -> list[str] | None:
    """Resolve a VFS path to a list of on-disk mip URLs, or None when
    the file isn't extracted.  Helper for the emit functions; matches
    ``categories_for_entry``'s mip-cascade logic.
    """
    if not vfs_path:
        return None
    basename = vfs_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    mip_files = extracted_mips.get(stem)
    if not mip_files:
        return None
    return [f"{base_dir}/{f}" for f in mip_files]


def mat_textures_for_entry(
    entry: CamoEntry,
    extracted_mips: dict[str, list[str]],
    *,
    mat_base_dir: str = MAT_BASE_DIR,
) -> dict[str, dict]:
    """Project a mat_* ``CamoEntry``'s ``<Textures>`` + ``<UV>`` blocks
    into the shape consumed by sidecar ``Skin.mat_textures``::

        {
          "<category>": {
            "albedo":   { "dds_mips": ["libraries/camo_mat/<file>.dd0", ...] },
            "mgn":      { "dds_mips": [...] },     # optional, Path B
            "anim_map": { "dds_mips": [...] },     # optional, Path B
            "uv":       { "scale": [sx, sy], "offset": [ox, oy] },
            "params":   { ...MgnParams as JSON... }  # optional, Path B
          }
        }

    Differs from :func:`categories_for_entry` in three ways:

    1. No hull-category filter — mat_* paints the entire ship, so
       ``tile``/``deckhouse``/``bulge`` are first-class entries here.
    2. The ``_mgn`` / ``_animmap`` sub-blocks of ``<Textures>`` are NOT
       skipped — when present, their texture paths land in ``mgn`` /
       ``anim_map`` and the ``<Part_mgn>``-embedded shader params land
       in ``params`` (see :func:`mgn_params_to_json`).  Consumers
       discriminate on ``params`` presence: Path A (no params) vs
       Path B (full ``camo_mode``/``mgn_influence``/etc. paramset).
    3. Emits ``"albedo"`` instead of ``"mask"`` to make the sidecar
       reader's discriminator obvious — these are pre-baked albedos,
       not zone masks.

    Categories absent from the entry's ``<Textures>`` block are dropped;
    the consumer falls back to the per-stem base albedo for those.
    """
    out: dict[str, dict] = {}
    for tag, vfs_path in entry.textures.items():
        cat = _tag_to_category(tag)
        mips = _resolve_mips(vfs_path, extracted_mips, mat_base_dir)
        if not mips:
            continue
        uv = entry.uv_transforms.get(cat, UvTransform())
        record: dict = {
            "albedo": {"dds_mips": mips},
            "uv":     {
                "scale":  list(uv.scale),
                "offset": list(uv.offset),
            },
        }
        # Path B extras: same XML tag (e.g. "Hull") keys mgn_textures /
        # anim_maps / mgn_params on the parsed CamoEntry. Surface them
        # when present so the consumer can pick the MGN shader path.
        mgn_path = entry.mgn_textures.get(tag)
        if mgn_path:
            mgn_mips = _resolve_mips(mgn_path, extracted_mips, mat_base_dir)
            if mgn_mips:
                record["mgn"] = {"dds_mips": mgn_mips}
        anim_path = entry.anim_maps.get(tag)
        if anim_path:
            anim_mips = _resolve_mips(anim_path, extracted_mips, mat_base_dir)
            if anim_mips:
                record["anim_map"] = {"dds_mips": anim_mips}
        params = entry.mgn_params.get(tag)
        if params is not None:
            record["params"] = mgn_params_to_json(params)
        out[cat] = record
    return out


def palette_for_mask_paths(
    db: CamouflageDb,
    mask_paths: Iterable[str],
    *,
    ship_name_hint: str | None = None,
) -> tuple[CamoEntry | None, list[ColorScheme]]:
    """Find the camo entry + resolved palettes for a list of mask paths.

    ``mask_paths`` are sidecar-style relative paths (e.g.
    ``textures_dds/ASB017_Montana_camo_01.dds``).  We match by basename.
    Returns the first hit's entry + its rolls; ``(None, [])`` if no
    mask resolves.

    ``ship_name_hint`` disambiguates between multiple `<camouflage>`
    blocks that share a mask file — see
    :meth:`CamouflageDb.find_entry_by_mask_filename`.
    """
    for path in mask_paths:
        fname = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        entry = db.find_entry_by_mask_filename(fname, ship_name_hint=ship_name_hint)
        if entry is None:
            continue
        return entry, db.resolve_palettes(entry)
    return None, []


__all__ = [
    # Library-relative base dirs (workspace-relative POSIX paths for sidecar payload)
    "MASKS_BASE_DIR",
    "MAT_BASE_DIR",
    "HULL_CATEGORIES",
    "TILE_BROADCAST_CATEGORIES",
    # Schema dataclasses
    "ColorScheme",
    "UvTransform",
    "MgnParams",
    "CamoEntry",
    # Parser
    "CamouflageDb",
    # Cache + GameParams discovery
    "ensure_camouflages_xml",
    "read_vehicle_permoflages",
    "read_universal_exteriors",
    # Display-name resolution
    "display_name_for_camo_entry",
    "display_name_for_exterior",
    # Mask + atlas extraction
    "ensure_camo_masks_for_entries",
    "ensure_mat_camo_textures",
    "list_extracted_mips",
    # Classifier
    "classify_part_category",
    # Sidecar adapters
    "categories_for_entry",
    "mat_textures_from_palette_entry",
    "path_b_categories_for_entry",
    "tile_categories_for_entry",
    "mat_textures_for_entry",
    "mgn_params_to_json",
    "palette_for_mask_paths",
]
