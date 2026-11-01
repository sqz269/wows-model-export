"""Game build discovery and build-specific toolkit calls.

The game keeps one ``bin/<build>/idx`` directory per installed build. The
highest-numbered one with ``.idx`` files is the build the toolkit reads by
default (``--game-dir``). The launcher usually keeps the previous build's
idx files too, until the next update deletes them, so two builds can be
compared without unpacking anything.

Also holds the build stamp used by the pipeline's on-disk caches
(``gameparams.json``, ``assets.bin``, ``camouflages.xml``, the VFS
manifest). Each cache gets a ``<file>.build`` sibling holding the build it
was made from; a cache whose stamp doesn't match the current build is
rebuilt on next use instead of silently serving last update's data.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit

STAMP_SUFFIX = ".build"


def bin_dir(config: PipelineConfig | None = None) -> Path:
    cfg = config or PipelineConfig.load()
    return cfg.require_game_dir() / "bin"


def available_builds(config: PipelineConfig | None = None) -> list[int]:
    """Builds that still have idx files on disk, oldest first."""
    root = bin_dir(config)
    builds: list[int] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return builds
    for entry in entries:
        if not entry.name.isdigit():
            continue
        idx = entry / "idx"
        try:
            if idx.is_dir() and any(p.suffix == ".idx" for p in idx.iterdir()):
                builds.append(int(entry.name))
        except OSError:
            continue
    return sorted(builds)


_CURRENT_TTL_S = 10.0
_current_cache: dict[str, tuple[float, int | None]] = {}
_current_lock = threading.Lock()


def current_build(config: PipelineConfig | None = None) -> int | None:
    """The build the toolkit reads by default, or ``None`` if none is found.

    Cached for a few seconds per game dir: cache checks call this on hot
    paths (every GameParams lookup goes through ``ensure_dump``)."""
    cfg = config or PipelineConfig.load()
    key = str(cfg.require_game_dir())
    now = time.monotonic()
    with _current_lock:
        hit = _current_cache.get(key)
        if hit and now - hit[0] < _CURRENT_TTL_S:
            return hit[1]
    builds = available_builds(cfg)
    build = builds[-1] if builds else None
    with _current_lock:
        _current_cache[key] = (now, build)
    return build


def idx_dir(build: int, config: PipelineConfig | None = None) -> Path:
    path = bin_dir(config) / str(build) / "idx"
    if not path.is_dir():
        raise FileNotFoundError(f"no idx files for build {build} at {path}")
    return path


def build_args(build: int | None, config: PipelineConfig | None = None) -> list[str]:
    """Toolkit global args that select ``build`` (or the current one)."""
    cfg = config or PipelineConfig.load()
    if build is None:
        return ["--game-dir", str(cfg.require_game_dir())]
    return ["--idx-files", str(idx_dir(build, cfg))]


def metadata_hashed(
    out_path: Path | str | os.PathLike,
    *,
    build: int | None = None,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Dump every VFS entry with CRC32 / size, plus content hashes for the
    records synthesised from ``assets.bin`` (``metadata --content-hashes``)."""
    cfg = config or PipelineConfig.load()
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        *build_args(build, cfg),
        "metadata", "--format", "json", "--content-hashes", str(out),
    ]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,), record_deps=False)


def gameparams_entity_hashes(
    out_path: Path | str | os.PathLike,
    *,
    build: int | None = None,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """One content hash per top-level GameParams entity
    (``game-params --entity-hashes``)."""
    cfg = config or PipelineConfig.load()
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [*build_args(build, cfg), "game-params", "--entity-hashes", str(out)]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,), record_deps=False)


def extract_from_build(
    patterns: list[str],
    out_dir: Path | str | os.PathLike,
    *,
    build: int,
    config: PipelineConfig | None = None,
) -> Path:
    """Extract files from a specific build's packages into ``out_dir``.

    Fails if that build's ``.pkg`` files are gone (the launcher deletes
    them with the update after next)."""
    cfg = config or PipelineConfig.load()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    argv = [*build_args(build, cfg), "extract", "--out-dir", str(out), *patterns]
    run_toolkit(argv, config=cfg, record_deps=False)
    return out


# ---------------------------------------------------------------------------
# Cache build stamps
# ---------------------------------------------------------------------------


def stamp_path(cache_file: Path) -> Path:
    return cache_file.with_name(cache_file.name + STAMP_SUFFIX)


def read_stamp(cache_file: Path) -> int | None:
    try:
        text = stamp_path(cache_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def write_stamp(cache_file: Path, build: int | None) -> None:
    if build is None:
        return
    stamp_path(cache_file).write_text(f"{build}\n", encoding="utf-8")


def cache_is_current(cache_file: Path, config: PipelineConfig | None = None) -> bool:
    """True if ``cache_file`` exists and was built from the current build.

    Unstamped caches (made before stamps existed) count as out of date.
    If no build can be found (no game dir, unusual install), any existing
    file is accepted so nothing breaks."""
    if not cache_file.is_file():
        return False
    try:
        build = current_build(config)
    except Exception:  # noqa: BLE001 - missing game dir etc.
        return True
    if build is None:
        return True
    return read_stamp(cache_file) == build


_cache_locks: dict[str, threading.RLock] = {}
_cache_locks_guard = threading.Lock()


def cache_lock(cache_file: Path) -> threading.RLock:
    """Lock for rebuilding one cache file. Exports running in parallel can
    all find a cache stale at once; the first rebuilds it and the rest
    wait, then find it current (rebuilds write a fixed temp file next to
    the cache, so two at once would collide)."""
    key = os.path.normcase(os.path.abspath(cache_file))
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.RLock())
