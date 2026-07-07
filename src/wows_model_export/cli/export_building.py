"""CLI for the building (shore-structure) export pipeline.

    wows-export-building --index PCBX007 PCBC001
    wows-export-building --species CoastalArtillery
    wows-export-building --all
    wows-export-building --index PCBX007 --skip-glb   # sidecar-only recompose

Outputs land under ``<workspace>/buildings/`` — see
``wows_model_export.building_export`` for the layout + sidecar contract.
"""

from __future__ import annotations

import argparse
import sys

from ..building_export import run_export
from ..config import PipelineConfig
from ..errors import ToolkitError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wows-export-building",
        description="Export GameParams Building entries (hull + guns GLBs, hitbox/armor sidecar).",
    )
    sel = parser.add_argument_group("selection (one required)")
    sel.add_argument("--index", nargs="+", metavar="PCBX007", help="explicit building indices")
    sel.add_argument("--species", metavar="CoastalArtillery", help="every building of a species")
    sel.add_argument("--all", action="store_true", dest="all_buildings", help="every Building entry")
    parser.add_argument(
        "--skip-glb",
        action="store_true",
        help="reuse GLBs already on disk; only recompose sidecars",
    )
    args = parser.parse_args(argv)

    if not (args.index or args.species or args.all_buildings):
        parser.error("select buildings via --index, --species, or --all")

    try:
        report = run_export(
            indices=args.index,
            species=args.species,
            all_buildings=args.all_buildings,
            skip_glb=args.skip_glb,
            config=PipelineConfig.load(),
        )
    except (ToolkitError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    n_warn = len(report.get("warnings") or [])
    print(f"done: {len(report['buildings'])} building(s), {n_warn} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
