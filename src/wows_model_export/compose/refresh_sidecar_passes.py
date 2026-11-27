"""Re-run single GameParams passes over ship sidecars already in the workspace.

A pass added to the scaffold (``_absorb_gameparams_passes``) reaches a ship's
sidecar only when the ship is scaffolded again, which also re-runs the toolkit
export, the armor and ammo steps and every other pass. This module re-runs only
the named passes on the sidecar on disk, against the GameParams cache, and
writes the sidecar back in its canonical form. No other section is rebuilt,
so the sidecar changes only in the sections the passes own. No toolkit call is
made, except by ``modules`` for a gun or torpedo module whose models differ
from the hull's placed ones (it reads their bones and, where the hull places
nothing or a model that turns differently, the hull model's node tree, below).

Passes (``PASSES``):

* ``air_defense`` — the AA auras (:func:`.scaffold_ship.air_defense_pass`).
* ``torpedoes`` — every ``ballistics.torpedoes`` entry's GameParams fields
  (:func:`.scaffold_ship.torpedo_profiles_pass`; added for ``flat_damage``,
  the flat term of a torpedo hit's damage). It owns the ``ballistics``
  section and changes only its ``torpedoes`` entries.
* ``mounts`` — the per-placement GameParams fields (``misc_filter``, arcs,
  reload, ``ammo_ids``, ...) on the top-level placements, from the rendered
  hull's components (:func:`.scaffold_ship.mounts_refresh_pass`, scaffold
  pass 3; added so a placement's ammo comes from the component its other
  fields come from). A mesh-swap variant folder keeps its ``misc_filter`` (the
  variant swap's). Owns the placement sections; every field it does not write
  is kept.
* ``hull_mounts`` — the per-placement GameParams fields (``misc_filter``,
  arcs, reload, ``ammo_ids``, ...) on every ``hulls.<H>`` snapshot, from that
  hull's components (:func:`.scaffold_ship.hull_mounts_pass`). Owns ``hulls``.
* ``hull_stats`` — every ``hulls.<H>.stats`` block re-derived from that
  hull's GameParams component (:func:`.scaffold_ship.hull_stats_pass`; added
  for the per-hull ``max_speed_kn``, ``turning_radius_m``, ``detection_km``
  and ``air_detection_km``). Owns ``hulls`` and changes only ``stats``.
* ``hull_wrecks`` — ``dead_asset_id`` on every ``hulls.<H>`` mount, the
  wreck model by the top-level placements' rule (``<asset_id>_dead`` when the
  game's file system holds that geometry, else ``null``), and the entry's
  ``subcategory`` without empty path segments
  (:func:`.scaffold_ship.hull_wrecks_pass`; added so a mount that only a
  non-rendered hull places names its wreck). Reads the VFS manifest of the
  refresh's config (``--build N`` / ``--workspace`` included) instead of
  GameParams; with no manifest there the pass is skipped and the result says
  so. Owns ``hulls`` and changes only those keys.
* ``hull_names`` — ``hulls.<H>.display_name``, the hull module's in-game
  name from the game's ``global.mo`` (:func:`.scaffold_ship.hull_names_pass`;
  "Mogami (A)", "Prowler-H1"). Owns ``hulls`` and changes only that key.
* ``ship_class`` — ``ship.class`` re-derived from the main battery's caliber
  (:func:`.scaffold_ship.ship_class_pass`). Owns ``ship``. Skipped for a
  ship whose ``build_deps.json`` records a ``class_override``.
* ``effects`` — the weapon-bound particle rows: ``effects.by_hull`` for every
  hull, the top-level weapon rows from the rendered hull (event-weapon mount
  rows included), ``effects.laser_beam``, and the same rows in every exterior
  ``effects`` payload with its substitutions (:func:`.scaffold_ship.effects_pass`).
  Hull and AA-aura rows are kept. Owns ``effects`` and ``exteriors``.
* ``modules`` — every upgrade module with the numbers it changes on each
  hull it fits, and per hull the modules of each slot it fits and the fitted
  one (:func:`.scaffold_ship.modules_pass`; METADATA_SPEC §11.6). A gun or
  torpedo module's hardpoints the hull places no mount at, or another model
  that turns differently, get the export's transform from the hull model's
  node tree (the toolkit's ``dump-bones``, against the refresh's config).
  When such a record cannot be read (no VFS manifest, no visual of the hull
  in it, a toolkit failure) the pass is skipped, the old section kept, and
  the result says so. Owns ``modules``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read import gameparams as _gp_read
from ..resolve import sidecar
from ..toolkit import vfs as _toolkit_vfs
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from .scaffold_ship import (
    ModuleHardpointsUnreadable,
    air_defense_pass,
    effects_pass,
    hull_mounts_pass,
    hull_names_pass,
    hull_stats_pass,
    hull_wrecks_pass,
    modules_pass,
    mounts_refresh_pass,
    ship_class_pass,
    torpedo_profiles_pass,
)

#: Pass name → (the sidecar sections it owns, ``fn(doc, ship_dict) -> doc``).
PASSES: dict[str, tuple[tuple[str, ...], Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]]] = {
    "air_defense": (("air_defense",), air_defense_pass),
    "torpedoes": (("ballistics",), torpedo_profiles_pass),
    "mounts": (sidecar.PLACEMENT_SECTIONS, mounts_refresh_pass),
    "hull_mounts": (("hulls",), hull_mounts_pass),
    "hull_names": (("hulls",), hull_names_pass),
    "hull_stats": (("hulls",), hull_stats_pass),
    "hull_wrecks": (("hulls",), hull_wrecks_pass),
    "ship_class": (("ship",), ship_class_pass),
    "effects": (("effects", "exteriors"), effects_pass),
    "modules": (("modules",), modules_pass),
}

#: Passes that read no GameParams: a refresh of only these skips loading the
#: GameParams cache and passes an empty ship dict.
NO_GAMEPARAMS: frozenset[str] = frozenset({"hull_wrecks"})


def _class_override(ship_dir: Path) -> str | None:
    """The ``class_override`` the ship's last export was made with, read from
    its ``build_deps.json`` (``reexport.class_override``); ``None`` if none."""
    path = ship_dir / "build_deps.json"
    if not path.is_file():
        return None
    try:
        import json

        reexport = json.loads(path.read_text(encoding="utf-8")).get("reexport") or {}
    except (OSError, ValueError):
        return None
    value = reexport.get("class_override") if isinstance(reexport, dict) else None
    return value if isinstance(value, str) and value else None


@dataclass
class RefreshResult:
    label: str
    sidecar_path: Path
    ship_id: str | None
    status: str  # "written" | "unchanged" | "would-write" | "skipped"
    changed_sections: list[str] = field(default_factory=list)
    detail: str = ""


def _changed_sections(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _strip_source(section: Any) -> Any:
    if isinstance(section, dict):
        return {k: v for k, v in section.items() if k != "source"}
    return section


def refresh_sidecar(
    label: str,
    *,
    workspace: Path,
    passes: tuple[str, ...] = ("air_defense",),
    config: PipelineConfig | None = None,
    dry_run: bool = False,
) -> RefreshResult:
    """Re-run ``passes`` on ``ships/<label>/<label>.meta.json``.

    The ship's GameParams entry is the sidecar's ``ship.wg_ship_full_id``
    (else ``ship.wg_ship_id``). A section whose content apart from its
    ``source`` stamp comes back equal keeps its old stamp, so a re-run that
    changes nothing leaves the file as it was. Raises ``ValueError`` when a
    pass would change a section it does not own.
    """
    unknown = [p for p in passes if p not in PASSES]
    if unknown:
        raise ValueError(f"unknown pass(es) {unknown}; known: {sorted(PASSES)}")
    path = Path(workspace) / "ships" / label / f"{label}.meta.json"
    if not path.is_file():
        return RefreshResult(label, path, None, "skipped", detail="no sidecar")
    doc = sidecar.read(path)
    ship = doc.get("ship") or {}
    ship_id = ship.get("wg_ship_full_id") or ship.get("wg_ship_id")
    if all(p in NO_GAMEPARAMS for p in passes):
        ship_dict: dict[str, Any] = {}
    else:
        if not ship_id:
            return RefreshResult(label, path, None, "skipped", detail="sidecar names no GameParams ship")
        _ensure_gameparams_dump(config=config)
        ship_dict = _gp_read.get_ship(ship_id)
        if ship_dict is None:
            return RefreshResult(label, path, ship_id, "skipped", detail="ship not in the GameParams cache")

    new = doc
    owned: set[str] = set()
    notes: list[str] = []
    for name in passes:
        sections, fn = PASSES[name]
        owned.update(sections)
        if name == "ship_class" and _class_override(path.parent):
            # A hand-typed class wins over the caliber rule.
            continue
        if name == "hull_wrecks":
            # The manifest of this refresh's config (its --build / --workspace), not the
            # environment's default; a missing one skips the pass and says so, instead of "unchanged".
            manifest = _toolkit_vfs.default_manifest_path(config)
            if not Path(manifest).is_file():
                notes.append(f"hull_wrecks skipped: no VFS manifest at {manifest}")
                continue
            new = fn(new, ship_dict, manifest_path=manifest)
            continue
        if name == "modules":
            # The hull visuals of this refresh's config (its --build / --workspace). A module
            # record whose transform cannot be read keeps the old section, and the result says so.
            try:
                new = fn(new, ship_dict, config=config)
            except ModuleHardpointsUnreadable as e:
                notes.append(f"modules skipped: {e}")
            continue
        new = fn(new, ship_dict)
    detail = "; ".join(notes)
    # Keep the old stamp on a section that came back equal.
    for key in owned:
        if key in doc and key in new and _strip_source(doc[key]) == _strip_source(new[key]):
            new = {**new, key: doc[key]}
    changed = _changed_sections(doc, new)
    stray = [k for k in changed if k not in owned]
    if stray:
        raise ValueError(f"{label}: passes {list(passes)} changed sections they do not own: {stray}")
    if not changed:
        status = "skipped" if len(notes) == len(passes) else "unchanged"
        return RefreshResult(label, path, ship_id, status, detail=detail)
    if dry_run:
        return RefreshResult(label, path, ship_id, "would-write", changed, detail=detail)
    sidecar.write(new, path)
    return RefreshResult(label, path, ship_id, "written", changed, detail=detail)


def workspace_ship_labels(workspace: Path) -> list[str]:
    """Every ``ships/<label>`` folder that holds ``<label>.meta.json``, sorted."""
    root = Path(workspace) / "ships"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / f"{p.name}.meta.json").is_file())


__all__ = ["NO_GAMEPARAMS", "PASSES", "RefreshResult", "refresh_sidecar", "workspace_ship_labels"]
