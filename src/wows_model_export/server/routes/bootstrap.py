"""``/api/bootstrap`` — workspace prerequisite status + build triggers.

The Settings page is the front door for first-run setup: configure
paths, then build the workspace artifacts the other tabs depend on.
This module wires the second half — the prereq inventory and the
"Build now" buttons.

Two prereqs today:

  ``snapshot``  →  ``<workspace>/.cache/snapshot.json`` (and as a
                   side-effect ``gameparams.json``). Built by
                   :func:`compose.snapshot`. Required by the Extract
                   tab; missing it surfaces as a 503 from
                   ``/api/extract/snapshot``.

  ``library``   →  ``<workspace>/libraries/accessories/index.json``.
                   Built by :func:`compose.build_accessory_library`.
                   Required by the Library tab; missing it surfaces
                   as the 404 the user keeps seeing.

GET ``/api/bootstrap`` returns per-target presence + mtime + size, plus
a top-level ``config_complete`` flag so the page can disable build
buttons when ``game_dir`` / ``toolkit_bin`` aren't set yet (the
composers would raise :class:`ConfigError` and the job would die
immediately).

POST ``/api/bootstrap/build`` submits the matching composer via the
shared :mod:`wows_model_export.server.jobs` runner (in-process call
inside the executor; see Stage 3 docstring there). The response
carries ``job_id`` so the client can poll ``/api/jobs/{id}`` for
progress.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ... import compose
from ...config import PipelineConfig
from ..jobs import JobLockedError, spawn_job

BootstrapTarget = Literal["snapshot", "library"]


class BootstrapBuildBody(BaseModel):
    """POST body. ``target`` is the prereq key from the GET response."""

    model_config = ConfigDict(extra="forbid")

    target: BootstrapTarget


def _target_status(path: Path) -> dict[str, Any]:
    """Pack a target's filesystem status. Missing-file fields are null
    so the client can render "not built yet" cleanly."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "present": False,
            "mtime_ms": None,
            "size_bytes": None,
        }
    return {
        "path": str(path),
        "present": True,
        "mtime_ms": int(st.st_mtime * 1000),
        "size_bytes": st.st_size,
    }


def make_router(config: PipelineConfig) -> APIRouter:
    """Build the ``/api/bootstrap`` router bound to the given workspace."""
    router = APIRouter()
    workspace = config.workspace
    cache_dir = config.cache_dir or (workspace / ".cache")
    snapshot_path = cache_dir / "snapshot.json"
    library_path = workspace / "libraries" / "accessories" / "index.json"

    # Per-target dispatch table. Stage 3: each entry pairs the composer
    # callable with a kwargs builder that closes over the request-time
    # config + path resolution. The display ``cmd`` keeps the legacy
    # `wows-*` invocation shape so the Settings UI's "what will this
    # build" preview reads naturally — the actual call goes through
    # `compose.*` in-process via spawn_job.
    _BootstrapDispatch = tuple[
        Callable[..., Any],   # target composer
        Callable[[], dict[str, Any]],  # kwargs builder (request-time)
        list[str],            # display cmd for the job log
    ]

    def _dispatch_for(target: BootstrapTarget) -> _BootstrapDispatch:
        if target == "snapshot":
            return (
                compose.snapshot,
                lambda: {
                    "output_path": snapshot_path,
                    "config":      config,
                },
                ["compose.snapshot", "--output", str(snapshot_path)],
            )
        if target == "library":
            return (
                compose.build_accessory_library,
                lambda: {"config": config},
                ["compose.build_accessory_library"],
            )
        raise ValueError(f"unknown bootstrap target: {target}")  # pragma: no cover

    def _cmd_for(target: BootstrapTarget) -> list[str]:
        # Display-only; preserved as a separate accessor so the GET
        # handler's response shape doesn't change.
        return _dispatch_for(target)[2]

    @router.get("/bootstrap")
    def get_bootstrap() -> dict[str, Any]:
        # Re-resolve the live config every request so the user can edit
        # game_dir / toolkit_bin via the Settings PUT and immediately
        # see the bootstrap buttons enable — without restarting the
        # backend. (The actual build still uses whatever env the
        # uvicorn process inherits, but that's an orthogonal concern.)
        live = PipelineConfig.load()
        missing: list[str] = []
        if live.game_dir is None:
            missing.append("game_dir")
        if live.toolkit_bin is None:
            missing.append("toolkit_bin")
        return {
            "workspace": str(workspace),
            "config_complete": not missing,
            "missing_config": missing,
            "targets": {
                "snapshot": {
                    "label": "GameParams + snapshot cache",
                    "description": (
                        "Dumps GameParams.data → gameparams.json + the "
                        "Vehicles/Permoflages snapshot the Extract tab "
                        "reads. Run this first; takes ~30 s on cold start."
                    ),
                    "job_label": "bootstrap:snapshot",
                    "cmd": _cmd_for("snapshot"),
                    "requires_config": ["game_dir", "toolkit_bin"],
                    **_target_status(snapshot_path),
                },
                "library": {
                    "label": "Accessory library index",
                    "description": (
                        "Walks every ship in the workspace, bundles "
                        "shared accessories into libraries/accessories/. "
                        "The Library tab won't load without this. "
                        "Empty until at least one ship has been extracted."
                    ),
                    "job_label": "bootstrap:library",
                    "cmd": _cmd_for("library"),
                    "requires_config": [],
                    **_target_status(library_path),
                },
            },
        }

    @router.post("/bootstrap/build")
    def post_bootstrap_build(body: BootstrapBuildBody) -> dict[str, Any]:
        # Cheap pre-check so the user gets a clean 412 instead of a
        # subprocess that exits with `ConfigError: WOWS_GAME_DIR …` in
        # stderr. The Settings UI already disables the button on
        # missing config, but a direct API caller deserves the same
        # error.
        if body.target == "snapshot":
            # wows-snapshot needs game_dir + toolkit_bin to run
            # ensure_dump.
            live = PipelineConfig.load()
            if live.game_dir is None or live.toolkit_bin is None:
                raise HTTPException(
                    status_code=412,
                    detail={
                        "ok": False,
                        "error": (
                            "game_dir and toolkit_bin must be configured "
                            "before building the snapshot. Set them on "
                            "the Settings page first."
                        ),
                    },
                )

        target_callable, kwargs_builder, cmd_display = _dispatch_for(
            body.target
        )
        label = f"bootstrap:{body.target}"
        try:
            job = spawn_job(
                kind="bootstrap",
                label=label,
                target=target_callable,
                kwargs=kwargs_builder(),
                cmd_display=cmd_display,
            )
        except JobLockedError as err:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "error": str(err),
                    "existing_job_id": err.existing_id,
                },
            ) from None
        # Client polls /api/jobs/{id} from here on.
        return {"ok": True, "job_id": job.id, "cmd": job.cmd}

    return router


__all__ = ["make_router"]
