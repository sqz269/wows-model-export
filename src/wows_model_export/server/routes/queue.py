"""``/api/queue`` — persistent FIFO extract queue.

Counterpart to ``/api/extract/run``: instead of spawning an immediate
job, ``POST /api/queue/enqueue`` appends an item to the queue and the
:mod:`server.queue` worker thread runs items one at a time. Each item
spawns through the regular :func:`jobs.spawn_job` path, so the existing
``/api/jobs/{id}`` polling endpoint surfaces the live log for whichever
item is currently running.

Endpoints:

  ``GET    /api/queue``                  - full snapshot
  ``POST   /api/queue/enqueue``          - append a pending item
  ``DELETE /api/queue/{queue_id}``       - remove pending or cancel running
  ``POST   /api/queue/reorder``          - reorder pending tail
  ``POST   /api/queue/clear-completed``  - drop done/failed/cancelled rows
  ``POST   /api/queue/pause``            - pause the worker
  ``POST   /api/queue/resume``           - resume the worker

Validation regexes match ``/api/extract/run`` byte-for-byte so the same
identifier shapes go through; the enqueue body shape mirrors
``RunExtractBody``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ...config import PipelineConfig
from .. import queue as queue_mod

# Mirror the extract.py regexes verbatim so a body that works for one
# endpoint works for the other.
VEHICLE_ID = re.compile(r"^[A-Za-z0-9_]{3,80}$")
LABEL_ID = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
QUEUE_ID = re.compile(r"^q-[A-Za-z0-9\-]{6,80}$")


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[str]


def _load_snapshot(snapshot_path: Path) -> dict[str, Any] | None:
    """Read the cached vehicle snapshot from disk.

    The enqueue handler needs to resolve the user-supplied vehicle id
    (top_key OR param_index) to (param_index, top_key_full, model_dir)
    so the queue item carries the resolved kwargs (the worker doesn't
    have to walk the snapshot). We read the cache file directly rather
    than depending on extract.py's in-memory SnapshotCache (different
    router; would couple the modules).

    Returns ``None`` if the file doesn't exist or is unparseable.
    """
    if not snapshot_path.is_file():
        return None
    try:
        with snapshot_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    workspace = config.workspace
    cache_dir = config.cache_dir or (workspace / ".cache")
    snapshot_path = cache_dir / "snapshot.json"

    @router.get("/queue")
    def get_queue() -> JSONResponse:
        return JSONResponse(
            content=queue_mod.snapshot(),
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/queue/enqueue")
    def post_enqueue(body: dict[str, Any] = Body(default={})) -> JSONResponse:
        # Body validation - same shape as /api/extract/run.
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

        # Resolve vehicle -> (param_index, top_key_full, model_dir) via
        # the on-disk snapshot. The worker bakes these into the item
        # so a snapshot rebuild between enqueue + dispatch doesn't move
        # the goalposts.
        snap = _load_snapshot(snapshot_path)
        if snap is None:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": (
                        f"snapshot cache missing at {snapshot_path}. "
                        "Build it from Settings -> Workspace artifacts."
                    ),
                },
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
                    "error": f"vehicle {vehicle} not in GameParams snapshot",
                },
            )
        param_index = str(veh.get("param_index") or "")
        top_key_full = str(veh.get("top_key") or "") or param_index
        model_dir = str(veh.get("model_dir") or "") or param_index

        item = queue_mod.enqueue(
            vehicle=           top_key_full,
            label=             label,
            permoflage=        permoflage,
            build_library=     bool(body.get("build_library", True)),
            toolkit_ship=      model_dir,
            gameparams_ship_id=top_key_full,
        )
        return JSONResponse(content={"ok": True, "queue_id": item.queue_id})

    @router.delete("/queue/{queue_id}")
    def delete_queue_item(
        queue_id: str = PathParam(...),
    ) -> JSONResponse:
        if not QUEUE_ID.match(queue_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid queue id"},
            )
        item = queue_mod.remove(queue_id)
        if item is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "queue item not found"},
            )
        return JSONResponse(
            content={
                "ok":       True,
                "queue_id": item.queue_id,
                "status":   item.status,
            }
        )

    @router.post("/queue/reorder")
    def post_reorder(body: ReorderBody) -> JSONResponse:
        # Validate every id passes the shape check before touching state.
        for qid in body.order:
            if not QUEUE_ID.match(qid):
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": f"invalid queue id in order: {qid!r}",
                    },
                )
        reordered = queue_mod.reorder(body.order)
        return JSONResponse(content={"ok": True, "reordered": reordered})

    @router.post("/queue/clear-completed")
    def post_clear_completed() -> JSONResponse:
        dropped = queue_mod.clear_completed()
        return JSONResponse(content={"ok": True, "dropped": dropped})

    @router.post("/queue/pause")
    def post_pause() -> JSONResponse:
        paused = queue_mod.set_paused(True)
        return JSONResponse(content={"ok": True, "paused": paused})

    @router.post("/queue/resume")
    def post_resume() -> JSONResponse:
        paused = queue_mod.set_paused(False)
        return JSONResponse(content={"ok": True, "paused": paused})

    return router


__all__ = ["make_router"]
