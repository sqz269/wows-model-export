"""Per-ship cleanup utility.

Lifted from ``tools/ship/teardown_ship.py`` (private I:-side warships repo).
Layer 4 (composer) — destructive disk I/O behind a ``dry_run=True``
default so the operation is safe to invoke ad-hoc.

When a ship's pipeline run was broken (mid-ingest crash, wrong toolkit
version, debugging artefacts) and you want to start over from a clean
state, this composer handles the spread-out cleanup so no stale
references remain.

What it (would) remove:

1. The ship's per-ship working directory under ``<workspace>/ships/<Ship>/``
   (hull GLB, sidecars, textures, skins, …).
2. Accessory-library entries: each accessory's ``used_by_ships`` list is
   pruned of the ship label; assets that become exclusive to this ship
   are flagged as orphans. With ``prune_orphans=True``, the orphaned
   asset directories are physically removed (the GLB + DDS chain + any
   sibling ``.rig_pivots.json`` etc.). Without it, they stay on disk with
   ``used_by_ships=[]`` so a later ingest can re-bind them without re-
   exporting.
3. The accessory ``index.json`` is rewritten with the pruned
   ``used_by_ships`` + bumped version timestamp.

Hand-flippable winding overrides (``flip_overrides.json``), per-asset
viewed state, and the dead-variant audit are asset-keyed, not ship-keyed
— they need no per-ship cleanup. Library ``.rig_pivots.json`` files
survive teardown (the pivots themselves are in asset-local frame and
stay valid); only their ``source.ship`` provenance field becomes stale
and a follow-up turret-autorig run can refresh it.

Canonical :class:`StepEvent` step names:

    "discover_artifacts"  "delete_ship_dir"  "clean_library_index"

Each step emits ``started`` -> ``completed`` (or ``skipped``).

Return value is a structured report (``dict[str, Any]``) so callers can
log + audit without parsing text. Keys:

    ``ship``                 — the input label.
    ``dry_run``              — whether changes were applied.
    ``workspace_dir``        — resolved ``<workspace>/ships/<Ship>/`` path
                               (always present, may not exist on disk).
    ``ship_dir_existed``     — True if the ship directory was present.
    ``ship_dir_deleted``     — True when the directory got removed
                               (``dry_run=False`` path only).
    ``ship_dir_size_mb``     — float MiB on disk (only when present).
    ``aliases``              — sorted aliases checked against the
                               library index's ``used_by_ships``.
    ``index_present``        — True if the library index was found.
    ``assets_exclusive``     — list of asset_ids exclusive to this ship.
    ``assets_shared``        — list of ``(asset_id, [other_ships])``
                               tuples whose ``used_by_ships`` was pruned
                               but the asset stayed in the index.
    ``orphans_pruned``       — list of asset_ids whose disk files were
                               deleted under ``prune_orphans=True``.
    ``rig_pivots_referencing``  — list of POSIX-style relative paths to
                               ``.rig_pivots.json`` files that name this
                               ship as the pivot source.

Mirrors the I:-side dry-run printout, but plain text printing is left to
the consumer; the composer only emits structured events + the report.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..types import OnEvent
from ._step_runner import StepRunner

# Library locations (relative to workspace).
_LIBRARY_REL = ("libraries", "accessories")
_INDEX_FILENAME = "index.json"
_RIG_PIVOTS_GLOB = "*.rig_pivots.json"


# ---------------------------------------------------------------------------
# Inventory helpers (pure — no mutation)
# ---------------------------------------------------------------------------


def _label_aliases(label: str) -> set[str]:
    """Names that might appear in ``used_by_ships`` for this ship.

    Handles the pre-sanitization era when ``Baltimore (old)`` /
    ``Essex (old)`` could leak into the index.
    """
    return {label, f"{label} (old)"}


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def _load_index(index_path: Path) -> dict | None:
    if not index_path.is_file():
        return None
    return json.loads(index_path.read_text(encoding="utf-8"))


def _inventory(
    label: str,
    *,
    ship_dir: Path,
    library_dir: Path,
    index_path: Path,
) -> dict[str, Any]:
    """Return a dict describing what teardown would touch. No mutation."""
    aliases = _label_aliases(label)
    out: dict[str, Any] = {
        "label":          label,
        "aliases":        sorted(aliases),
        "workspace_dir":  ship_dir,
    }

    out["ship_dir_existed"] = ship_dir.is_dir()
    out["ship_dir_size_mb"] = (
        _dir_size_mb(ship_dir) if ship_dir.is_dir() else 0.0
    )

    idx = _load_index(index_path)
    if idx is None:
        out["index_present"]   = False
        out["assets_exclusive"] = []
        out["assets_shared"]    = []
    else:
        out["index_present"] = True
        exclusive: list[str] = []
        shared: list[tuple[str, list[str]]] = []
        for asset_id, info in (idx.get("assets") or {}).items():
            ships = list(info.get("used_by_ships") or [])
            if not any(s in aliases for s in ships):
                continue
            other = [s for s in ships if s not in aliases]
            if other:
                shared.append((asset_id, other))
            else:
                exclusive.append(asset_id)
        out["assets_exclusive"] = exclusive
        out["assets_shared"]    = shared

    # rig_pivots.json files referencing this ship as the source of pivots.
    rig_referencing: list[Path] = []
    if library_dir.is_dir():
        for f in library_dir.rglob(_RIG_PIVOTS_GLOB):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            src = (data.get("source") or {}).get("ship")
            if src in aliases:
                rig_referencing.append(f)
    out["rig_pivots_referencing"] = rig_referencing

    return out


# ---------------------------------------------------------------------------
# Mutation helpers (only invoked when dry_run is False)
# ---------------------------------------------------------------------------


def _delete_library_asset(
    asset_id: str,
    info: dict[str, Any],
    *,
    library_dir: Path,
) -> bool:
    """Remove all files belonging to an orphan accessory.

    Uses the asset's directory (parent of ``info.glb``) as the removal
    root. Returns ``True`` when the directory was actually removed.
    """
    glb_rel = info.get("glb")
    if not glb_rel:
        return False
    asset_dir = library_dir / Path(glb_rel).parent
    if not asset_dir.is_dir():
        return False
    # Safety: never delete the library root itself, even if a malformed
    # entry had ``glb="something.glb"`` with no parent.
    if asset_dir.resolve() == library_dir.resolve():
        return False
    shutil.rmtree(asset_dir)
    return True


def _clean_library_index(
    inv: dict[str, Any],
    *,
    library_dir: Path,
    index_path: Path,
    prune_orphans: bool,
) -> dict[str, Any]:
    """Drop the ship from ``used_by_ships``; optionally prune orphans on disk.

    Returns ``{"orphans_pruned": [...], "shared_pruned": [...]}``
    describing what changed.
    """
    aliases = set(inv["aliases"])
    out: dict[str, Any] = {"orphans_pruned": [], "shared_pruned": []}

    idx = _load_index(index_path)
    if idx is None:
        return out
    assets = idx.get("assets") or {}

    for asset_id, info in list(assets.items()):
        ships = list(info.get("used_by_ships") or [])
        if not any(s in aliases for s in ships):
            continue
        kept = [s for s in ships if s not in aliases]
        if kept:
            info["used_by_ships"] = kept
            out["shared_pruned"].append(asset_id)
            continue
        # Asset was exclusive to this ship.
        if prune_orphans:
            if _delete_library_asset(asset_id, info, library_dir=library_dir):
                out["orphans_pruned"].append(asset_id)
            assets.pop(asset_id, None)
        else:
            info["used_by_ships"] = []

    idx["asset_count"] = len(assets)
    idx["version"] = datetime.now(UTC).strftime("%Y-%m-%d")
    index_path.write_text(
        json.dumps(idx, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def teardown_ship(
    ship: str,
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    dry_run: bool = True,
    prune_orphans: bool = False,
    on_event: OnEvent | None = None,
) -> dict[str, Any]:
    """Remove a ship's per-ship working directory + library bindings.

    Parameters:
        ship           Filesystem label (matches the ``ships/<Ship>/``
                       folder name; case-sensitive).
        workspace      Pipeline workspace; defaults to
                       ``config.workspace``. Ship dir resolved as
                       ``<workspace>/ships/<Ship>/`` and accessory
                       library as ``<workspace>/libraries/accessories/``.
        config         Resolved :class:`PipelineConfig`; loaded on demand.
        dry_run        ``True`` (default) walks the inventory and emits
                       events but doesn't touch disk. Pass ``False`` to
                       actually delete the ship dir + rewrite the
                       library index.
        prune_orphans  When ``True`` AND ``dry_run=False``, also delete
                       library asset directories whose ``used_by_ships``
                       becomes empty after pruning. Default ``False``
                       keeps the GLBs on disk so a later ingest can
                       re-bind without re-extracting.
        on_event       Optional :class:`StepEvent` callback. Steps:
                       ``discover_artifacts``, ``delete_ship_dir``,
                       ``clean_library_index`` — each emits ``started``
                       -> ``completed`` (or ``skipped``).

    Returns the structured report described in the module docstring.
    Raises :class:`StepError` on any per-step failure.

    Notes:
        * The Unity-side mirror (the publish target of
          :func:`wows_model_export.compose.publish`) is **not** touched
          here — Unity owns its own .meta files and projects often want
          to retain the mirror after a pipeline-side teardown. Use the
          consumer's own delete mechanism instead.
        * ``.rig_pivots.json`` files that name this ship as the pivot
          source are reported (so the caller can re-run
          ``turret_autorig`` to refresh provenance) but are NOT removed:
          the pivots themselves stay valid in asset-local frame.
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)

    ship_dir = (workspace / "ships" / ship).resolve()
    library_dir = (workspace / Path(*_LIBRARY_REL)).resolve()
    index_path = library_dir / _INDEX_FILENAME

    runner = StepRunner(on_event)
    report: dict[str, Any] = {
        "ship":              ship,
        "dry_run":           dry_run,
        "prune_orphans":     prune_orphans and not dry_run,
        "workspace_dir":     ship_dir,
        "ship_dir_existed":  False,
        "ship_dir_deleted":  False,
        "ship_dir_size_mb":  0.0,
        "aliases":           [],
        "index_present":     False,
        "assets_exclusive":  [],
        "assets_shared":     [],
        "orphans_pruned":    [],
        "rig_pivots_referencing": [],
        "step_timings_ms":   {},
    }

    # ── Step: discover_artifacts ──────────────────────────────────────
    with runner.step("discover_artifacts", detail=ship) as ctx:
        inv = _inventory(
            ship,
            ship_dir=ship_dir,
            library_dir=library_dir,
            index_path=index_path,
        )
        report["aliases"]          = inv["aliases"]
        report["ship_dir_existed"] = inv["ship_dir_existed"]
        report["ship_dir_size_mb"] = inv["ship_dir_size_mb"]
        report["index_present"]    = inv["index_present"]
        report["assets_exclusive"] = inv["assets_exclusive"]
        report["assets_shared"]    = inv["assets_shared"]
        report["rig_pivots_referencing"] = [
            str(p.relative_to(workspace)).replace("\\", "/")
            for p in inv["rig_pivots_referencing"]
        ]
        ctx.annotate(
            f"ship_dir={'present' if inv['ship_dir_existed'] else 'missing'} "
            f"exclusive={len(inv['assets_exclusive'])} "
            f"shared={len(inv['assets_shared'])}",
            data={
                "ship_dir_existed":  inv["ship_dir_existed"],
                "exclusive_count":   len(inv["assets_exclusive"]),
                "shared_count":      len(inv["assets_shared"]),
                "rig_referencing":   len(inv["rig_pivots_referencing"]),
            },
        )

    # ── Step: delete_ship_dir ─────────────────────────────────────────
    if inv["ship_dir_existed"]:
        if dry_run:
            runner.emit(
                "delete_ship_dir", "skipped",
                detail=f"dry_run — would delete {ship_dir}",
            )
        else:
            with runner.step("delete_ship_dir", detail=str(ship_dir)) as ctx:
                shutil.rmtree(ship_dir)
                report["ship_dir_deleted"] = True
                ctx.annotate(
                    f"{inv['ship_dir_size_mb']:.1f} MB removed",
                    data={"size_mb": inv["ship_dir_size_mb"]},
                )
    else:
        runner.emit(
            "delete_ship_dir", "skipped",
            detail="ship_dir not present",
        )

    # ── Step: clean_library_index ─────────────────────────────────────
    if not inv["index_present"]:
        runner.emit(
            "clean_library_index", "skipped",
            detail="accessory library index missing",
        )
    elif dry_run:
        runner.emit(
            "clean_library_index", "skipped",
            detail=(
                f"dry_run — would prune "
                f"{len(inv['assets_exclusive'])} exclusive + "
                f"{len(inv['assets_shared'])} shared used_by_ships"
            ),
        )
    else:
        with runner.step("clean_library_index", detail=str(index_path)) as ctx:
            cleanup = _clean_library_index(
                inv,
                library_dir=library_dir,
                index_path=index_path,
                prune_orphans=prune_orphans,
            )
            report["orphans_pruned"] = cleanup["orphans_pruned"]
            ctx.annotate(
                f"shared_pruned={len(cleanup['shared_pruned'])} "
                f"orphans_pruned={len(cleanup['orphans_pruned'])}",
                data={
                    "shared_pruned":  len(cleanup["shared_pruned"]),
                    "orphans_pruned": len(cleanup["orphans_pruned"]),
                },
            )

    report["step_timings_ms"] = dict(runner.spans)
    return report


__all__ = ["teardown_ship"]
