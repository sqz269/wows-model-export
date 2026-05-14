"""``/api/jobs/*`` — generic job polling.

The job runner (:mod:`wows_model_export.server.jobs`) is shared across
``extract`` / ``skin`` / ``bootstrap`` kinds. The legacy URLs under
``/api/extract/jobs/*`` predate the bootstrap path; clients that don't
care about extraction (the Settings page's Build buttons, future
non-extract tools) prefer this kind-neutral prefix.

The extract endpoints stay registered too — moving them would break
the existing UI mid-flight, and they're trivial to keep.
"""

from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from ..jobs import (
    cancel_job,
    get_job,
    job_to_dict,
    job_to_summary,
    list_jobs,
)

# Same shape the Node side enforced; mirrored from extract.py so the
# two route modules don't drift apart silently.
JOB_ID = re.compile(r"^[A-Za-z0-9\-]{6,40}$")


def make_router() -> APIRouter:
    """Build the kind-neutral ``/api/jobs/*`` router.

    No config dependency — every handler reads from the module-level
    job registry. Kept separate from :mod:`extract` so the extract
    routes can move to their own file later without dragging the
    polling endpoints with them.
    """
    router = APIRouter()

    @router.get("/jobs")
    def list_all() -> JSONResponse:
        return JSONResponse(
            content={"jobs": [job_to_summary(j) for j in list_jobs()]},
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/jobs/{job_id}")
    def get_one(job_id: str = PathParam(...)) -> JSONResponse:
        if not JOB_ID.match(job_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid job_id"},
            )
        job = get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "job not found"},
            )
        return JSONResponse(
            content=job_to_dict(job),
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/jobs/{job_id}/cancel")
    def cancel_one(job_id: str = PathParam(...)) -> JSONResponse:
        if not JOB_ID.match(job_id):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "invalid job_id"},
            )
        job = cancel_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "job not found"},
            )
        return JSONResponse(content=job_to_dict(job))

    return router


__all__ = ["make_router"]
