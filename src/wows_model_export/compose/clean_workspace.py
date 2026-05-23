"""Workspace-level cleanup + re-extract composer.

Counterpart to per-ship :func:`compose.teardown_ship.teardown_ship`. Two
operations:

* :func:`clean_workspace`        - tear down every extracted ship under
                                   ``<workspace>/ships/``, optionally wipe
                                   ``<workspace>/libraries/accessories/``
                                   too. No re-extract.

* :func:`clean_and_reextract`    - same cleanup, then replay each ship's
                                   ingest (and optionally each ship's skin
                                   packs) using args captured from the
                                   pre-clean sidecars.

The scan-then-clean order is load-bearing: every sidecar lives INSIDE the
per-ship directory that the teardown step deletes, so we have to snapshot
the replay plans into memory before tearing anything down.

Replay-arg sourcing (per ship):

* ``provenance.extract_args``    - written by :func:`compose.ingest_ship`
                                   at the end of each extract. Carries the
                                   exact ``(vehicle, label, permoflage,
                                   build_library)`` tuple the user invoked.

* Fallback                       - older sidecars (or those whose
                                   ``stamp_provenance`` step warned) lack
                                   the block. We synthesize a plan from
                                   ``ship.wg_ship_full_id`` +
                                   ``permoflage="auto"`` + ``build_library=True``,
                                   which matches the webview's default
                                   extract button.

Skin replay sources are recovered by parsing ``skins[].source``:

* ``"loose:<dir>"``              - loose-mod folder; replay via
                                   ``source_kind="loose_mod"`` +
                                   ``skin_source=<dir>``.

* ``"vfs:<asset_id> via <ext>"`` - VFS variant; replay via
                                   ``source_kind="vfs_variant"`` +
                                   ``skin_source=<asset_id>`` (the
                                   composer's ``_resolve_wg_source``
                                   re-derives the exterior).

Skins without one of those prefixes (auto-generated default + scaffold-
discovered mat_albedo schemes) are skipped during replay - the re-ingest
will recreate them.

Per-ship errors during teardown / re-ingest / replay are collected, not
propagated: one stale Vehicle id shouldn't abort the whole batch. The
return dict carries ``errors: [{label, phase, message}, ...]`` so callers
can surface a "3 of 12 ships failed" summary.

Canonical :class:`StepEvent` step names (top-level; the inner composers
emit their own sub-step events through the parent's ``on_event``):

    "scan_extracted_ships"
    "wipe_library_accessories"     (skipped when ``prune_library=False``)
    "teardown_ships"
    "reextract_ships"              (clean_and_reextract only)
    "replay_skin_packs"            (clean_and_reextract only, gated on replay_skins)
"""
from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import PipelineConfig
from ..errors import CancelledError, StepError
from ..resolve import sidecar as _sidecar
from ..types import OnEvent
from . import ingest_ship as _ingest_ship_mod
from . import skin_pack as _skin_pack_mod
from . import teardown_ship as _teardown_ship_mod
from ._step_runner import StepRunner

# Sidecar fields probed for replay-arg fallback. Module-level so the
# scan path is the single source of truth on which fields matter.
_SIDECAR_FALLBACK_VEHICLE_FIELD = "wg_ship_full_id"

# Skin source-label grammar (matches the strings written by
# compose.skin_pack.ingest_skin_pack).
_LOOSE_SOURCE_RE = re.compile(r"^loose:(?P<path>.+)$")
_VFS_SOURCE_RE = re.compile(
    r"^vfs:(?P<asset_id>\S+)\s+via\s+(?P<exterior_id>\S+)$"
)


# ---------------------------------------------------------------------------
# Replay plan dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkinReplayPlan:
    """One row of replay args for :func:`compose.skin_pack.ingest_skin_pack`."""

    skin_id:       str
    source_kind:   Literal["loose_mod", "vfs_variant"]
    skin_source:   str
    display_name:  str | None = None


