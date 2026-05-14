"""In-memory job runner shared by the ``/api/extract/*`` endpoints.

Port of ``webview/src/server/jobs.ts``. Same locking + GC semantics
because the Svelte client polls on a fixed cadence — change the
behaviour and you change the client's user-visible state machine.

Threading model:

* ``_jobs`` and ``_job_locks`` are protected by ``_lock`` (a
  ``threading.Lock``) since they're touched both from request handlers
  (FastAPI thread pool) and from per-job stdout/stderr reader threads.
* Each spawned subprocess gets two daemon reader threads — one for
  ``stdout`` and one for ``stderr`` — that loop on ``readline`` and
  append into the job buffer through :func:`append_bounded`.
* A third daemon thread blocks on ``Popen.wait()`` and flips the job's
  terminal state when the child exits.

Cancellation: ``Popen.terminate()`` sends SIGTERM on POSIX and falls
through to ``TerminateProcess`` on Windows. Same behaviour as the Node
side's ``child.kill('SIGTERM')``.

GC: any access (``gc_jobs`` + ``spawn_job``/``list_jobs``) drops
completed jobs older than ``JOB_RETENTION_MS``. State is in-process —
a server restart wipes the table, which is fine for a single-user
dev tool.
"""

from __future__ import annotations

import os
import random
import string
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

JobState = Literal["running", "done", "failed", "cancelled"]
JobKind = Literal["extract", "skin"]

# 1 hour. Match the Node side exactly — the client polls completed jobs
# for a while after they finish to render the final log tail.
JOB_RETENTION_MS: int = 60 * 60 * 1000
# 1 MiB per stream. Trim from the head with a marker on overflow.
JOB_MAX_OUTPUT: int = 1 * 1024 * 1024


class JobLockedError(Exception):
    """Raised when ``spawn_job`` is called with a label already running.

    Carries ``existing_id`` so the caller can surface the conflicting
    job id in the HTTP 409 body.
    """

    def __init__(self, message: str, existing_id: str) -> None:
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class Job:
    """In-memory representation of one tracked subprocess.

    Field names use the snake_case Python convention; the JSON
    serialiser exposes them verbatim, matching the Node-side
    response shape ``(started_at, finished_at, exit_code)``.

    ``proc`` is the live ``Popen`` — set to ``None`` after exit so
    requests can introspect ``state`` without grabbing the lock.
    """

    id: str
    kind: JobKind
    label: str
    state: JobState
    cmd: list[str]
    started_at: int
    finished_at: int | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)


