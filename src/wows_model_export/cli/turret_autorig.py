"""CLI wrapper for :func:`wows_model_export.compose.turret_autorig.autorig_asset`.

Extract turret rig pivots for a single library asset. Argv shape::

    wows-turret-autorig <asset_id>
        [--library-root P]
        [--output P]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.turret_autorig import autorig_asset
from ..errors import ConfigError, StepError, ToolkitError
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STEP_ERROR,
    EXIT_UNEXPECTED,
    add_common_args,
    build_printer,
    resolve_config,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-turret-autorig",
        description="Extract turret rig pivots for a single library asset.",
    )
    ap.add_argument(
        "asset_id",
        help="WG asset identifier (e.g. AGM034_16in50_Mk7).",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Override the accessory library root (default: "
             "<workspace>/libraries/accessories).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override the output JSON path (default: "
             "<asset_dir>/<asset_id>.rig_pivots.json).",
    )
    add_common_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    try:
        output_path = autorig_asset(
            args.asset_id,
            config=cfg,
            library_root=args.library_root,
            output_path=args.output,
            on_event=printer,
        )
    except StepError as e:
        print(f"\nerror: step {e.step!r} failed: {e.detail or e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(f"rig pivots {args.asset_id!r} -> {output_path}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
