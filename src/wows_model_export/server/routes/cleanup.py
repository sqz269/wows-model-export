"""``/api/cleanup`` — workspace-wide cleanup + re-extract.

Counterpart to ``/api/bootstrap`` (which is for per-target snapshot /
library builds): this router exposes the
:func:`compose.clean_workspace` + :func:`compose.clean_and_reextract`
composers behind two endpoints.

* ``GET  /api/cleanup``       — inventory: ships on disk, replay plans
                                recovered, library presence + size. Cheap;
                                drives the dry-run preview in the Settings
                                cleanup card.

* ``POST /api/cleanup/run``   — spawn a job. Body discriminates between
                                wipe-only and clean-and-reextract via
                                ``mode``. Long-running (minutes to hours
                                for ``mode="reextract"``); the client
                                polls ``/api/jobs/{id}`` for progress.

Each cleanup job acquires the lock label ``cleanup:run`` so concurrent
clicks return 409 with the existing job id (mirrors the bootstrap-build
pattern). Job kind is ``"cleanup"`` so the webview's job panel can
label it distinctly from extract / bootstrap jobs.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ... import compose
from ...config import PipelineConfig
from ..jobs import JobLockedError, spawn_job

CleanupMode = Literal["wipe", "reextract"]


class CleanupRunBody(BaseModel):
    """POST body for ``/api/cleanup/run``."""

    model_config = ConfigDict(extra="forbid")

    mode:           CleanupMode = "wipe"
    prune_library:  bool = True
    replay_skins:   bool = True


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    workspace = config.workspace
    ships_root = workspace / "ships"
    library_dir = workspace / "libraries" / "accessories"

    @router.get("/cleanup")
    def get_cleanup() -> dict[str, Any]:
        """Inventory + dry-run summary.

        Cheap — only walks the per-ship sidecars to build the replay
        plan list. Library + ships-dir size each call ``Path.rglob``,
        which can take a second or two on a multi-GB workspace; the
        Settings UI calls this once per page mount so the cost is
        acceptable.
        """
        on_disk: list[str] = []
        if ships_root.is_dir():
            on_disk = sorted(
                p.name for p in ships_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )

        plans = compose.scan_extracted_ships(workspace)
        planned_labels = {p.label for p in plans}
        unrecoverable = [n for n in on_disk if n not in planned_labels]

        # Provenance breakdown so the UI can warn about fallback ships.
        stamped = sum(1 for p in plans if p.provenance_source == "stamped")
        fallback = len(plans) - stamped
        total_skins = sum(len(p.skins) for p in plans)

        return {
            "workspace":          str(workspace),
            "ships": {
                "on_disk":         len(on_disk),
                "planned":         len(plans),
                "unrecoverable":   unrecoverable,
                "stamped":         stamped,
                "fallback":        fallback,
                "total_skins":     total_skins,
                "size_mb":         (
                    _dir_size_mb(ships_root) if ships_root.is_dir() else 0.0
                ),
            },
            "library": {
                "present":         library_dir.is_dir(),
                "size_mb":         (
                    _dir_size_mb(library_dir) if library_dir.is_dir() else 0.0
                ),
            },
        }

    @router.post("/cleanup/run")
    def post_cleanup_run(body: CleanupRunBody) -> dict[str, Any]:
        """Spawn a cleanup job; returns ``{ok, job_id, cmd}``.

        Wire-shape mirrors ``/api/bootstrap/build`` and
        ``/api/extract/run`` so the client's existing job-polling code
        reuses without changes.
        """
        if body.mode == "reextract":
            target = compose.clean_and_reextract
            kwargs: dict[str, Any] = {
                "workspace":      workspace,
                "config":         config,
                "prune_library":  body.prune_library,
                "replay_skins":   body.replay_skins,
            }
            cmd_display: list[str] = [
                "compose.clean_and_reextract",
                *(
                    [] if body.prune_library else ["--no-prune-library"]
                ),
                *(
                    [] if body.replay_skins else ["--no-replay-skins"]
                ),
            ]
        else:
            target = compose.clean_workspace
            kwargs = {
                "workspace":      workspace,
                "config":         config,
                "prune_library":  body.prune_library,
            }
            cmd_display = [
                "compose.clean_workspace",
                *(
                    [] if body.prune_library else ["--no-prune-library"]
                ),
            ]

        # One concurrent cleanup at a time — the rmtree races are real
        # (filesystem-locking) and a second reextract on top of an
        # in-flight one would re-stomp half-written outputs.
        label = "cleanup:run"
        try:
            job = spawn_job(
                kind="cleanup",
                label=label,
                target=target,
                kwargs=kwargs,
                cmd_display=cmd_display,
            )
        except JobLockedError as err:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok":               False,
                    "error":            str(err),
                    "existing_job_id":  err.existing_id,
                },
            ) from None
        return {"ok": True, "job_id": job.id, "cmd": job.cmd}

    return router


__all__ = ["make_router"]
