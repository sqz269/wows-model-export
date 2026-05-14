"""CLI wrapper for :func:`wows_model_export.compose.decal_library.build_decal_library`.

Mirror WG's ``dyndecals/`` into ``<library_root>/`` with a manifest.
Argv shape::

    wows-build-decal-library
        [--library-root P]
        [--source-dir P]
        [--patch-id ID]
        [--force]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.decal_library import build_decal_library
from ..errors import ConfigError, StepError, ToolkitError
from ..types import DecalLibraryResult
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
        prog="wows-build-decal-library",
        description="Mirror WG dyndecals/ into the library with a manifest.",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Where to build the library (default: "
             "<workspace>/libraries/decals).",
    )
    ap.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Source dyndecals/ directory. Defaults to the WG install's "
             "pre-extracted res_unpack/dyndecals/.",
    )
    ap.add_argument(
        "--patch-id",
        default=None,
        help="Optional patch identifier stamped into the manifest.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Copy every file regardless of mtime/size compare.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: DecalLibraryResult) -> str:
    bits = [
        f"library {result.library_root}",
        f"copied={result.decals_copied}",
        f"skipped={result.decals_skipped}",
        f"manifest={result.manifest_path.name}",
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
        result = build_decal_library(
            workspace=cfg.workspace,
            config=cfg,
            library_root=args.library_root,
            force=args.force,
            on_event=printer,
            source_dir=args.source_dir,
            patch_id=args.patch_id,
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
