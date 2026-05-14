"""Pipeline configuration.

`PipelineConfig` carries the four paths every layer needs:

    game_dir     — WoWS Steam install (read by toolkit subcommands)
    toolkit_bin  — wowsunpack executable
    workspace    — per-ship working dirs + libraries/ + cache live under here
    cache_dir    — defaults to `workspace / ".cache"`; holds GameParams dump

Resolution order, picked at `PipelineConfig.load()` time:

    game_dir     ← WOWS_GAME_DIR env var
                ← user-config file (see `user_config_path()`)
                ← (no default — required for any toolkit op)
    toolkit_bin  ← WOWS_TOOLKIT_BIN env var
                ← user-config file
                ← shutil.which("wowsunpack")
                ← (no default — required for any toolkit op)
    workspace    ← WOWS_WORKSPACE env var
                ← cwd (so a checkout used as the workspace "just works")
    cache_dir    ← (not configurable directly — derived from workspace)

The user-config file (`%APPDATA%/wows-model-export/config.json` on
Windows, `~/.config/wows-model-export/config.json` elsewhere) lets the
webview Settings page persist `game_dir` / `toolkit_bin` /
`toolkit_timeout_s` once instead of asking users to wrangle env vars on
every shell. Env vars still win — the file is a fallback layer, not an
override. `workspace` stays env/CLI-only because it's the bootstrap key
(the rest of the config tree depends on it).

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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

logger = logging.getLogger(__name__)

# Keys persisted into the user-config JSON. The dict shape mirrors the
# `PipelineConfig` fields but excludes `workspace` (bootstrap-only) and
# `cache_dir` (derived). The webview Settings PUT validates against this
# allowlist before writing.
_USER_SETTINGS_KEYS: tuple[str, ...] = (
    "game_dir",
    "toolkit_bin",
    "toolkit_timeout_s",
)

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

    @classmethod
    def load(cls, *, env: dict[str, str] | None = None) -> PipelineConfig:
        """Resolve a config from environment + user-config file.

        Passing `env=` is for tests — defaults to `os.environ`. Never
        raises; missing values become `None` and surface as `ConfigError`
        only when something tries to use them.

        Precedence (highest wins): env var → user-config file →
        platform default. The user-config file is the persisted output
        of the webview Settings page; see :func:`user_config_path`.
        """
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

        workspace = _path_from_env(e, "WOWS_WORKSPACE") or Path.cwd()

        cache_dir = workspace / ".cache"

        toolkit_timeout_s = _float_from_env(e, "WOWS_TOOLKIT_TIMEOUT")
        if toolkit_timeout_s is None:
            raw = user_settings.get("toolkit_timeout_s")
            if isinstance(raw, int | float):
                toolkit_timeout_s = float(raw)

        return cls(
            game_dir=game_dir,
            toolkit_bin=toolkit_bin,
            workspace=workspace,
            cache_dir=cache_dir,
            toolkit_timeout_s=toolkit_timeout_s,
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


__all__ = [
    "PipelineConfig",
    "user_config_path",
    "load_user_settings",
    "save_user_settings",
]
