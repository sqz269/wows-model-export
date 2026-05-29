"""Layer 2 — subprocess wrappers around the wowsunpack CLI.

Slow (subprocess spawn + Rust-side asset parsing), writes files, returns
typed paths + stderr + elapsed time as `ToolkitResult`. The binary
location is resolved via `PipelineConfig.toolkit_bin` (env var
`WOWS_TOOLKIT_BIN`, then `shutil.which("wowsunpack")`, then a build-time
default).

Public surface — one function per `wowsunpack` subcommand we care about:

    Ship / asset export:
        export_ship          — full ship → single GLB + side files
        export_model         — single accessory / sub-model → GLB
        batch_export_model   — many sub-models in one invocation

    Data dumps:
        armor_json           — armor materials_table + zones
        ammo_json            — per-shell ballistic profiles
        dump_gameparams      — GameParams JSON dump
        dump_bones           — asset bone tree → JSON
        fetch_bones          — asset bone tree → parsed dict
        metadata_json        — VFS manifest

    File ops:
        extract              — pull files from VFS by glob
        swizzle_dir          — emit glTF-conformant DDS siblings

    Cache helpers (idempotent, build on demand):
        ensure_manifest      — VFS manifest at <cache_dir>/wows_manifest.json
        default_manifest_path — resolve location without building
"""

from __future__ import annotations

from . import assets_bin
from .ammo import ammo_json
from .armor import armor_json
from .bones import dump_bones, fetch_bones
from .gameparams import dump_gameparams
from .map import export_map, list_spaces
from .ship import (
    batch_export_model,
    export_model,
    export_ship,
    ingest_ship_bundle,
    ingest_ship_supported,
)
from .swizzle import swizzle_dir
from .vfs import default_manifest_path, ensure_manifest, extract, metadata_json

__all__ = [
    # Ship / asset export
    "export_ship",
    "ingest_ship_bundle",
    "ingest_ship_supported",
    "export_model",
    "batch_export_model",
    # Map export
    "export_map",
    "list_spaces",
    # Data dumps
    "armor_json",
    "ammo_json",
    "dump_gameparams",
    "dump_bones",
    "fetch_bones",
    "metadata_json",
    # File ops
    "extract",
    "swizzle_dir",
    # Cache helpers
    "default_manifest_path",
    "ensure_manifest",
    # Submodules
    "assets_bin",
]
