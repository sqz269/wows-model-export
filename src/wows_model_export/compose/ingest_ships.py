"""Compose `ingest_ships` -- ingest a group of new ships in one process.

The batch form of :func:`~.ingest_ship.ingest_ship`, for ships that are not
in the workspace yet (``wows-ingest-ship`` with several ships or a
``--ships-file``, and ``--jobs N``). It runs the ingests the way
``wows-builds reexport`` runs re-exports
(:func:`.build_tracking.reexport_exports`):

- with ``jobs`` > 1 the shared caches are brought to the current build
  first (:func:`.build_tracking.prewarm_caches`), so the workers find them
  current instead of each rebuilding the same file;
- each ingest runs on its own thread, in its own shared-texture export
  session (``ingest_ship`` opens it and claims its ship folder), and
  writes only its own ship folder;
- no ingest builds the accessory library. When asked, the library is built
  once after the group (:func:`.ingest_ship.build_library_and_refresh`),
  followed by the post-library sidecar refresh of every ship that was
  ingested, with the arguments a single ingest's own refresh uses;
- a failed ingest is reported and the others go on.

The ingests are threads of one process on purpose: the accessory-library
build and the toolkit, camo and particle caches lock with
:class:`threading.Lock`, which another process does not see. Run one
group at a time, never several such processes side by side.

Each ingest gets the arguments the single-ship command passes, with two
differences: it never prompts (an ambiguous ship name fails that ship),
and the library build moves to the end of the group. The sidecar's
``provenance.extract_args.build_library`` and the ``build_deps.json``
replay block still record the library intent, so a replay
(``wows-builds reexport``, clean and re-extract) builds it again. One
output differs on purpose: a single ingest with ``build_library`` runs the
library build inside its own build-input recording, so the ship's
``build_deps.json`` also lists the game files of the library assets that
build exported (which depends on what the library already held). Here
those files are recorded only in the library assets' own
``build_deps.json``, as in a re-export or an extract-queue drain. (Which
ship's file lists a camouflage texture several ships share depends on
which ingest extracted it first, in a group as across single ingests.)
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import PipelineConfig, bind_active
from ..errors import CancelledError, StepError
from ..types import OnEvent
from . import build_tracking as _build_tracking

#: Most ingests one group runs at once. Each one adds a toolkit export's CPU
#: load and its own working memory to the process (GameParams is parsed once
#: and shared): three small ships peaked near 17 GB, one alone near 13 GB.
MAX_INGEST_JOBS = 3


@dataclass(frozen=True)
class ShipIngestSpec:
    """One ship of a group: the per-ship arguments of :func:`ingest_ship`.

    ``ship_input`` is what the single-ship command takes as its ship
    argument (display name, model directory or GameParams Vehicle id);
    the other fields match its options (``label`` = ``--label``,
    ``toolkit_ship`` = ``--toolkit-ship``, ...)."""

    ship_input: str
    label: str | None = None
    toolkit_ship: str | None = None
    gameparams_ship_id: str | None = None
    hull: str | None = None
    variant_permoflage: str = "auto"
    export_exterior_hulls: bool = False
    class_override: str | None = None
    ship_key_suffix: str | None = None

    @property
    def folder_label(self) -> str:
        """The ship folder the ingest writes (``ships/<label>``): the
        label, else the toolkit name, else the ship input, as a
        non-interactive :func:`ingest_ship` picks it."""
        return self.label or self.toolkit_ship or self.ship_input


def check_specs(specs: Sequence[ShipIngestSpec]) -> None:
    """Raise :class:`ValueError` when two ships would write one folder.

    Folder names are compared case-insensitively (the workspace may sit on
    a case-insensitive file system)."""
    seen: dict[str, str] = {}
    for spec in specs:
        if not spec.ship_input:
            raise ValueError("a ship entry has no ship name")
        key = spec.folder_label.casefold()
        if key in seen:
            raise ValueError(
                f"{spec.ship_input!r} and {seen[key]!r} would both write "
                f"ships/{spec.folder_label}; give one of them its own label"
            )
        seen[key] = spec.ship_input


def ingest_ships(
    specs: Sequence[ShipIngestSpec],
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    jobs: int = 1,
    build_library: bool = False,
    rebuild_library: bool = False,
    on_event: OnEvent | None = None,
    on_start: Callable[[int, int, ShipIngestSpec], None] | None = None,
    on_done: Callable[[dict[str, Any]], None] | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Ingest every ship of ``specs``, up to ``jobs`` at a time.

    Parameters:
        specs
            The ships, one :class:`ShipIngestSpec` each. Two ships that
            would write the same folder are refused before anything runs.
        workspace, config
            As for :func:`ingest_ship`.
        jobs
            Ingests to run at once, 1 to :data:`MAX_INGEST_JOBS`. With 1
            the ships run one after another on the calling thread.
        build_library, rebuild_library
            Build the accessory library once after the group (additive,
            or a full regenerate with ``rebuild_library``), then refresh
            the sidecar of every ship that was ingested. Skipped when no
            ship was ingested or the group was cancelled.
        on_event
            Step events of every ingest and of the library build. With
            ``jobs`` > 1 each ingest's events carry its folder label in
            ``detail`` and ``data["export"]`` (they interleave).
        on_start
            ``on_start(index, total, spec)`` as each ingest starts.
        on_done
            Called with each ship's report entry as its ingest ends.
        cancel
            Set it to stop the group: running ingests stop at their next
            step boundary, the ones not started are skipped, and the
            library is not built. Ctrl+C (``KeyboardInterrupt``) while
            waiting does the same.

    Returns a report::

        {"jobs": 3, "cancelled": False,
         "ships": [{"index", "ship", "label", "ok", "error", "step",
                    "warnings", "step_timings_ms", "wall_s"}, ...],
         "library": None | {"rebuild", "ships", "ok", "error",
                            "warnings", "wall_s"} | {"skipped": reason},
         "wall_s": {"ingests", "library", "total"}}

    ``ships`` is in input order. A ship that failed has ``ok`` False and
    the error (and, for a step failure, the step) that stopped it.
    """
    from .ingest_ship import build_library_and_refresh, ingest_ship

    specs = list(specs)
    if not specs:
        raise ValueError("no ships to ingest")
    jobs = int(jobs or 1)
    if not 1 <= jobs <= MAX_INGEST_JOBS:
        raise ValueError(f"jobs must be 1 to {MAX_INGEST_JOBS}, not {jobs}")
    check_specs(specs)

    cfg = config or PipelineConfig.load()
    ws = Path(workspace) if workspace is not None else cfg.workspace
    total = len(specs)
    jobs = min(jobs, total)
    wants_library = bool(build_library or rebuild_library)
    cancel = cancel or threading.Event()

    t_start = time.perf_counter()
    entries: list[dict[str, Any] | None] = [None] * total
    lock = threading.Lock()
    started = 0

    def finish(index: int, entry: dict[str, Any]) -> None:
        with lock:
            entries[index] = entry
        if on_done is not None:
            on_done(entry)

    def run_one(index: int, spec: ShipIngestSpec) -> None:
        nonlocal started
        base = {"index": index, "ship": spec.ship_input, "label": spec.folder_label}
        if cancel.is_set():
            finish(index, {**base, "ok": False, "error": "not started: the group was cancelled"})
            return
        with lock:
            order = started
            started += 1
        if on_start is not None:
            on_start(order, total, spec)
        emit = (
            _build_tracking._labelled(on_event, spec.folder_label)
            if jobs > 1 and on_event is not None else on_event
        )
        t0 = time.perf_counter()
        try:
            result = ingest_ship(
                spec.ship_input,
                workspace=ws,
                config=cfg,
                forced_label=spec.label,
                interactive=False,
                class_override=spec.class_override,
                ship_key_suffix=spec.ship_key_suffix,
                # Built once after the group; the intent is still recorded.
                build_library=False,
                rebuild_library=False,
                provenance_build_library=wants_library,
                variant_permoflage=spec.variant_permoflage,
                export_exterior_hulls=spec.export_exterior_hulls,
                toolkit_ship_override=spec.toolkit_ship,
                gameparams_ship_id=spec.gameparams_ship_id,
                hull=spec.hull,
                on_event=emit,
                cancel=cancel,
            )
        except CancelledError as err:
            cancel.set()
            finish(index, {
                **base, "ok": False, "step": err.step, "error": "cancelled",
                "wall_s": round(time.perf_counter() - t0, 1),
            })
            return
        except StepError as err:
            finish(index, {
                **base, "ok": False, "step": err.step,
                "error": str(err.detail or err),
                "wall_s": round(time.perf_counter() - t0, 1),
            })
            return
        except Exception as err:  # noqa: BLE001 - one ship never stops the group
            finish(index, {
                **base, "ok": False, "error": f"{type(err).__name__}: {err}",
                "wall_s": round(time.perf_counter() - t0, 1),
            })
            return
        finish(index, {
            **base,
            "label": result.label,
            "toolkit_ship": result.ship_id,
            "ok": True,
            "sidecar": (
                str(result.scaffold.sidecar_path)
                if result.scaffold and result.scaffold.sidecar_path else None
            ),
            "warnings": list(result.warnings),
            "step_timings_ms": dict(result.step_timings_ms),
            "wall_s": round(time.perf_counter() - t0, 1),
        })

    if jobs > 1:
        _build_tracking.prewarm_caches(cfg, ships=True)
    t_ingests = time.perf_counter()
    try:
        if jobs == 1:
            for i, spec in enumerate(specs):
                run_one(i, spec)
        else:
            with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="ingest") as pool:
                pending = {pool.submit(bind_active(run_one), i, spec)
                           for i, spec in enumerate(specs)}
                try:
                    while pending:
                        # A timeout keeps the wait interruptible (Ctrl+C).
                        _done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    cancel.set()
                    wait(pending)
    except KeyboardInterrupt:
        cancel.set()
    ingests_s = time.perf_counter() - t_ingests

    ships = [
        e if e is not None else {
            "index": i, "ship": specs[i].ship_input, "label": specs[i].folder_label,
            "ok": False, "error": "cancelled before it finished",
        }
        for i, e in enumerate(entries)
    ]
    ingested = [e for e in ships if e["ok"]]
    by_index = {e["index"]: specs[e["index"]] for e in ingested}

    library: dict[str, Any] | None = None
    library_s = 0.0
    if wants_library:
        if cancel.is_set():
            library = {"skipped": "the group was cancelled"}
        elif not ingested:
            library = {"skipped": "no ship was ingested"}
        else:
            # The refresh a single ingest runs after its own library build
            # (ingest_ship's _refresh_sidecar) passes the same arguments.
            refresh_specs = [
                {
                    "label": e["label"],
                    "toolkit_ship": e["toolkit_ship"],
                    "gameparams_ship_id": by_index[e["index"]].gameparams_ship_id,
                    "permoflage": by_index[e["index"]].variant_permoflage,
                    "class_override": by_index[e["index"]].class_override,
                    "ship_key_suffix": by_index[e["index"]].ship_key_suffix,
                    "export_exterior_hulls": by_index[e["index"]].export_exterior_hulls,
                }
                for e in ingested
            ]
            library = {"rebuild": bool(rebuild_library), "ships": len(refresh_specs)}
            t_lib = time.perf_counter()
            try:
                drained = build_library_and_refresh(
                    workspace=ws,
                    config=cfg,
                    refresh_specs=refresh_specs,
                    rebuild_library=rebuild_library,
                    on_event=on_event,
                    cancel=cancel,
                )
                library.update(ok=True, warnings=list(drained.warnings))
            except KeyboardInterrupt:
                cancel.set()
                library.update(ok=False, error="cancelled")
            except CancelledError:
                library.update(ok=False, error="cancelled")
            except Exception as err:  # noqa: BLE001
                detail = err.detail if isinstance(err, StepError) and err.detail else err
                library.update(ok=False, error=f"{type(err).__name__}: {detail}")
            library_s = time.perf_counter() - t_lib
            library["wall_s"] = round(library_s, 1)

    return {
        "jobs": jobs,
        "cancelled": cancel.is_set(),
        "ships": ships,
        "library": library,
        "wall_s": {
            "ingests": round(ingests_s, 1),
            "library": round(library_s, 1),
            "total": round(time.perf_counter() - t_start, 1),
        },
    }


__all__ = ["MAX_INGEST_JOBS", "ShipIngestSpec", "check_specs", "ingest_ships"]
