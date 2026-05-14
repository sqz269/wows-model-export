"""``/api/extract/*`` — picker payload + ingest job runner.

Endpoints:

  ``GET  /api/extract/snapshot``        — full vehicles + permoflages dump
  ``POST /api/extract/run``             — kick off ``wows-ingest-ship``
  ``POST /api/extract/skin``            — kick off ``wows-ingest-skin-pack``
  ``GET  /api/extract/jobs``            — list all known jobs
  ``GET  /api/extract/jobs/{id}``       — one job (state + accumulated logs)
  ``POST /api/extract/jobs/{id}/cancel`` — SIGTERM the child

Snapshot is dumped via the ``wows-snapshot`` CLI entry point. The output
JSON file lives at ``<workspace>/.cache/snapshot.json``; we cache the
parsed value in memory keyed on the joint
``(gameparams.json mtime, snapshot.json mtime)`` so a refresh of
GameParams or a manual re-dump invalidates automatically.

Port of ``webview/src/server/endpoints/extract.ts``. The validation
regexes (``VEHICLE_ID`` / ``LABEL_ID`` / ``SHIP_FOLDER_ID`` / ``SKIN_ID``
/ ``JOB_ID``) are copied verbatim — same charset, same length limits.
Hostile bodies can't inject extra spawn args; the child also runs
without a shell so the threat surface is minimal.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse

from ...config import PipelineConfig
from ..jobs import (
    JobLockedError,
    cancel_job,
    get_job,
    job_to_dict,
    job_to_summary,
    list_jobs,
    spawn_job,
)


# ID-shape guards. Each call uses a tight regex so a hostile body can't
# inject extra spawn args; the child runs without a shell so the threat
# surface is small but a small charset keeps the failure modes obvious.
VEHICLE_ID = re.compile(r"^[A-Za-z0-9_]{3,80}$")
LABEL_ID = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
SHIP_FOLDER_ID = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
SKIN_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
JOB_ID = re.compile(r"^[A-Za-z0-9\-]{6,40}$")


class _SnapshotCache:
    """Module-level wrapper holding the cached snapshot + an in-flight
    asyncio lock.

    The Node side used a closure-captured ``_cache`` + ``_inflight``
    promise pair; in Python the equivalent is one instance of this
    class per FastAPI app (created inside :func:`make_router`).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # ``key`` is the mtime pair as a string; ``data`` is the
        # parsed snapshot. We never share with other consumers, so
        # plain attribute storage is fine.
        self._key: str | None = None
        self._data: dict[str, Any] | None = None


