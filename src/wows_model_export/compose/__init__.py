"""Layer 4 — orchestrators.

Chain `toolkit` invocations, `resolve` transforms, and `read` passes into
end-to-end procedures. Slow (seconds to minutes), write files, emit
`StepEvent`s through an optional `on_event` callback.

Public surface — flat function names override submodule attributes
of the same name (e.g. `compose.scaffold_ship` is the *function*; the
*submodule* stays reachable via `from wows_model_export.compose.scaffold_ship
import _internal_helper`).

    from wows_model_export.compose import scaffold_ship, build_accessory_library
    result = scaffold_ship("Montana", config=cfg, on_event=printer)
    lib    = build_accessory_library(config=cfg, on_event=printer)

For the sidecar mutation API (write / apply_variant_asset_swaps /
absorb_*), use the submodule namespace directly::

    from wows_model_export.compose import sidecar
    sidecar.write(doc, path)
    sidecar.apply_variant_asset_swaps(doc, ...)

Lifted modules so far:

    sidecar                       — sidecar write + mutation shim
                                    (authority in resolve.sidecar)
    scaffold_ship                 — one-shot per-ship scaffold
                                    (from tools/ship/scaffold_ship.py)
    accessory_library             — fleet-wide accessory library build
                                    (from tools/ship/build_accessory_library.py)
    attached_accessories_library  — per-asset attached-accessories
                                    resolve pass
                                    (from tools/ship/asset_attachments_resolve.py)
    dead_variant_audit            — destroyed-turret variant audit
                                    (from tools/ship/dead_variant_audit.py)

End-to-end composers still pending: ``ingest_ship``, ``ingest_skin_pack``,
``publish``, ``snapshot``.
"""

from __future__ import annotations

# Submodule namespaces — `sidecar` doesn't collide with any function
# name. The two big orchestrator submodules (scaffold_ship,
# accessory_library) DO collide with their entry functions; Python
# binding rules give the function the flat name and leave the module
# accessible via full-path imports.
from . import (
    attached_accessories_library,
    dead_variant_audit,
    sidecar,
)

# Flat function re-exports. These shadow the same-named submodules in
# the package namespace, which is intentional — the function is the
# more common reach for these.
from .accessory_library import build_accessory_library
from .scaffold_ship import scaffold_ship

__all__ = [
    # Submodules
    "attached_accessories_library",
    "dead_variant_audit",
    "sidecar",
    # Composer functions (also shadow same-named submodules)
    "scaffold_ship",
    "build_accessory_library",
]
