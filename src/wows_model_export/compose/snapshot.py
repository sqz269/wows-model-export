"""Single-pass dump of every Vehicle + permoflage for the Extract picker.

Lifted from ``tools/extract/snapshot.py`` (private I:-side warships repo).
The I:-side module also subsumed ``tools/extract/list_ships.py`` and
``tools/extract/list_permoflages.py``; those CLIs are not re-lifted here
and the migration plan retires them.

Layer 4 (composer) — reads :func:`wows_model_export.read.gameparams.load_full`
(the cached flat GameParams dict), walks every ``Ship`` entity, joins to
``Exterior`` records, classifies camo topology against
``camouflages.xml``, and emits the picker payload as JSON.

Output shape (stable contract for the Extract webview):

```
{
  "vehicles": [
    { "param_index": "...",  "top_key": "...",  "display_name": "...",
      "model_dir": "...",  "tier": 9,  "nation": "usa",  "species": "...",
      "class": "BB",  "is_premium": false,  "permoflages_count": 7,
      "shares_model_dir_with": [...],  "group": "...",
      "armaments": [...], "vfs_status": "ok", "peculiarities": [...]
    },
    ...
  ],
  "permoflages_by_vehicle": {
    "<top_key>": [
      { "exterior_id": "...", "display_name": "...", "camouflage": "...",
        "peculiarity": "...", "topology": "...", "is_native": false,
        "mesh_swap_dir": "...", "category_textures": {...} },
      ...
    ]
  },
  "peculiarity_labels": {
    "<peculiarity>": {
      "label": "...", "source": "...",
      "sample_names": [...], "exterior_count": N
    }
  },
  "summary": {
    "vehicle_count": N,
    "permoflage_count": M,
    "ships_with_permoflages": K
  }
}
```

VFS health classification (the ``vfs_status`` field) is gated on a
``wowsunpack metadata`` JSON dump. The dump is cached at
``<cache_dir>/vfs_meta.json``; when missing or stale relative to the
GameParams cache, this composer reuses the existing
:func:`wows_model_export.toolkit.vfs.metadata_json` toolkit wrapper to
refresh it. Failure surfaces as ``vfs_status="unknown"`` per Vehicle —
the picker degrades gracefully.

Canonical :class:`StepEvent` step names:

    "ensure_gameparams"   "enumerate_vehicles"   "enumerate_permoflages"
    "join_metadata"       "write_snapshot"

Each step emits ``started`` -> ``completed`` (or ``failed``).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp
from ..read import localization as _localization
from ..resolve import camo as wg_camo
from ..resolve import gameparams_autofill as _gp_autofill
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from ..toolkit.vfs import metadata_json as _vfs_metadata_json
from ..types import OnEvent, SnapshotResult, StepEvent

# Cached VFS metadata location (relative to PipelineConfig.cache_dir).
_VFS_META_FILENAME = "vfs_meta.json"
_GAMEPARAMS_FILENAME = "gameparams.json"

# Species → 2-char class code. Mirrors the sidecar's species map.
_SPECIES_TO_CLASS = {
    "Destroyer":  "DD",
    "Cruiser":    "CA",
    "Battleship": "BB",
    "AirCarrier": "CV",
    "Submarine":  "SS",
}

# Vehicle top-level component-key tail → canonical armament tag.
_ARMAMENT_TAGS: dict[str, str] = {
    "Artillery":         "main",
    "ATBA":              "secondary",
    "AirDefense":        "aa",
    "AirDefence":        "aa",
    "AirDedense":        "aa",
    "Torpedoes":         "torpedoes",
    "Missiles":          "missiles",
    "AirArmament":       "aircraft",
    "Fighter":           "aircraft",
    "DiveBomber":        "aircraft",
    "TorpedoBomber":     "aircraft",
    "TorpedoBomder":     "aircraft",
    "SkipBomber":        "aircraft",
    "DepthChargeGuns":   "depth_charges",
    "DepthCharge":       "depth_charges",
    "PingerGun":         "pinger",
    "Lasers":            "lasers",
    "WaveArtillery":     "wave_artillery",
    "SubTorpedoes":      "sub_torpedoes",
}

# ShipUpgradeInfo.<entry>.components keys (lowercase) → canonical tag.
_COMPONENT_LIST_TAGS: dict[str, str] = {
    "artillery":     "main",
    "atba":          "secondary",
    "airDefense":    "aa",
    "airDefence":    "aa",
    "torpedoes":     "torpedoes",
    "missiles":      "missiles",
    "airArmament":   "aircraft",
    "depthCharges":  "depth_charges",
    "pinger":        "pinger",
}

# Topology tags mirror scaffold_ship._TOPO_*.
_TOPO_MESH_SWAP      = "mesh_swap"
_TOPO_MAT_ALBEDO     = "mat_albedo"
_TOPO_MAT_PALETTE    = "mat_palette"
_TOPO_HULL_PALETTE   = "hull_palette"
_TOPO_TILE_BROADCAST = "tile_broadcast"
_TOPO_OTHER          = "other"

# Targeted overrides for franchise names the heuristic can't recover
# from Exterior display names alone.
_PECULIARITY_OVERRIDES: dict[str, str] = {
    "startrek":         "Star Trek",
    "highSchoolFleet":  "High School Fleet",
    "logh":             "LOGH",
    "rangers":          "Power Rangers",
    "ch_dragons":       "LNY Dragons",
}


# ---------------------------------------------------------------------------
# Event helper
# ---------------------------------------------------------------------------


class _StepRunner:
    """Records per-step wall time + emits ``StepEvent`` boundaries."""

    def __init__(self, on_event: OnEvent | None) -> None:
        self.on_event = on_event
        self.t0 = time.monotonic()
        self.spans: dict[str, float] = {}

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0

    def emit(
        self,
        step: str,
        state: str,
        *,
        detail: str = "",
        step_ms: float | None = None,
        data: dict | None = None,
    ) -> None:
        if self.on_event is None:
            return
        ev = StepEvent(
            step=step,
            state=state,  # type: ignore[arg-type]
            detail=detail,
            elapsed_ms=self._elapsed_ms(),
            step_ms=step_ms,
            data=data,
        )
        try:
            self.on_event(ev)
        except Exception:
            pass

    def step(self, name: str, detail: str = ""):
        return _StepCtx(self, name, detail)


class _StepCtx:
    def __init__(self, runner: _StepRunner, step: str, detail: str) -> None:
        self.runner = runner
        self.step = step
        self.detail = detail
        self.t_start = 0.0
        self.completed_detail = ""
        self.completed_data: dict | None = None

    def __enter__(self) -> _StepCtx:
        self.t_start = time.monotonic()
        self.runner.emit(self.step, "started", detail=self.detail)
        return self

    def annotate(self, detail: str, data: dict | None = None) -> None:
        self.completed_detail = detail
        if data is not None:
            self.completed_data = data

    def __exit__(self, exc_type, exc, tb) -> bool:
        step_ms = (time.monotonic() - self.t_start) * 1000.0
        self.runner.spans[self.step] = step_ms
        if exc is None:
            self.runner.emit(
                self.step, "completed",
                detail=self.completed_detail or self.detail,
                step_ms=step_ms, data=self.completed_data,
            )
            return False
        self.runner.emit(
            self.step, "failed",
            detail=f"{type(exc).__name__}: {exc}",
            step_ms=step_ms,
        )
        if isinstance(exc, StepError):
            return False
        raise StepError(
            step=self.step,
            underlying=exc,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Armament classification
# ---------------------------------------------------------------------------


def _vehicle_armaments(v: dict[str, Any]) -> list[str]:
    """Return the sorted set of canonical armament tags for a Vehicle.

    Two complementary walks unioned: top-level ``[A-Z]_<Component>`` keys
    (catches rare/modern armament) and ``ShipUpgradeInfo.<entry>.components``
    list values (catches the standard armament types). See the I:-side
    docstring for full rationale.
    """
    found: set[str] = set()

    for key in v.keys():
        if len(key) < 3 or key[1] != "_":
            continue
        prefix = key[0]
        if not ("A" <= prefix <= "Z"):
            continue
        tail = key[2:]
        if "_" in tail:
            head, _, suffix = tail.rpartition("_")
            if suffix.isdigit():
                tail = head
        tag = _ARMAMENT_TAGS.get(tail)
        if tag:
            found.add(tag)

    sui = v.get("ShipUpgradeInfo")
    if isinstance(sui, dict):
        for entry in sui.values():
            if not isinstance(entry, dict):
                continue
            comps = entry.get("components")
            if not isinstance(comps, dict):
                continue
            for ck, cv in comps.items():
                if not isinstance(cv, list) or not cv:
                    continue
                tag = _COMPONENT_LIST_TAGS.get(ck)
                if tag:
                    found.add(tag)

    return sorted(found)


def _classify_species(species: str | None) -> str | None:
    """Map ``typeinfo.species`` to a 2-char class code."""
    if not species:
        return None
    mapped = _SPECIES_TO_CLASS.get(species)
    if mapped:
        return mapped
    return species[:2].upper()


def _model_dir_from_vehicle(vehicle: dict[str, Any]) -> str | None:
    components = _gp_autofill.resolve_components(vehicle, hull_choice="upgraded")
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


# ---------------------------------------------------------------------------
# VFS health classification
# ---------------------------------------------------------------------------


def _ensure_vfs_meta_json(
    config: PipelineConfig,
    *,
    refresh: bool,
) -> Path | None:
    """Materialise the wowsunpack metadata JSON; return None if dumping fails.

    Cached on disk; refreshed when ``refresh=True`` or when the cache
    file is older than the GameParams cache (the natural "new build"
    signal — both come from the same game directory).
    """
    try:
        cache_dir = config.require_cache_dir()
    except Exception:
        return None

    cache = cache_dir / _VFS_META_FILENAME
    gp_cache = cache_dir / _GAMEPARAMS_FILENAME

    if cache.is_file() and not refresh and gp_cache.is_file():
        if cache.stat().st_mtime >= gp_cache.stat().st_mtime:
            return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        _vfs_metadata_json(cache, config=config)
    except Exception as exc:
        print(f"[snapshot] wowsunpack metadata dump failed: {exc}",
              file=sys.stderr)
        return None
    return cache


def _load_vfs_index(
    json_path: Path,
) -> tuple[dict[str, list[str]], set[str]]:
    """Return ``(basename -> list of full dir paths, files set)``.

    Accepts either the toolkit's JSON metadata shape or a CSV fallback.
    Both schemas carry the same fields (``path``, ``is_directory``); the
    JSON form is the natural product of the lifted-layer toolkit.

    Paths are slash-prefixed (``/content/...``); we strip the leading
    slash so they compare directly against GameParams model paths.
    """
    basename_to_paths: dict[str, list[str]] = {}
    files: set[str] = set()
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return basename_to_paths, files

    # The JSON dump emits an array of records with ``path`` +
    # ``is_directory``; tolerate either bool or string forms.
    if isinstance(data, dict):
        rows = data.get("entries") or data.get("files") or data.get("paths")
    else:
        rows = data
    if not isinstance(rows, list):
        return basename_to_paths, files

    for row in rows:
        if not isinstance(row, dict):
            continue
        p = (row.get("path") or "").lstrip("/")
        if not p:
            continue
        is_dir = row.get("is_directory")
        if isinstance(is_dir, str):
            is_dir_bool = is_dir.lower() == "true"
        else:
            is_dir_bool = bool(is_dir)
        if is_dir_bool:
            bn = p.rsplit("/", 1)[-1] if "/" in p else p
            basename_to_paths.setdefault(bn, []).append(p)
        else:
            files.add(p)

    return basename_to_paths, files


def _classify_vfs_status(
    model_dir: str | None,
    basename_to_paths: dict[str, list[str]] | None,
    files: set[str] | None,
) -> str:
    """Categorise extraction-readiness based on what's actually in the VFS."""
    if not model_dir:
        return "none"
    if basename_to_paths is None or files is None:
        return "unknown"
    paths = basename_to_paths.get(model_dir, [])
    if not paths:
        return "no_dir"
    # Several content roots can share a basename — animation siblings
    # carry no .visual, so prefer the path that actually has one.
    full = next(
        (p for p in paths if f"{p}/{model_dir}.visual" in files),
        paths[0],
    )
    if f"{full}/{model_dir}.visual" not in files:
        return "no_visual"
    if f"{full}/{model_dir}.splash" not in files:
        return "no_splash"
    return "ok"


