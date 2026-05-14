"""CLI wrapper for :func:`wows_model_export.compose.ammo_profiles.build_ammo_profiles`.

Build / refresh ``<library_root>/ammo_profiles.json``. Argv shape::

    wows-build-ammo-profiles
        [--output P]
        [--library-root P]
        [--refresh-gameparams]
        [--pretty]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.ammo_profiles import build_ammo_profiles
from ..errors import ConfigError, StepError, ToolkitError
from ..types import AmmoProfilesResult
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
        prog="wows-build-ammo-profiles",
        description="Build / refresh the projectile ammo_profiles.json.",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        dest="output_path",
        help="Override output path (default: "
             "<library_root>/ammo_profiles.json).",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Override the projectile library root (used only to derive "
             "the default output path).",
    )
    ap.add_argument(
        "--refresh-gameparams",
        action="store_true",
        help="Force a fresh GameParams ensure_dump (skip cache).",
    )
    ap.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the output JSON for human inspection.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: AmmoProfilesResult) -> str:
    bits = [
        f"profiles -> {result.output_path}",
        f"count={result.profiles_count}",
    ]
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

    try:
        result = build_ammo_profiles(
            output_path=args.output_path,
            workspace=cfg.workspace,
            config=cfg,
            library_root=args.library_root,
            on_event=printer,
            refresh_gameparams=args.refresh_gameparams,
            pretty=args.pretty,
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
