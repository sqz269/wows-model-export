"""`wowsunpack armor --json` subcommand wrapper.

Dumps the ship's per-mount armor materials_table + zones as JSON. Output
shape feeds the sidecar's `armor.materials_table` and `armor.zones`
sections (see `docs/contracts/sidecar-schema.md` §5 once that lift lands;
historical reference: `tools/contracts/METADATA_SPEC.md` §5).

Per-material payload:
    { thickness_mm, layers, zones }

Keys are stringified u32 material IDs that match the per-vertex
`_MATERIAL_ID` attribute baked into the hull GLB by `export_ship`.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def armor_json(
    ship: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
    hull: str | None = None,
    vehicle: str | None = None,
) -> ToolkitResult:
    """Dump the ship's armor materials_table + zones to ``out_path``.

    ``hull``: optional ``PAUH*`` hull-upgrade identifier; when set,
    armor table reflects that hull tier specifically. Default (None)
    uses the stock hull.

    ``vehicle``: optional explicit GameParams vehicle id (param name like
    ``PASC108_Baltimore_1944`` or short index like ``PASC108``). When set,
    the armor map binds to that exact param instead of first-match on the
    model directory — required when several params share one model dir (a
    current ship plus a legacy re-release re-skin, e.g. Baltimore).
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--game-dir", str(cfg.require_game_dir()), "armor", ship, "--json", str(out)]
    if vehicle is not None:
        argv += ["--vehicle", vehicle]
    if hull is not None:
        argv += ["--hull", hull]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


__all__ = ["armor_json"]
