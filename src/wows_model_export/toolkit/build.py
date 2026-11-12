"""Game build discovery and build-specific toolkit calls.

The game keeps one ``bin/<build>/idx`` directory per installed build. The
highest-numbered one with ``.idx`` files is the build the toolkit reads by
default (``--game-dir``). The launcher usually keeps the previous build's
idx files too, until the next update deletes them, so two builds can be
compared without unpacking anything.

An append-only build archive (``PipelineConfig.archive_dir``, filled by
``compose.build_archive``) keeps every build's ``bin/<build>/`` folder and
every package ever seen, laid out like the install, so an archived build
stays readable after the launcher has deleted it. A config pinned to a
build (``PipelineConfig.for_build``) reads it through :func:`source_args`:
from the archive when the build is archived there, else from the install
while the install still has its idx files.

Also holds the build stamp used by the pipeline's on-disk caches
(``gameparams.json``, ``assets.bin``, ``camouflages.xml``, the VFS
manifest). Each cache gets a ``<file>.build`` sibling holding the build it
was made from; a cache whose stamp doesn't match the build the config
reads is rebuilt on next use instead of silently serving another build's
data.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import PipelineConfig
from ..errors import ConfigError
from ..types import ToolkitResult
from ._subprocess import run_toolkit

STAMP_SUFFIX = ".build"

SourceOrigin = Literal["install", "archive"]


def bin_dir(config: PipelineConfig | None = None) -> Path:
    """The install's ``bin/`` folder."""
    cfg = config or PipelineConfig.load()
    return cfg.require_game_dir() / "bin"


def _builds_with_idx(root: Path) -> list[int]:
    """Numeric ``<root>/<build>`` folders that hold ``.idx`` files."""
    builds: list[int] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return builds
    for entry in entries:
        if entry.name.isdigit() and _has_idx(entry / "idx"):
            builds.append(int(entry.name))
    return sorted(builds)


def _has_idx(path: Path) -> bool:
    try:
        return path.is_dir() and any(p.suffix == ".idx" for p in path.iterdir())
    except OSError:
        return False


def available_builds(config: PipelineConfig | None = None) -> list[int]:
    """Builds the install still has idx files for, oldest first."""
    cfg = config or PipelineConfig.load()
    if cfg.game_dir is None:
        return []
    return _builds_with_idx(cfg.game_dir / "bin")


def archived_builds(config: PipelineConfig | None = None) -> list[int]:
    """Builds the archive holds idx files for, oldest first (``[]`` when no
    archive is configured)."""
    cfg = config or PipelineConfig.load()
    if cfg.archive_dir is None:
        return []
    return _builds_with_idx(cfg.archive_dir / "bin")


def known_builds(config: PipelineConfig | None = None) -> list[int]:
    """Every build the pipeline can read: the install's and the archive's."""
    cfg = config or PipelineConfig.load()
    return sorted(set(available_builds(cfg)) | set(archived_builds(cfg)))


_CURRENT_TTL_S = 10.0
_current_cache: dict[str, tuple[float, int | None]] = {}
_current_lock = threading.Lock()


def install_current_build(config: PipelineConfig | None = None) -> int | None:
    """The build the install runs now (its newest with idx files), or
    ``None`` without an install. Ignores any pinned build.

    Cached for a few seconds per game dir: cache checks call this on hot
    paths (every GameParams lookup goes through ``ensure_dump``)."""
    cfg = config or PipelineConfig.load()
    if cfg.game_dir is None:
        return None
    key = str(cfg.game_dir)
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


def current_build(config: PipelineConfig | None = None) -> int | None:
    """The build this config reads: its pinned build, else the one the
    install runs now (``None`` if none is found)."""
    cfg = config or PipelineConfig.load()
    if cfg.build is not None:
        return cfg.build
    cfg.require_game_dir()
    return install_current_build(cfg)


