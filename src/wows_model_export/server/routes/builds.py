"""``/api/builds``: game-update diffs and stale-export tracking.

* ``GET  /api/builds``             builds on disk, saved manifests, cache
                                   stamps, and every export's state against
                                   the current build (``needs_snapshot`` when
                                   the current build has no manifest yet).
* ``GET  /api/builds/diff``        what changed between two builds
                                   (``?from=&to=``; defaults: the newest
                                   older build -> current). Lists are capped.
* ``POST /api/builds/snapshot``    job: save the current build's manifest.
* ``POST /api/builds/reexport``    job: re-run stale (or named) exports with
                                   their original arguments.

See :mod:`wows_model_export.compose.build_tracking` for the model.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ...compose import build_manifest as bm
from ...compose import build_tracking as bt
from ...compose._step_runner import StepRunner
from ...config import PipelineConfig
from ...toolkit import build as tb
from ...types import OnEvent
from ..jobs import JobLockedError, spawn_job

_CACHE_FILES = (
    "gameparams.json",
    "assets.bin",
    "camouflages.xml",
    "wows_manifest.json",
    "skel_ext_hashes.json",
)
_LIST_CAP = 300
_JOB_LABEL = "builds:run"


class SnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: bool = False


class ReexportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str] = Field(default_factory=list)
    kind: str | None = None
    include_untracked: bool = False
    replay_skins: bool = True
    build_library: bool = True
    jobs: int = Field(default=bt.DEFAULT_REEXPORT_JOBS, ge=1, le=16)


def _cap(items: list[str], n: int = _LIST_CAP) -> dict[str, Any]:
    return {"total": len(items), "items": items[:n]}


def _snapshot_job(
    *, config: PipelineConfig, refresh: bool,
    on_event: OnEvent | None = None, cancel: threading.Event | None = None,
) -> dict[str, Any]:
    runner = StepRunner(on_event, cancel=cancel)
    build = tb.current_build(config)
    with runner.step("build_manifest", detail=f"build {build}") as ctx:
        path = bm.ensure_manifest(build, config=config, refresh=refresh)
        m = bm.load_manifest(build, config=config, create=False)
        ctx.annotate(
            f"{len(m.files)} files, {len(m.records)} records, "
            f"{len(m.gameparams)} GameParams, {len(m.camouflages)} camouflage entries"
        )
    refreshed = bt.refresh_fingerprints(build, config=config) if refresh else 0
    return {"build": build, "path": str(path), "refingerprinted": refreshed}


def _reexport_job(
    *, config: PipelineConfig, body: dict[str, Any],
    on_event: OnEvent | None = None, cancel: threading.Event | None = None,
) -> dict[str, Any]:
    runner = StepRunner(on_event, cancel=cancel)
    _, statuses = bt.workspace_status(config=config)
    names = set(body.get("names") or ())
    kind = body.get("kind")
    targets = [
        st for st in statuses
        if (not kind or st.kind == kind)
        and (
            st.name in names if names
            else st.state == "stale"
            or (body.get("include_untracked") and st.state in ("untracked", "error"))
        )
    ]
    runner.emit("select_exports", "completed", detail=f"{len(targets)} export(s)")

    def progress(i: int, total: int, st: bt.ExportStatus) -> None:
        runner.emit("reexport", "started", detail=f"[{i + 1}/{total}] {st.kind} {st.name} ({st.state})")

    report = bt.reexport_exports(
        targets,
        config=config,
        replay_skins=bool(body.get("replay_skins", True)),
        build_library=bool(body.get("build_library", True)),
        jobs=int(body.get("jobs") or bt.DEFAULT_REEXPORT_JOBS),
        on_event=on_event,
        cancel=cancel,
        on_progress=progress,
    )
    failed = report["failed"]
    skipped = report["skipped"]
    runner.emit(
        "reexport_summary",
        "failed" if failed else "completed",
        detail=f"re-exported {len(report['reexported'])}/{len(targets) - len(skipped)}"
        + (f"; failed: {', '.join(f['name'] for f in failed)}" if failed else "")
        + (f"; skipped (removed from the game): {', '.join(s['name'] for s in skipped)}"
           if skipped else ""),
    )
    return report


def make_router(config: PipelineConfig) -> APIRouter:
    router = APIRouter()

    def _status() -> dict[str, Any]:
        builds = tb.available_builds(config)
        current = builds[-1] if builds else None
        stored = bm.stored_builds(config)
        cache = config.require_cache_dir()
        caches = []
        for name in _CACHE_FILES:
            path = cache / name
            stamp = tb.read_stamp(path)
            caches.append({
                "name": name,
                "exists": path.is_file(),
                "build": stamp,
                "current": path.is_file() and stamp is not None and stamp == current,
            })
        doc: dict[str, Any] = {
            "current_build": current,
            "builds_with_idx": builds,
            "stored_manifests": stored,
            "caches": caches,
            "needs_snapshot": current is not None and current not in stored,
            "exports": None,
            "counts": {},
        }
        if current is not None and current in stored:
            _, statuses = bt.workspace_status(config=config)
            doc["exports"] = [s.as_dict(limit=40) for s in statuses]
            doc["counts"] = dict(Counter(s.state for s in statuses))
        return doc

    @router.get("/builds")
    async def get_builds() -> dict[str, Any]:
        return await asyncio.to_thread(_status)

    @router.get("/builds/diff")
    async def get_diff(
        from_build: int | None = Query(default=None, alias="from"),
        to_build: int | None = Query(default=None, alias="to"),
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            to_b = to_build or tb.current_build(config)
            stored = bm.stored_builds(config)
            if to_b is None or to_b not in stored:
                raise HTTPException(status_code=409, detail={
                    "ok": False, "error": f"no saved manifest for build {to_b}",
                })
            from_b = from_build or max((b for b in stored if b < to_b), default=None)
            if from_b is None or from_b not in stored:
                raise HTTPException(status_code=404, detail={
                    "ok": False, "error": "no older build with a saved manifest to compare against",
                })
            old = bm.load_manifest(from_b, config=config, create=False)
            new = bm.load_manifest(to_b, config=config, create=False)
            d = bm.diff_manifests(old, new)
            particles = sorted(
                p for p in d["records"]["changed"] + d["records"]["added"]
                if (new.records.get(p) or ["", "", ""])[2] in bm.PARTICLE_TYPES
            )
            return {
                "from_build": from_b,
                "to_build": to_b,
                "summary": d["summary"],
                "by_area": d["by_area"],
                "format_signals": d["format_signals"],
                "incomparable": d["incomparable"],
                "spaces": {
                    name: {k: len(v) for k, v in kinds.items()}
                    for name, kinds in d["spaces"].items()
                },
                "ship_model_dirs": d["ship_model_dirs"],
                "particles": _cap(particles),
                "gameparams": {k: _cap(v) for k, v in d["gameparams"].items()},
                "camouflages": {k: _cap(v) for k, v in d["camouflages"].items()},
                "sections": d["sections"],
            }

        return await asyncio.to_thread(run)

    def _spawn(target, kwargs: dict[str, Any], cmd: list[str]) -> dict[str, Any]:
        try:
            job = spawn_job(kind="builds", label=_JOB_LABEL, target=target,
                            kwargs=kwargs, cmd_display=cmd)
        except JobLockedError as err:
            raise HTTPException(status_code=409, detail={
                "ok": False, "error": str(err), "existing_job_id": err.existing_id,
            }) from None
        return {"ok": True, "job_id": job.id, "cmd": job.cmd}

    @router.post("/builds/snapshot")
    def post_snapshot(body: SnapshotBody) -> dict[str, Any]:
        return _spawn(
            _snapshot_job,
            {"config": config, "refresh": body.refresh},
            ["wows-builds", "snapshot", *(["--refresh"] if body.refresh else [])],
        )

    @router.post("/builds/reexport")
    def post_reexport(body: ReexportBody) -> dict[str, Any]:
        cmd = ["wows-builds", "reexport", *body.names]
        if body.kind:
            cmd += ["--kind", body.kind]
        if body.include_untracked:
            cmd.append("--include-untracked")
        if not body.replay_skins:
            cmd.append("--no-skins")
        if not body.build_library:
            cmd.append("--no-library")
        cmd += ["--jobs", str(body.jobs)]
        return _spawn(_reexport_job, {"config": config, "body": body.model_dump()}, cmd)

    return router


__all__ = ["make_router"]
