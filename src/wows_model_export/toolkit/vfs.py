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
- `ensure_manifest` — idempotent helper that returns a usable manifest
                     path, building one via ``metadata_json`` if it
                     isn't already on disk. Mirrors
                     ``toolkit.gameparams.ensure_dump``.
- `default_manifest_path` — return the canonical manifest location
                     (env var ``WOWS_VFS_MANIFEST`` or
                     ``<cache_dir>/wows_manifest.json``) without
                     touching disk.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit

DEFAULT_MANIFEST_FILENAME = "wows_manifest.json"


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
    # anything if the glob matches nothing, AND `out` is a directory
    # (run_toolkit's check uses `Path.is_file()` which would always fail
    # on a dir and raise even after a successful extract). Callers that
    # need a presence check should walk `out_dir`.
    result = run_toolkit(argv, config=cfg)
    # Re-stamp `output_paths` so the docstring contract holds even though
    # we skipped the post-exit existence check.
    return ToolkitResult(
        output_paths=(out,),
        stderr=result.stderr,
        elapsed_ms=result.elapsed_ms,
        data=result.data,
        stdout=result.stdout,
    )


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


def default_manifest_path(config: PipelineConfig | None = None) -> Path:
    """Return the default VFS manifest location without building it.

    Resolution order:
        1. ``WOWS_VFS_MANIFEST`` env var (verbatim).
        2. ``<config.cache_dir>/wows_manifest.json`` (config loaded from
           env when ``None``).
        3. ``<config.workspace>/.cache/wows_manifest.json`` as a final
           fallback for unconfigured ``cache_dir``.

    Pure read of config + env; does not call the toolkit. Use
    :func:`ensure_manifest` to actually materialise the file.
    """
    env_override = os.environ.get("WOWS_VFS_MANIFEST")
    if env_override:
        return Path(env_override).expanduser()
    cfg = config or PipelineConfig.load()
    cache_dir = cfg.cache_dir or (cfg.workspace / ".cache")
    return cache_dir / DEFAULT_MANIFEST_FILENAME


def ensure_manifest(
    *,
    manifest_path: Path | None = None,
    refresh: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Ensure a VFS manifest exists at ``manifest_path``; build via
    :func:`metadata_json` if missing.

    ``manifest_path`` defaults to :func:`default_manifest_path`.

    Idempotent: if the file already exists and ``refresh`` is False,
    returns the path without re-extracting. Pass ``refresh=True`` after
    a game patch (or delete the file) to force a fresh dump.

    Mirrors :func:`wows_model_export.toolkit.gameparams.ensure_dump`.
    """
    cfg = config or PipelineConfig.load()
    if manifest_path is None:
        manifest_path = default_manifest_path(cfg)
    else:
        manifest_path = Path(manifest_path)
    if manifest_path.is_file() and not refresh:
        return manifest_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_json(manifest_path, config=cfg)
    return manifest_path


__all__ = [
    "extract",
    "metadata_json",
    "default_manifest_path",
    "ensure_manifest",
    "DEFAULT_MANIFEST_FILENAME",
]
