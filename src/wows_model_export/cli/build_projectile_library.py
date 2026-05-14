"""CLI wrapper for :func:`wows_model_export.compose.projectile_library.build_projectile_library`.

Build / refresh the fleet-wide projectile library. Argv shape::

    wows-build-projectile-library
        [--library-root P]
        [--rebuild]
        [--mode dds|both|png|none]
        [--only ID,ID,...]
        [--manifest P] [--refresh-manifest]
        [--emission-intensity F]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.projectile_library import build_projectile_library
from ..errors import ConfigError, StepError, ToolkitError
from ..types import ProjectileLibraryResult
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
        prog="wows-build-projectile-library",
        description="Build / refresh the fleet-wide projectile library.",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Where to build the library (default: "
             "<workspace>/libraries/projectiles).",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate every projectile GLB + DDS from scratch.",
    )
    ap.add_argument(
        "--mode",
        default="dds",
        help="Texture output mode: dds (default) / both / png / none.",
    )
    ap.add_argument(
        "--only",
        default=None,
        help="Comma-separated list of projectile asset_ids to build.",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to the projectile manifest JSON (auto-derived when "
             "unset).",
    )
    ap.add_argument(
        "--refresh-manifest",
        action="store_true",
        help="Re-walk the GameParams dump to refresh the manifest.",
    )
    ap.add_argument(
        "--emission-intensity",
        type=float,
        default=2.5,
        help="Emissive synthesis intensity multiplier (default: 2.5).",
    )
    add_common_args(ap)
    return ap


def _summarize(result: ProjectileLibraryResult) -> str:
    bits = [
        f"library {result.library_root}",
        f"built={result.projectiles_built}",
        f"audited={result.projectiles_audited}",
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

    only: tuple[str, ...] | None = None
    if args.only:
        only = tuple(s.strip() for s in args.only.split(",") if s.strip())

    try:
        result = build_projectile_library(
            workspace=cfg.workspace,
            config=cfg,
            library_root=args.library_root,
            rebuild=args.rebuild,
            on_event=printer,
            manifest_path=args.manifest,
            refresh_manifest=args.refresh_manifest,
            mode=args.mode,
            only=only,
            emission_intensity=args.emission_intensity,
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
