"""`wowsunpack game-params` subcommand wrapper.

Dumps `GameParams.data` (binary entity table) as JSON. The full dump is
~2.8 GB and lives at `$WOWS_WORKSPACE/.cache/gameparams.json`. Per-ship
dumps (``ship_id=PASB018``) are a few MB and useful as ad-hoc references.

The fleet-wide cache is consumed by `resolve.variant_accessory_swaps`,
`scaffold.sidecar_autofill`, `wg_camo` etc. — see those modules for the
read side. This module only handles the *write*: extracting fresh
JSON from the live game install, plus ``ensure_dump`` — an idempotent
helper that builds the cache only if it isn't already on disk.
"""

from __future__ import annotations

import os
import sys
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


def ensure_dump(
    *,
    cache_path: Path | None = None,
    refresh: bool = False,
    config: PipelineConfig | None = None,
) -> Path:
    """Ensure the full GameParams cache exists at ``cache_path``; build if missing.

    ``cache_path`` defaults to ``<config.cache_dir>/gameparams.json``.

    Idempotent: if the file already exists and ``refresh`` is False the
    existing path is returned without re-extracting. Pass ``refresh=True``
    after a game patch (or delete the file) to force a fresh dump.

    The build calls :func:`dump_gameparams` with ``full=True`` and writes
    atomically through a sibling ``.tmp`` path so a SIGINT mid-write
    cannot leave a truncated 3 GB file that would pass an ``is_file()``
    guard on the next call but fail to parse.
    """
    cfg = config or PipelineConfig.load()
    if cache_path is None:
        cache_path = cfg.require_cache_dir() / "gameparams.json"
    else:
        cache_path = Path(cache_path)

    if cache_path.is_file() and not refresh:
        return cache_path

    print(
        f"[gameparams] dumping GameParams.json -> {cache_path} "
        "(~30-90s, ~2.8 GB)...",
        file=sys.stderr,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic dump: write to a sibling .tmp path, then os.replace into place.
    # Without this, a SIGINT mid-write leaves a truncated 3 GB file that
    # passes the is_file() guard on the next call and then fails parsing.
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        # ``pretty=False`` (the "ugly" form) is about 30% smaller on disk
        # and noticeably faster to parse than the pretty form. All current
        # consumers handle either format, so the format choice is opaque
        # to callers.
        dump_gameparams(tmp_path, full=True, pretty=False, config=cfg)
        if not tmp_path.is_file():
            raise RuntimeError(
                f"gameparams: dump did not produce {tmp_path}"
            )
        os.replace(tmp_path, cache_path)
    finally:
        # Best-effort cleanup if the toolkit succeeded but replace raised
        # (e.g. permission denied on the final path).
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return cache_path


__all__ = ["dump_gameparams", "ensure_dump"]
