"""`wowsunpack game-params` subcommand wrapper.

Dumps `GameParams.data` (binary entity table) as JSON. The full dump is
~2.8 GB and lives at `$WOWS_WORKSPACE/.cache/gameparams.json`. Per-ship
dumps (``ship_id=PASB018``) are a few MB and useful as ad-hoc references.

The fleet-wide cache is consumed by `resolve.variant_accessory_swaps`,
`scaffold.sidecar_autofill`, `wg_camo` etc. — see those modules for the
read side. This module only handles the *write*: extracting fresh
JSON from the live game install.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PipelineConfig
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def dump_gameparams(
    out_path: Path | str | os.PathLike,
    *,
    ship_id: str | None = None,
    full: bool = False,
    pretty: bool = True,
    config: PipelineConfig | None = None,
) -> ToolkitResult:
    """Dump GameParams as JSON.

    ``ship_id``: filter to a single entry (e.g. ``"PASA008"``). Mutually
    exclusive with ``full`` — passing both raises ``ValueError``.

    ``full``: dump every entity. Produces the ~2.8 GB fleet-wide JSON.

    ``pretty``: pretty-printed JSON (default). Set to ``False`` for the
    compact ("ugly") form — smaller on disk, identical semantics.

    Renamed from the I:-side ``game_params`` to make the verb-noun
    pattern consistent with other toolkit dumpers (``dump_bones``,
    ``armor_json``, ``ammo_json``).
    """
    if ship_id is not None and full:
        raise ValueError("dump_gameparams: ship_id and full are mutually exclusive")

    cfg = config or PipelineConfig.load()
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = [
        "--game-dir", str(cfg.require_game_dir()),
        "game-params",
    ]
    if full:
        argv.append("--full")
    if not pretty:
        argv.append("--ugly")
    if ship_id and not full:
        argv += ["--id", ship_id]
    argv.append(str(out))

    return run_toolkit(argv, config=cfg, expect_outputs=(out,))


__all__ = ["dump_gameparams"]
