"""``/api/consumers/*`` — generic downstream consumer dispatcher.

This router lists descriptors discovered via the
:mod:`wows_model_export.extensions` entry-point system and forwards
action invocations through the existing :func:`spawn_job` machinery.
The router itself contains no hardcoded consumer module names — only
the discovery glue.

Endpoints:

    ``GET  /api/consumers``                            — list descriptors
    ``POST /api/consumers/{consumer_id}/{action_id}/run`` — kick off an action

Job lifecycle is shared with the extract route: poll
``/api/extract/jobs/{id}`` and cancel via the same path. (The prefix
predates the multi-kind job runner; renaming to ``/api/jobs/*`` is a
follow-up that doesn't block this work.)
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from ...config import PipelineConfig
from ...extensions import ConsumerDescriptor, discover
from ..jobs import JobLockedError, spawn_job

# Identical shape to the existing extract route IDs. Kept tight so a
# hostile body can't sneak shell-style payloads through into the label
# string we use as the job-lock key.
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _describe(d: ConsumerDescriptor) -> dict[str, Any]:
    """Serialise a descriptor to the wire shape (no handler reference)."""
    return {
        "id":           d.id,
        "display_name": d.display_name,
        "description":  d.description,
        "actions": [
            {
                "id":          a.id,
                "label":       a.label,
                "description": a.description,
                "params": [
                    {
                        "id":          p.id,
                        "label":       p.label,
                        "kind":        p.kind,
                        "default":     p.default,
                        "description": p.description,
                    }
                    for p in a.params
                ],
            }
            for a in d.actions
        ],
    }


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()
    # Discovery cache is module-level (see extensions.discover); calling
    # here is cheap on subsequent app builds within one process.
    consumers = discover()
    by_id = {d.id: d for d in consumers}

    @router.get("/consumers")
    def list_consumers() -> JSONResponse:
        return JSONResponse(
            content={"consumers": [_describe(d) for d in consumers]},
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/consumers/{consumer_id}/{action_id}/run")
    def run_action(
        consumer_id: str = PathParam(...),
        action_id: str = PathParam(...),
        body: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        if not ID_RE.match(consumer_id) or not ID_RE.match(action_id):
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "error": "invalid consumer/action id"},
            )
        d = by_id.get(consumer_id)
        if d is None:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "error": f"consumer {consumer_id!r} not found"},
            )
        action = next((a for a in d.actions if a.id == action_id), None)
        if action is None:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "error": f"action {action_id!r} not found"},
            )
        if action.handler is None:
            raise HTTPException(
                status_code=500,
                detail={"ok": False, "error": "action has no handler"},
            )

        # Build handler kwargs from the action's declared params only.
        # The body may carry extra keys (UI bookkeeping, future fields)
        # but the action's params list is the source of truth for what
        # the handler accepts. Missing params fall back to the param's
        # declared default.
        kwargs: dict[str, Any] = {}
        for p in action.params:
            kwargs[p.id] = body.get(p.id, p.default)
        kwargs["config"] = config

        # Per-consumer label namespace so different consumers run in
        # parallel; re-running the same action serialises against
        # itself via the spawn_job label lock.
        label = f"consumer__{consumer_id}__{action_id}"
        cmd_display: list[str] = [f"consumer:{consumer_id}.{action_id}"]
        for p in action.params:
            cmd_display.append(f"--{p.id}={kwargs[p.id]!r}")

        try:
            job = spawn_job(
                kind="consumer",
                label=label,
                target=action.handler,
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

    return router


__all__ = ["make_router"]
