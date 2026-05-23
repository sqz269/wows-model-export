"""Persistent FIFO queue for ``compose.ingest_ship`` calls.

Sits on top of the existing :mod:`jobs` runner: the queue holds the
*ordering* + *persistence*, and each pending item gets dispatched as a
normal :func:`jobs.spawn_job` call when its turn comes. From the rest
of the system's point of view (UI job panel, /api/jobs/{id} polling,
cancel), a queue-spawned ingest looks identical to a direct
``/api/extract/run`` ingest.

Threading model:

* One **daemon worker thread** (``_worker_loop``). Loops forever:

    1. Wait on a :class:`threading.Condition` until something is pending
       (and ``_paused`` is False).
    2. Pick the head of the pending list. Spawn an ``ingest_ship`` job
       via :func:`spawn_job`. Stamp ``job_id`` + ``started_at`` on the
       queue item.
    3. Poll the underlying job until it leaves ``running``. Copy the
       terminal state (``done`` / ``failed`` / ``cancelled``) + error
       blurb onto the queue item.
    4. Persist + loop.

* Routes / public-API callers acquire the same Condition's lock when
  reading or mutating. ``Condition.notify_all`` wakes the worker after
  every state change so the user doesn't see a polling lag.

Persistence: JSON at ``<workspace>/.cache/extract_queue.json``. Atomic
temp-then-rename writes on every state change so a crashed backend
loses at most the in-flight item. On startup we downgrade any item
left in ``running`` to ``failed`` with a "backend restarted mid-run"
blurb (the underlying executor died with the process; no way to
re-attach).

Backwards-compat: the schema field ``version`` is the lever for future
breaking changes. v0 → drop the file silently. Single-user dev tool;
not worth a migration runner.

Job-lock interaction: the worker calls ``spawn_job(label=item.label)``.
If a user clicked "Run extract" directly on the same ship a moment
earlier, the same label is already locked and ``spawn_job`` raises
:class:`JobLockedError`. The worker catches that, sleeps briefly, and
retries — the queue item stays ``pending``. The user's direct click
wins; the queue absorbs the conflict.
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .. import compose
from ..config import PipelineConfig
from .jobs import JobLockedError, cancel_job, get_job, spawn_job

logger = logging.getLogger(__name__)

QueueStatus = Literal["pending", "running", "done", "failed", "cancelled"]

# Schema version of the persistence file. Bump on a breaking change to
# QueueItem's on-disk shape; older files load as an empty queue.
_SCHEMA_VERSION = 1

# How long the worker sleeps when it hits a JobLockedError (another job
# with the same ship label is already running). The condition wait wakes
# us early on a state change, so this is just a backstop.
_LABEL_BUSY_BACKOFF_S = 2.0

# How often the worker polls the underlying job's state. The poll runs
# without holding the queue lock so routes stay responsive.
_JOB_POLL_INTERVAL_S = 0.5

# Soft cap on completed-item history kept in the persistence file. Past
# this we drop the oldest completed entries so the file doesn't grow
# without bound over a long-running workspace.
_COMPLETED_HISTORY_CAP = 100


# ---------------------------------------------------------------------------
# QueueItem dataclass + serialisation
# ---------------------------------------------------------------------------


@dataclass
class QueueItem:
    """One row in the extract queue.

    ``vehicle`` / ``label`` / ``permoflage`` / ``build_library`` are the
    user-facing identity (mirror the ``/api/extract/run`` body shape).
    ``toolkit_ship`` + ``gameparams_ship_id`` are the resolved kwargs
    captured at enqueue time so the worker thread doesn't need to walk
    the GameParams snapshot every time the queue rolls forward.

    ``status`` tracks the lifecycle. The worker is the only writer for
    the ``running`` -> terminal transition; remove / cancel / reorder
    routes change ``pending``-side state under the same lock.
    """

    queue_id:        str
    vehicle:         str
    label:           str
    permoflage:      str | None
    build_library:   bool
    # Pre-resolved kwargs (the enqueue route does this lookup once so the
    # worker stays snapshot-independent).
    toolkit_ship:    str
    gameparams_ship_id: str
    # Lifecycle
    status:          QueueStatus
    job_id:          str | None
    enqueued_at:     int
    started_at:      int | None = None
    finished_at:     int | None = None
    error:           str | None = None


def _item_to_dict(item: QueueItem) -> dict[str, Any]:
    return asdict(item)


def _item_from_dict(d: dict[str, Any]) -> QueueItem | None:
    """Lenient loader: return ``None`` for malformed rows.

    Loose validation — a missing field or an unexpected ``status`` drops
    the row instead of poisoning the whole queue. Defensive because we
    can't trust an old file across a schema change.
    """
    try:
        return QueueItem(
            queue_id=          str(d["queue_id"]),
            vehicle=           str(d["vehicle"]),
            label=             str(d["label"]),
            permoflage=        (
                None if d.get("permoflage") is None else str(d["permoflage"])
            ),
            build_library=     bool(d.get("build_library", True)),
            toolkit_ship=      str(d["toolkit_ship"]),
            gameparams_ship_id=str(d["gameparams_ship_id"]),
            status=            d.get("status", "pending"),
            job_id=            d.get("job_id"),
            enqueued_at=       int(d.get("enqueued_at", _now_ms())),
            started_at=        d.get("started_at"),
            finished_at=       d.get("finished_at"),
            error=             d.get("error"),
        )
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------


# All queue items in user-facing order: pending head first, then
# running (at most one at a time from the worker), then completed in
# enqueue order. The worker promotes the head from pending to running.
_items: list[QueueItem] = []

# Re-entrant lock so the worker can call internal helpers that also
# acquire it. The Condition gives the worker an efficient idle wait
# (woken by enqueue / resume / cancel notifications).
_cond = threading.Condition(threading.RLock())

# Worker handle. ``configure`` is idempotent — repeated calls leave the
# existing live thread in place.
_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()

# Paused state — when True the worker doesn't pull the next pending,
# but lets the in-flight item finish naturally.
_paused: bool = False

# Set at configure() time; the worker needs it to pass into
# compose.ingest_ship as the `config` kwarg.
_config: PipelineConfig | None = None

# Persistence path. Set at configure() time so tests can hand a tmp dir.
_persistence_path: Path | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_queue_id() -> str:
    """Random base36 id, similar shape to ``jobs._new_job_id`` but
    prefixed with ``q-`` so the source is obvious in logs."""
    ts = _now_ms()
    n = ts
    digits = string.digits + string.ascii_lowercase
    out: list[str] = []
    if n == 0:
        out.append("0")
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"q-{''.join(reversed(out))}-{tail}"


def _next_pending() -> QueueItem | None:
    """Return the first ``pending`` item, or ``None``. Caller holds lock."""
    for it in _items:
        if it.status == "pending":
            return it
    return None


def _current_running() -> QueueItem | None:
    """Return the queue-side ``running`` item, or ``None``."""
    for it in _items:
        if it.status == "running":
            return it
    return None


def _persist() -> None:
    """Write the current queue to disk atomically. Caller holds lock.

    Caps completed history at ``_COMPLETED_HISTORY_CAP`` so a long-lived
    workspace doesn't grow an unbounded JSON. We trim the OLDEST
    completed entries by enqueued_at — pending + running are always
    preserved.
    """
    if _persistence_path is None:
        return

    # Trim completed history if over cap.
    completed = [it for it in _items if it.status in ("done", "failed", "cancelled")]
    if len(completed) > _COMPLETED_HISTORY_CAP:
        # Sort completed by finished_at (or enqueued_at as fallback);
        # keep the most recent N. Use IDs since list ordering is
        # interspersed with pending/running.
        completed.sort(
            key=lambda it: (it.finished_at or it.enqueued_at),
            reverse=True,
        )
        keep_ids = {it.queue_id for it in completed[:_COMPLETED_HISTORY_CAP]}
        _items[:] = [
            it for it in _items
            if it.status in ("pending", "running") or it.queue_id in keep_ids
        ]

    payload = {
        "version": _SCHEMA_VERSION,
        "items":   [_item_to_dict(it) for it in _items],
    }

    _persistence_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".queue-", suffix=".json", dir=str(_persistence_path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)
            fp.write("\n")
        os.replace(tmp, _persistence_path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load() -> None:
    """Read the persistence file into ``_items``. Caller holds lock.

    Items left in ``running`` are downgraded to ``failed`` because the
    underlying executor died with the previous process — no way to
    re-attach to that job. We keep ``pending`` items as-is so the
    worker picks them up.
    """
    global _items
    _items = []
    if _persistence_path is None or not _persistence_path.is_file():
        return
    try:
        with _persistence_path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("queue persistence parse failed: %s", e)
        return
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        logger.warning(
            "queue persistence schema mismatch "
            "(expected v%s, got %r) — starting empty",
            _SCHEMA_VERSION, payload.get("version") if isinstance(payload, dict) else None,
        )
        return
    raw_items = payload.get("items") or []
    for d in raw_items:
        if not isinstance(d, dict):
            continue
        it = _item_from_dict(d)
        if it is None:
            continue
        if it.status == "running":
            it.status = "failed"
            it.error = "backend restarted mid-run"
            it.finished_at = it.finished_at or _now_ms()
        _items.append(it)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _spawn_for_item(item: QueueItem) -> tuple[str, list[str]] | None:
    """Spawn the underlying ingest_ship job. Returns ``(job_id, cmd)``.

    Returns ``None`` when the label is locked by a competing direct
    "Run extract" call — caller should leave the item ``pending`` and
    retry after a short sleep. Other failures (config errors, bad
    snapshot) bubble out and get caught by the worker loop, which marks
    the item ``failed``.
    """
    kwargs: dict[str, Any] = {
        "ship_input":            item.toolkit_ship,
        "config":                _config,
        "forced_label":          item.label,
        "toolkit_ship_override": item.toolkit_ship,
        "gameparams_ship_id":    item.gameparams_ship_id,
        "interactive":           False,
        "build_library":         item.build_library,
    }
    if item.permoflage is not None:
        kwargs["variant_permoflage"] = item.permoflage

    cmd_display: list[str] = [
        "compose.ingest_ship",
        item.toolkit_ship,
        "--label", item.label,
        "--gameparams-ship-id", item.gameparams_ship_id,
    ]
    if item.permoflage is not None:
        cmd_display += ["--variant-permoflage", item.permoflage]
    if item.build_library:
        cmd_display.append("--build-library")

    try:
        job = spawn_job(
            kind="extract",
            label=item.label,
            target=compose.ingest_ship,
            kwargs=kwargs,
            cmd_display=cmd_display,
        )
    except JobLockedError:
        return None
    return job.id, list(job.cmd)


def _worker_loop() -> None:
    """Main worker loop. Daemon thread; ends only on process exit."""
    while not _worker_stop.is_set():
        # ── Phase 1: wait until there's something to do ──
        with _cond:
            while not _worker_stop.is_set():
                if not _paused:
                    item = _next_pending()
                    if item is not None:
                        break
                # Idle: sleep until enqueue / resume / cancel wakes us.
                _cond.wait(timeout=10.0)
            if _worker_stop.is_set():
                return
            # Try to spawn under the lock — spawn_job is fast (~µs) and
            # holding the lock keeps "I'm about to start this" atomic
            # against a concurrent remove/cancel.
            spawn = _spawn_for_item(item)
            if spawn is None:
                # Label busy — leave the item pending and back off so
                # we don't hot-spin. The cond wait wakes early on
                # state changes (cancel of the direct job, etc.).
                _cond.wait(timeout=_LABEL_BUSY_BACKOFF_S)
                continue
            job_id, _cmd = spawn
            item.status = "running"
            item.job_id = job_id
            item.started_at = _now_ms()
            _persist()
            # Wake watchers so the UI sees the transition immediately.
            _cond.notify_all()

        # ── Phase 2: wait for the underlying job to terminate ──
        # Released the lock so /api/queue/* routes stay responsive.
        while not _worker_stop.is_set():
            j = get_job(job_id)
            if j is None or j.state != "running":
                break
            time.sleep(_JOB_POLL_INTERVAL_S)

        # ── Phase 3: record terminal state ──
        with _cond:
            j = get_job(job_id)
            if j is None:
                # Should be rare — gc_jobs only drops completed jobs
                # past JOB_RETENTION_MS, and we just polled it as
                # running. Treat as failed.
                item.status = "failed"
                item.error = "underlying job vanished from runner"
            elif j.state == "done":
                item.status = "done"
            elif j.state == "cancelled":
                item.status = "cancelled"
                item.error = "cancelled"
            else:
                item.status = "failed"
                if isinstance(j.error, dict):
                    item.error = str(j.error.get("message") or "unknown")
                else:
                    item.error = "unknown"
            item.finished_at = _now_ms()
            _persist()
            _cond.notify_all()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure(config: PipelineConfig) -> None:
    """Wire persistence + load + start the worker. Idempotent.

    Called from :func:`server.main.create_app` so the queue is live
    whenever the FastAPI app is. Safe to call multiple times — if the
    worker thread is already alive, we just refresh the config in case
    the workspace changed.
    """
    global _config, _persistence_path, _worker_thread

    with _cond:
        _config = config
        new_path = (config.cache_dir or (config.workspace / ".cache")) / "extract_queue.json"
        # Re-load when the path changes (workspace switched in dev).
        if _persistence_path != new_path:
            _persistence_path = new_path
            _load()
            _cond.notify_all()
        elif _persistence_path is not None and not _items:
            # First configure() on this path — load the file.
            _load()
            _cond.notify_all()

        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_stop.clear()
            _worker_thread = threading.Thread(
                target=_worker_loop,
                daemon=True,
                name="extract-queue-worker",
            )
            _worker_thread.start()


def enqueue(
    *,
    vehicle:        str,
    label:          str,
    permoflage:     str | None,
    build_library:  bool,
    toolkit_ship:   str,
    gameparams_ship_id: str,
) -> QueueItem:
    """Append a new pending item to the queue. Returns the created item."""
    item = QueueItem(
        queue_id=          _new_queue_id(),
        vehicle=           vehicle,
        label=             label,
        permoflage=        permoflage,
        build_library=     build_library,
        toolkit_ship=      toolkit_ship,
        gameparams_ship_id=gameparams_ship_id,
        status=            "pending",
        job_id=            None,
        enqueued_at=       _now_ms(),
    )
    with _cond:
        _items.append(item)
        _persist()
        _cond.notify_all()
    return item


def remove(queue_id: str) -> QueueItem | None:
    """Drop a pending item, or cancel a running one. Returns the affected item.

    Returns ``None`` when the id is unknown. For ``done`` / ``failed`` /
    ``cancelled`` items, removes the row from the displayed list (the
    user already saw the outcome).
    """
    with _cond:
        target = None
        for i, it in enumerate(_items):
            if it.queue_id == queue_id:
                target = (i, it)
                break
        if target is None:
            return None
        i, item = target

        if item.status == "pending":
            _items.pop(i)
            _persist()
            _cond.notify_all()
            return item
        if item.status == "running":
            # Cancel the underlying job. The worker's poll loop will
            # observe the terminal state and finalise the item.
            if item.job_id:
                cancel_job(item.job_id)
            # Don't pop yet — let the worker write the final state.
            _cond.notify_all()
            return item
        # Terminal — just drop from the list.
        _items.pop(i)
        _persist()
        _cond.notify_all()
        return item


def reorder(order: list[str]) -> int:
    """Reorder the pending tail to match ``order``.

    ``order`` is a list of queue_ids; non-pending items in the list are
    ignored. Pending items not mentioned in ``order`` go to the end of
    the pending section, preserving their existing order. Returns the
    count of items actually reordered.
    """
    with _cond:
        pending_by_id = {it.queue_id: it for it in _items if it.status == "pending"}
        if not pending_by_id:
            return 0
        # Build the new pending order: requested ids first (filtered to
        # those that actually exist + are pending), then any remaining.
        new_pending: list[QueueItem] = []
        seen: set[str] = set()
        for qid in order:
            it = pending_by_id.get(qid)
            if it is None or qid in seen:
                continue
            new_pending.append(it)
            seen.add(qid)
        for it in pending_by_id.values():
            if it.queue_id not in seen:
                new_pending.append(it)

        # Rebuild _items: keep non-pending in original positions; replace
        # pending entries in their existing slots with the new order.
        non_pending = [it for it in _items if it.status != "pending"]
        # Convention: pending always at the head of the displayed list.
        # That matches the worker's "head of pending" semantics.
        _items[:] = new_pending + non_pending
        _persist()
        _cond.notify_all()
        return len(new_pending)


def clear_completed() -> int:
    """Drop every item with a terminal status. Returns the count dropped."""
    with _cond:
        before = len(_items)
        _items[:] = [it for it in _items if it.status in ("pending", "running")]
        dropped = before - len(_items)
        if dropped:
            _persist()
            _cond.notify_all()
        return dropped


def set_paused(paused: bool) -> bool:
    """Toggle the worker pause flag. Returns the new value."""
    global _paused
    with _cond:
        _paused = bool(paused)
        # Wake the worker so it sees the new flag (resume) or the next
        # idle wait picks up the pause and skips the dispatch.
        _cond.notify_all()
        return _paused


def list_items() -> list[QueueItem]:
    """Snapshot copy of the queue. Order matches the on-disk list."""
    with _cond:
        return list(_items)


def get_item(queue_id: str) -> QueueItem | None:
    with _cond:
        for it in _items:
            if it.queue_id == queue_id:
                return it
        return None


def snapshot() -> dict[str, Any]:
    """Composite payload for ``GET /api/queue``.

    Returns ``items`` (in queue order), ``paused``, and the running
    item's underlying ``job_id`` (or None) so the client can decide
    whether to poll the jobs endpoint for live log output.
    """
    with _cond:
        items = list(_items)
        running = next((it for it in items if it.status == "running"), None)
        return {
            "items":           [_item_to_dict(it) for it in items],
            "paused":          _paused,
            "running_job_id":  running.job_id if running else None,
            "pending_count":   sum(1 for it in items if it.status == "pending"),
            "completed_count": sum(
                1 for it in items if it.status in ("done", "failed", "cancelled")
            ),
        }


# Test hook — let unit tests drain everything before re-configuring.
def _reset_for_tests() -> None:
    """Stop the worker, clear state, drop the persistence pointer.

    NOT public API; tests only. Production code calls :func:`configure`.
    """
    global _items, _config, _persistence_path, _worker_thread, _paused
    _worker_stop.set()
    with _cond:
        _cond.notify_all()
    if _worker_thread is not None and _worker_thread.is_alive():
        _worker_thread.join(timeout=5.0)
    _worker_thread = None
    with _cond:
        _items = []
        _config = None
        _persistence_path = None
        _paused = False
    _worker_stop.clear()


__all__ = [
    "QueueItem",
    "QueueStatus",
    "configure",
    "enqueue",
    "remove",
    "reorder",
    "clear_completed",
    "set_paused",
    "list_items",
    "get_item",
    "snapshot",
]
