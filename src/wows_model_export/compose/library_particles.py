"""Build the shared particle library — one decode pass, one on-disk artefact.

Replaces the per-ship inlining model that landed in the original Tier-B
work (2026-05-16). Each Effect record in ``content/assets.bin`` is
bit-identical across every ship that references it; inlining records
into per-ship sidecars duplicated each record dozens of times and grew
``BA_Montana.meta.json`` from 723 KB to 6.5 MB.

This module emits a single ``library/particles/records.json`` keyed by
VFS path; downstream consumers (webview, Unity / Blender publishers)
join against it by ``attachment.particle_path``. Texture refs in each
record's renderer / animation blocks are extracted into the existing
``content/effects_textures/`` cache and stamped with
``textureUrl0`` / ``textureUrl1`` / ``motionVectorsTextureUrl`` so
consumers don't repeat the lookup. Velocity-field ``.vfd`` resources
referenced by ``velocityField.fieldSourceName`` are extracted to their
original ``content/particles/velocity_fields`` paths so the webview can
fetch them directly through ``repoUrl(fieldSourceName)``.

Idempotent + mtime-gated: :func:`ensure_built` re-decodes only when the
records artefact is missing or older than the cached ``assets.bin``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read.particles import ParticleStore
from ..resolve.sidecar._helpers import _now_iso
from ..toolkit import assets_bin as _assets_bin
from . import effects_textures as _eff_tex

LIBRARY_ROOT = Path("library") / "particles"
RECORDS_FILE = LIBRARY_ROOT / "records.json"
INDEX_FILE = LIBRARY_ROOT / "index.json"
# Bump whenever the per-record JSON shape emitted by ``read.particles``
# changes (new/renamed fields), so ``ensure_built`` regenerates the library
# even though assets.bin itself is unchanged. v2: 2026-06-08 decode
# expansions — full renderer scalar/bool cluster (lightingShineness/Ambient/
# Diffuse/Transmission, hideStartCos/Speed, softParticleDepthScale,
# opacityMultiplier, spinRate*, explicitOrientation*, scaleX, billboard,
# velocityOriented), per-system ``intensities`` + ``distance`` configs,
# coordinate-style byte fix. A library built before these landed carries a
# truncated schema that the mtime gate alone accepts forever (assets.bin
# predates the parser change). v3: 2026-06-24 — per-system ``name`` decoded
# from the System+0x198 ResourceRef (previously the one fully-dropped field;
# present in 100% of systems).
SCHEMA_VERSION = 3


def library_paths(workspace: Path) -> dict[str, Path]:
    """Resolve absolute on-disk paths for the library artefacts."""
    ws = workspace.resolve()
    return {
        "root": ws / LIBRARY_ROOT,
        "records": ws / RECORDS_FILE,
        "index": ws / INDEX_FILE,
    }


def is_current(
    records_path: Path,
    assets_bin_path: Path,
    index_path: Path | None = None,
) -> bool:
    """True iff ``records_path`` is newer than ``assets_bin_path`` AND was
    built by the current decode schema.

    The mtime check alone is insufficient: the parser's output shape can
    change while assets.bin stays untouched, leaving a permanently-accepted
    stale artefact. When ``index_path`` is provided, the recorded
    ``schema_version`` must match :data:`SCHEMA_VERSION`.
    """
    if not records_path.is_file() or not assets_bin_path.is_file():
        return False
    if records_path.stat().st_mtime < assets_bin_path.stat().st_mtime:
        return False
    if index_path is not None:
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if index.get("schema_version") != SCHEMA_VERSION:
            return False
    return True


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically.

    Writes to a sibling ``<target>.tmp`` then ``os.replace`` swaps it
    into place. Survives SIGINT and cross-device tmp paths (the tmp
    file lives next to the target, same volume).
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def build(
    *,
    config: PipelineConfig | None = None,
    extract_textures: bool = True,
) -> dict[str, Any]:
    """Build the particle library from cached assets.bin.

    Decodes every Effect record (high-quality variant per base path),
    optionally extracts every referenced DDS texture into the workspace
    cache, and stamps ``textureUrl*`` URLs onto each record. Writes
    ``library/particles/records.json`` + ``index.json`` under the
    workspace.
    """
    cfg = config or PipelineConfig.load()
    workspace = cfg.workspace.resolve()
    paths = library_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)

    assets_bin_path = _assets_bin.ensure_dump(config=cfg)

    records: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    with ParticleStore.open(assets_bin_path) as store:
        for path in store.names():
            rec = store.get(path)
            if rec is None:
                unresolved.append(path)
            else:
                records[path] = rec

    textures_extracted = 0
    textures_missing: set[str] = set()
    velocity_fields_extracted = 0
    velocity_fields_missing: set[str] = set()
    atlas_stamped = 0
    atlas_entries = 0
    if extract_textures and records:
        tex_paths = _eff_tex.collect_texture_paths(records)
        if tex_paths:
            resolved_urls, textures_missing = _eff_tex.ensure_textures_on_disk(
                tex_paths, config=cfg,
            )
            _eff_tex.stamp_texture_urls(records, resolved_urls)
            textures_extracted = len(resolved_urls)

        velocity_field_paths = _eff_tex.collect_velocity_field_paths(records)
        if velocity_field_paths:
            resolved_vfd, velocity_fields_missing = (
                _eff_tex.ensure_velocity_fields_on_disk(
                    velocity_field_paths,
                    config=cfg,
                )
            )
            velocity_fields_extracted = len(resolved_vfd)

        # Atlas-mapped textures: the 117 ``.tga`` refs that don't ship
        # individually but live as named UV regions inside the 6
        # ``particles*.dds`` atlas pages. The manifest extraction also
        # pulls the 6 atlas DDS pages into the texture cache.
        atlas_map = _eff_tex.ensure_atlas_assets_on_disk(config=cfg)
        if atlas_map:
            atlas_entries = len(atlas_map)
            atlas_stamped = _eff_tex.stamp_atlas_urls(records, atlas_map)

    # Atomic write: a SIGINT mid-write would otherwise leave a truncated
    # records.json that ``is_current`` then accepts (mtime updated before
    # content). Pattern matches ``effects_textures.ensure_textures_on_disk``.
    _atomic_write_text(
        paths["records"],
        json.dumps(records, indent=2, sort_keys=True),
    )
    _atomic_write_text(
        paths["index"],
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "built_at": _now_iso(),
                "record_count": len(records),
                "unresolved_count": len(unresolved),
                "textures_extracted": textures_extracted,
                "textures_missing": len(textures_missing),
                "velocity_fields_extracted": velocity_fields_extracted,
                "velocity_fields_missing": len(velocity_fields_missing),
                "atlas_entries": atlas_entries,
                "atlas_stamped": atlas_stamped,
                "paths": sorted(records.keys()),
            },
            indent=2,
            sort_keys=True,
        ),
    )

    return {
        "status": "built",
        "paths_decoded": len(records),
        "paths_unresolved": len(unresolved),
        "textures_extracted": textures_extracted,
        "textures_missing": len(textures_missing),
        "velocity_fields_extracted": velocity_fields_extracted,
        "velocity_fields_missing": len(velocity_fields_missing),
        "atlas_entries": atlas_entries,
        "atlas_stamped": atlas_stamped,
        "records_path": str(paths["records"]),
        "index_path": str(paths["index"]),
    }


def ensure_built(
    *,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    """Build the library only when stale or missing.

    Returns a ``status='cached'`` dict when the existing records
    artefact is newer than the assets.bin source.
    """
    cfg = config or PipelineConfig.load()
    workspace = cfg.workspace.resolve()
    paths = library_paths(workspace)

    assets_bin_path = _assets_bin.default_path(cfg)
    if is_current(paths["records"], assets_bin_path, paths["index"]):
        return {
            "status": "cached",
            "records_path": str(paths["records"]),
            "index_path": str(paths["index"]),
        }

    return build(config=cfg)


__all__ = [
    "LIBRARY_ROOT",
    "RECORDS_FILE",
    "INDEX_FILE",
    "SCHEMA_VERSION",
    "library_paths",
    "is_current",
    "build",
    "ensure_built",
]
