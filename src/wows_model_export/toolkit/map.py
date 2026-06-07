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


# ── space-listing helper (no toolkit dep, fast) ───────────────────────


def list_spaces(config: PipelineConfig | None = None) -> list[str]:
    """Enumerate available space VFS paths (``"spaces/14_Atlantic"`` …).

    Prefers a local ``res_unpack/spaces/`` directory under
    ``--game-dir`` when present (most game installs unpack on first
    launch; a 1-stat directory scan is ~ms). Falls back to the cached
    VFS manifest under ``cache_dir`` when ``res_unpack`` is absent.

    Returns the list sorted alphabetically. Empty list signals "neither
    source is available" — the caller should surface that as a 503 with
    setup guidance, not a 500.
    """
    cfg = config or PipelineConfig.load()
    game_dir = cfg.require_game_dir()

    unpacked = Path(game_dir) / "res_unpack" / "spaces"
    if unpacked.is_dir():
        return sorted(
            f"spaces/{p.name}"
            for p in unpacked.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    # Fallback: scan the cached VFS manifest. Same shape as `metadata_json`
    # output. We don't auto-build it here — that's a multi-second op and
    # belongs to a separate "build manifest" endpoint.
    from .vfs import default_manifest_path

    manifest_path = default_manifest_path(cfg)
    if not manifest_path.is_file():
        return []

    import json
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    # Manifest is `{path: {size, crc32, ...}}`. Spaces are directories
    # so we collect the parent of any file under `spaces/<name>/`.
    seen: set[str] = set()
    for vfs_path in manifest:
        if vfs_path.startswith("spaces/"):
            parts = vfs_path.split("/", 2)
            if len(parts) >= 2:
                seen.add(f"spaces/{parts[1]}")
    return sorted(seen)


__all__ = ["export_map", "list_spaces"]

# Defensive: surface ToolkitError for `import *` consumers.
_ = ToolkitError