# ---------------------------------------------------------------------------
# Camo topology / hull-swap resolution
# ---------------------------------------------------------------------------


def _classify_camo_topology(entry: wg_camo.CamoEntry) -> str:
    """Inline of scaffold_ship._classify_topology; keep in sync."""
    keys = set(entry.textures)
    has_hull_specific = bool(keys & {"Hull", "DeckHouse", "Bulge"})
    has_tile = "Tile" in keys
    has_palette = bool(entry.color_schemes)
    is_mat = entry.name.startswith("mat_")

    if is_mat and has_hull_specific:
        return _TOPO_MAT_PALETTE if has_palette else _TOPO_MAT_ALBEDO
    if not is_mat and has_hull_specific and has_palette:
        return _TOPO_HULL_PALETTE
    if has_tile and has_palette:
        return _TOPO_TILE_BROADCAST
    return _TOPO_OTHER


def _path_to_dir(model_path: str) -> str | None:
    parts = model_path.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[-1].endswith(".model"):
        return parts[-2]
    return None


def _is_ship_hull_path(p: str) -> bool:
    """Top-level ship hull path (no _Bow / _MidFront / _Stern suffix)."""
    if not isinstance(p, str) or not p.endswith(".model"):
        return False
    if "/ship/" not in p.replace("\\", "/"):
        return False
    stem = p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:-len(".model")]
    suffixes = ("_Bow", "_MidFront", "_MidBack", "_Stern", "_Front", "_Back")
    return not any(stem.endswith(s) for s in suffixes)


