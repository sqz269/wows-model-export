"""Public types for the pipeline API.

This module is the *contract* between the pipeline and its consumers.
Two kinds of types live here:

1. **Process types** — `StepEvent`, the result dataclasses each composer
   returns, the `OnEvent` callback alias. These describe how callers
   interact with running operations.

2. **Stats types** — small structured payloads emitted as parts of
   composer results (e.g. `AttachmentResolveStats`, `PublishCounts`).
   Kept here rather than buried inside layer modules so consumers can
   import them from one place.

Schema types (the shape of sidecar / library / attached-accessories
JSON on disk) are intentionally **not** in this module. They belong with
the readers that parse them — `wows_model_export.read.sidecar` etc. —
so the schema and the parser evolve together.

All result types are `frozen=True` so consumers can rely on stable
identity, and unknown extras get rejected at construction time rather
than silently absorbed. Path fields are real `Path` objects, never
`str`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Re-export PipelineConfig so consumers have one canonical import path:
# `from wows_model_export.types import PipelineConfig`.
from .config import PipelineConfig

# ---------------------------------------------------------------------------
# Step events
# ---------------------------------------------------------------------------

StepState = Literal["started", "progress", "completed", "failed", "skipped"]


@dataclass(frozen=True)
class StepEvent:
    """One progress notification from a composer.

    Composers emit events at step boundaries (`started` / `completed` /
    `failed` / `skipped`) and optionally during long-running steps
    (`progress`). A no-op `on_event=None` skips emission entirely.

    Fields:
        step        Canonical step name (e.g. ``"export_hull"``,
                    ``"scaffold"``, ``"build_accessory_library"``).
                    Stable across versions — consumers can branch on it.
        state       One of `started` / `progress` / `completed` /
                    `failed` / `skipped`. `failed` always precedes the
                    composer raising `StepError`.
        detail      Free-text human summary. Don't parse this; use
                    ``data`` for structured payload.
        elapsed_ms  Wall time since the composer started, in ms.
        step_ms     Wall time since *this step* started; `None` for
                    `started` events (no prior reference).
        data        Optional structured payload — counts, warnings,
                    interim file paths. Step-specific; documented per
                    composer.
    """

    step:        str
    state:       StepState
    detail:      str = ""
    elapsed_ms:  float = 0.0
    step_ms:     float | None = None
    data:        dict | None = None


# Callback signature for composers. Sync only — async consumers wrap by
# pushing events through a queue; see `migration/PIPELINE_API.md`
# §"Why callbacks, not async generators".
OnEvent = Callable[[StepEvent], None]


# ---------------------------------------------------------------------------
# Toolkit results (Layer 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolkitResult:
    """Outcome of one `wows_model_export.toolkit.*` subprocess call.

    `output_paths` contains every file the toolkit wrote on this call —
    a single GLB for `export_ship`, the (glb, dds-dir) tuple for
    `export_model --raw-dds-dir`, etc. `stderr` is captured even on
    success (the toolkit prints diagnostics there); consumers can
    surface or discard it.
    """

    output_paths: tuple[Path, ...]
    stderr:       str
    elapsed_ms:   float


# ---------------------------------------------------------------------------
# Composer results (Layer 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaffoldResult:
    """Outcome of `compose.scaffold_ship`.

    See `tools/ship/scaffold_ship.py::scaffold` for the operation each
    field corresponds to. Optional paths are `None` when the
    corresponding step was skipped (`--skip-export`, `--skip-armor`, …).
    """

    ship_id:                 str
    workspace_dir:           Path
    hull_glb:                Path | None
    placements_json:         Path | None
    skel_ext_json:           Path | None
    material_mappings_json:  Path | None
    armor_json:              Path | None
    ammo_json:               Path | None
    sidecar_path:            Path | None
    textures_dds_dir:        Path | None
    variant_routed:          bool = False
    variant_permoflage:      str | None = None
    warnings:                tuple[str, ...] = ()
    step_timings_ms:         dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    """Outcome of `compose.ingest_ship` — the top-level per-ship op.

    Wraps a `ScaffoldResult` (the sidecar + GLB write) plus the followup
    passes (legacy scan, skel_ext resolve, accessory-library refresh,
    optional publish). `library_refreshed` is `True` when the post-ingest
    accessory-library rebuild ran.
    """

    ship_id:                  str
    label:                    str
    workspace_dir:            Path
    scaffold:                 ScaffoldResult
    legacy_scan_path:         Path | None
    accessories_json_path:    Path | None
    library_refreshed:        bool = False
    published_to:             Path | None = None
    warnings:                 tuple[str, ...] = ()
    step_timings_ms:          dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentResolveStats:
    """Per-asset diagnostics from `resolve.attached_accessories`.

    Mirrors the existing `ResolveStats` in
    `tools/ship/asset_attachments_resolve.py` so the lift is a
    straight rename. Documented here so the type is part of the public
    surface, not a layer-2 internal.
    """

    candidates_total:                  int = 0
    candidates_in_kept_records:        int = 0
    unresolved_p0_hashes:              int = 0
    filtered_skinned:                  int = 0
    attachments_live:                  int = 0
    attachments_dead:                  int = 0
    distinct_assets:                   int = 0
    convention_b_external_y_conjugate: int = 0
    convention_b_host_space_children:  int = 0


@dataclass(frozen=True)
class AccessoryLibraryResult:
    """Outcome of `compose.build_accessory_library`.

    `assets_built` is the count of unique `asset_id`s that got a fresh
    GLB + DDS mip chain in this run. `attachment_stats` maps `asset_id`
    → `AttachmentResolveStats` for every asset whose
    `<asset>.attached_accessories.json` was (re)written.
    """

    library_root:        Path
    assets_built:        int
    assets_audited:      int
    auto_flipped:        tuple[str, ...] = ()
    warnings:            tuple[str, ...] = ()
    attachment_stats:    dict[str, AttachmentResolveStats] = field(default_factory=dict)
    step_timings_ms:     dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SkinPackResult:
    """Outcome of `compose.ingest_skin_pack`.

    A skin pack is either a loose-mod folder (per-asset DDS overrides
    living in `res_mods/`) or a VFS-variant slice (e.g. an Azur Lane
    permoflage's textures). The result tells the caller which sidecar
    got the new `skins[]` entry and whether the loose-mod path triggered
    a swizzle pass.
    """

    ship_id:        str
    sidecar_path:   Path
    skin_id:        str
    source:         Literal["loose_mod", "vfs_variant"]
    swizzled:       bool = False
    warnings:       tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishCounts:
    """Per-domain counts for one `compose.publish` run."""

    copied:   int = 0
    skipped:  int = 0  # up-to-date by mtime+size
    deleted:  int = 0


@dataclass(frozen=True)
class PublishResult:
    """Outcome of `compose.publish` (was `publish_to_unity.py`).

    Generic publisher — `target_dir` is the consumer-side destination
    (Unity `Assets/Ships/Pipeline/` historically; configurable now). The
    per-domain counts let the caller assert e.g. "exactly one ship
    refreshed, library untouched."
    """

    target_dir:   Path
    ships:        PublishCounts = PublishCounts()
    library:      PublishCounts = PublishCounts()
    projectiles:  PublishCounts = PublishCounts()
    decals:       PublishCounts = PublishCounts()
    warnings:     tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of `compose.snapshot` (was `tools/extract/snapshot.py`).

    Materialises the picker payload the webview's Extract page reads.
    `vehicles_count` and `permoflages_count` mirror the size of the two
    arrays inside the JSON; useful as a smoke check.
    """

    output_path:         Path
    vehicles_count:      int
    permoflages_count:   int
    cache_refreshed:     bool = False


__all__ = [
    # Process types
    "StepEvent",
    "StepState",
    "OnEvent",
    # Results
    "ToolkitResult",
    "ScaffoldResult",
    "IngestResult",
    "AttachmentResolveStats",
    "AccessoryLibraryResult",
    "SkinPackResult",
    "PublishCounts",
    "PublishResult",
    "SnapshotResult",
    # Config (re-exported from .config)
    "PipelineConfig",
]
