"""``/api/extract/*`` — picker payload + ingest job runner.

Endpoints:

  ``GET  /api/extract/snapshot``        — full vehicles + permoflages dump
  ``POST /api/extract/run``             — kick off :func:`compose.ingest_ship`
  ``POST /api/extract/skin``            — kick off :func:`compose.ingest_skin_pack`
  ``GET  /api/extract/jobs``            — list all known jobs
  ``GET  /api/extract/jobs/{id}``       — one job (state + accumulated logs)
  ``POST /api/extract/jobs/{id}/cancel`` — flip the cancel flag

Snapshot is built via ``compose.snapshot`` directly. The output JSON
file lives at ``<workspace>/.cache/snapshot.json``; we cache the parsed
value in memory keyed on the joint ``(gameparams.json mtime,
snapshot.json mtime)`` so a refresh of GameParams or a manual re-dump
invalidates automatically.

Stage 3 swap: extract / skin / snapshot all run as in-process composer
calls inside the shared ``ThreadPoolExecutor`` (see
:mod:`wows_model_export.server.jobs`). The validation regexes
(``VEHICLE_ID`` / ``LABEL_ID`` / ``SHIP_FOLDER_ID`` / ``SKIN_ID`` /
``JOB_ID``) are still enforced — even though we no longer spawn a
shell, the regexes keep IDs bounded for downstream use (filesystem
paths, sidecar lookups, etc.).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from ... import compose
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
        """Resolve a fresh-enough snapshot blob, building the cache if cold.

        Stage 3 swap: builds the snapshot in-process via
        :func:`compose.snapshot` instead of spawning ``wows-snapshot``.
        We offload the call to a background thread via
        :func:`asyncio.to_thread` so the FastAPI event loop stays
        responsive — ``compose.snapshot`` does heavy synchronous work
        (~30 s on cold GameParams).

        Concurrent first callers still share one composer call via the
        ``asyncio.Lock`` so we don't load + parse the 2.8 GB
        GameParams blob N times in parallel.
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
                # asyncio.to_thread keeps the event loop responsive
                # while the composer parses GameParams. We don't gate
                # this through spawn_job because (a) the result isn't
                # interesting to the UI's job table, and (b) the
                # asyncio.Lock above already serialises concurrent
                # callers — the spawn_job lock would be redundant.
                try:
                    await asyncio.to_thread(
                        compose.snapshot,
                        output_path=snapshot_cache_path,
                        config=config,
                    )
                except Exception as exc:
                    # Compose-level failures (StepError, ConfigError,
                    # ToolkitError) bubble through to the GET handler
                    # which renders them as a 500.
                    raise RuntimeError(
                        f"compose.snapshot failed: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
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

        # Build the kwargs dict for compose.ingest_ship. The composer
        # signature mirrors the CLI flags one-to-one — see
        # `compose/ingest_ship.py::ingest_ship` for the full param list.
        # `interactive=False` is critical: we run inside a worker
        # thread that has no stdin, so the ambiguity-resolve prompt
        # path (lines 219-231 of ingest_ship.py) would deadlock the
        # job. The composer raises `StepError("resolve_identity")`
        # cleanly when ambiguous + non-interactive, which the job
        # runner surfaces as a failed job with a parseable error.
        kwargs: dict[str, Any] = {
            "ship_input":            positional,
            "config":                config,
            "forced_label":          label,
            "toolkit_ship_override": model_dir,
            "gameparams_ship_id":    top_key_full,
            "interactive":           False,
            "build_library":         bool(body.get("build_library")),
            "and_publish":           bool(body.get("and_publish")),
            "publish_force":         bool(body.get("publish_force")),
        }
        # `variant_permoflage` defaults to "auto" inside the composer —
        # only override when the body explicitly sets it. Keeps the
        # default behaviour identical to the prior CLI invocation when
        # the body omits the field.
        if permoflage is not None:
            kwargs["variant_permoflage"] = permoflage

        # Display-only command line preserves the legacy "what the user
        # asked the server to do" header that the Extract panel renders.
        # We rebuild the wows-ingest-ship-style argv string for that
        # purpose only — not actually run as a shell.
        cmd_display: list[str] = [
            "compose.ingest_ship",
            positional,
            "--label",         label,
            "--toolkit-ship",  model_dir,
            "--gameparams-ship-id", top_key_full,
        ]
        if permoflage is not None:
            cmd_display += ["--variant-permoflage", permoflage]
        if body.get("build_library"):
            cmd_display.append("--build-library")
        if body.get("and_publish"):
            cmd_display.append("--and-publish")
        if body.get("publish_force"):
            cmd_display.append("--publish-force")

        try:
            job = spawn_job(
                kind="extract",
                label=label,
                target=compose.ingest_ship,
                kwargs=kwargs,
                cmd_display=cmd_display,
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

        # Map the route body to compose.ingest_skin_pack kwargs:
        #
        #   route source -> composer source_kind
        #   "loose"      -> "loose_mod"   (skin_source is a folder path)
        #   "wg" / "vfs" -> "vfs_variant" (skin_source is a Vehicle or
        #                                  Exterior GameParams id)
        #
        # The composer's auto-detect path is also valid; we set
        # source_kind explicitly here so a body that names a non-
        # existent path still routes the way the user asked instead of
        # silently falling through to vfs_variant.
        if source == "loose":
            source_kind: str = "loose_mod"
        else:
            source_kind = "vfs_variant"

        # exterior_id from the body is informational — the composer
        # resolves Vehicle → Exterior internally for vfs_variant. We
        # log it in the display string so the user can correlate the
        # pick on the page with the running job, but don't pass it as
        # a kwarg (the composer signature has no exterior_id field).
        kwargs: dict[str, Any] = {
            "skin_source": source_arg,
            "ship_id":     ship,
            "config":      config,
            "skin_id":     skin_id,
            "source_kind": source_kind,
        }
        if display_name:
            kwargs["display_name"] = str(display_name)

        # Display-only command line preserves the legacy header. The
        # `--source` / `--exterior` flags here are illustrative — they
        # don't match a runnable CLI invocation since the actual
        # `wows-ingest-skin-pack` arg shape is different. The job log
        # uses this purely for the "what did this job do" header.
        cmd_display: list[str] = [
            "compose.ingest_skin_pack",
            source_arg,
            "--ship", ship,
            "--source-kind", source_kind,
            "--skin-id", skin_id,
        ]
        if exterior_id:
            cmd_display += ["# exterior", str(exterior_id)]
        if display_name:
            cmd_display += ["--display-name", str(display_name)]

        # Different skins for the same ship may run in parallel; the
        # same skin re-trigger is serialised against itself.
        lock_label = f"{ship}__skin__{skin_id}"
        try:
            job = spawn_job(
                kind="skin",
                label=lock_label,
                target=compose.ingest_skin_pack,
                kwargs=kwargs,
                cmd_display=cmd_display,
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