def _mesh_swap_dir_for_exterior(ext: dict[str, Any]) -> str | None:
    """Resolve an Exterior's hull-swap target dir if any.

    Equivalent to ``resolve.gameparams_autofill.resolve_variant_model_dir``
    but takes the already-loaded Exterior dict directly so we don't
    re-look-up the Vehicle each call.
    """
    if not isinstance(ext, dict):
        return None
    hull_config = ext.get("hullConfig")
    if isinstance(hull_config, dict):
        for hull_key in ("A_Hull", "B_Hull"):
            entry = hull_config.get(hull_key)
            if isinstance(entry, dict):
                model = entry.get("model")
                if isinstance(model, str) and model:
                    d = _path_to_dir(model)
                    if d:
                        return d
    pm = ext.get("peculiarityModels")
    if isinstance(pm, dict):
        for src, dst in pm.items():
            if not _is_ship_hull_path(src):
                continue
            if isinstance(dst, dict):
                dst = dst.get("model")
            if not isinstance(dst, str):
                continue
            d = _path_to_dir(dst)
            if d:
                return d
    return None


# ---------------------------------------------------------------------------
# Localization (cached per-process)
# ---------------------------------------------------------------------------


def _resolve_display_name(
    exterior_id: str,
    *,
    loc_db: _localization.LocalizationDb | None,
) -> str:
    """Look up an Exterior's localized display name, with humanizer fallback."""
    if loc_db is not None:
        name = loc_db.exterior_display_name(exterior_id)
        if name:
            return name
    try:
        return _localization.humanize_exterior_name(exterior_id)
    except Exception:
        return exterior_id


