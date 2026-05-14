"""`wowsunpack dump-bones --json` subcommand wrapper.

Reads a single asset's `.visual` node tree (every named bone with its
rest-pose 4×4 matrix). Two forms:

- `dump_bones(vfs_path, out_path)`  — writes JSON to disk, returns
                                      `ToolkitResult`. Use when you
                                      want a persistent artifact.
- `fetch_bones(vfs_path)`           — uses a tempfile internally and
                                      returns the parsed dict
                                      directly. Use when the bones
                                      doc is consumed in-process and
                                      doesn't need to be cached.

Both forms target a VFS `.geometry` path; the toolkit auto-resolves the
paired `.visual` from `assets.bin`. WG's universal naming convention
(`Rotate_Y` / `Rotate_X` / `Roll_Back<N>` / `HP_gunFire<N>` for turrets;
weapon-named root with `HP_gunFire<N>` children for AA mounts) lets
consumers pick pivots by name — see `turret_autorig` for the canonical
extractor.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def dump_bones(
    geometry_vfs_path: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Write the asset's bone tree as JSON to ``out_path``.

    The output is the toolkit's verbatim `dump-bones --json` format:
    `{"asset": "<id>", "nodes": [{"name", "parent", "matrix"} …]}`. See
    `wows-toolkit/src/cli/dump_bones.rs` for the authoritative schema.
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "--game-dir", str(cfg.require_game_dir()),
        "dump-bones", geometry_vfs_path,
        "--json", str(out),
    ]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


def fetch_bones(
    geometry_vfs_path: str,
    *,
    config: PipelineConfig | None = None,
) -> dict:
    """Return the parsed bones JSON for an asset.

    Writes to a tempfile, reads, deletes — caller never sees a path.
    Use this when the bones doc is consumed in-process (e.g. by
    `turret_autorig`'s pivot extractor) and doesn't need to persist.

    Raises `ToolkitError` if the toolkit subprocess fails.
    """
    cfg = config or PipelineConfig.load()

    # Write to a tempfile; delete after read.
    with tempfile.NamedTemporaryFile(
        suffix=".bones.json", delete=False, mode="w", encoding="utf-8"
    ) as tf:
        tmp_path = Path(tf.name)
    try:
        dump_bones(geometry_vfs_path, tmp_path, config=cfg)
        return json.loads(tmp_path.read_text(encoding="utf-8"))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


__all__ = ["dump_bones", "fetch_bones"]
