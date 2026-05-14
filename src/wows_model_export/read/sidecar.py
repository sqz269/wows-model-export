"""Public read entry for sidecar documents.

Lifted from ``tools/ship/sidecar.py`` — the schema authority lives in
:mod:`wows_model_export.resolve.sidecar`; this module is the public
read shim. Use it when you want to load + validate a
``<Ship>.meta.json`` sidecar from disk, or look up the canonical
on-disk path for one.

Symbols re-exported here are stable per the §1.2 schema evolution
table in ``tools/contracts/METADATA_SPEC.md``. Anything beyond the
read surface (writers, mutating absorbs, schema constructors) lives
in :mod:`wows_model_export.compose.sidecar` and
:mod:`wows_model_export.resolve.sidecar`.
"""

from __future__ import annotations

from ..resolve.sidecar import (
    DDS_MIP_SUFFIXES,
    HITBOX_TOKEN_MAP,
    MODELS_SUBDIR,
    PLACEMENT_SECTIONS,
    SCHEMA_VERSION,
    SECTION_TO_SPECIES,
    SIDECAR_SUFFIX,
    SPECIES_TO_SECTION,
    UNIVERSAL_HITBOX_ZONES,
    VALID_SHADER_INTENTS,
    VALID_SHIP_CLASSES,
    VALID_STAGES,
    SidecarSchemaError,
    build_ship_key,
    normalise_hitbox_token,
    read,
    sidecar_path_for,
)

__all__ = [
    # Read surface
    "read",
    "sidecar_path_for",
    "build_ship_key",
    "normalise_hitbox_token",
    # Errors
    "SidecarSchemaError",
    # Schema version + filesystem conventions
    "SCHEMA_VERSION",
    "SIDECAR_SUFFIX",
    "MODELS_SUBDIR",
    # Vocabularies / constants
    "SPECIES_TO_SECTION",
    "SECTION_TO_SPECIES",
    "PLACEMENT_SECTIONS",
    "VALID_SHIP_CLASSES",
    "UNIVERSAL_HITBOX_ZONES",
    "HITBOX_TOKEN_MAP",
    "VALID_SHADER_INTENTS",
    "VALID_STAGES",
    "DDS_MIP_SUFFIXES",
]
