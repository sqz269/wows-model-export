"""CLI wrapper for :func:`wows_model_export.compose.find_ship_variants.find_ship_variants`.

Enumerate every Vehicle in GameParams + cross-link by mesh-swap
``nativePermoflage``. Argv shape::

    wows-find-ship-variants
        [--name PREFIX]
        [--include-exteriors]
        [--refresh]
        [--output P]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from ..compose.find_ship_variants import find_ship_variants
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
        prog="wows-find-ship-variants",
        description="Enumerate Vehicles in GameParams + cross-link by "
                    "mesh-swap nativePermoflage.",
    )
    ap.add_argument(
        "--name",
        default=None,
        help="Case-insensitive needle to filter Vehicles by display "
             "name or GameParams id. Default: no filter.",
    )
    ap.add_argument(
        "--include-exteriors",
        action="store_true",
        help="Include matching Exterior records in the result payload.",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Force a fresh GameParams ensure_dump (skip cache).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the JSON survey to this path. Default: print to "
             "stdout.",
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
        survey = find_ship_variants(
            name=args.name,
            include_exteriors=args.include_exteriors,
            refresh=args.refresh,
            output_json=args.output,
            config=cfg,
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

    if args.output is None:
        # Composer didn't write -- emit the survey to stdout for shell use.
        sys.stdout.write(json.dumps(survey, indent=2, default=str))
        sys.stdout.write("\n")
    else:
        n_v = len(survey.get("vehicles") or [])
        n_e = len(survey.get("exteriors") or [])
        print(
            f"survey -> {args.output}  vehicles={n_v}  exteriors={n_e}",
            file=sys.stderr,
        )
    return EXIT_OK


__all__ = ["main"]
