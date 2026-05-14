"""Shared event printers for cli/* entry points.

Two modes:

* ``make_text_printer(stderr=True, quiet=False)`` -- human-readable
  per-step output; the default. One line per :class:`StepEvent`,
  formatted like::

      [    1234ms] scaffold completed  (step=678ms)  detail

  Emits to stderr by default so stdout stays clean. ``quiet=True``
  returns a no-op printer (use for ``--quiet``).

* ``make_json_printer(stream=sys.stdout)`` -- one JSON object per line
  on stdout. Used with ``--json-events``. Lets the webview's Vite
  middleware (or any other supervisor) parse structured events from
  the subprocess.

Both return an :data:`~wows_model_export.types.OnEvent` callable
suitable for passing to a composer.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import IO

from ..types import OnEvent, StepEvent


def make_text_printer(*, stderr: bool = True, quiet: bool = False) -> OnEvent | None:
    """Return a human-readable per-event printer.

    Output goes to stderr by default to keep stdout free for actual
    structured output. Set ``stderr=False`` to print to stdout. When
    ``quiet=True``, returns ``None`` so the composer skips emission
    entirely.
    """
    if quiet:
        return None

    stream: IO[str] = sys.stderr if stderr else sys.stdout

    def _print(event: StepEvent) -> None:
        elapsed = f"[{event.elapsed_ms:>8.0f}ms]"
        step = event.step
        state = event.state
        parts = [elapsed, f"{step} {state}"]
        if event.step_ms is not None:
            parts.append(f"(Δ {event.step_ms:.0f}ms)")
        if event.detail:
            parts.append(event.detail)
        print("  ".join(parts), file=stream, flush=True)

    return _print


def make_json_printer(*, stream: IO[str] | None = None) -> OnEvent:
    """Return a JSON-per-line printer for ``--json-events``.

    One ``json.dumps(asdict(event))`` line per event on the given
    ``stream`` (default: stdout), with explicit flush so the consumer
    sees each line as it arrives.
    """
    target: IO[str] = stream if stream is not None else sys.stdout

    def _print(event: StepEvent) -> None:
        payload = asdict(event)
        target.write(json.dumps(payload, default=str))
        target.write("\n")
        target.flush()

    return _print


__all__ = ["make_text_printer", "make_json_printer"]
