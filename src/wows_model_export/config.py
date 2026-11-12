"""Pipeline configuration.

`PipelineConfig` carries the four paths every layer needs:

    game_dir     — WoWS Steam install (read by toolkit subcommands)
    toolkit_bin  — wowsunpack executable
    workspace    — per-ship working dirs + libraries/ + cache live under here
    cache_dir    — defaults to `workspace / ".cache"`; holds GameParams dump

Two optional fields select a game build other than the install's:

    archive_dir  — an append-only archive of game builds laid out like the
                   install (`bin/<build>/idx`, `res_packages/`); see
                   `compose.build_archive`
    build        — the pinned game build (`None` = the build the install
                   runs now). A pinned config reads that build (from the
                   archive, else from the install while its idx files are
                   still there) and writes into its own workspace,
                   `<workspace>/builds/<build>/`, so an older build's
                   exports never overwrite the current ones. Made with
                   `PipelineConfig.for_build`.

Resolution order, picked at `PipelineConfig.load()` time:

    game_dir     ← WOWS_GAME_DIR env var
                ← user-config file (see `user_config_path()`)
                ← (no default — required for any toolkit op)
    toolkit_bin  ← WOWS_TOOLKIT_BIN env var
                ← user-config file
                ← shutil.which("wowsunpack")
                ← (no default — required for any toolkit op)
    workspace    ← WOWS_WORKSPACE env var
                ← user-config file
                ← cwd (so a checkout used as the workspace "just works")
    cache_dir    ← (not configurable directly — derived from workspace)
    archive_dir  ← WOWS_ARCHIVE_DIR env var
                ← user-config file
                ← (no default — no archive)
    build        ← WOWS_BUILD env var (a CLI's `--build` wins)
                ← (default: the install's current build)

The user-config file (`%APPDATA%/wows-model-export/config.json` on
Windows, `~/.config/wows-model-export/config.json` elsewhere) lets the
webview Settings page persist every knob once instead of asking users
to wrangle env vars on every shell. Env vars still win — the file is a
fallback layer, not an override. Note that the user-config file lives
in the platform user-data dir (not under workspace), so persisting
workspace from there is not circular.

`load()` does **not** raise on missing values; it returns a config with
the field unresolved (`None`). Layer 2 / Layer 4 entries that actually
need a value raise `ConfigError` at use time with a precise pointer at
which env var to set. This keeps cheap reads (`read.sidecar`,
`read.library_index`) from failing just because the game install isn't
configured on the host.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from .errors import ConfigError

logger = logging.getLogger(__name__)

# Keys persisted into the user-config JSON. The dict shape mirrors the
# `PipelineConfig` fields but excludes `workspace` (bootstrap-only) and
# `cache_dir` (derived). The webview Settings PUT validates against this
# allowlist before writing.
_USER_SETTINGS_KEYS: tuple[str, ...] = (
    "game_dir",
    "toolkit_bin",
    "workspace",
    "toolkit_timeout_s",
    "archive_dir",
)

# Folder under a workspace that holds the per-build workspaces of pinned
# configs: ``<workspace>/builds/<build>/``.
BUILD_WORKSPACES_DIR = "builds"


def build_workspace(base: Path, build: int) -> Path:
    """The workspace a config pinned to ``build`` writes into."""
    return Path(base) / BUILD_WORKSPACES_DIR / str(int(build))

# Sentinel for "not yet resolved". We use `None` rather than a class-level
# sentinel so dataclass equality + hashing stay predictable.
_UNRESOLVED = None


@dataclass(frozen=True)
class PipelineConfig:
    """Resolved pipeline paths.

    A field set to `None` means "not configured" — the consumer should
    call `require_*()` to get a helpful `ConfigError` at the point of
    use, rather than a generic `AttributeError` deep inside a composer.
    """

    game_dir:    Path | None = None
    toolkit_bin: Path | None = None
    workspace:   Path        = field(default_factory=Path.cwd)
    cache_dir:   Path | None = None
    # User-overridable settings carried alongside paths.
    toolkit_timeout_s: float | None = None
    # Game-build selection (see the module docstring). ``base_workspace``
    # is the workspace a pinned config was derived from; ``None`` when
    # the config is not pinned.
    archive_dir: Path | None = None
    build: int | None = None
    base_workspace: Path | None = None

    @classmethod
    def load(cls, *, env: dict[str, str] | None = None) -> PipelineConfig:
        """Resolve a config from environment + user-config file.

        Passing `env=` is for tests — defaults to `os.environ`. Never
        raises; missing values become `None` and surface as `ConfigError`
        only when something tries to use them.

        Precedence (highest wins): env var → user-config file →
        platform default. The user-config file is the persisted output
        of the webview Settings page; see :func:`user_config_path`.

        Inside :func:`activated` (and in a CLI process given ``--build``,
        see :func:`set_process_default`) this returns that config
        instead, so a call deep in the pipeline that falls back to
        ``PipelineConfig.load()`` keeps reading the pinned build.
        """
        if env is None:
            active = active_config()
            if active is not None:
                return active
        e = env if env is not None else os.environ
        user_settings = load_user_settings()

        game_dir = _path_from_env(e, "WOWS_GAME_DIR") or _path_from_dict(
            user_settings, "game_dir"
        )

        toolkit_bin = _path_from_env(e, "WOWS_TOOLKIT_BIN") or _path_from_dict(
            user_settings, "toolkit_bin"
        )
        if toolkit_bin is None:
            found = shutil.which("wowsunpack") or shutil.which("wowsunpack.exe")
            toolkit_bin = Path(found) if found else None

        workspace = (
            _path_from_env(e, "WOWS_WORKSPACE")
            or _path_from_dict(user_settings, "workspace")
            or Path.cwd()
        )

        cache_dir = workspace / ".cache"

        toolkit_timeout_s = _float_from_env(e, "WOWS_TOOLKIT_TIMEOUT")
        if toolkit_timeout_s is None:
            raw = user_settings.get("toolkit_timeout_s")
            if isinstance(raw, int | float):
                toolkit_timeout_s = float(raw)

        archive_dir = _path_from_env(e, "WOWS_ARCHIVE_DIR") or _path_from_dict(
            user_settings, "archive_dir"
        )

        cfg = cls(
            game_dir=game_dir,
            toolkit_bin=toolkit_bin,
            workspace=workspace,
            cache_dir=cache_dir,
            toolkit_timeout_s=toolkit_timeout_s,
            archive_dir=archive_dir,
        )
        build = _int_from_env(e, "WOWS_BUILD")
        return cfg.for_build(build) if build is not None else cfg

    # ----- game build selection ---------------------------------------------

    @property
    def root_workspace(self) -> Path:
        """The workspace this config belongs to: itself, or the one a
        pinned config's build workspace lives in."""
        return self.base_workspace or self.workspace

    @property
    def root_cache_dir(self) -> Path:
        """Cache shared by every build workspace of the root workspace
        (the per-build manifests live here, so a diff sees every build)."""
        if self.base_workspace is not None:
            return self.base_workspace / ".cache"
        return self.cache_dir or (self.workspace / ".cache")

    def for_build(self, build: int | None) -> PipelineConfig:
        """This config pinned to game ``build``.

        ``None``, or the build the install runs now, gives the unpinned
        config of the root workspace (it follows the install). Any other
        build gives a config that reads that build and writes into
        ``<workspace>/builds/<build>/``. Whether the build can be read
        (archived, or still in the install) is checked when a toolkit
        call needs it; :func:`toolkit.build.require_source` checks it
        up front.
        """
        root = self.root_workspace
        unpinned = replace(
            self, build=None, base_workspace=None, workspace=root,
            cache_dir=root / ".cache",
        )
        if build is None:
            return unpinned
        build = int(build)
        from .toolkit import build as _build  # late: toolkit imports config

        if _build.install_current_build(unpinned) == build:
            return unpinned
        ws = build_workspace(root, build)
        return replace(
            self, build=build, base_workspace=root, workspace=ws,
            cache_dir=ws / ".cache",
        )

    # ----- accessors that raise on missing values ----------------------------

    def require_game_dir(self) -> Path:
        if self.game_dir is None:
            raise ConfigError(
                "WOWS_GAME_DIR is not set; point it at the World of Warships "
                "install (the directory containing WorldOfWarships.exe)."
            )
        return self.game_dir

    def require_toolkit_bin(self) -> Path:
        if self.toolkit_bin is None:
            raise ConfigError(
                "wowsunpack binary not found; set WOWS_TOOLKIT_BIN, put "
                "wowsunpack on PATH, or build it from the wows-toolkit "
                "fork (cargo build --release --bin wowsunpack)."
            )
        return self.toolkit_bin

    def require_cache_dir(self) -> Path:
        # cache_dir is always derived from workspace; this just enforces
        # that the directory exists at the point of use.
        if self.cache_dir is None:
            raise ConfigError("cache_dir is unset (workspace was not resolved).")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir

    def require_archive_dir(self) -> Path:
        if self.archive_dir is None:
            raise ConfigError(
                "no build archive configured; set WOWS_ARCHIVE_DIR or the "
                "Settings page's archive directory (a folder laid out like "
                "the game install: bin/<build>/idx and res_packages/)."
            )
        return self.archive_dir


