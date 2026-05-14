"""Exterior permoflage swap extraction + per-swap comparison helpers.

Lifted from ``tools/ship/compare_exterior_swaps.py`` (private I:-side
repo). Layer 3 (resolve): pure transforms over GameParams Exterior /
Ship dicts; the CLI half (driver, text-report writer, ``main()``) is
dropped — composer ``compose.skin_pack.ingest_skin_pack`` owns the
toolkit-extract + driving logic.

Public surface:

  * :func:`asset_id_from_model_path`  — extract asset_id from a VFS path
  * :func:`extract_swaps`             — flatten ``Exterior`` mount overrides
                                        into ``[{group, hp_name,
                                        swap_asset_id, swap_model, kind,
                                        vanilla_asset_id?}, ...]``
  * :func:`load_exterior`             — fetch an Exterior dict via the
                                        cached GameParams reader
  * :func:`load_ship`                 — fetch a Ship dict via the cached
                                        GameParams reader

The Exterior swap walker handles three swap-declaration shapes:

    * ``nodesConfig.<group>.<HP>.model``  — per-mount HP→swap (e.g.
      PAES488 main turrets).
    * ``hullConfig.<hull>.caps.<HP>.model`` (when ``capActive`` truthy)
      — per-mount cap overrides.
    * ``peculiarityModels[<vanilla_path>] = <swap_path>`` — global
      path-based rewrite (e.g. PAES428 Cleveland Azur secondaries).

This module is intentionally not re-exported via ``resolve/__init__.py``
yet; the parent agent will wire it in alongside the rest of the resolve
surface. Consumers must import the submodule directly:

    from wows_model_export.resolve import exterior_compare
"""
from __future__ import annotations

import re
from pathlib import Path

from ..read import gameparams as _gp

VFS_MODEL_RE = re.compile(
    r"content/gameplay/[^/]+/[^/]+(?:/[^/]+)*/([^/]+)\.model$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# GameParams loaders
# ---------------------------------------------------------------------------


def load_exterior(exterior_id: str, *, region: str = "NA") -> dict:
    """Load an Exterior GameParams entity by ID.

    The ``region`` parameter is accepted for API compatibility with the
    private repo's CLI variant, but ignored on this side: the
    public-package ``read.gameparams`` cache stores only one realm
    (the unwrapped dict), and that's where the lookup happens.

    Raises :class:`KeyError` when the Exterior isn't found.
    """
    del region  # signature parity; cache holds one realm only.
    ext = _gp.get_exterior(exterior_id)
    if ext is None:
        # Final fallback: maybe the caller passed a non-Exterior ID;
        # surface a clear KeyError so the composer can wrap it.
        raise KeyError(f"Exterior {exterior_id!r} not found in GameParams")
    return ext


def load_ship(ship_id: str, *, region: str = "NA") -> dict:
    """Load a Ship (Vehicle) GameParams entity by full ID or prefix.

    ``region`` carries through for parity with the CLI variant; ignored
    here for the same reason as :func:`load_exterior`.

    Raises :class:`KeyError` when no Vehicle entity matches.
    """
    del region
    ship = _gp.get_ship(ship_id)
    if ship is None:
        raise KeyError(f"Ship {ship_id!r} not found in GameParams")
    return ship


# ---------------------------------------------------------------------------
# Swap-map extraction
# ---------------------------------------------------------------------------


def asset_id_from_model_path(p: str) -> str:
    """Extract the asset_id (filename stem) from a ``.model`` VFS path.

    Falls back to ``Path(p).stem`` when the path doesn't match the
    standard ``content/gameplay/<scope>/<category>[/<subcategory>]/<asset>``
    layout — useful when a mod ships a non-gameplay-tree variant.
    """
    m = VFS_MODEL_RE.search(p)
    return m.group(1) if m else Path(p).stem


def extract_swaps(exterior: dict) -> list[dict]:
    """Flatten an Exterior's mount overrides into a list of swap entries.

    Walks three swap-declaration shapes (see module docstring). Each entry
    is shaped::

        {
          "group":               str,
          "hp_name":             str | None,
          "swap_asset_id":       str,
          "swap_model":          str,           # VFS .model path
          "kind":                "nodesConfig" |
                                 "hullConfig.caps" |
                                 "peculiarityModels",
          "vanilla_asset_id":    str | absent,  # only for peculiarityModels
        }

    Hull-model and ``_dead.model`` peculiarity entries are filtered out —
    hull is handled via the variant ``--hull`` arg in the composer, and
    dead variants ride along with the live model via ``export-model``.
    """
    out: list[dict] = []
    for grp, hp_map in (exterior.get("nodesConfig") or {}).items():
        if not isinstance(hp_map, dict):
            continue
        for hp_name, hp_val in hp_map.items():
            model = hp_val.get("model") if isinstance(hp_val, dict) else None
            if not model:
                continue
            out.append({
                "group": grp,
                "hp_name": hp_name,
                "swap_asset_id": asset_id_from_model_path(model),
                "swap_model": model,
                "kind": "nodesConfig",
            })

    # hullConfig.<hull>.caps — sometimes carries director/cap overrides.
    # `capActive: False` means the cap is the vanilla one (no swap intent).
    for hull_name, hull in (exterior.get("hullConfig") or {}).items():
        if not isinstance(hull, dict):
            continue
        for hp_name, hp_val in (hull.get("caps") or {}).items():
            if not isinstance(hp_val, dict):
                continue
            if not hp_val.get("capActive"):
                continue                      # not really a swap
            model = hp_val.get("model")
            if not model:
                continue
            out.append({
                "group": f"{hull_name}.caps",
                "hp_name": hp_name,
                "swap_asset_id": asset_id_from_model_path(model),
                "swap_model": model,
                "kind": "hullConfig.caps",
            })

    # peculiarityModels[<vanilla_path>] = <swap_path> — global rewrite
    # dict. Vanilla-path key gives us the vanilla asset_id directly, so
    # no HP lookup is needed downstream. Filter:
    #   * skip /ship/ entries (hull-level, handled via variant_asset_id)
    #   * skip *_dead.model entries (live-model `export-model` already
    #     drags the dead sibling along)
    # Note: PAES428 Cleveland keeps a 1:1 vanilla→swap dict (one entry
    # per accessory class), so the same swap can apply to many vanilla
    # mounts at draw time. We emit one row per dict entry.
    for vanilla_path, swap_path in (exterior.get("peculiarityModels") or {}).items():
        if not isinstance(vanilla_path, str) or not isinstance(swap_path, str):
            continue
        if "/ship/" in vanilla_path:
            continue                          # hull-level; handled via variant
        if vanilla_path.endswith("_dead.model") or swap_path.endswith("_dead.model"):
            continue                          # rides along with the live model
        van_aid = asset_id_from_model_path(vanilla_path)
        swap_aid = asset_id_from_model_path(swap_path)
        if not van_aid or not swap_aid:
            continue
        out.append({
            "group": "peculiarityModels",
            "hp_name": None,                   # not HP-keyed
            "swap_asset_id": swap_aid,
            "swap_model": swap_path,
            "kind": "peculiarityModels",
            "vanilla_asset_id": van_aid,
        })
    return out


__all__ = [
    "VFS_MODEL_RE",
    "asset_id_from_model_path",
    "extract_swaps",
    "load_exterior",
    "load_ship",
]
