"""CLI for composed buildings (an old-format base model plus GameParams gun mounts).

    wows-compose-building --spec composites.json
    wows-compose-building --spec composites.json --id CMBC001 CMBA001
    wows-compose-building --spec composites.json --skip-glb     # sidecar-only recompose

Outputs land under ``<workspace>/buildings/<id>/`` in the building export
layout; the spec format is documented in
``wows_model_export.building_compose``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..building_compose import run_compose
from ..errors import ConfigError, ToolkitError
from ._args import add_build_args, resolve_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wows-compose-building",
        description="Compose buildings from an old-format base model and GameParams gun mounts.",
    )
    parser.add_argument("--spec", required=True, type=Path, help="composite spec JSON (wows_building_composites/v1)")
    parser.add_argument("--id", nargs="+", dest="ids", metavar="CMBC001", help="compose only these spec ids")
    parser.add_argument("--skip-glb", action="store_true", help="reuse GLBs already on disk; only recompose sidecars")
    parser.add_argument("--refresh-guns", action="store_true", help="re-export gun GLBs already in the gun library")
    add_build_args(parser)
    args = parser.parse_args(argv)

    try:
        report = run_compose(
            spec_path=args.spec,
            ids=args.ids,
            skip_glb=args.skip_glb,
            refresh_guns=args.refresh_guns,
            config=resolve_config(args),
        )
    except (ToolkitError, ConfigError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    n_warn = len(report.get("warnings") or [])
    print(f"done: {len(report['buildings'])} composite(s), {n_warn} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