# ---------------------------------------------------------------------------
# Peculiarity label derivation
# ---------------------------------------------------------------------------


def _humanize_peculiarity_key(key: str) -> str:
    """Snake_case -> Title Case, dropping a trailing year/numeric suffix."""
    parts = [p for p in key.split("_") if p]
    while parts and parts[-1].isdigit():
        parts.pop()
    return " ".join(p[:1].upper() + p[1:] for p in parts) or key


def _common_prefix_at_word_boundary(strings: list[str]) -> str:
    """Longest prefix shared by every string, trimmed at a word boundary."""
    if not strings:
        return ""
    if len(strings) == 1:
        return strings[0]
    s1, s2 = min(strings), max(strings)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    pfx = s1[:i]
    next_s1 = s1[i] if i < len(s1) else ""
    next_s2 = s2[i] if i < len(s2) else ""
    splits_word = (
        (next_s1 and next_s1.isalnum())
        or (next_s2 and next_s2.isalnum())
    )
    if splits_word:
        while pfx and pfx[-1].isalnum():
            pfx = pfx[:-1]
    return pfx.rstrip(" \t,:.;-")


def derive_peculiarity_label(
    peculiarity: str, display_names: list[str],
) -> tuple[str, str, list[str]]:
    """Derive a display label for a WG peculiarity tag from Exterior names.

    Returns ``(label, source, sample_names)`` where ``source`` is one of
    ``override`` | ``single`` | ``prefix`` | ``firstword`` | ``phrase`` |
    ``humanize``. See the I:-side docstring for the heuristic stack.
    """
    if peculiarity in _PECULIARITY_OVERRIDES:
        unique = sorted(set(display_names))
        return _PECULIARITY_OVERRIDES[peculiarity], "override", unique[:3]

    if not display_names:
        return _humanize_peculiarity_key(peculiarity), "humanize", []

    unique = sorted(set(display_names))
    samples = unique[:3]

    if len(unique) == 1:
        return unique[0], "single", samples

    pfx = _common_prefix_at_word_boundary(unique)
    if pfx and len(pfx) >= 4:
        return pfx, "prefix", samples

    first_words: Counter[str] = Counter()
    for n in display_names:
        toks = n.split()
        if toks:
            first_words[toks[0]] += 1
    if first_words:
        top_word, top_count = first_words.most_common(1)[0]
        if top_count >= 2 and top_count / len(display_names) >= 0.5:
            return top_word.rstrip(",:.;-"), "firstword", samples

    phrases: Counter[str] = Counter()
    for n in display_names:
        toks = n.split()
        for i in range(len(toks) - 1):
            phrases[f"{toks[i]} {toks[i+1]}"] += 1
    if phrases:
        top_phrase, top_count = phrases.most_common(1)[0]
        if (
            top_count >= 2
            and top_count / len(display_names) >= 0.3
            and len(top_phrase) >= 5
        ):
            return top_phrase.rstrip(",:.;-"), "phrase", samples

    return _humanize_peculiarity_key(peculiarity), "humanize", samples


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _name_from_top_key(top_key: str) -> str:
    parts = top_key.split("_", 1)
    if len(parts) == 2 and parts[0].startswith("P"):
        return parts[1].replace("_", " ")
    return top_key