def _int_from_env(env: Mapping[str, str], key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    return int(raw) if raw.isdigit() else None


def _path_from_env(env: Mapping[str, str], key: str) -> Path | None:
    raw = env.get(key)
    if not raw:
        return None
    return Path(raw).expanduser()


def _path_from_dict(d: dict[str, Any], key: str) -> Path | None:
    raw = d.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw).expanduser()


def _float_from_env(env: Mapping[str, str], key: str) -> float | None:
    raw = env.get(key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def user_config_path() -> Path:
    """Path the webview Settings page reads/writes.

    Windows:  ``%APPDATA%/wows-model-export/config.json``
    macOS:    ``~/Library/Application Support/wows-model-export/config.json``
    Linux:    ``$XDG_CONFIG_HOME/wows-model-export/config.json`` (or
              ``~/.config/...`` when ``XDG_CONFIG_HOME`` is unset).

    The path is **not** created here — callers create the parent on
    write, and tolerate a missing file on read.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "wows-model-export" / "config.json"


def load_user_settings(path: Path | None = None) -> dict[str, Any]:
    """Read the persisted user settings from disk.

    Returns ``{}`` when the file is missing, unreadable, or carries a
    non-object JSON root. Errors are swallowed (logged at WARNING) so a
    malformed file never breaks startup — the operator can re-save from
    the Settings page to overwrite it.
    """
    p = path if path is not None else user_config_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as err:
        logger.warning("user-config read failed at %s: %s", p, err)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        logger.warning("user-config parse failed at %s: %s", p, err)
        return {}
    if not isinstance(data, dict):
        return {}
    # Strip unknown keys at read time so legacy / typo'd entries don't
    # poison downstream consumers. The Settings PUT validates writes
    # against the same allowlist.
    return {k: v for k, v in data.items() if k in _USER_SETTINGS_KEYS}


def save_user_settings(settings: dict[str, Any], path: Path | None = None) -> Path:
    """Atomically write the user settings JSON.

    Filters to the known-key allowlist (``_USER_SETTINGS_KEYS``) so a
    caller can pass a fuller dict without leaking stray fields onto
    disk. Parent directory created as needed. Returns the path written.

    Atomic via tempfile + ``os.replace`` so an interrupted write never
    leaves a half-written file the next ``load_user_settings`` would
    barf on.
    """
    p = path if path is not None else user_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: dict[str, Any] = {}
    for k in _USER_SETTINGS_KEYS:
        if k in settings and settings[k] not in (None, ""):
            clean[k] = settings[k]
    # Write to a temp file in the same dir, then atomically rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".json", dir=str(p.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(clean, fp, indent=2, sort_keys=True)
            fp.write("\n")
        os.replace(tmp, p)
    except Exception:
        # Best-effort cleanup on failure — the os.replace would have
        # consumed the tmp on success, so this only runs on the error
        # path.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


# ---------------------------------------------------------------------------
# Active config: what PipelineConfig.load() returns inside a pinned run
# ---------------------------------------------------------------------------

# Per-context override (a server job running for one build) and a
# process-wide one (a CLI given --build). Most of the pipeline threads its
# config through explicitly; these make the remaining
# ``config or PipelineConfig.load()`` fallbacks read the same build.
T = TypeVar("T")

_ACTIVE: ContextVar[PipelineConfig | None] = ContextVar("wows_active_config", default=None)
_PROCESS_DEFAULT: PipelineConfig | None = None
_PROCESS_LOCK = threading.Lock()


def active_config() -> PipelineConfig | None:
    """The config :meth:`PipelineConfig.load` returns instead of resolving
    one, or ``None``."""
    return _ACTIVE.get() or _PROCESS_DEFAULT


@contextmanager
def activated(config: PipelineConfig) -> Iterator[PipelineConfig]:
    """Make ``config`` what ``PipelineConfig.load()`` returns in this
    context. Worker threads started inside need the context passed on
    (``build_deps.bind``)."""
    token = _ACTIVE.set(config)
    try:
        yield config
    finally:
        _ACTIVE.reset(token)


def bind_active(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap ``fn`` so a worker thread runs it under the caller's active
    config (``activated``). Returns ``fn`` itself when none is active."""
    cfg = _ACTIVE.get()
    if cfg is None:
        return fn

    def run(*args: Any, **kwargs: Any) -> T:
        with activated(cfg):
            return fn(*args, **kwargs)

    return run


def set_process_default(config: PipelineConfig | None) -> None:
    """Make ``config`` what ``PipelineConfig.load()`` returns in every
    thread of this process (``None`` clears it). For one-build processes
    such as a CLI run with ``--build``; never for the webview server,
    which serves several builds."""
    global _PROCESS_DEFAULT
    with _PROCESS_LOCK:
        _PROCESS_DEFAULT = config


__all__ = [
    "BUILD_WORKSPACES_DIR",
    "PipelineConfig",
    "activated",
    "active_config",
    "bind_active",
    "build_workspace",
    "set_process_default",
    "user_config_path",
    "load_user_settings",
    "save_user_settings",
]