@dataclass(frozen=True)
class ShipReplayPlan:
    """One row of replay args for :func:`compose.ingest_ship.ingest_ship`.

    ``permoflage`` follows the ingest_ship contract: ``"auto"`` /
    ``"none"`` / an explicit exterior_id, or ``None`` (which the composer
    treats as ``"auto"`` for legacy callers).

    ``provenance_source`` is ``"stamped"`` when the plan came from a
    ``provenance.extract_args`` block, or ``"fallback"`` when we
    synthesised it from ``ship.wg_ship_full_id``. Reported in the result
    so the caller can warn the user "N ships replayed with fallback args".
    """

    label:              str
    vehicle:            str
    permoflage:         str | None
    build_library:      bool
    skins:              tuple[SkinReplayPlan, ...]
    sidecar_path:       Path
    provenance_source:  Literal["stamped", "fallback"]


# ---------------------------------------------------------------------------
# Scan helpers (pure - no mutation)
# ---------------------------------------------------------------------------


def _parse_skin_source(source: str | None) -> tuple[
    Literal["loose_mod", "vfs_variant"], str
] | None:
    """Return ``(source_kind, skin_source)`` or ``None`` when the label
    can't be replayed.

    A returned ``skin_source`` carries the value to pass into
    :func:`compose.skin_pack.ingest_skin_pack` as its first positional:

      - ``loose_mod`` -> filesystem path of the loose-mod folder.
      - ``vfs_variant`` -> the variant asset_id; ``ingest_skin_pack``
        re-derives the exterior_id internally via the WG resolver, so we
        don't have to thread it through.

    ``None`` covers the auto-generated default skin (no ``source`` field)
    and the scaffold-discovered mat_albedo schemes - those are recreated
    by the re-ingest, not via a skin_pack replay.
    """
    if not isinstance(source, str) or not source:
        return None
    m = _LOOSE_SOURCE_RE.match(source)
    if m:
        return ("loose_mod", m.group("path"))
    m = _VFS_SOURCE_RE.match(source)
    if m:
        return ("vfs_variant", m.group("asset_id"))
    return None


def _skin_plans_from_sidecar(doc: dict[str, Any]) -> tuple[SkinReplayPlan, ...]:
    """Extract replay plans for every skin pack that named a source label.

    Order-preserving: replay runs in sidecar-declared order, which is
    insertion order from the original ingests.
    """
    out: list[SkinReplayPlan] = []
    for s in doc.get("skins") or ():
        if not isinstance(s, dict):
            continue
        parsed = _parse_skin_source(s.get("source"))
        if parsed is None:
            continue
        kind, skin_source = parsed
        skin_id = s.get("skin_id")
        if not isinstance(skin_id, str) or not skin_id:
            continue
        display = s.get("display_name")
        out.append(SkinReplayPlan(
            skin_id=skin_id,
            source_kind=kind,
            skin_source=skin_source,
            display_name=display if isinstance(display, str) else None,
        ))
    return tuple(out)


def _plan_from_sidecar(sidecar_path: Path) -> ShipReplayPlan | None:
    """Build a replay plan from one ship's sidecar.

    Returns ``None`` when neither the stamped provenance block nor the
    fallback ``ship.wg_ship_full_id`` is present - we can't replay a ship
    we don't know how to invoke.
    """
    try:
        doc = _sidecar.read(sidecar_path)
    except Exception:
        return None

    prov = (doc.get("provenance") or {}).get("extract_args") or {}
    label = sidecar_path.parent.name  # ships/<label>/<label>.meta.json

    if isinstance(prov, dict) and prov.get("vehicle"):
        plan = ShipReplayPlan(
            label=str(prov.get("label") or label),
            vehicle=str(prov["vehicle"]),
            permoflage=(
                str(prov["permoflage"])
                if isinstance(prov.get("permoflage"), str) else None
            ),
            build_library=bool(prov.get("build_library", True)),
            skins=_skin_plans_from_sidecar(doc),
            sidecar_path=sidecar_path,
            provenance_source="stamped",
        )
        return plan

    # Fallback: synthesise from the ship section. Matches the webview's
    # default extract invocation (auto permoflage + build_library=True).
    ship = doc.get("ship") or {}
    vehicle = ship.get(_SIDECAR_FALLBACK_VEHICLE_FIELD) or ship.get("wg_ship_id")
    if not isinstance(vehicle, str) or not vehicle:
        return None
    return ShipReplayPlan(
        label=label,
        vehicle=vehicle,
        permoflage="auto",
        build_library=True,
        skins=_skin_plans_from_sidecar(doc),
        sidecar_path=sidecar_path,
        provenance_source="fallback",
    )


