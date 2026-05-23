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

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ... import compose
from ...config import PipelineConfig
from ..jobs import JobLockedError, list_jobs, spawn_job

BootstrapTarget = Literal["snapshot", "library", "projectiles"]


def _build_projectiles_combined(
    *,
    workspace: Path,
    config: PipelineConfig,
    on_event: Any = None,
    cancel: Any = None,
) -> dict[str, Any]:
    """Chain build_projectile_library + build_ammo_profiles into one job.

    The Projectiles tab needs BOTH the geometry index AND the ammo
    profiles JSON to render. Combining them in one dispatch means the
    bootstrap "projectiles" target builds everything the tab needs in
    a single click. Step events from both composers flow through the
    parent's ``on_event`` verbatim; the ``step`` field names disambiguate
    the source (``build_projectile_library:…`` vs ``write_profiles``).

    Returns the merged outcome so the job log carries useful summary
    data (asset_count + profile_count).
    """
    lib_result = compose.build_projectile_library(
        workspace=workspace,
        config=config,
        on_event=on_event,
        cancel=cancel,
    )
    ammo_result = compose.build_ammo_profiles(
        workspace=workspace,
        config=config,
        on_event=on_event,
        cancel=cancel,
    )
    return {
        "library":      lib_result,
        "ammo_profiles": ammo_result,
    }


class BootstrapBuildBody(BaseModel):
    """POST body. ``target`` is the prereq key from the GET response."""

    model_config = ConfigDict(extra="forbid")

    target: BootstrapTarget


class BootstrapResetBody(BaseModel):
    """POST body for ``/api/bootstrap/reset``. Same target key as build."""

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
    projectiles_index_path = workspace / "libraries" / "projectiles" / "index.json"

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
        if target == "projectiles":
            return (
                _build_projectiles_combined,
                lambda: {
                    "workspace": workspace,
                    "config":    config,
                },
                [
                    "compose.build_projectile_library",
                    "&&",
                    "compose.build_ammo_profiles",
                ],
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
                "projectiles": {
                    "label": "Projectile library + ammo profiles",
                    "description": (
                        "Exports every shell / torpedo / bomb / depth "
                        "charge mesh from GameParams + builds the per-"
                        "ammo ballistic profile JSON. The Projectiles "
                        "tab won't load without both files."
                    ),
                    "job_label": "bootstrap:projectiles",
                    "cmd": _cmd_for("projectiles"),
                    "requires_config": ["game_dir", "toolkit_bin"],
                    **_target_status(projectiles_index_path),
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
        if body.target in ("snapshot", "projectiles"):
            # wows-snapshot needs game_dir + toolkit_bin to run
            # ensure_dump. The projectiles dispatch chains
            # build_projectile_library + build_ammo_profiles, both of
            # which call into the toolkit (geometry export + GameParams
            # parse) so the same pre-check applies.
            live = PipelineConfig.load()
            if live.game_dir is None or live.toolkit_bin is None:
                raise HTTPException(
                    status_code=412,
                    detail={
                        "ok": False,
                        "error": (
                            "game_dir and toolkit_bin must be configured "
                            f"before building {body.target}. Set them on "
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

    # ── /api/bootstrap/reset ────────────────────────────────────────────
    # Wipe a target's on-disk state so the next build starts from scratch.
    # Synchronous because it's a single rmtree call (tens of MB / seconds);
    # no need for the job runner. After reset, GET /api/bootstrap reports
    # present=false and the existing Build button rebuilds.
    #
    #   snapshot → wipes the cache_dir entirely. That covers
    #              gameparams.json + snapshot.json AND the toolkit's
    #              transient scratch (geom/, dds/, per_swap/, vfs_extract/,
    #              skel_ext_hashes.json, camouflages.xml). Conservative —
    #              "reset the cache" should leave nothing behind.
    #
    #   library  → wipes libraries/accessories/ entirely. Drops index.json,
    #              every shared GLB + textures dir, the winding audit, and
    #              flip_overrides.json (which the rebuild re-applies from a
    #              fresh score pass).
    #
    #   projectiles → wipes libraries/projectiles/ entirely. Drops the
    #              index.json + ammo_profiles.json + every projectile
    #              GLB + DDS chain. Rebuild walks GameParams +
    #              re-exports from the toolkit.
    @router.post("/bootstrap/reset")
    def post_bootstrap_reset(body: BootstrapResetBody) -> dict[str, Any]:
        # 409 if a build for this target is in flight — would race with
        # the rmtree and leave the workspace in a half-built state.
        label = f"bootstrap:{body.target}"
        for j in list_jobs():
            if j.label == label and j.state == "running":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "ok": False,
                        "error": (
                            f"a build for '{body.target}' is in progress; "
                            f"wait for it to finish or cancel it first"
                        ),
                        "existing_job_id": j.id,
                    },
                )

        if body.target == "snapshot":
            path = cache_dir
        elif body.target == "library":
            path = workspace / "libraries" / "accessories"
        elif body.target == "projectiles":
            path = workspace / "libraries" / "projectiles"
        else:  # pragma: no cover - pydantic Literal already gates this
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "error": f"unknown target: {body.target}"},
            )

        # Idempotent: missing dir is a no-op success. shutil.rmtree handles
        # the recursive delete; we surface OS errors as 500 with detail so
        # the user can see e.g. a Windows file-lock from a still-running
        # toolkit process.
        existed = path.exists()
        if existed:
            try:
                shutil.rmtree(path)
            except OSError as err:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "ok": False,
                        "error": f"failed to remove {path}: {err}",
                    },
                ) from None

        return {
            "ok": True,
            "target": body.target,
            "path": str(path),
            "existed": existed,
        }

    return router


__all__ = ["make_router"]
