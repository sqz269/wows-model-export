"""``/api/settings`` — read + persist the user-config file.

The webview Settings page is the GUI for this. ``GET`` returns the
backend's currently-resolved view of every config field PLUS provenance
(``env`` / ``file`` / ``default`` / ``unconfigured``) so the page can
flag which knobs the user actually controls. ``PUT`` validates a
partial body and writes the user-config file atomically.

Persisting does **not** hot-swap the running backend. The launched
FastAPI app captured its :class:`PipelineConfig` at startup; existing
routes keep using the old values until the operator restarts
``wows-webview-serve``. The response signals ``restart_required: true``
so the UI can prompt for it. (A future iteration could route every
handler through ``app.state.config`` and live-swap — out of scope for
this first cut to keep the route refactor small.)

The set of writable keys is governed by ``_USER_SETTINGS_KEYS`` in
:mod:`wows_model_export.config`. ``workspace`` is **not** writable from
here — it's the bootstrap key that determines where the config file
itself lives, so the user has to keep using ``--workspace`` /
``$WOWS_WORKSPACE`` to swap it. The GET response includes workspace as
read-only metadata so the page can render it for transparency.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ...config import (
    PipelineConfig,
    load_user_settings,
    save_user_settings,
    user_config_path,
)


class SettingsPatch(BaseModel):
    """PUT body — every key optional and patch-merged with the existing
    file. Empty strings / ``None`` clear an override (the field falls
    back to env / auto-discovery / default at startup).

    ``extra='forbid'`` so a typo'd key produces a 422 instead of being
    silently dropped — the webview's :file:`Settings.svelte` always
    sends the known keys, so a stray field signals a client bug.
    """

    model_config = ConfigDict(extra="forbid")

    game_dir: str | None = None
    toolkit_bin: str | None = None
    workspace: str | None = None
    toolkit_timeout_s: float | None = None


# Per-field validation. Returns ``None`` on success or a human-readable
# error string. Paths must exist iff a value was supplied — clearing a
# field (sending ``null`` / empty string) is always valid and removes
# the override.
def _validate(key: str, raw: Any) -> tuple[Any, str | None]:
    if raw is None or raw == "":
        return (None, None)
    if key in ("game_dir", "toolkit_bin", "workspace"):
        if not isinstance(raw, str):
            return (None, f"{key} must be a string path")
        p = Path(raw).expanduser()
        if not p.exists():
            return (None, f"{key}: path does not exist: {p}")
        if key == "toolkit_bin" and p.is_dir():
            return (None, f"{key}: expected an executable file, got a directory")
        if key in ("game_dir", "workspace") and not p.is_dir():
            return (None, f"{key}: expected a directory, got a file")
        return (str(p), None)
    if key == "toolkit_timeout_s":
        if isinstance(raw, bool):
            # ``bool`` is a subclass of ``int`` — reject it explicitly so
            # ``true`` doesn't quietly become ``1.0``.
            return (None, f"{key} must be a number")
        if not isinstance(raw, int | float):
            return (None, f"{key} must be a number")
        v = float(raw)
        if v <= 0:
            return (None, f"{key} must be positive (got {v})")
        return (v, None)
    return (None, f"unknown setting: {key}")


def _field_source(
    env_var: str,
    file_value: Any,
    auto_discovered: bool = False,
) -> str:
    """Classify where a field's currently-resolved value came from.

    Returns ``"env"`` | ``"file"`` | ``"auto"`` | ``"default"`` |
    ``"unconfigured"``. The webview surfaces this as a badge next to
    each input so the user knows whether their override is taking
    effect.
    """
    if os.environ.get(env_var):
        return "env"
    if file_value not in (None, ""):
        return "file"
    if auto_discovered:
        return "auto"
    return "unconfigured"


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the ``/api/settings`` router.

    The captured ``config`` is what the server booted with. We always
    re-read the persisted file + env on every request — that way the
    GET response reflects edits the user made elsewhere (manual file
    edits, env-var changes from a re-exec), not just the startup
    snapshot. The startup ``config`` is still used to derive
    workspace + cache_dir (those don't change at runtime).
    """
    router = APIRouter()

    @router.get("/settings")
    def get_settings() -> dict[str, Any]:
        # Re-read each call so the page reflects out-of-band edits.
        live = PipelineConfig.load()
        file_settings = load_user_settings()
        return {
            "config_path": str(user_config_path()),
            # `running_*` snapshots the values the FastAPI process
            # booted with. A field's `value` reflects what
            # PipelineConfig.load() returns RIGHT NOW (env + file),
            # which can diverge after a Settings PUT — the UI uses
            # the gap to nudge the user to restart the backend.
            "running_workspace": str(config.workspace),
            "running_cache_dir": str(config.cache_dir) if config.cache_dir else None,
            "fields": {
                "game_dir": {
                    "value": str(live.game_dir) if live.game_dir else None,
                    "source": _field_source("WOWS_GAME_DIR", file_settings.get("game_dir")),
                    "env_var": "WOWS_GAME_DIR",
                },
                "toolkit_bin": {
                    "value": str(live.toolkit_bin) if live.toolkit_bin else None,
                    "source": _field_source(
                        "WOWS_TOOLKIT_BIN",
                        file_settings.get("toolkit_bin"),
                        # ``auto`` only when nothing else set it and a
                        # ``which`` lookup found it — i.e. the value
                        # exists but neither env nor file provided it.
                        auto_discovered=(
                            live.toolkit_bin is not None
                            and not os.environ.get("WOWS_TOOLKIT_BIN")
                            and not file_settings.get("toolkit_bin")
                        ),
                    ),
                    "env_var": "WOWS_TOOLKIT_BIN",
                },
                "workspace": {
                    "value": str(live.workspace),
                    "source": _field_source(
                        "WOWS_WORKSPACE",
                        file_settings.get("workspace"),
                    ),
                    "env_var": "WOWS_WORKSPACE",
                },
                "toolkit_timeout_s": {
                    "value": live.toolkit_timeout_s,
                    "source": _field_source(
                        "WOWS_TOOLKIT_TIMEOUT",
                        file_settings.get("toolkit_timeout_s"),
                    ),
                    "env_var": "WOWS_TOOLKIT_TIMEOUT",
                },
            },
        }

    @router.put("/settings")
    def put_settings(body: SettingsPatch) -> dict[str, Any]:
        # Start from what's on disk so a partial body (one field) leaves
        # the others untouched. `exclude_unset=True` so omitted keys
        # don't get treated as "user cleared this" — only explicit
        # nulls/empties drop the override. The Settings.svelte page
        # sends all known keys every save, so this is mostly belt-and-
        # braces for direct API callers.
        current = load_user_settings()
        merged: dict[str, Any] = dict(current)
        patch = body.model_dump(exclude_unset=True)
        errors: dict[str, str] = {}
        for key, raw in patch.items():
            cleaned, err = _validate(key, raw)
            if err is not None:
                errors[key] = err
                continue
            if cleaned is None:
                # Empty / cleared — drop the override so resolution
                # falls back to env / auto / default.
                merged.pop(key, None)
            else:
                merged[key] = cleaned

        if errors:
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "errors": errors},
            )

        path = save_user_settings(merged)
        return {
            "ok": True,
            "restart_required": True,
            "config_path": str(path),
            "saved": merged,
        }

    return router


__all__ = ["make_router"]
