"""CLI wrapper for :func:`wows_model_export.compose.accessory_library.build_accessory_library`.

Drives the fleet-wide accessory-library builder. Argv shape::

    wows-build-accessory-library
        [--library-root P]
        [--only A,B,C]
        [--rebuild]
        [--audit-winding] [--auto-flip-winding]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.accessory_library import build_accessory_library
from ..errors import ConfigError, StepError, ToolkitError
from ..types import AccessoryLibraryResult
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
        prog="wows-build-accessory-library",
        description="Build / refresh the fleet-wide accessory library.",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Where to build the library (default: "
             "<workspace>/libraries/accessories).",
    )
    ap.add_argument(
        "--only",
        default=None,
        help="Comma-separated list of ship labels to walk; sidecars "
             "from other ships are ignored. Default: every in-tree ship.",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate every asset GLB + DDS from scratch.",
    )
    ap.add_argument(
        "--audit-winding",
        action="store_true",
        help="Run the joint A+B winding heuristic and print results.",
    )
    ap.add_argument(
        "--auto-flip-winding",
        action="store_true",
        help="Flip GLBs that score a high-confidence FLIP verdict and "
             "persist them to flip_overrides.json (source: 'auto').",
    )
    add_common_args(ap)
    return ap


def _summarize(result: AccessoryLibraryResult) -> str:
    bits = [
        f"library {result.library_root}",
        f"built={result.assets_built}",
        f"audited={result.assets_audited}",
    ]
    if result.auto_flipped:
        bits.append(f"auto_flipped={len(result.auto_flipped)}")
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    total = result.step_timings_ms.get("total")
    if total is not None:
        bits.append(f"total={total:.0f}ms")
    return "  ".join(bits)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    only_ships: tuple[str, ...] | None = None
    if args.only:
        only_ships = tuple(s.strip() for s in args.only.split(",") if s.strip())

    try:
        result = build_accessory_library(
            workspace=cfg.workspace,
            config=cfg,
            library_root=args.library_root,
            only_ships=only_ships,
            rebuild=args.rebuild,
            audit_winding=args.audit_winding,
            auto_flip_winding=args.auto_flip_winding,
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

    print(_summarize(result), file=sys.stderr)
    for warn in result.warnings:
        print(f"  warn: {warn}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