def _safe_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (ValueError, OverflowError):
            return None
    return None


def _build_snapshot(
    *,
    config: PipelineConfig,
    runner: _StepRunner,
    refresh: bool,
) -> dict[str, Any]:
    """Walk the GameParams flat dict once; emit ships + permoflages."""
    # ── Step: ensure_gameparams ────────────────────────────────────────
    cache_refreshed_local = False
    with runner.step("ensure_gameparams") as ctx:
        gp_cache = _ensure_gameparams_dump(config=config, refresh=refresh)
        cache_refreshed_local = refresh
        ctx.annotate(str(gp_cache))

    flat = _gp.load_full(refresh=refresh)

    # Optional dependencies — every failure degrades gracefully.
    camo_db: wg_camo.CamouflageDb | None = None
    try:
        camo_db = wg_camo.CamouflageDb.load(config=config)
    except Exception as exc:
        print(f"[snapshot] camouflages.xml load failed: {exc}",
              file=sys.stderr)

    loc_db: _localization.LocalizationDb | None = None
    try:
        loc_db = _localization.load(game_dir=config.require_game_dir())
    except Exception as exc:
        print(f"[snapshot] localization load failed: {exc}", file=sys.stderr)

    vfs_basenames: dict[str, list[str]] | None = None
    vfs_files: set[str] | None = None
    vfs_json = _ensure_vfs_meta_json(config, refresh=refresh)
    if vfs_json is not None:
        try:
            vfs_basenames, vfs_files = _load_vfs_index(vfs_json)
        except Exception as exc:
            print(f"[snapshot] vfs_meta parse failed: {exc}", file=sys.stderr)
            vfs_basenames, vfs_files = None, None

    # ── Step: enumerate_vehicles ───────────────────────────────────────
    ship_records: list[tuple[str, dict[str, Any], str | None]] = []
    by_model_dir: defaultdict[str, list[str]] = defaultdict(list)
    with runner.step("enumerate_vehicles") as ctx:
        for top_key, v in flat.items():
            if not isinstance(v, dict):
                continue
            ti = v.get("typeinfo") or {}
            if not isinstance(ti, dict) or ti.get("type") != "Ship":
                continue
            model_dir = _model_dir_from_vehicle(v)
            if model_dir:
                by_model_dir[model_dir].append(top_key)
            ship_records.append((top_key, v, model_dir))
        ctx.annotate(f"{len(ship_records)} ship(s)",
                     data={"count": len(ship_records)})

    # ── Step: enumerate_permoflages + join_metadata ────────────────────
    vehicles: list[dict[str, Any]] = []
    permoflages_by_vehicle: dict[str, list[dict[str, Any]]] = {}
    perm_count = 0
    ships_with_perm = 0
    display_names_by_peculiarity: dict[str, dict[str, str]] = {}

    with runner.step("enumerate_permoflages") as perm_ctx:
        for top_key, v, model_dir in ship_records:
            ti = v.get("typeinfo") or {}
            species = (ti.get("species") or "").strip() or None
            nation = (ti.get("nation") or "").strip().lower() or None
            ship_class = _classify_species(species)
            peers = sorted(
                k for k in by_model_dir.get(model_dir or "", []) if k != top_key
            )
            native = v.get("nativePermoflage") or None
            permo_ids = [pid for pid in (v.get("permoflages") or [])
                         if isinstance(pid, str)]

            vehicles.append({
                "param_index":           str(v.get("index") or ""),
                "top_key":               top_key,
                "display_name":          _name_from_top_key(top_key),
                "model_dir":             model_dir,
                "tier":                  _safe_int(v.get("level")),
                "nation":                nation,
                "species":               species,
                "class":                 ship_class,
                "is_premium":            bool(v.get("isPremium", False)),
                "is_in_test":            bool(v.get("isInTest", False)),
                "is_paper":              bool(v.get("isPaperShip", False)),
                "native_permoflage":     native,
                "permoflages_count":     len(permo_ids),
                "shares_model_dir_with": peers,
                "group":                 (v.get("group") or None),
                "armaments":             _vehicle_armaments(v),
                "vfs_status":            _classify_vfs_status(
                    model_dir, vfs_basenames, vfs_files
                ),
                "peculiarities":         [],
            })

            # Permoflage records for this Vehicle.
            records: list[dict[str, Any]] = []
            for ext_id in permo_ids:
                ext = flat.get(ext_id)
                if not isinstance(ext, dict):
                    records.append({
                        "exterior_id":   ext_id,
                        "display_name":  _resolve_display_name(ext_id, loc_db=loc_db),
                        "camouflage":    None,
                        "peculiarity":   None,
                        "topology":      _TOPO_OTHER,
                        "is_native":     ext_id == native,
                        "mesh_swap_dir": None,
                    })
                    continue
                ti2 = ext.get("typeinfo") or {}
                if not isinstance(ti2, dict) or ti2.get("type") != "Exterior":
                    continue
                camo_name = (ext.get("camouflage") or "").strip()
                peculiarity = (ext.get("peculiarity") or "").strip()
                mesh_swap_dir = _mesh_swap_dir_for_exterior(ext)
                chosen_entry: wg_camo.CamoEntry | None = None
                entries_list: list[wg_camo.CamoEntry] = []
                if mesh_swap_dir:
                    topo = _TOPO_MESH_SWAP
                elif camo_name and camo_db and camo_name in camo_db.entries:
                    entries_list = camo_db.entries.get(camo_name) or []
                    chosen_entry = next(
                        (e for e in entries_list if top_key in (e.target_ships or [])),
                        entries_list[0] if entries_list else None,
                    )
                    topo = (
                        _classify_camo_topology(chosen_entry)
                        if chosen_entry else _TOPO_OTHER
                    )
                else:
                    topo = _TOPO_OTHER
                display_name = _resolve_display_name(ext_id, loc_db=loc_db)
                category_textures: dict[str, dict[str, str]] | None = None
                if topo in (_TOPO_MAT_PALETTE, _TOPO_HULL_PALETTE,
                            _TOPO_TILE_BROADCAST) and chosen_entry is not None:
                    tex_map: dict[str, dict[str, str]] = {}
                    for tag, vfs_path in chosen_entry.textures.items():
                        if tag.endswith("_mgn") or tag.endswith("_animmap"):
                            continue
                        cat = wg_camo._tag_to_category(tag)
                        basename = vfs_path.replace("\\", "/").rsplit("/", 1)[-1]
                        is_atlas = wg_camo._is_mat_camo_path(vfs_path)
                        lib_dir = (
                            "libraries/camo_mat" if is_atlas
                            else "libraries/camo_masks"
                        )
                        tex_map[cat] = {
                            "lib_path": f"{lib_dir}/{basename}",
                            "kind":     "atlas" if is_atlas else "mask",
                        }
                    if tex_map:
                        category_textures = tex_map
                records.append({
                    "exterior_id":       ext_id,
                    "display_name":      display_name,
                    "camouflage":        camo_name or None,
                    "peculiarity":       peculiarity or None,
                    "topology":          topo,
                    "is_native":         ext_id == native,
                    "mesh_swap_dir":     mesh_swap_dir,
                    "category_textures": category_textures,
                })
                if peculiarity and display_name:
                    bucket = display_names_by_peculiarity.setdefault(peculiarity, {})
                    bucket.setdefault(ext_id, display_name)

            # Stable sort: native first, then mesh-swaps, then alphabetical.
            topo_order = {
                _TOPO_MESH_SWAP: 0,
                _TOPO_MAT_PALETTE: 1,
                _TOPO_MAT_ALBEDO: 1,
                _TOPO_HULL_PALETTE: 2,
                _TOPO_TILE_BROADCAST: 3,
                _TOPO_OTHER: 4,
            }
            records.sort(key=lambda e: (
                not e["is_native"],
                topo_order.get(e["topology"], 9),
                e["exterior_id"],
            ))
            if records:
                permoflages_by_vehicle[top_key] = records
                ships_with_perm += 1
                perm_count += len(records)

            pecs = sorted({r["peculiarity"] for r in records
                           if r.get("peculiarity")})
            if pecs:
                vehicles[-1]["peculiarities"] = pecs

        perm_ctx.annotate(
            f"{perm_count} permoflage(s)",
            data={
                "permoflage_count":       perm_count,
                "ships_with_permoflages": ships_with_perm,
            },
        )

    # ── Step: join_metadata ───────────────────────────────────────────
    peculiarity_labels: dict[str, dict[str, Any]] = {}
    with runner.step("join_metadata") as join_ctx:
        # Sort vehicles by nation/class/tier/top_key for stable rendering.
        vehicles.sort(key=lambda e: (
            e.get("nation") or "",
            e.get("class") or "",
            e.get("tier") or 0,
            e.get("top_key") or "",
        ))

        for pec, ext_names in display_names_by_peculiarity.items():
            names = list(ext_names.values())
            label, source, samples = derive_peculiarity_label(pec, names)
            peculiarity_labels[pec] = {
                "label":          label,
                "source":         source,
                "sample_names":   samples,
                "exterior_count": len(ext_names),
            }
        join_ctx.annotate(
            f"{len(peculiarity_labels)} peculiarity label(s)",
            data={"label_count": len(peculiarity_labels)},
        )

    return {
        "vehicles": vehicles,
        "permoflages_by_vehicle": permoflages_by_vehicle,
        "peculiarity_labels": peculiarity_labels,
        "summary": {
            "vehicle_count":          len(vehicles),
            "permoflage_count":       perm_count,
            "ships_with_permoflages": ships_with_perm,
        },
        # Side-channel so `snapshot()` can populate SnapshotResult.cache_refreshed.
        "_meta": {"cache_refreshed": cache_refreshed_local},
    }


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def snapshot(
    *,
    output_path: Path,
    config: PipelineConfig | None = None,
    refresh: bool = False,
    on_event: OnEvent | None = None,
) -> SnapshotResult:
    """Build the Vehicles + permoflages picker payload.

    Walks ``GameParams.json`` once, joins to ``Exterior`` records,
    classifies camo topology against ``camouflages.xml``, and writes the
    JSON output the Extract webview consumes.

    Parameters:
        output_path  JSON destination. Parent directories are created
                     on demand.
        config       Resolved :class:`PipelineConfig`; loaded on demand
                     when ``None``. Needed to resolve the GameParams
                     cache + game dir.
        refresh      Force a fresh ``ensure_dump`` (skip the cache).
                     Use after a game patch. Re-extracts the VFS
                     metadata too.
        on_event     Optional :class:`StepEvent` callback. Steps:
                     ``ensure_gameparams``, ``enumerate_vehicles``,
                     ``enumerate_permoflages``, ``join_metadata``,
                     ``write_snapshot`` — each emits ``started`` ->
                     ``completed``.

    Returns a :class:`SnapshotResult` carrying the on-disk output path,
    the ``vehicles[]`` / ``permoflages_by_vehicle`` counts, and a flag
    indicating whether the GameParams cache was rebuilt this call.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    names above) when any step fails.
    """
    cfg = config or PipelineConfig.load()
    output_path = Path(output_path)
    runner = _StepRunner(on_event)

    payload = _build_snapshot(config=cfg, runner=runner, refresh=refresh)
    meta = payload.pop("_meta", {})
    cache_refreshed = bool(meta.get("cache_refreshed", False))

    with runner.step("write_snapshot", detail=str(output_path)) as ctx:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, ensure_ascii=False)
        output_path.write_text(body, encoding="utf-8")
        ctx.annotate(
            f"vehicles={payload['summary']['vehicle_count']} "
            f"permoflages={payload['summary']['permoflage_count']}",
            data={
                "vehicle_count":   payload["summary"]["vehicle_count"],
                "permoflage_count": payload["summary"]["permoflage_count"],
            },
        )

    return SnapshotResult(
        output_path=output_path,
        vehicles_count=payload["summary"]["vehicle_count"],
        permoflages_count=payload["summary"]["permoflage_count"],
        cache_refreshed=cache_refreshed,
    )


__all__ = ["snapshot", "derive_peculiarity_label"]
