"""Pipeline configuration.

`PipelineConfig` carries the four paths every layer needs:

    game_dir     — WoWS Steam install (read by toolkit subcommands)
    toolkit_bin  — wowsunpack executable
    workspace    — per-ship working dirs + libraries/ + cache live under here
    cache_dir    — defaults to `workspace / ".cache"`; holds GameParams dump

Resolution order, picked at `PipelineConfig.load()` time:

    game_dir     ← WOWS_GAME_DIR env var
                ← (no default — required for any toolkit op)
    toolkit_bin  ← WOWS_TOOLKIT_BIN env var
                ← shutil.which("wowsunpack")
                ← (no default — required for any toolkit op)
    workspace    ← WOWS_WORKSPACE env var
                ← cwd (so a checkout used as the workspace "just works")
    cache_dir    ← (not configurable directly — derived from workspace)

`load()` does **not** raise on missing values; it returns a config with
the field unresolved (`None`). Layer 2 / Layer 4 entries that actually
need a value raise `ConfigError` at use time with a precise pointer at
which env var to set. This keeps cheap reads (`read.sidecar`,
`read.library_index`) from failing just because the game install isn't
configured on the host.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

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
        """Resolve a config from environment variables.

        Passing `env=` is for tests — defaults to `os.environ`. Never
        raises; missing values become `None` and surface as `ConfigError`
        only when something tries to use them.
        """
        e = env if env is not None else os.environ

        game_dir = _path_from_env(e, "WOWS_GAME_DIR")

        toolkit_bin = _path_from_env(e, "WOWS_TOOLKIT_BIN")
        if toolkit_bin is None:
            found = shutil.which("wowsunpack") or shutil.which("wowsunpack.exe")
            toolkit_bin = Path(found) if found else None

        workspace = _path_from_env(e, "WOWS_WORKSPACE") or Path.cwd()

        cache_dir = workspace / ".cache"

        timeout_raw = e.get("WOWS_TOOLKIT_TIMEOUT")
        toolkit_timeout_s: float | None
        try:
            toolkit_timeout_s = float(timeout_raw) if timeout_raw else None
        except ValueError:
            toolkit_timeout_s = None

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


def _path_from_env(env: dict[str, str], key: str) -> Path | None:
    raw = env.get(key)
    if not raw:
        return None
    return Path(raw).expanduser()


__all__ = ["PipelineConfig"]
