"""`wowsunpack ammo --json` subcommand wrapper.

Dumps per-shell ballistic profiles + aggregate ranges as JSON. Output
feeds the sidecar's `ballistics.shells` / `ballistics.torpedoes`
sections. Walks every hull upgrade's mounts + alternative loadouts,
unions the ``ammoList`` references, and resolves each to its
``Projectile`` GameParam.

Per-shell fields (see contract docs for full reference):
    ammo_type, caliber_mm, mass_kg, muzzle_velocity_mps,
    air_drag_coefficient, krupp, cap, cap_normalize_max_deg,
    fuze_arming_threshold_mm, fuze_delay_s, ricochet_min_deg,
    ricochet_always_deg, alpha_damage, alpha_piercing_he_mm,
    alpha_piercing_cs_mm, burn_probability, max_range_m
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def ammo_json(
    ship: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
    hull: str | None = None,
    vehicle: str | None = None,
) -> ToolkitResult:
    """Dump the ship's per-shell ballistic profiles to ``out_path``.

    ``hull``: optional ``PAUH*`` hull-upgrade identifier for the
    ``ranges`` calculation. Shells are always the union across hulls
    (a shell present on hull-stock is the same projectile on hull-elite);
    only the aggregate range needs a hull-specific reference.

    ``vehicle``: optional explicit GameParams vehicle id (param name like
    ``PASC108_Baltimore_1944`` or short index like ``PASC108``). When set,
    the ammo/ranges bind to that exact param instead of first-match on the
    model directory — required when several params share one model dir (a
    current ship plus a legacy re-release re-skin, e.g. Baltimore).
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--game-dir", str(cfg.require_game_dir()), "ammo", ship, "--json", str(out)]
    if vehicle is not None:
        argv += ["--vehicle", vehicle]
    if hull is not None:
        argv += ["--hull", hull]
    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


__all__ = ["ammo_json"]
