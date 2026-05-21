"""In-memory job runner shared by the ``/api/extract/*`` endpoints.

Stage-3 rewrite: jobs run as composer calls inside a module-level
``ThreadPoolExecutor`` instead of as ``subprocess.Popen`` children
shelling out to ``wows-*`` console scripts. Two reasons it had to
move:

1. PyInstaller / single-exe packaging. The frozen exe doesn't carry
   the ``wows-ingest-ship`` etc. shims, so the old runner couldn't
   spawn them. In-process calls sidestep PATH lookup entirely.
2. Step-granular cancel + structured progress. ``compose.*`` already
   emits :class:`wows_model_export.types.StepEvent` notifications and
   the StepRunner's cancel hook (added in Phase 0) lets us tear
   down work cleanly between steps instead of SIGTERMing a child.

Wire-shape compatibility: the existing Svelte client polls
``GET /api/jobs/{id}`` for ``state`` / ``stdout`` / ``exit_code`` /
``cmd`` / ``finished_at``. Every one of those keys keeps its
old meaning — ``stdout`` is now synthesized from the
:class:`StepEvent` stream in the same line format ``cli/_emit.py``
emits, ``exit_code`` is a synthetic ``0`` / ``1`` / ``-1`` flag
mapped from the terminal state, and ``cmd`` carries the composer's
target name + label (display-only, not shell-runnable). New optional
fields (``events``, ``result``, ``error``) are additive — old
clients ignore them.

Threading model:

* ``_jobs`` and ``_job_locks`` are protected by ``_lock``. Touched
  from request handlers (FastAPI thread pool) AND from the executor's
  worker threads via the ``on_event`` closure / ``add_done_callback``.
* ``_executor = ThreadPoolExecutor(max_workers=4, ...)`` at module
  scope bounds concurrency. The single-user dev tool doesn't need
  more, and capping protects against e.g. four ``snapshot`` jobs
  each loading a 2.8 GB GameParams blob simultaneously.
* Each job's ``cancel_event`` is a per-job :class:`threading.Event`
  the route handler flips on POST to ``/api/jobs/{id}/cancel``;
  the StepRunner sees it at the next step boundary and raises
  :class:`CancelledError` (a :class:`StepError` subclass).

GC: identical semantics to the prior subprocess-based runner —
completed jobs older than ``JOB_RETENTION_MS`` get dropped on every
endpoint access so the table can't grow over a long dev session.
"""

from __future__ import annotations

import random
import string
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..errors import CancelledError
from ..types import StepEvent

JobState = Literal["running", "done", "failed", "cancelled"]
JobKind = Literal["extract", "skin", "bootstrap", "rig", "consumer"]

# 1 hour. The client polls completed jobs for a while after they finish
# to render the final log tail; keeping a generous retention window
# means a poller that's a few seconds late still sees terminal state.
JOB_RETENTION_MS: int = 60 * 60 * 1000
# 1 MiB stdout cap. Trim from the head with a marker on overflow so the
# client always sees the most recent N bytes. Matches the old runner's
# byte budget exactly so the wire shape stays byte-identical for
# realistic job log sizes.
JOB_MAX_OUTPUT: int = 1 * 1024 * 1024


