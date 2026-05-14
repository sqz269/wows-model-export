"""`wowsunpack extract` + `metadata` subcommand wrappers.

Low-level VFS operations:

- `extract`        — pull files from the WoWS VFS to disk by glob
                     patterns. Used by `ingest_skin_pack` and the
                     compare-* family for fetching specific assets
                     from `assets.bin`.
- `metadata_json`  — dump the full VFS manifest (file paths, sizes,
                     CRC32) as JSON. Useful when looking up
                     case-correct VFS paths before calling
                     `export_model`.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def extract(
    files: list[str],
    out_dir: Path | str | os.PathLike,
    *,
    flatten: bool = False,
    strip_prefix: bool = False,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Extract files from the WoWS VFS to ``out_dir``.

    ``files`` accepts glob patterns:
        - ``"**/camouflages.xml"``
        - ``"content/gameplay/usa/ship/**/*.dds"``

    ``flatten`` writes files at ``out_dir/<basename>`` instead of
    preserving the VFS path layout.

    ``strip_prefix`` drops the matched-pattern prefix from the output
    path (toolkit semantics — see ``wowsunpack extract --help``).

    `ToolkitResult.output_paths` carries only the resolved out_dir,
    since the actual file set is determined by the glob match and can
    be arbitrarily large. Callers walk `out_dir` to enumerate results.
    """
    cfg = config or PipelineConfig.load()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    argv = [
        "--game-dir", str(cfg.require_game_dir()),
        "extract", "--out-dir", str(out),
    ]
    if flatten:
        argv.append("--flatten")
    if strip_prefix:
        argv.append("--strip-prefix")
    argv.extend(files)

    # Don't pass expect_outputs — extract can succeed without writing
    # anything if the glob matches nothing. Callers that need a
    # presence check should walk `out_dir`.
    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


def metadata_json(
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Dump the full VFS manifest as JSON to ``out_path``.

    Output payload: file paths, sizes, CRC32 — useful for looking up
    case-correct VFS paths before calling ``export_model``.
    """
    cfg = config or PipelineConfig.load()
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        "--game-dir", str(cfg.require_game_dir()),
        "metadata", "--format", "json", str(out),
    ]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


__all__ = ["extract", "metadata_json"]
