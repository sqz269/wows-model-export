"""Layer 1 — pure data readers.

Operate on disk paths or already-loaded JSON dicts. Return typed
dataclasses. No subprocess calls, no network, no file writes. Fast (μs
to low-ms per call).

Public surface — two access patterns:

1. **Submodule namespaces** — for callers who want module-scoped names
   without worrying about top-level collisions::

       from wows_model_export.read import localization, gameparams, mfm
       db   = localization.load(game_dir)
       data = gameparams.load_full()
       mat  = mfm.parse_mfm(path)

2. **Specific symbols** flattened to the package namespace — for
   distinctive names where the source is obvious::

       from wows_model_export.read import LocalizationDb, MaterialPrototype, get_ship

Generic verbs (``load``, ``load_full``) are intentionally **not**
re-exported flat — they live behind their submodule to avoid
ambiguity.

Lifted modules so far:

    mfm           — .mfm MaterialPrototype reader (from wg_mfm.py)
    localization  — .mo gettext reader (from wg_localization.py)
    gameparams    — GameParams JSON entity readers (from gameparams.py)
"""

from __future__ import annotations

# Submodule namespaces
from . import gameparams, localization, mfm

# Specific symbols — distinctive names safe at top level
from .gameparams import (
    get_entity,
    get_exterior,
    get_projectile,
    get_ship,
    iter_top_level_keys,
    resolve_ship_id,
    unload_full,
)
from .localization import (
    LocalizationDb,
    humanize_exterior_name,
    latest_bin_dir,
    mo_path,
)
from .mfm import (
    DEFAULT_EMISSIVE_POWER,
    MaterialPrototype,
    get_emissive_power,
    parse_mfm,
)

__all__ = [
    # Submodules
    "gameparams",
    "localization",
    "mfm",
    # gameparams symbols
    "get_entity",
    "get_ship",
    "get_exterior",
    "get_projectile",
    "iter_top_level_keys",
    "resolve_ship_id",
    "unload_full",
    # localization symbols
    "LocalizationDb",
    "humanize_exterior_name",
    "latest_bin_dir",
    "mo_path",
    # mfm symbols
    "MaterialPrototype",
    "parse_mfm",
    "get_emissive_power",
    "DEFAULT_EMISSIVE_POWER",
]