class JobLockedError(Exception):
    """Raised when ``spawn_job`` is called with a label already running.

    Carries ``existing_id`` so the route handler can surface the
    conflicting job id in the HTTP 409 body — same shape the
    subprocess-based runner used.
    """

    def __init__(self, message: str, existing_id: str) -> None:
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class Job:
    """In-memory representation of one tracked composer call.

    Field map (kept compatible with the prior subprocess-based shape so
    the Svelte client doesn't need a code change):

    * ``id`` / ``kind`` / ``label`` / ``state`` / ``started_at`` /
      ``finished_at`` — unchanged.
    * ``cmd`` — display string of the form
      ``[compose.ingest_ship, label]``. Not shell-runnable (the runner
      doesn't shell out anymore) but useful as a "what did this job
      do" header in the UI.
    * ``stdout`` — synthesized from the composer's :class:`StepEvent`
      stream in the same line format ``cli/_emit.make_text_printer``
      writes. Lets the existing client render progress without code
      changes.
    * ``stderr`` — empty during normal operation; on failure it
      carries the composer exception's traceback. ``cancel_job``
      appends a ``[cancelled by user]`` marker so the UI matches what
      the SIGTERM-based runner produced.
    * ``exit_code`` — synthetic: 0 for done, 1 for failed, -1 for
      cancelled. Mirrors the ``Popen`` exit codes the prior runner
      surfaced for the same terminal states.

    New optional fields (additive — old clients ignore them):

    * ``events`` — full ordered list of structured composer events.
      Lets a future SSE / progress-aware client skip the stdout
      regex-parse round-trip.
    * ``result`` — the composer's typed return value, serialised to
      a JSON-friendly dict on read (Path → str etc.).
    * ``error`` — ``{"type", "message", "step"}`` on failure.

    Internal fields (excluded from ``repr``):

    * ``future`` — the ``Future`` we submit to the executor.
    * ``cancel_event`` — flipped by ``cancel_job``; observed by the
      StepRunner at the next step boundary.
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
    events: list[StepEvent] = field(default_factory=list)
    result: Any = None
    error: dict[str, Any] | None = None
    future: Future[Any] | None = field(default=None, repr=False)
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False
    )


# Module-level state. The lock guards both maps because they have to
# stay consistent (the lock keyed by label points at an id that must
# still be in ``_jobs`` and in state=running).
_jobs: dict[str, Job] = {}
_job_locks: dict[str, str] = {}
_lock = threading.Lock()

# Reverse index from a Future to its Job, populated under ``_lock`` at
# spawn time. The ``add_done_callback`` runs on either an executor
# thread (most cases) or the calling thread (when the Future was already
# done at attach time, an unlikely race). Either way the lookup needs
# to be O(1) and lock-protected — searching ``_jobs.values()`` for the
# matching ``future`` would race with ``gc_jobs``.
_jobs_by_future: dict[Future[Any], Job] = {}

# Bounded worker pool. Four feels right for a single-user dev tool:
# concurrent skin-pack + extract + bootstrap is realistic, plus headroom
# for a snapshot rebuild. Higher fan-out invites OOM on the ~200 MB
# wowsunpack subprocess (each ``compose.scaffold_ship`` peaks ~3
# wowsunpack children for hull + armor + ammo).
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job-")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_job_id() -> str:
    """Generate a job id matching the legacy Node-side format roughly.

    The Node code emitted ``<ms-base36>-<6-char-base36-random>``. We
    use a similar layout because the client's job id regex
    (``JOB_ID = /^[A-Za-z0-9\\-]{6,40}$/``) accepts both shapes and a
    sortable timestamp helps eyeball debugging.
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

    Keep the most recent ``JOB_MAX_OUTPUT`` characters plus a one-line
    marker once we start dropping data. Same shape as the legacy Node
    helper of the same name.
    """
    combined = prev + chunk
    if len(combined) <= JOB_MAX_OUTPUT:
        return combined
    return (
        "… [earlier output truncated] …\n"
        + combined[len(combined) - JOB_MAX_OUTPUT :]
    )


def _format_event_line(event: StepEvent) -> str:
    """Render a :class:`StepEvent` as one ``stdout`` line.

    Mirrors :func:`wows_model_export.cli._emit.make_text_printer`'s
    format byte-for-byte::

        [    1234ms]  step state  (Δ 678ms)  detail

    Keeping the format identical means the existing client renders
    in-process job output the same way it rendered subprocess output —
    no UI change needed for Phase 1.
    """
    elapsed = f"[{event.elapsed_ms:>8.0f}ms]"
    parts = [elapsed, f"{event.step} {event.state}"]
    if event.step_ms is not None:
        parts.append(f"(Δ {event.step_ms:.0f}ms)")
    if event.detail:
        parts.append(event.detail)
    return "  ".join(parts) + "\n"


def gc_jobs() -> None:
    """Drop completed jobs older than ``JOB_RETENTION_MS``.

    Cheap — runs on every endpoint access so the table never grows
    unbounded over a long dev session. Scrubs ``_jobs_by_future`` too
    so dead Future references don't pin memory after a long-running
    composer returns.
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
            if job.future is not None:
                _jobs_by_future.pop(job.future, None)
            # Only drop the lock if it still points at this job — a
            # newer run on the same label may have already overwritten.
            if _job_locks.get(job.label) == jid:
                _job_locks.pop(job.label, None)


