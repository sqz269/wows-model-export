"""Shared step-event helper for `compose/*` orchestrators.

Replaces the ad-hoc `_StepRunner` / `_StepCtx` / `_StepTimer` helpers
that each composer reinvented during the migration. Single source of
truth for: wall-time measurement, `on_event` emission, automatic
`StepError` wrapping on context-manager exit.

Two usage patterns:

1. **Context-manager** (preferred for try-narrow scope)::

        runner = StepRunner(on_event)
        with runner.step("export_hull", detail="Montana") as ctx:
            result = toolkit.export_ship("Montana", ...)
            ctx.annotate(f"wrote {result.output_paths[0].name}")
        # auto-emits started on entry, completed (with step_ms) on
        # success, failed + raises StepError(step="export_hull",
        # underlying=exc) on exception.

2. **Procedural** (when control flow doesn't fit a `with` block —
   e.g. interleaved branches, early-out)::

        runner.start("emit_sidecar")
        if dry_run:
            runner.skip("emit_sidecar", detail="dry-run")
        else:
            try:
                sidecar_write(doc, path)
                runner.complete(detail=f"wrote {path.name}")
            except Exception as e:
                runner.fail("emit_sidecar", detail=str(e))
                raise StepError(step="emit_sidecar", underlying=e) from e

Both fill `runner.step_timings_ms` (a `dict[str, float]`) ready to
pass into the composer's result dataclass via
`step_timings_ms=dict(runner.step_timings_ms)`.

A no-op when `on_event is None` (zero overhead — composer callers that
don't care about progress pay nothing per step boundary).

Listener safety: callbacks that raise are swallowed at emission time;
a buggy listener can't kill a long-running ingest.
"""

from __future__ import annotations

import threading
import time
from contextlib import AbstractContextManager
from types import TracebackType

from ..errors import CancelledError, StepError
from ..types import OnEvent, StepEvent, StepState


