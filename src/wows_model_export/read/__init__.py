"""Layer 1 — pure data readers.

Operate on disk paths or already-loaded JSON dicts. Return typed
dataclasses. No subprocess calls, no network, no file writes. Fast (μs
to low-ms per call).

Public surface — two access patterns:

1. **Submodule namespaces** — for callers who want module-scoped names::

       from wows_model_export.read import localization, gameparams, mfm, sidecar
       db   = localization.load(game_dir)
       data = gameparams.load_full()
       mat  = mfm.parse_mfm(path)
       doc  = sidecar.read(path)

2. **Specific symbols** flattened — for distinctive names::

       from wows_model_export.read import LocalizationDb, MaterialPrototype, get_ship

Generic verbs (``load``, ``load_full``, ``read``) are intentionally
**not** re-exported flat — they live behind their submodule to avoid
ambiguity.

Lifted modules so far:

    mfm           — .mfm MaterialPrototype reader (from wg_mfm.py)
    localization  — .mo gettext reader (from wg_localization.py)
    gameparams    — GameParams JSON entity readers (from gameparams.py)
    sidecar       — sidecar document read shim (authority lives in
                    resolve.sidecar — from tools/ship/sidecar.py)
    bw_geometry   — BigWorld .geometry binary parser
                    (from bw_geometry.py)
"""

from __future__ import annotations

# Submodule namespaces
from . import bw_geometry, gameparams, localization, mfm, sidecar

# gameparams symbols — distinctive names safe at top level
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

# sidecar — schema constants + path helpers (the read() function itself
# stays scoped to read.sidecar.read to avoid colliding with the read
# layer name; flat callers should use `from wows_model_export.read.sidecar import read`).
from .sidecar import (
    HITBOX_TOKEN_MAP,
    MODELS_SUBDIR,
    SCHEMA_VERSION,
    SIDECAR_SUFFIX,
    SidecarSchemaError,
    build_ship_key,
    normalise_hitbox_token,
    sidecar_path_for,
)

__all__ = [
    # Submodules
    "bw_geometry",
    "gameparams",
    "localization",
    "mfm",
    "sidecar",
    # gameparams
    "get_entity",
    "get_ship",
    "get_exterior",
    "get_projectile",
    "iter_top_level_keys",
    "resolve_ship_id",
    "unload_full",
    # localization
    "LocalizationDb",
    "humanize_exterior_name",
    "latest_bin_dir",
    "mo_path",
    # mfm
    "MaterialPrototype",
    "parse_mfm",
    "get_emissive_power",
    "DEFAULT_EMISSIVE_POWER",
    # sidecar
    "SCHEMA_VERSION",
    "SIDECAR_SUFFIX",
    "MODELS_SUBDIR",
    "HITBOX_TOKEN_MAP",
    "SidecarSchemaError",
    "sidecar_path_for",
    "build_ship_key",
    "normalise_hitbox_token",
]
