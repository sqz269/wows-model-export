"""Re-run single GameParams passes over ship sidecars already in the workspace.

A pass added to the scaffold (``_absorb_gameparams_passes``) reaches a ship's
sidecar only when the ship is scaffolded again, which also re-runs the toolkit
export, the armor and ammo steps and every other pass. This module re-runs only
the named passes on the sidecar on disk, against the GameParams cache, and
writes the sidecar back in its canonical form. No toolkit call is made and no
other section is rebuilt, so the sidecar changes only in the sections the
passes own.

Passes (``PASSES``):

* ``air_defense`` — the AA auras (:func:`.scaffold_ship.air_defense_pass`).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read import gameparams as _gp_read
from ..resolve import sidecar
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from .scaffold_ship import air_defense_pass

#: Pass name → (the sidecar sections it owns, ``fn(doc, ship_dict) -> doc``).
PASSES: dict[str, tuple[tuple[str, ...], Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]]] = {
    "air_defense": (("air_defense",), air_defense_pass),
}


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
    if not ship_id:
        return RefreshResult(label, path, None, "skipped", detail="sidecar names no GameParams ship")
    _ensure_gameparams_dump(config=config)
    ship_dict = _gp_read.get_ship(ship_id)
    if ship_dict is None:
        return RefreshResult(label, path, ship_id, "skipped", detail="ship not in the GameParams cache")

    new = doc
    owned: set[str] = set()
    for name in passes:
        sections, fn = PASSES[name]
        owned.update(sections)
        new = fn(new, ship_dict)
    # Keep the old stamp on a section that came back equal.
    for key in owned:
        if key in doc and key in new and _strip_source(doc[key]) == _strip_source(new[key]):
            new = {**new, key: doc[key]}
    changed = _changed_sections(doc, new)
    stray = [k for k in changed if k not in owned]
    if stray:
        raise ValueError(f"{label}: passes {list(passes)} changed sections they do not own: {stray}")
    if not changed:
        return RefreshResult(label, path, ship_id, "unchanged")
    if dry_run:
        return RefreshResult(label, path, ship_id, "would-write", changed)
    sidecar.write(new, path)
    return RefreshResult(label, path, ship_id, "written", changed)


def workspace_ship_labels(workspace: Path) -> list[str]:
    """Every ``ships/<label>`` folder that holds ``<label>.meta.json``, sorted."""
    root = Path(workspace) / "ships"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / f"{p.name}.meta.json").is_file())


__all__ = ["PASSES", "RefreshResult", "refresh_sidecar", "workspace_ship_labels"]