# ---------------------------------------------------------------------------
# Where a build is read from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameSource:
    """One readable game build: its idx folder and package folder.

    ``root`` is laid out like the install (``bin/<build>/``,
    ``res_packages/``): the install itself, or the build archive."""

    build: int
    origin: SourceOrigin
    root: Path

    @property
    def bin_dir(self) -> Path:
        """``bin/<build>/``: the idx files plus the loose ``res/`` (texts)."""
        return self.root / "bin" / str(self.build)

    @property
    def idx_dir(self) -> Path:
        return self.bin_dir / "idx"

    @property
    def pkg_dir(self) -> Path:
        return self.root / "res_packages"

    def toolkit_args(self) -> list[str]:
        # With --idx-files given, --game-dir only tells the toolkit where
        # the loose translations are (bin/<build>/res/texts); it no longer
        # picks the newest build.
        return [
            "--game-dir", str(self.root),
            "--idx-files", str(self.idx_dir),
            "--pkg-dir", str(self.pkg_dir),
        ]

    def as_dict(self) -> dict[str, object]:
        return {"build": self.build, "origin": self.origin, "root": str(self.root)}


def find_source(build: int, config: PipelineConfig | None = None) -> GameSource | None:
    """Where ``build`` can be read from: the archive first (it keeps every
    package the build's idx files name), then the install. ``None`` if
    neither has its idx files."""
    cfg = config or PipelineConfig.load()
    build = int(build)
    if cfg.archive_dir is not None:
        src = GameSource(build, "archive", cfg.archive_dir)
        if _has_idx(src.idx_dir) and src.pkg_dir.is_dir():
            return src
    if cfg.game_dir is not None:
        src = GameSource(build, "install", cfg.game_dir)
        if _has_idx(src.idx_dir):
            return src
    return None


def require_source(build: int, config: PipelineConfig | None = None) -> GameSource:
    """:func:`find_source`, raising ``ConfigError`` that says what is
    readable instead."""
    cfg = config or PipelineConfig.load()
    src = find_source(build, cfg)
    if src is not None:
        return src
    where = []
    if cfg.archive_dir is not None:
        where.append(f"the archive {cfg.archive_dir} has {archived_builds(cfg) or 'no builds'}")
    else:
        where.append("no archive is configured (WOWS_ARCHIVE_DIR)")
    if cfg.game_dir is not None:
        where.append(f"the install has {available_builds(cfg) or 'no builds'}")
    raise ConfigError(f"game build {build} is not readable: " + "; ".join(where))


def config_source(config: PipelineConfig | None = None) -> GameSource | None:
    """The source a config reads: its pinned build's, or (unpinned) the
    install's current build. ``None`` when unpinned without an install."""
    cfg = config or PipelineConfig.load()
    if cfg.build is not None:
        return require_source(cfg.build, cfg)
    build = install_current_build(cfg)
    if build is None or cfg.game_dir is None:
        return None
    return GameSource(build, "install", cfg.game_dir)


def source_args(config: PipelineConfig | None = None) -> list[str]:
    """Toolkit global args that select the build ``config`` reads.

    Unpinned: ``--game-dir <install>`` (the toolkit picks the newest
    build). Pinned: that build's idx and package folders."""
    cfg = config or PipelineConfig.load()
    if cfg.build is None:
        return ["--game-dir", str(cfg.require_game_dir())]
    return require_source(cfg.build, cfg).toolkit_args()


def res_bin_dir(config: PipelineConfig | None = None) -> Path:
    """``bin/<build>/`` of the build ``config`` reads (its loose ``res/``,
    e.g. the localization catalogue)."""
    cfg = config or PipelineConfig.load()
    src = config_source(cfg)
    if src is None:
        raise ConfigError("no game build found; set WOWS_GAME_DIR or pin an archived build")
    return src.bin_dir


def idx_dir(build: int, config: PipelineConfig | None = None) -> Path:
    src = find_source(build, config)
    if src is None:
        raise FileNotFoundError(f"no idx files for build {build} in the install or the archive")
    return src.idx_dir


def build_args(build: int | None, config: PipelineConfig | None = None) -> list[str]:
    """Toolkit global args that select ``build`` (``None``: the build the
    config reads)."""
    cfg = config or PipelineConfig.load()
    if build is None:
        return source_args(cfg)
    return require_source(build, cfg).toolkit_args()


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