def _on_job_done(future: Future[Any]) -> None:
    """``Future.add_done_callback`` body: flip the job's terminal state.

    Runs on whichever thread completed the Future — usually a worker
    in ``_executor``, but can be the calling thread if the Future was
    already done when ``add_done_callback`` attached (rare; possible
    for a synchronous failure inside ``submit``). Either way: grab the
    lock, classify the outcome, mark terminal state, release the
    label lock so a re-run can start.
    """
    with _lock:
        job = _jobs_by_future.get(future)
    # Defensive: the Future outlived its Job entry. ``gc_jobs`` clears
    # the reverse index when a job is dropped, but a poll racing with
    # gc could in theory hit this path.
    if job is None:
        return

    with _lock:
        # ``cancel_job`` already may have flipped state="cancelled"
        # eagerly so the UI unblocks immediately. Don't overwrite that;
        # we only flip from "running" here. The cancel branch below
        # still records the finishing timestamp.
        already_terminal = job.state != "running"

        if future.cancelled() or job.cancel_event.is_set():
            # Either the executor cancelled the Future before it ran
            # (Future.cancel returns True) OR the StepRunner observed
            # the cancel flag mid-step and raised CancelledError.
            if not already_terminal:
                job.state = "cancelled"
            job.exit_code = -1
        else:
            try:
                exc = future.exception()
            except Exception:  # noqa: BLE001 — defensive
                # ``future.exception()`` itself raised; treat as failed.
                exc = RuntimeError("future.exception() raise")

            if exc is not None:
                # Cancellation can also bubble through the exception
                # path when the StepRunner raised CancelledError mid-
                # step (Future.cancel() returns False because the
                # callable was already running). Discriminate explicitly
                # so the UI shows "cancelled" instead of "failed".
                if isinstance(exc, CancelledError):
                    if not already_terminal:
                        job.state = "cancelled"
                    job.exit_code = -1
                    job.error = {
                        "type":    "CancelledError",
                        "message": str(exc),
                        "step":    getattr(exc, "step", None),
                    }
                else:
                    if not already_terminal:
                        job.state = "failed"
                    job.exit_code = 1
                    job.stderr = append_bounded(
                        job.stderr,
                        "".join(
                            traceback.format_exception(
                                type(exc), exc, exc.__traceback__
                            )
                        ),
                    )
                    job.error = {
                        "type":    type(exc).__name__,
                        "message": str(exc),
                        # ``StepError`` carries .step; other exception
                        # types just leave it None.
                        "step":    getattr(exc, "step", None),
                    }
            else:
                if not already_terminal:
                    job.state = "done"
                job.exit_code = 0
                job.result = future.result()

        # Always set finished_at — both for the natural path and for
        # the cancel-already-flipped-state case. The Svelte client uses
        # this to compute elapsed time for the row.
        if job.finished_at is None:
            job.finished_at = _now_ms()

        # Release the label lock so a follow-up run on the same label
        # can start. Only release if it still names this job — a
        # cancelled job that took a moment to actually exit might find
        # the label already re-claimed.
        if _job_locks.get(job.label) == job.id:
            _job_locks.pop(job.label, None)


