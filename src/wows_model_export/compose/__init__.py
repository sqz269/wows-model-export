"""Layer 4 — orchestrators.

Chain `toolkit` invocations, `resolve` transforms, and `read` passes into
end-to-end procedures. Slow (seconds to minutes), write files, emit
`StepEvent`s through an optional `on_event` callback.

Public surface — flat function names that collide with their submodule
of the same name win the package binding (function takes the name; the
submodule remains reachable via full-path `from .. import ...` imports).

    from wows_model_export.compose import (
        ingest_ship, scaffold_ship, build_accessory_library, ingest_skin_pack,
        autorig_asset, resolve_decorative_placements, scan_legacy_glb,
        find_ship_variants, publish, snapshot, teardown_ship,
        build_projectile_library, build_decal_library, build_ammo_profiles,
    )
    result = ingest_ship("Montana", config=cfg, on_event=printer)
    lib    = build_accessory_library(config=cfg, on_event=printer)
    pl     = build_projectile_library(config=cfg, on_event=printer)

For the sidecar mutation API (write / apply_variant_asset_swaps /
absorb_*), use the submodule namespace::

    from wows_model_export.compose import sidecar
    sidecar.write(doc, path)
    sidecar.apply_variant_asset_swaps(doc, ...)

For the attached-accessories resolver, the audit pass, or any composer
where the function name is ambiguous in flat namespace, use submodule
access::

    from wows_model_export.compose.attached_accessories_library import resolve_library
    from wows_model_export.compose.dead_variant_audit import audit_library
"""

from __future__ import annotations

# Submodule namespaces that don't collide with a flat function of the
# same name (these are always reachable via attribute access on the
# package object).
from . import (
    attached_accessories_library,
    dead_variant_audit,
    sidecar,
)

# Flat function re-exports. Submodules of the same name still exist
# (importable as `from wows_model_export.compose.scaffold_ship import …`)
# but get shadowed in the package binding by the function.
from .accessories_scan import scan_legacy_glb
from .accessory_library import build_accessory_library
from .ammo_profiles import build_ammo_profiles
from .decal_library import build_decal_library
from .find_ship_variants import find_ship_variants
from .ingest_ship import ingest_ship, resolve_ship_identity
from .projectile_library import build_projectile_library
from .publish import publish
from .scaffold_ship import scaffold_ship
from .skel_ext_resolve import resolve_decorative_placements
from .skin_pack import ingest_skin_pack
from .snapshot import snapshot
from .teardown_ship import teardown_ship
from .turret_autorig import autorig_asset, autorig_asset_full

__all__ = [
    # Submodules
    "attached_accessories_library",
    "dead_variant_audit",
    "sidecar",
    # Per-ship composer functions (shadow same-named submodules)
    "ingest_ship",
    "scaffold_ship",
    "build_accessory_library",
    "ingest_skin_pack",
    "autorig_asset",
    "autorig_asset_full",
    "resolve_decorative_placements",
    "resolve_ship_identity",
    "scan_legacy_glb",
    "find_ship_variants",
    "teardown_ship",
    # Fleet-wide composers
    "build_projectile_library",
    "build_decal_library",
    "build_ammo_profiles",
    # Cross-cutting
    "publish",
    "snapshot",
]
