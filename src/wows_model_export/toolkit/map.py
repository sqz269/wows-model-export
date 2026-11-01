"""Map / dock / operations-space export subcommand of `wowsunpack`.

Single callable:

- `export_map` — whole-space VFS dir → single GLB containing terrain +
                 water + static-model placements + (optionally)
                 vegetation. Wraps `wowsunpack export-map`.

See ``reference/maps/map_extraction.md`` for the surrounding format
notes, ``reference/maps/map_extraction_audit_2026_05_21.md`` for the
original audit, and the ``reference/systems/map_pass_*`` notes for the
current verification state. The current verification branch surfaces
non-model scene extras and can optionally emit a joined map collision
manifest sidecar for obstacle/collision-model inspection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import PipelineConfig
from ..errors import ToolkitError
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def export_map(
    space_dir: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
    lod: int = 0,
    terrain_step: int = 4,
    no_terrain: bool = False,
    no_water: bool = False,
    no_vegetation: bool = False,
    no_textures: bool = False,
    vegetation_density: float = 0.0,
    max_texture_size: int | None = None,
    lightmap_density: int = 4,
    collision_manifest_json: Path | str | os.PathLike | None = None,
) -> ToolkitResult:
    """Export a single space (battle map / dock / operations scenario) to
    a single GLB.

    ``space_dir`` is the VFS-relative path to the space directory, e.g.
    ``"spaces/14_Atlantic"``, ``"spaces/dock_Dunkirk"``,
    ``"spaces/s02_Naval_Defense"``. Listing the full set is the caller's
    responsibility (see :func:`list_spaces`).

    Output GLB contains:

    - ``Terrain`` node — heightmap mesh (decimable via ``terrain_step``;
      see audit doc for the approximation caveat — RLE tile data isn't
      decoded).
    - ``Water`` node — flat plane at the space bounds.
    - One node per ``space.bin.models[]`` instance, named after the
      prototype's resolved asset name (``OBC008_47``) when known, else
      ``Instance_47``. Node extras include landscape/min-quality/LOD
      metadata and shallow dye/material-override presence.
    - Scene extras for bounds, fog, obstacles, particles, decals, probes,
      user objects, and engine point lights.
    - When ``no_vegetation=False``, one glTF node per tree from forest
      Layer[0] Primary LOD0. The webview collapses these into
      ``THREE.InstancedMesh`` buckets per species at load time.
    - Optional ``collision_manifest_json`` sidecar with obstacle
      placements joined to raw collision-model face loops. This is
      diagnostic/proxy data, not native runtime solver parity.

    ``terrain_step`` decimates the heightmap (1 = full, 4 = default, 8 =
    coarse). ``vegetation_density`` (m) is a per-species
    one-tree-per-cell decimation; ``0`` keeps every parsed tree.

    ``max_texture_size`` caps any dimension of emitted textures via box
    filter; ``None`` keeps original sizes (large maps push GLB size into
    hundreds of MB).

    `ToolkitResult.output_paths` carries the single GLB on success.
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    collision_out = (
        Path(collision_manifest_json).resolve()
        if collision_manifest_json is not None
        else None
    )
    if collision_out is not None:
        collision_out.parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = [
        "--game-dir", str(cfg.require_game_dir()),
        "export-map", space_dir,
        "--output", str(out),
        "--lod", str(lod),
        "--terrain-step", str(terrain_step),
        "--vegetation-density", str(vegetation_density),
        "--lightmap-density", str(lightmap_density),
    ]
    if no_terrain:
        argv.append("--no-terrain")
    if no_water:
        argv.append("--no-water")
    if no_vegetation:
        argv.append("--no-vegetation")
    if no_textures:
        argv.append("--no-textures")
    if max_texture_size is not None:
        argv += ["--max-texture-size", str(max_texture_size)]
    if collision_out is not None:
        argv += ["--collision-manifest-json", str(collision_out)]

    expect_outputs = (out, collision_out) if collision_out is not None else (out,)
    return run_toolkit(argv, config=cfg, expect_outputs=expect_outputs)


def export_decal_textures(
    space_dir: str,
    out_dir: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Export a space's static-decal texture payload as PNGs.

    Wraps ``wowsunpack export-decals``: parses ``space.bin``
    ``staticDecals[]``, resolves every referenced texture against the VFS
    (the stored ``.tga`` references are authoring-time; the shipped client
    carries split-mip DDS conversions), decodes alpha-preserving, and
    writes flattened PNGs plus a ``decal_textures.json`` mapping
    (``{vfs_path: file_name}``, schema ``wows.map.decal_textures.v1``)
    into ``out_dir``.

    Consumers pair this store with ``static_decal_manifest.json``: each
    decal record's ``texture_paths`` triple resolves through the mapping
    (or the same deterministic flattening rule) to the PNG files.
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    argv: list[str] = [
        "--game-dir", str(cfg.require_game_dir()),
        "export-decals", space_dir,
        "--out-dir", str(out),
    ]
    return run_toolkit(argv, config=cfg, expect_outputs=(out / "decal_textures.json",))


# ── space listing (cached per game build) ─────── ───────────────────────


_SPACES_LIST_FILENAME = "spaces_list.json"


def _spaces_from_manifest(manifest_path: Path) -> list[str]:
    """``spaces/<name>`` for every space directory holding files in a VFS
    manifest (``wowsunpack metadata --format json``: a list of rows with a
    leading-slash ``path``)."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        paths = (row.get("path", "") if isinstance(row, dict) else str(row) for row in data)
    else:
        paths = iter(data)
    seen: set[str] = set()
    for vfs_path in paths:
        parts = vfs_path.replace("\\", "/").lstrip("/").split("/", 2)
        if len(parts) == 3 and parts[0] == "spaces" and parts[1]:
            seen.add(f"spaces/{parts[1]}")
    return sorted(seen)


def list_spaces(config: PipelineConfig | None = None) -> list[str]:
    """Enumerate the current game build's space VFS paths
    (``"spaces/14_Atlantic"`` ...), sorted.

    Read from the VFS manifest and kept in a build-stamped
    ``spaces_list.json`` beside it, so only the first call after a game
    update pays for the manifest. ``res_unpack/`` is not consulted: it is a
    one-off manual unpack that misses spaces later updates add and still
    lists the ones they remove.
    """
    from . import build as _build
    from .vfs import ensure_manifest

    cfg = config or PipelineConfig.load()
    cfg.require_game_dir()
    cache_dir = cfg.cache_dir or (cfg.workspace / ".cache")
    cache = cache_dir / _SPACES_LIST_FILENAME
    if not _build.cache_is_current(cache, cfg):
        with _build.cache_lock(cache):
            if not _build.cache_is_current(cache, cfg):
                spaces = _spaces_from_manifest(ensure_manifest(config=cfg))
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_name(cache.name + ".tmp")
                tmp.write_text(json.dumps(spaces, indent=0) + "\n", encoding="utf-8")
                os.replace(tmp, cache)
                _build.write_stamp(cache, _build.current_build(cfg))
    try:
        return list(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return []


__all__ = ["export_map", "export_decal_textures", "list_spaces"]

# Defensive: surface ToolkitError for `import *` consumers.
_ = ToolkitError