def scan_extracted_ships(workspace: Path) -> list[ShipReplayPlan]:
    """Walk ``<workspace>/ships/`` and yield a replay plan per ship.

    Order is lexicographic on the label so the replay runs deterministic
    and the event stream is easy to follow. Ships whose sidecar is
    unparseable or whose vehicle id is unrecoverable are silently
    dropped - the result dict tracks ``ships_unrecoverable`` for those.
    """
    ships_root = workspace / "ships"
    if not ships_root.is_dir():
        return []
    plans: list[ShipReplayPlan] = []
    for entry in sorted(ships_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sidecar_path = entry / f"{entry.name}.meta.json"
        if not sidecar_path.is_file():
            continue
        plan = _plan_from_sidecar(sidecar_path)
        if plan is not None:
            plans.append(plan)
    return plans


# ---------------------------------------------------------------------------
# clean_workspace - teardown only
# ---------------------------------------------------------------------------


def clean_workspace(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    prune_library: bool = True,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Tear down every extracted ship; optionally wipe the accessory library.

    Parameters:
        workspace      Workspace root; defaults to ``config.workspace``.
        config         Pre-resolved :class:`PipelineConfig`; loaded on demand.
        prune_library  When ``True`` (default), also rmtree
                       ``<workspace>/libraries/accessories/`` before per-ship
                       teardown. Per-ship teardown then short-circuits the
                       library-index cleanup step (no index to prune).
        on_event       Optional :class:`StepEvent` callback. Top-level steps:
                       ``scan_extracted_ships``,
                       ``wipe_library_accessories`` (skipped when
                       ``prune_library=False``), ``teardown_ships``. Each
                       per-ship teardown also emits its own substream of
                       events through the same callback.
        cancel         Cooperative cancel; honored at step boundaries.

    Returns a dict with: ``ship_count`` (input), ``ships_torn_down`` (list
    of labels actually rmtree'd), ``ships_unrecoverable`` (sidecars we
    couldn't parse), ``library_existed``, ``library_wiped``,
    ``errors`` ([{label, phase, message}, ...]).
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)

    runner = StepRunner(on_event, cancel=cancel)
    report: dict[str, Any] = {
        "ship_count":            0,
        "ships_torn_down":       [],
        "ships_unrecoverable":   [],
        "library_existed":       False,
        "library_wiped":         False,
        "errors":                [],
        "step_timings_ms":       {},
    }

    # ── Step: scan_extracted_ships ────────────────────────────────────
    with runner.step("scan_extracted_ships", detail=str(workspace)) as ctx:
        plans = scan_extracted_ships(workspace)
        # Anything in ships/ that scan dropped (parse failure / no
        # vehicle id) becomes ships_unrecoverable - we still rmtree the
        # dir, just can't tell the caller what was in it for a hypothetical
        # re-extract.
        ships_root = workspace / "ships"
        on_disk: list[str] = []
        if ships_root.is_dir():
            on_disk = sorted(
                p.name for p in ships_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        planned = {p.label for p in plans}
        report["ships_unrecoverable"] = [n for n in on_disk if n not in planned]
        report["ship_count"] = len(on_disk)
        ctx.annotate(
            f"{len(plans)} replay-able / {len(on_disk)} on disk",
            data={
                "planned": len(plans),
                "on_disk": len(on_disk),
                "unrecoverable": len(report["ships_unrecoverable"]),
            },
        )

    # ── Step: wipe_library_accessories ────────────────────────────────
    library_dir = workspace / "libraries" / "accessories"
    if not prune_library:
        runner.emit(
            "wipe_library_accessories", "skipped",
            detail="prune_library=False",
        )
    elif not library_dir.is_dir():
        report["library_existed"] = False
        runner.emit(
            "wipe_library_accessories", "skipped",
            detail="library dir not present",
        )
    else:
        report["library_existed"] = True
        with runner.step(
            "wipe_library_accessories", detail=str(library_dir),
        ) as ctx:
            try:
                shutil.rmtree(library_dir)
                report["library_wiped"] = True
                ctx.annotate("rmtree'd library/accessories")
            except OSError as e:
                report["errors"].append({
                    "label":   "<library>",
                    "phase":   "wipe_library_accessories",
                    "message": f"{type(e).__name__}: {e}",
                })
                # Re-raise so the step records as failed; the outer
                # caller propagates upward.
                raise StepError(
                    step="wipe_library_accessories",
                    underlying=e,
                    detail=f"failed to remove {library_dir}: {e}",
                ) from e

    # ── Step: teardown_ships ──────────────────────────────────────────
    # Tear down BOTH the planned ships (replay-able) and the
    # unrecoverable ones - the user asked for a clean workspace, not just
    # a clean replay set. We iterate from the on-disk listing rather than
    # `plans` so unrecoverable ship dirs still get rmtree'd.
    if not on_disk:
        runner.emit(
            "teardown_ships", "skipped",
            detail="no ship directories under <workspace>/ships",
        )
    else:
        with runner.step(
            "teardown_ships", detail=f"{len(on_disk)} ship(s)",
        ) as ctx:
            torn: list[str] = []
            for ship_label in on_disk:
                try:
                    _teardown_ship_mod.teardown_ship(
                        ship_label,
                        workspace=workspace,
                        config=cfg,
                        dry_run=False,
                        prune_orphans=False,  # library already nuked if requested
                        on_event=on_event,
                        cancel=cancel,
                    )
                    torn.append(ship_label)
                except CancelledError:
                    raise
                except Exception as e:
                    # Collect per-ship failures; keep going so one bad
                    # ship doesn't trap the rest.
                    report["errors"].append({
                        "label":   ship_label,
                        "phase":   "teardown",
                        "message": f"{type(e).__name__}: {e}",
                    })
            report["ships_torn_down"] = torn
            ctx.annotate(
                f"{len(torn)} torn down, {len(on_disk) - len(torn)} failed",
                data={
                    "torn_down": len(torn),
                    "failed":    len(on_disk) - len(torn),
                },
            )

    report["step_timings_ms"] = dict(runner.step_timings_ms)
    return report


# ---------------------------------------------------------------------------
# clean_and_reextract - teardown + replay
# ---------------------------------------------------------------------------


def clean_and_reextract(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    prune_library: bool = True,
    replay_skins: bool = True,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Clean the workspace, then replay every extracted ship.

    The scan + cleanup phases match :func:`clean_workspace`. The
    re-extract phase iterates the captured :class:`ShipReplayPlan`s and
    calls :func:`compose.ingest_ship` per ship. When ``replay_skins`` is
    true, each replayable skin (one whose ``source`` parses cleanly) is
    re-ingested via :func:`compose.skin_pack.ingest_skin_pack` after its
    parent ship's extract finishes.

    Parameters:
        workspace       Workspace root; defaults to ``config.workspace``.
        config          Pre-resolved :class:`PipelineConfig`.
        prune_library   See :func:`clean_workspace`.
        replay_skins    When ``True`` (default), re-ingest each ship's
                        skin packs after the base extract. Set ``False``
                        for a faster "just the base ships" pass.
        on_event        Optional :class:`StepEvent` callback. Inner
                        composers emit through the same callback, so a
                        single subscriber sees the full event stream.
        cancel          Cooperative cancel; honored at step boundaries.

    Returns the :func:`clean_workspace` dict, plus:

      - ``plans``                   list of ``{label, vehicle, permoflage,
                                    build_library, skin_count,
                                    provenance_source}`` (one per ship the
                                    scan recovered).
      - ``ships_reextracted``       labels for which the re-ingest succeeded.
      - ``skins_replayed``          ``[{label, skin_id, source_kind}, ...]``.
      - ``errors``                  per-ship failures across teardown,
                                    re-ingest, and skin replay (one entry
                                    per failure; phase identifies which).
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)

    runner = StepRunner(on_event, cancel=cancel)
    report: dict[str, Any] = {
        "ship_count":           0,
        "ships_torn_down":      [],
        "ships_unrecoverable":  [],
        "library_existed":      False,
        "library_wiped":        False,
        "plans":                [],
        "ships_reextracted":    [],
        "skins_replayed":       [],
        "errors":               [],
        "step_timings_ms":      {},
    }

    # ── Phase 1: scan ─────────────────────────────────────────────────
    with runner.step("scan_extracted_ships", detail=str(workspace)) as ctx:
        plans = scan_extracted_ships(workspace)
        ships_root = workspace / "ships"
        on_disk: list[str] = []
        if ships_root.is_dir():
            on_disk = sorted(
                p.name for p in ships_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        planned = {p.label for p in plans}
        report["ships_unrecoverable"] = [n for n in on_disk if n not in planned]
        report["ship_count"] = len(on_disk)
        report["plans"] = [
            {
                "label":              p.label,
                "vehicle":            p.vehicle,
                "permoflage":         p.permoflage,
                "build_library":      p.build_library,
                "skin_count":         len(p.skins),
                "provenance_source":  p.provenance_source,
            }
            for p in plans
        ]
        ctx.annotate(
            f"{len(plans)} ship(s) ready to replay; "
            f"{len(report['ships_unrecoverable'])} unrecoverable",
            data={
                "planned":       len(plans),
                "unrecoverable": len(report["ships_unrecoverable"]),
            },
        )

    # ── Phase 2: wipe library ────────────────────────────────────────
    library_dir = workspace / "libraries" / "accessories"
    if not prune_library:
        runner.emit(
            "wipe_library_accessories", "skipped",
            detail="prune_library=False",
        )
    elif not library_dir.is_dir():
        runner.emit(
            "wipe_library_accessories", "skipped",
            detail="library dir not present",
        )
    else:
        report["library_existed"] = True
        with runner.step(
            "wipe_library_accessories", detail=str(library_dir),
        ) as ctx:
            try:
                shutil.rmtree(library_dir)
                report["library_wiped"] = True
                ctx.annotate("rmtree'd library/accessories")
            except OSError as e:
                report["errors"].append({
                    "label":   "<library>",
                    "phase":   "wipe_library_accessories",
                    "message": f"{type(e).__name__}: {e}",
                })
                raise StepError(
                    step="wipe_library_accessories",
                    underlying=e,
                    detail=f"failed to remove {library_dir}: {e}",
                ) from e

    # ── Phase 3: teardown ────────────────────────────────────────────
    if not on_disk:
        runner.emit(
            "teardown_ships", "skipped",
            detail="no ship directories under <workspace>/ships",
        )
    else:
        with runner.step(
            "teardown_ships", detail=f"{len(on_disk)} ship(s)",
        ) as ctx:
            torn: list[str] = []
            for ship_label in on_disk:
                try:
                    _teardown_ship_mod.teardown_ship(
                        ship_label,
                        workspace=workspace,
                        config=cfg,
                        dry_run=False,
                        prune_orphans=False,
                        on_event=on_event,
                        cancel=cancel,
                    )
                    torn.append(ship_label)
                except CancelledError:
                    raise
                except Exception as e:
                    report["errors"].append({
                        "label":   ship_label,
                        "phase":   "teardown",
                        "message": f"{type(e).__name__}: {e}",
                    })
            report["ships_torn_down"] = torn
            ctx.annotate(
                f"{len(torn)} torn down, {len(on_disk) - len(torn)} failed",
                data={
                    "torn_down": len(torn),
                    "failed":    len(on_disk) - len(torn),
                },
            )

    # ── Phase 4: re-extract ──────────────────────────────────────────
    if not plans:
        runner.emit(
            "reextract_ships", "skipped",
            detail="no replay plans recovered",
        )
    else:
        with runner.step(
            "reextract_ships", detail=f"{len(plans)} ship(s)",
        ) as ctx:
            reextracted: list[str] = []
            for plan in plans:
                try:
                    _ingest_ship_mod.ingest_ship(
                        ship_input=plan.vehicle,
                        workspace=workspace,
                        config=cfg,
                        forced_label=plan.label,
                        gameparams_ship_id=plan.vehicle,
                        variant_permoflage=plan.permoflage or "auto",
                        build_library=plan.build_library,
                        interactive=False,
                        on_event=on_event,
                        cancel=cancel,
                    )
                    reextracted.append(plan.label)
                except CancelledError:
                    raise
                except Exception as e:
                    report["errors"].append({
                        "label":   plan.label,
                        "phase":   "reextract",
                        "message": f"{type(e).__name__}: {e}",
                    })
            report["ships_reextracted"] = reextracted
            ctx.annotate(
                f"{len(reextracted)}/{len(plans)} re-extracted",
                data={
                    "succeeded": len(reextracted),
                    "failed":    len(plans) - len(reextracted),
                },
            )

    # ── Phase 5: replay skin packs ───────────────────────────────────
    if not replay_skins:
        runner.emit(
            "replay_skin_packs", "skipped",
            detail="replay_skins=False",
        )
    else:
        skin_targets = [
            (p, s)
            for p in plans
            if p.label in report["ships_reextracted"]
            for s in p.skins
        ]
        if not skin_targets:
            runner.emit(
                "replay_skin_packs", "skipped",
                detail="no replay-able skins on the recovered ships",
            )
        else:
            with runner.step(
                "replay_skin_packs", detail=f"{len(skin_targets)} skin(s)",
            ) as ctx:
                replayed: list[dict[str, str]] = []
                for plan, skin in skin_targets:
                    try:
                        _skin_pack_mod.ingest_skin_pack(
                            skin_source=skin.skin_source,
                            ship_id=plan.label,
                            workspace=workspace,
                            config=cfg,
                            skin_id=skin.skin_id,
                            display_name=skin.display_name,
                            source_kind=skin.source_kind,
                            on_event=on_event,
                            cancel=cancel,
                        )
                        replayed.append({
                            "label":       plan.label,
                            "skin_id":     skin.skin_id,
                            "source_kind": skin.source_kind,
                        })
                    except CancelledError:
                        raise
                    except Exception as e:
                        report["errors"].append({
                            "label":   f"{plan.label}::{skin.skin_id}",
                            "phase":   "replay_skin",
                            "message": f"{type(e).__name__}: {e}",
                        })
                report["skins_replayed"] = replayed
                ctx.annotate(
                    f"{len(replayed)}/{len(skin_targets)} skins replayed",
                    data={
                        "succeeded": len(replayed),
                        "failed":    len(skin_targets) - len(replayed),
                    },
                )

    report["step_timings_ms"] = dict(runner.step_timings_ms)
    return report


__all__ = [
    "SkinReplayPlan",
    "ShipReplayPlan",
    "scan_extracted_ships",
    "clean_workspace",
    "clean_and_reextract",
]
