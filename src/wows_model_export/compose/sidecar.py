"""Public write + mutation entry for sidecar documents.

Lifted from ``tools/ship/sidecar.py`` — the schema authority lives in
:mod:`wows_model_export.resolve.sidecar`; this module is the public
write/mutate shim, exposing operations with disk side-effects or that
mutate the document tree.

Use these when you're producing or transforming a sidecar in place:

* :func:`write` / :func:`dumps` — serialise to canonical bytes.
* :func:`new_document` / :func:`new_document_from_placements` —
  build a fresh ``<Ship>.meta.json`` document.
* :func:`absorb_*` family — fold toolkit / GameParams / placements
  JSON output into an existing doc (idempotent re-runs).
* :func:`apply_variant_asset_swaps` — apply Exterior
  ``peculiarityModels`` swaps + bone-mismatch Y-flip correction.
* :func:`merge_preserving` / :func:`derive_attach_to` /
  :func:`alias_active_hull_to_top_level` — document-tree
  transforms.
"""

from __future__ import annotations

from ..resolve.sidecar import (
    absorb_ballistics_json,
    absorb_gameparams_armor,
    absorb_gameparams_camera,
    absorb_gameparams_hitbox,
    absorb_gameparams_mounts,
    absorb_gameparams_ship,
    absorb_gameparams_torpedoes,
    absorb_gameparams_variants,
    absorb_per_hull_placements,
    absorb_placements_json,
    alias_active_hull_to_top_level,
    apply_variant_asset_swaps,
    derive_attach_to,
    dumps,
    merge_preserving,
    new_document,
    new_document_from_placements,
    ship_from_placements,
    write,
)

__all__ = [
    # Serialisation
    "write",
    "dumps",
    # Document constructors
    "new_document",
    "new_document_from_placements",
    "ship_from_placements",
    # Absorb passes (toolkit / GameParams ingestion)
    "absorb_placements_json",
    "absorb_per_hull_placements",
    "absorb_gameparams_ship",
    "absorb_gameparams_mounts",
    "absorb_gameparams_armor",
    "absorb_gameparams_hitbox",
    "absorb_gameparams_camera",
    "absorb_gameparams_torpedoes",
    "absorb_gameparams_variants",
    "absorb_ballistics_json",
    # Mutating transforms
    "apply_variant_asset_swaps",
    "merge_preserving",
    "derive_attach_to",
    "alias_active_hull_to_top_level",
]