def spawn_job(
    *,
    kind: JobKind,
    label: str,
    target: Callable[..., Any],
    kwargs: dict[str, Any],
    cmd_display: list[str] | None = None,
) -> Job:
    """Submit a composer call to the executor as a tracked job.

    Returns the freshly-registered :class:`Job`. Raises
    :class:`JobLockedError` when ``label`` is already attached to a
    running job — same 409 contract as the subprocess-based runner.

    Parameters:
        kind         Bucket the job belongs to. Drives nothing
                     server-side; the client uses it to colour rows.
        label        Job lock key. While a job with this label is
                     ``running``, a second ``spawn_job(label=…)``
                     raises :class:`JobLockedError`.
        target       The composer callable. Must accept ``on_event``
                     and ``cancel`` kwargs (every ``compose.*`` entry
                     does as of Phase 0).
        kwargs       Keyword arguments to pass to ``target``. The
                     runner injects ``on_event`` and ``cancel``
                     automatically; pass any other params (config,
                     workspace, ship_input, …) here.
        cmd_display  Display-only command label. Kept compatible with
                     the legacy ``cmd`` field's shape (a list of
                     strings) so the existing UI renders it. Default:
                     ``[target.__qualname__, label]``.
    """
    gc_jobs()

    # Cheap label-collision check before we burn an id. Same dance as
    # the subprocess-based runner — keeps the ``running`` check inside
    # the lock so a concurrent re-spawn can't slip through.
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
    cmd = list(cmd_display) if cmd_display is not None else [
        getattr(target, "__qualname__", repr(target)),
        label,
    ]

    job = Job(
        id=job_id,
        kind=kind,
        label=label,
        state="running",
        cmd=cmd,
        started_at=_now_ms(),
        finished_at=None,
        exit_code=None,
        stdout="",
        stderr="",
        events=[],
        result=None,
        error=None,
    )

    # Per-job event listener. Two responsibilities:
    #   1. Append the structured event to job.events so a future
    #      events-aware client can render structured progress.
    #   2. Append a formatted line to job.stdout matching the existing
    #      cli/_emit.py format so today's polling client renders
    #      progress without changing.
    # Runs on the executor's worker thread; we hold ``_lock`` for the
    # mutation so a concurrent ``GET /api/jobs/{id}`` sees a coherent
    # snapshot.
    def on_event(event: StepEvent) -> None:
        line = _format_event_line(event)
        with _lock:
            job.events.append(event)
            job.stdout = append_bounded(job.stdout, line)

    # Inject the runner-managed channels into the composer kwargs.
    # Caller-supplied on_event / cancel are intentionally clobbered —
    # the route should never set them; if it did, we'd lose progress
    # tracking and cancel.
    full_kwargs = {**kwargs, "on_event": on_event, "cancel": job.cancel_event}

    with _lock:
        _jobs[job_id] = job
        _job_locks[label] = job_id

    # ``submit`` may raise (e.g. executor shutdown after gc_jobs); roll
    # back our bookkeeping so a panic-restart doesn't leave a phantom
    # ``running`` entry the gc never collects.
    try:
        future = _executor.submit(target, **full_kwargs)
    except Exception:
        with _lock:
            _jobs.pop(job_id, None)
            if _job_locks.get(label) == job_id:
                _job_locks.pop(label, None)
        raise

    job.future = future
    with _lock:
        _jobs_by_future[future] = job

    # Attach AFTER inserting into _jobs_by_future so the callback's
    # lookup can never miss. add_done_callback may fire synchronously
    # if the Future is already done (rare for a fresh submit, but
    # possible if the executor is shutting down).
    future.add_done_callback(_on_job_done)

    return job


