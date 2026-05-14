"""Layer 4 — orchestrators.

Chain `toolkit` invocations, `resolve` transforms, and `read` passes into
end-to-end procedures. Slow (seconds to minutes), write files, emit
`StepEvent`s through an optional `on_event` callback.

Public surface — submodule namespaces plus the write-side sidecar shim:

    from wows_model_export.compose import sidecar
    sidecar.write(doc, path)
    sidecar.apply_variant_asset_swaps(doc, ...)

Lifted modules so far:

    sidecar      — sidecar document write + mutation shim (authority
                   lives in resolve.sidecar; from tools/ship/sidecar.py)

End-to-end composers (`scaffold_ship`, `ingest_ship`,
`build_accessory_library`, `ingest_skin_pack`, `publish`, `snapshot`)
are pending.
"""

from __future__ import annotations

# Submodule namespaces
from . import sidecar

__all__ = ["sidecar"]