class StepRunner:
    """Per-composer event + timing tracker.

    Construct once at composer entry, fill in `step_timings_ms` as
    steps complete, then `dict(runner.step_timings_ms)` into the
    result dataclass.

    Cancellation: pass a ``cancel: threading.Event`` and the runner
    will check it at every step boundary (``start`` / ``complete`` /
    ``progress`` / context-manager entry). When set, a
    :class:`CancelledError` is raised carrying the step about to run
    (or just completed) — composers' existing ``except StepError``
    clauses then propagate the cancellation up the call chain without
    needing per-composer awareness.

    The check is at step granularity only: a long-running ``runner.step``
    block (e.g. a multi-minute toolkit subprocess) won't honor cancel
    until that step finishes. That's a deliberate trade-off — a
    sub-step polling loop would force every composer step to be
    re-entrant, which they aren't today.
    """

    def __init__(
        self,
        on_event: OnEvent | None,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self.on_event = on_event
        self.cancel = cancel
        self.step_timings_ms: dict[str, float] = {}
        self._t_run = time.perf_counter()
        self._active_step: str | None = None
        self._t_active: float | None = None

    # ── cancellation check ─────────────────────────────────────────────

    def _check_cancelled(self, step: str) -> None:
        """Raise :class:`CancelledError` when the cancel flag is set.

        Called at every step boundary — invisible no-op when no cancel
        event was passed (the common CLI / library case). The
        ``underlying`` slot carries a fresh ``KeyboardInterrupt`` as a
        sentinel; the surface contract is `isinstance(exc,
        CancelledError)`, not the wrapped type.
        """
        if self.cancel is not None and self.cancel.is_set():
            raise CancelledError(
                step=step,
                underlying=KeyboardInterrupt("cancelled by caller"),
                detail="cancelled at step boundary",
            )

    # ── elapsed-time accessor ──────────────────────────────────────────

    def _elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t_run) * 1000.0

    # ── primitive emit ─────────────────────────────────────────────────

    def emit(
        self,
        step: str,
        state: StepState,
        *,
        detail: str = "",
        step_ms: float | None = None,
        data: dict | None = None,
    ) -> None:
        """Emit a one-off ``StepEvent``.

        Catches and discards any exception the callback raises — a
        buggy listener shouldn't crash the composer.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(StepEvent(
                step=step,
                state=state,
                detail=detail,
                elapsed_ms=self._elapsed_ms(),
                step_ms=step_ms,
                data=data,
            ))
        except Exception:
            pass

    # ── procedural API ─────────────────────────────────────────────────

    def start(self, step: str, *, detail: str = "") -> None:
        """Mark a step as started (procedural use).

        Honors cancellation: raises :class:`CancelledError` when the
        cancel flag was set before this step ran. The check happens
        before the ``started`` event fires, so a cancelled job's event
        log doesn't include phantom "started" entries for steps that
        never executed.
        """
        # Check first so we don't emit a "started" event for a step
        # we're about to abort. The CancelledError propagates through
        # the composer's existing ``except StepError`` chain — no
        # per-composer cancel awareness required.
        self._check_cancelled(step)
        self._active_step = step
        self._t_active = time.perf_counter()
        self.emit(step, "started", detail=detail)

    def complete(
        self,
        *,
        detail: str = "",
        data: dict | None = None,
    ) -> None:
        """Mark the active step as completed; records ``step_ms``.

        Honors cancellation: raises :class:`CancelledError` if the
        cancel flag was set during the step. The completed event still
        fires (we record the timing) but the next step's ``start`` /
        the composer's return path sees the raise. We deliberately
        check *after* the emit so a step that finished its work right
        before cancel doesn't leave the composer's bookkeeping in a
        half-finished state.
        """
        if self._active_step is None or self._t_active is None:
            return
        step_ms = (time.perf_counter() - self._t_active) * 1000.0
        self.step_timings_ms[self._active_step] = step_ms
        self.emit(
            self._active_step, "completed",
            detail=detail,
            step_ms=step_ms,
            data=data,
        )
        completed_step = self._active_step
        self._active_step = None
        self._t_active = None
        self._check_cancelled(completed_step)

    def progress(
        self,
        step: str | None = None,
        *,
        detail: str = "",
        data: dict | None = None,
    ) -> None:
        """Emit a ``progress`` event for the active (or named) step.

        Long-running composer steps that want to publish intra-step
        notifications (a per-asset counter, a wowsunpack stdout line)
        call this. Honors cancellation at the same granularity as
        ``start``/``complete`` — call it from inside a ``runner.step``
        block to get cooperative cancel mid-step.
        """
        target = step or self._active_step or ""
        # Cancel check before the emit, same rationale as ``start``:
        # don't surface a progress line for work we're about to abort.
        self._check_cancelled(target)
        self.emit(target, "progress", detail=detail, data=data)

    def skip(self, step: str, *, detail: str = "") -> None:
        """Mark a step as skipped (no timing recorded).

        Doesn't require a prior ``start()``; used both as a standalone
        marker (`runner.skip("publish", detail="and_publish=False")`)
        and to abort an in-flight step (resets active state).
        """
        if self._active_step == step:
            self._active_step = None
            self._t_active = None
        self.emit(step, "skipped", detail=detail)

    def fail(
        self,
        step: str,
        *,
        detail: str = "",
        data: dict | None = None,
    ) -> None:
        """Mark a step as failed. Caller is responsible for raising
        ``StepError`` separately — kept decoupled so callers can choose
        to swallow the failure into a warning where appropriate.
        """
        step_ms: float | None = None
        if self._active_step == step and self._t_active is not None:
            step_ms = (time.perf_counter() - self._t_active) * 1000.0
            self.step_timings_ms[step] = step_ms
            self._active_step = None
            self._t_active = None
        self.emit(step, "failed", detail=detail, step_ms=step_ms, data=data)

    # ── context-manager API ────────────────────────────────────────────

    def step(self, name: str, detail: str = "") -> StepContext:
        """Context manager: emits started/completed/failed automatically.

        On exception, emits ``failed`` and re-raises wrapped in
        :class:`StepError` (unless the exception is already a
        ``StepError`` — those propagate as-is to avoid double-wrap from
        nested composers).
        """
        return StepContext(self, name, detail)


class StepContext(AbstractContextManager["StepContext"]):
    """Context wrapper produced by :meth:`StepRunner.step`.

    Stored fields:

        step               canonical step name
        detail             entry-side human label (shown on `started`)
        completed_detail   override label for `completed` (set via annotate)
        completed_data     structured payload for `completed`
    """

    def __init__(self, runner: StepRunner, step: str, detail: str) -> None:
        self.runner = runner
        self.step = step
        self.detail = detail
        self.completed_detail = ""
        self.completed_data: dict | None = None
        self._t_start: float = 0.0

    def __enter__(self) -> StepContext:
        # Cancel check before we emit "started" — symmetric with
        # ``StepRunner.start``. A cancelled composer's event log
        # doesn't grow phantom started entries.
        self.runner._check_cancelled(self.step)
        self._t_start = time.perf_counter()
        self.runner.emit(self.step, "started", detail=self.detail)
        return self

    def annotate(self, detail: str, data: dict | None = None) -> None:
        """Override the completion detail / data before the step ends.

        Useful when the success detail isn't known until the work is
        done (e.g. "wrote N triangles", "rebuilt M assets").
        """
        self.completed_detail = detail
        if data is not None:
            self.completed_data = data

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        step_ms = (time.perf_counter() - self._t_start) * 1000.0
        self.runner.step_timings_ms[self.step] = step_ms

        if exc is None:
            self.runner.emit(
                self.step, "completed",
                detail=self.completed_detail or self.detail,
                step_ms=step_ms,
                data=self.completed_data,
            )
            return False

        # Step failed.
        self.runner.emit(
            self.step, "failed",
            detail=str(exc),
            step_ms=step_ms,
        )
        if isinstance(exc, StepError):
            # Don't double-wrap — let it propagate as-is (this is what
            # ingest_ship relies on when its sub-composer fails).
            return False
        raise StepError(
            step=self.step,
            underlying=exc,
            detail=str(exc),
        ) from exc


__all__ = ["StepRunner", "StepContext"]