def cancel_job(job_id: str) -> Job | None:
    """Mark a job cancelled and signal the cancel flag.

    Two-stage cancel:

    1. Set ``job.cancel_event`` so the StepRunner's ``_check_cancelled``
       sees it at the next step boundary and raises ``CancelledError``.
    2. Call ``Future.cancel()``. This succeeds (returns True) if the
       executor hasn't started running the callable yet — in that case
       the Future is "cancelled" and our ``_on_job_done`` flip happens
       almost immediately. If the callable is already running,
       ``Future.cancel()`` returns False (no-op). That's fine: the
       StepRunner check above is the cancellation channel for in-
       flight composers; the Future's ``done_callback`` still fires
       when the composer eventually raises ``CancelledError``.

    Eagerly flips ``state="cancelled"`` so the UI unblocks immediately
    instead of polling for ~one step duration. ``_on_job_done`` will
    see ``already_terminal=True`` and avoid clobbering the state, but
    will still record ``finished_at`` and release the label lock.

    Returns the post-cancel snapshot of the job, or ``None`` if the id
    is unknown — same contract as the subprocess-based runner.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if job.state == "running":
            job.cancel_event.set()
            # Eagerly mark cancelled — same UX as the SIGTERM path:
            # the client sees the new state on the next poll without
            # waiting for the StepRunner to observe the flag.
            job.state = "cancelled"
            job.stderr = append_bounded(
                job.stderr, "\n[cancelled by user]\n"
            )
            future = job.future

    # Future.cancel() outside the lock — it grabs its own internal
    # lock and we don't want to invert the order.
    if job.state == "cancelled" and future is not None:
        try:
            future.cancel()
        except Exception:
            # Future.cancel never raises in CPython, but be safe; the
            # cancel_event channel is the real cancellation signal so
            # a Future.cancel failure isn't load-bearing.
            pass

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


def _jsonify(value: Any) -> Any:
    """Recursively convert a composer result value to JSON-friendly form.

    Handles ``Path`` (→ str), dataclasses (→ asdict-equivalent walk),
    tuples / sets (→ list), and falls through for primitives. Defensive
    fallback for anything else: ``str(value)`` so a future composer
    field type doesn't break the wire.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonify(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        # Manual walk rather than ``dataclasses.asdict`` — asdict
        # deep-copies, which is wasted work, AND it doesn't know how to
        # serialise a ``Path`` field, so we'd post-process anyway.
        return {
            f: _jsonify(getattr(value, f))
            for f in value.__dataclass_fields__
        }
    return str(value)


def job_to_dict(job: Job) -> dict[str, Any]:
    """Serialise a job to the wire shape the client expects.

    Backward-compatible keys: ``id`` / ``kind`` / ``label`` / ``state``
    / ``cmd`` / ``started_at`` / ``finished_at`` / ``exit_code`` /
    ``stdout`` / ``stderr`` carry their old meanings — see the ``Job``
    docstring for what each maps to in the in-process runner.

    Additive keys (new in Stage 3): ``events`` (list of structured
    events), ``result`` (composer return value, JSON-serialised),
    ``error`` (``{type, message, step}`` on failure). Old clients
    ignoring extra fields keep working.
    """
    return {
        "id":          job.id,
        "kind":        job.kind,
        "label":       job.label,
        "state":       job.state,
        "cmd":         list(job.cmd),
        "started_at":  job.started_at,
        "finished_at": job.finished_at,
        "exit_code":   job.exit_code,
        "stdout":      job.stdout,
        "stderr":      job.stderr,
        # New fields below are additive — old clients ignore extras.
        "events": [
            {
                "step":       e.step,
                "state":      e.state,
                "detail":     e.detail,
                "elapsed_ms": e.elapsed_ms,
                "step_ms":    e.step_ms,
                "data":       _jsonify(e.data) if e.data is not None else None,
            }
            for e in job.events
        ],
        "result": _jsonify(job.result),
        "error":  job.error,
    }


def job_to_summary(job: Job) -> dict[str, Any]:
    """Smaller serialisation for the list endpoint (no stdout / events).

    Matches the trimmed shape the Node side returned from
    ``GET /api/extract/jobs``.
    """
    return {
        "id":          job.id,
        "kind":        job.kind,
        "label":       job.label,
        "state":       job.state,
        "started_at":  job.started_at,
        "finished_at": job.finished_at,
        "exit_code":   job.exit_code,
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
