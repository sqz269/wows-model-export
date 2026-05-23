"""`wowsunpack extract` driver for ``content/assets.bin``.

The Effect blob inside ``assets.bin`` is the on-disk form of every
particle authoring record the game ships (3329 records in build
12267945). The toolkit doesn't have a dedicated dump subcommand for it,
so we extract the raw file (~170 MB) and let
:class:`wows_model_export.read.particles.ParticleStore` mmap it.

Resolution order for the cache path (low → high precedence):

1. ``<config.cache_dir>/assets.bin`` — the canonical pipeline cache.
2. ``$WOWS_ASSETS_BIN`` — explicit override; useful when an external
   workflow already extracted the file to a known location.

``ensure_dump`` mirrors :func:`wows_model_export.toolkit.gameparams.ensure_dump`:
idempotent, atomic (writes to ``<path>.tmp`` then ``os.replace``), and
returns the resolved path.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..config import PipelineConfig
from . import vfs as _vfs


def default_path(config: PipelineConfig | None = None) -> Path:
    """Return the path to the assets.bin cache without touching disk.

    Resolution: ``$WOWS_ASSETS_BIN`` env var, then
    ``<config.cache_dir>/assets.bin``.
    """
    env_path = os.environ.get("WOWS_ASSETS_BIN")
    if env_path:
        return Path(env_path).expanduser()
    cfg = config or PipelineConfig.load()
    cache_dir = cfg.cache_dir or (cfg.workspace / ".cache")
    return cache_dir / "assets.bin"


def ensure_dump(
    *,
    cache_path: Path | None = None,
    refresh: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Ensure ``assets.bin`` is on disk at ``cache_path``; extract if missing.

    ``cache_path`` defaults to :func:`default_path`.

    Idempotent. The toolkit's ``extract`` subcommand pulls the file out
    of the WoWS VFS to a temp directory; we then ``os.replace`` it into
    place so a SIGINT mid-extract can't leave a truncated file.
    """
    cfg = config or PipelineConfig.load()
    if cache_path is None:
        cache_path = default_path(cfg)
    else:
        cache_path = Path(cache_path)

    if cache_path.is_file() and not refresh:
        return cache_path

    print(
        f"[assets_bin] extracting assets.bin -> {cache_path} "
        "(~170 MB)...",
        file=sys.stderr,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Extract to a fresh tmp directory; the toolkit preserves the
    # ``content/assets.bin`` VFS path, so the file lands at
    # ``<tmp>/content/assets.bin``.
    with tempfile.TemporaryDirectory(prefix="wms-assetsbin-") as td:
        out_dir = Path(td)
        _vfs.extract(["**/assets.bin"], out_dir=out_dir, config=cfg)
        candidates = list(out_dir.rglob("assets.bin"))
        if not candidates:
            raise RuntimeError(
                f"assets_bin: extract did not produce any assets.bin under {out_dir}"
            )
        # In the typical case there's a single match at content/assets.bin.
        # If the VFS ever exposes alternates (per-region, fallback), we
        # take the largest — the canonical file is the heaviest by a
        # wide margin.
        src = max(candidates, key=lambda p: p.stat().st_size)
        # shutil.move rather than os.replace — the system temp dir is
        # often on a different drive than cache_dir, and os.replace
        # raises WinError 17 across drives on Windows.
        shutil.move(str(src), str(cache_path))
    return cache_path


__all__ = ["default_path", "ensure_dump"]