# Module-level state. The lock guards both maps because they have to
# stay consistent (the lock keyed by label points at an id that must
# still be in ``_jobs`` and in state=running).
_jobs: dict[str, Job] = {}
_job_locks: dict[str, str] = {}
_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_job_id() -> str:
    """Generate a job id matching the Node-side format roughly.

    The Node code emits ``<ms-base36>-<6-char-base36-random>``. We use
    a similar layout (a sortable timestamp + random tail) because the
    client's job id regex (``JOB_ID = /^[A-Za-z0-9\\-]{6,40}$/``)
    accepts both shapes.
    """
    ts = _now_ms()
    base36 = _to_base36(ts)
    tail = "".join(
        random.choices(string.ascii_lowercase + string.digits, k=6)
    )
    return f"{base36}-{tail}"


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = string.digits + string.ascii_lowercase
    out: list[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def append_bounded(prev: str, chunk: str) -> str:
    """Append ``chunk`` to ``prev`` with a 1 MiB head-trim cap.

    Matches the Node side's ``appendBounded`` byte-for-byte: keep the
    most recent ``JOB_MAX_OUTPUT`` characters plus a one-line marker
    once we start dropping data.
    """
    combined = prev + chunk
    if len(combined) <= JOB_MAX_OUTPUT:
        return combined
    return (
        "… [earlier output truncated] …\n"
        + combined[len(combined) - JOB_MAX_OUTPUT :]
    )


def gc_jobs() -> None:
    """Drop completed jobs older than ``JOB_RETENTION_MS``.

    Cheap — runs on every endpoint access so the table never grows
    unbounded over a long dev session.
    """
    now = _now_ms()
    with _lock:
        stale = [
            jid
            for jid, j in _jobs.items()
            if j.state != "running"
            and j.finished_at is not None
            and now - j.finished_at > JOB_RETENTION_MS
        ]
        for jid in stale:
            job = _jobs.pop(jid)
            # Only drop the lock if it still points at this job — a
            # newer run on the same label may have already overwritten.
            if _job_locks.get(job.label) == jid:
                _job_locks.pop(job.label, None)


def _drain_stream(
    job: Job,
    stream: Any,
    stream_name: Literal["stdout", "stderr"],
) -> None:
    """Reader-thread body. Append decoded lines to the job buffer.

    Decodes byte chunks as UTF-8 with replacement so a stray byte in
    the child's output doesn't kill the reader. Line-buffered: we
    grab one line at a time so the client sees progress without
    blocking on EOF.
    """
    try:
        for raw in iter(stream.readline, b""):
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace")
            with _lock:
                if stream_name == "stdout":
                    job.stdout = append_bounded(job.stdout, text)
                else:
                    job.stderr = append_bounded(job.stderr, text)
    except Exception as exc:  # noqa: BLE001 — log + keep going
        with _lock:
            job.stderr = append_bounded(
                job.stderr,
                f"\n[{stream_name} reader error] {exc}\n",
            )
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _supervise(job: Job) -> None:
    """Wait for the child to exit and flip the job's terminal state."""
    proc = job.proc
    if proc is None:
        return
    try:
        code = proc.wait()
    except Exception as exc:  # noqa: BLE001 — defensive
        with _lock:
            job.stderr = append_bounded(
                job.stderr, f"\n[wait error] {exc}\n"
            )
            job.state = "failed"
            job.finished_at = _now_ms()
            job.proc = None
            if _job_locks.get(job.label) == job.id:
                _job_locks.pop(job.label, None)
        return

    with _lock:
        # If cancel() already marked the job, keep its terminal label
        # but record the exit code so the API can surface it.
        if job.state == "cancelled":
            job.exit_code = code
            job.proc = None
            return
        job.exit_code = code
        job.state = "done" if code == 0 else "failed"
        job.finished_at = _now_ms()
        job.proc = None
        if _job_locks.get(job.label) == job.id:
            _job_locks.pop(job.label, None)


def spawn_job(
    *,
    kind: JobKind,
    label: str,
    cmd: list[str],
    cwd: Path,
) -> Job:
    """Start a subprocess + register it as an active job.

    Returns the freshly-registered :class:`Job`. Raises
    :class:`JobLockedError` when ``label`` is already attached to a
    running job (the Node side returned HTTP 409 with the same
    information; the FastAPI handler does too).
    """
    gc_jobs()
    with _lock:
        existing = _job_locks.get(label)
        if existing:
            ex = _jobs.get(existing)
            if ex and ex.state == "running":
                raise JobLockedError(
                    f"another job for '{label}' is already running "
                    f"(id={existing})",
                    existing_id=existing,
                )

    job_id = _new_job_id()
    # PYTHONUNBUFFERED forces line-buffered stdout/stderr in any Python
    # child (the entry point AND any nested subprocess.run('python ...')
    # calls). Without it, Python detects its stdout is a pipe and
    # switches to 4 KB block buffering, so progress prints sit in the
    # buffer for tens of seconds before the client poll sees them.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    # On Windows, hide the spawned console window — same as
    # ``windowsHide: true`` in the Node version.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
        shell=False,
        creationflags=creationflags,
        bufsize=0,
    )
    job = Job(
        id=job_id,
        kind=kind,
        label=label,
        state="running",
        cmd=list(cmd),
        started_at=_now_ms(),
        finished_at=None,
        exit_code=None,
        stdout="",
        stderr="",
        proc=proc,
    )
    with _lock:
        _jobs[job_id] = job
        _job_locks[label] = job_id

    # Two stream readers + one supervisor — all daemon so they don't
    # hold the process open at shutdown.
    threading.Thread(
        target=_drain_stream,
        args=(job, proc.stdout, "stdout"),
        name=f"job-{job_id}-stdout",
        daemon=True,
    ).start()
    threading.Thread(
        target=_drain_stream,
        args=(job, proc.stderr, "stderr"),
        name=f"job-{job_id}-stderr",
        daemon=True,
    ).start()
    threading.Thread(
        target=_supervise,
        args=(job,),
        name=f"job-{job_id}-supervise",
        daemon=True,
    ).start()

    return job


def cancel_job(job_id: str) -> Job | None:
    """SIGTERM the subprocess and mark the job cancelled.

    Returns the post-cancel snapshot of the job, or ``None`` if the
    id is unknown. The supervisor thread records the eventual exit
    code via the ``state == 'cancelled'`` branch in :func:`_supervise`.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if job.state == "running" and job.proc is not None:
            try:
                job.proc.terminate()
            except Exception as exc:  # noqa: BLE001
                # SIGTERM may fail on Windows for some processes; mark
                # cancelled anyway so the UI unblocks.
                job.stderr = append_bounded(
                    job.stderr, f"\n[kill failed] {exc}\n"
                )
            job.state = "cancelled"
            job.finished_at = _now_ms()
            job.stderr = append_bounded(
                job.stderr, "\n[cancelled by user]\n"
            )
            if _job_locks.get(job.label) == job.id:
                _job_locks.pop(job.label, None)
        return job


def get_job(job_id: str) -> Job | None:
    """Return the job snapshot, or ``None`` if unknown."""
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    """Return all known jobs, newest started first."""
    gc_jobs()
    with _lock:
        out = list(_jobs.values())
    out.sort(key=lambda j: j.started_at, reverse=True)
    return out


def job_to_dict(job: Job) -> dict[str, Any]:
    """Serialise a job to the wire shape the client expects.

    Drops the internal ``proc`` handle and renames nothing — the Node
    side and FastAPI side both emit snake_case ``started_at`` /
    ``finished_at`` / ``exit_code``, so the dataclass field names map
    directly.
    """
    d = asdict(job)
    d.pop("proc", None)
    return d


def job_to_summary(job: Job) -> dict[str, Any]:
    """Smaller serialisation for the list endpoint (no stdout/stderr).

    Matches the trimmed shape the Node side returns from
    ``GET /api/extract/jobs``.
    """
    return {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "state": job.state,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
    }


__all__ = [
    "Job",
    "JobKind",
    "JobLockedError",
    "JobState",
    "JOB_MAX_OUTPUT",
    "JOB_RETENTION_MS",
    "append_bounded",
    "cancel_job",
    "gc_jobs",
    "get_job",
    "job_to_dict",
    "job_to_summary",
    "list_jobs",
    "spawn_job",
]
