"""Refresh only the ``death`` block of existing ship sidecars.

No geometry, textures or other sections are touched: the block is re-derived from
GameParams (the ship's ``wg_ship_full_id``, upgraded hull, as the scaffold does) and
replaces the previous one. Use it to backfill sidecars written before the block existed.

    python -m wows_model_export.compose.refresh_death_presentation <sidecar.meta.json>...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read.gameparams import get_ship
from ..resolve import death_presentation as _death
from ..resolve import sidecar as _sidecar
from ..resolve.gameparams_autofill import resolve_components
from ..toolkit import gameparams as _tk_gameparams


def refresh_death_presentation(sidecar_path: Path, *, config: PipelineConfig | None = None) -> dict[str, Any]:
    """Rewrite ``sidecar_path``'s ``death`` block in place; return a short audit."""
    cfg = config or PipelineConfig.load()
    doc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    ship_meta = doc.get("ship") or {}
    vehicle_id = ship_meta.get("wg_ship_full_id") or ship_meta.get("wg_ship_id")
    if not vehicle_id:
        return {"path": str(sidecar_path), "status": "no ship id"}
    ship = get_ship(vehicle_id)
    if not ship:
        return {"path": str(sidecar_path), "status": f"{vehicle_id} not in GameParams"}
    hull = resolve_components(ship, hull_choice="upgraded").get("hull") or {}
    block = _death.collect_death_block(hull, cache_root=_death.sequence_cache_root(cfg), config=cfg)
    if not block:
        return {"path": str(sidecar_path), "status": "no death data"}
    _sidecar.write(_sidecar.absorb_death_presentation(doc, block), sidecar_path)
    seq = [p["path"] for s in block.get("sequence", []) for p in s.get("particles", [])]
    return {
        "path": str(sidecar_path),
        "status": "ok",
        "sequence": seq,
        "effects": sorted(block.get("effects", {})),
        "windows": block.get("depth_windows_bw"),
    }


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__)
        return 2
    cfg = PipelineConfig.load()
    _tk_gameparams.ensure_dump(config=cfg)
    bad = 0
    for p in paths:
        try:
            audit = refresh_death_presentation(p, config=cfg)
        except Exception as e:  # keep going over a batch
            audit = {"path": str(p), "status": f"failed: {e}"}
        if audit["status"] != "ok":
            bad += 1
        print(json.dumps(audit, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