def _validate_id(name: str, value: str, pattern: re.Pattern[str], error: str) -> None:
    if not pattern.match(value):
        raise HTTPException(status_code=400, detail={"ok": False, "error": error})


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    workspace = config.workspace
    cache_dir = config.cache_dir or (workspace / ".cache")
    gp_cache_path = cache_dir / "gameparams.json"
    snapshot_cache_path = cache_dir / "snapshot.json"

    snap_cache = _SnapshotCache()

    # ── Snapshot ensure ─────────────────────────────────────────────────
    async def ensure_snapshot() -> dict[str, Any]:
        """Resolve a fresh-enough snapshot blob, spawning ``wows-snapshot``
        on cold cache.

        Mirrors the Node side's cache logic 1-to-1. Concurrent first
        callers share one ``wows-snapshot`` subprocess via the
        ``asyncio.Lock`` so we don't fan out parallel spawns.
        """
        if not gp_cache_path.exists():
            raise FileNotFoundError(
                f"gameparams_cache_missing at {gp_cache_path}. "
                "Run `wows-find-ship-variants --refresh` first."
            )
        # Match Node's Math.floor(mtimeMs) — millisecond precision.
        gp_mtime = int(gp_cache_path.stat().st_mtime * 1000)
        snap_mtime = (
            int(snapshot_cache_path.stat().st_mtime * 1000)
            if snapshot_cache_path.exists()
            else 0
        )
        key = f"{gp_mtime}:{snap_mtime}"
        # Fast path: cached value is still fresh.
        if snap_cache._data is not None and snap_cache._key == key:
            return snap_cache._data

        async with snap_cache._lock:
            # Re-check inside the lock — another coroutine may have
            # populated the cache while we were waiting for it.
            if snap_cache._data is not None and snap_cache._key == key:
                return snap_cache._data
            # Skip the dump when an existing snapshot.json is at least
            # as fresh as gameparams.json — saves the ~30 s parse on
            # cold start when a previous run already wrote one.
            if not snapshot_cache_path.exists() or snap_mtime < gp_mtime:
                cache_dir.mkdir(parents=True, exist_ok=True)
                proc = await asyncio.create_subprocess_exec(
                    "wows-snapshot",
                    "--output",
                    str(snapshot_cache_path),
                    cwd=str(workspace),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"wows-snapshot exit={proc.returncode}: "
                        f"{stderr.decode('utf-8', errors='replace')}"
                    )
            with snapshot_cache_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            refreshed = int(snapshot_cache_path.stat().st_mtime * 1000)
            snap_cache._key = f"{gp_mtime}:{refreshed}"
            snap_cache._data = data
            return data

    # ── /api/extract/snapshot ───────────────────────────────────────────
    @router.get("/extract/snapshot")
    async def get_snapshot() -> JSONResponse:
        try:
            data = await ensure_snapshot()
        except FileNotFoundError as err:
            # Same 503 the Node side returned on cold GameParams cache.
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": str(err), "stderr": ""},
            )
        except RuntimeError as err:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": str(err), "stderr": ""},
            )
        return JSONResponse(
            content=data, headers={"Cache-Control": "no-cache"}
        )

    # ── /api/extract/run — wows-ingest-ship ─────────────────────────────
    @router.post("/extract/run")
    async def run_extract(body: dict[str, Any] = Body(default={})) -> JSONResponse:
        vehicle = str(body.get("vehicle") or "")
        label = str(body.get("label") or "")
        if not VEHICLE_ID.match(vehicle):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "vehicle must be 3-80 chars [A-Za-z0-9_]",
                },
            )
        if not LABEL_ID.match(label):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "label must be 1-80 chars [A-Za-z0-9_-]",
                },
            )
        perm_raw = body.get("permoflage")
        permoflage: str | None = None
        if perm_raw == "none" or perm_raw == "auto":
            permoflage = perm_raw
        elif isinstance(perm_raw, str) and perm_raw:
            if not VEHICLE_ID.match(perm_raw):
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": (
                            "permoflage must be 3-80 chars [A-Za-z0-9_], "
                            "\"auto\", \"none\", or null"
                        ),
                    },
                )
            permoflage = perm_raw

        # Resolve model_dir + the full top_key from the cached snapshot
        # so we can pin --gameparams-ship-id without an extra Python
        # round-trip.
        try:
            snap = await ensure_snapshot()
        except FileNotFoundError as err:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": str(err), "stderr": ""},
            )
        except RuntimeError as err:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": str(err)},
            )
        vehicles = snap.get("vehicles") or []
        veh = next(
            (
                v
                for v in vehicles
                if isinstance(v, dict)
                and (v.get("top_key") == vehicle or v.get("param_index") == vehicle)
            ),
            None,
        )
        if veh is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": f"vehicle {vehicle} not in GameParams",
                },
            )
        param_index = str(veh.get("param_index") or "")
        top_key_full = str(veh.get("top_key") or "") or param_index
        model_dir = str(veh.get("model_dir") or "") or param_index
        positional = model_dir or param_index

        # `wows-ingest-ship` accepts a positional ship arg (even when
        # --toolkit-ship overrides it; argparse enforces presence).
        # Pass model_dir so the displayed command matches what the
        # runner spawns.
        args: list[str] = [
            positional,
            "--label",
            label,
            "--toolkit-ship",
            model_dir,
            "--gameparams-ship-id",
            top_key_full,
            "--non-interactive",
        ]
        if permoflage is not None:
            args += ["--variant-permoflage", permoflage]
        # Defaults match the Node side: skip_legacy true unless
        # explicitly false; the other three default to false.
        if body.get("skip_legacy", True) is not False:
            args.append("--skip-legacy")
        if body.get("build_library"):
            args.append("--build-library")
        if body.get("and_publish"):
            args.append("--and-publish")
        if body.get("publish_force"):
            args.append("--publish-force")

        cmd = ["wows-ingest-ship", *args]
        try:
            job = spawn_job(kind="extract", label=label, cmd=cmd, cwd=workspace)
        except JobLockedError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": str(err),
                    "existing_job_id": err.existing_id,
                },
            )
        return JSONResponse(content={"ok": True, "job_id": job.id, "cmd": job.cmd})

    # ── /api/extract/skin — wows-ingest-skin-pack ───────────────────────
    @router.post("/extract/skin")
    async def run_skin(body: dict[str, Any] = Body(default={})) -> JSONResponse:
        ship = str(body.get("ship") or "")
        source = str(body.get("source") or "")
        source_arg = str(body.get("source_arg") or "")
        skin_id = str(body.get("skin_id") or "")
        exterior_id = body.get("exterior_id")
        display_name = body.get("display_name")

        if not SHIP_FOLDER_ID.match(ship):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "ship must be 1-80 chars [A-Za-z0-9_-]",
                },
            )
        if source not in ("wg", "vfs", "loose"):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "source must be 'wg', 'vfs', or 'loose'",
                },
            )
        if not SKIN_ID.match(skin_id):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "skin_id must be 1-64 chars [A-Za-z0-9_-]",
                },
            )
        if source in ("wg", "vfs"):
            if not VEHICLE_ID.match(source_arg):
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": f"{source} source_arg must be 3-80 chars [A-Za-z0-9_]",
                    },
                )
            if source == "vfs" and not exterior_id:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": "vfs source requires exterior_id",
                    },
                )
            if exterior_id and not VEHICLE_ID.match(str(exterior_id)):
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": "exterior_id must be 3-80 chars [A-Za-z0-9_]",
                    },
                )
        else:
            # loose-mod: server-side absolute path. We only check that
            # the dir exists; the CLI does the real shape validation.
            if not source_arg:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": "loose source_arg (folder path) required",
                    },
                )
            arg_path = Path(source_arg)
            if not arg_path.exists() or not arg_path.is_dir():
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": f"loose source dir not found: {source_arg}",
                    },
                )

        # Sidecar must exist for the target ship — ingest_skin_pack
        # refuses to run otherwise. A clearer error here beats a
        # spawn-time failure.
        sidecar_path = workspace / "ships" / ship / f"{ship}.meta.json"
        if not sidecar_path.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": (
                        f"sidecar missing: {sidecar_path}. Run an "
                        "extract first."
                    ),
                },
            )

        args: list[str] = [
            ship,
            "--source",
            f"{source}:{source_arg}",
            "--skin-id",
            skin_id,
        ]
        if exterior_id:
            args += ["--exterior", str(exterior_id)]
        if display_name:
            args += ["--display-name", str(display_name)]

        # Different skins for the same ship may run in parallel; the
        # same skin re-trigger is serialised against itself.
        lock_label = f"{ship}__skin__{skin_id}"
        cmd = ["wows-ingest-skin-pack", *args]
        try:
            job = spawn_job(
                kind="skin", label=lock_label, cmd=cmd, cwd=workspace
            )
        except JobLockedError as err:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error": str(err),
                    "existing_job_id": err.existing_id,
                },
            )
        return JSONResponse(content={"ok": True, "job_id": job.id, "cmd": job.cmd})

    # ── /api/extract/jobs[/{id}[/cancel]] ───────────────────────────────
    @router.get("/extract/jobs")
    def list_all_jobs() -> JSONResponse:
        return JSONResponse(
            content={"jobs": [job_to_summary(j) for j in list_jobs()]},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/extract/jobs/{job_id}")
    def get_one_job(job_id: str = PathParam(...)) -> JSONResponse:
        if not JOB_ID.match(job_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid job id"},
            )
        job = get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "job not found"},
            )
        return JSONResponse(
            content={"ok": True, "job": job_to_dict(job)},
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/extract/jobs/{job_id}/cancel")
    def cancel_one_job(job_id: str = PathParam(...)) -> JSONResponse:
        if not JOB_ID.match(job_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid job id"},
            )
        existing = get_job(job_id)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "job not found"},
            )
        after = cancel_job(job_id) or existing
        return JSONResponse(content={"ok": True, "job": job_to_dict(after)})

    return router


__all__ = ["make_router"]
