"""CLI wrapper for :func:`wows_model_export.compose.dedup_textures.dedup_textures`.

Reclaims disk by hardlinking byte-identical duplicate textures (chiefly
the ~16.8 MB engine-global ``ship_atlas_detail.dds`` dumped into every
accessory/ship dir — ~20 GB on a full workspace) to shared canonicals.
Zero consumer impact: each file stays at its path with identical bytes.

Argv shape::

    wows-dedup-textures
        [--target NAME ...]      (default: ship_atlas_detail.dds)
        [--dry-run]
        [common flags ...]
"""
from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.dedup_textures import DedupResult, dedup_textures
from ..errors import ConfigError, StepError
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
        prog="wows-dedup-textures",
        description="Hardlink byte-identical duplicate textures (e.g. "
                    "ship_atlas_detail.dds) to shared canonicals to reclaim disk.",
    )
    ap.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="NAME",
        help="Texture filename to dedup (repeatable). Default: "
             "ship_atlas_detail.dds.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be reclaimed without modifying any files.",
    )
    add_common_args(ap)
    return ap


def _summarize(r: DedupResult) -> str:
    bits = [
        ("DRY-RUN " if r.dry_run else "") + f"scanned={r.files_scanned}",
        f"linked={r.files_linked}",
        f"already={r.already_linked}",
        f"canonicals={r.canonicals_created}",
        f"reclaimed={r.bytes_reclaimed / 1e9:.2f} GB",
    ]
    if r.skipped_mismatch:
        bits.append(f"mismatch={r.skipped_mismatch}")
    if r.skipped_error:
        bits.append(f"errors={r.skipped_error}")
    return "  ".join(bits)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)
    targets = tuple(args.target) if args.target else ("ship_atlas_detail.dds",)

    try:
        result = dedup_textures(
            workspace=cfg.workspace,
            config=cfg,
            targets=targets,
            dry_run=args.dry_run,
            on_event=printer,
        )
    except StepError as e:
        print(f"\nerror: step {e.step!r} failed: {e.detail or e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except ConfigError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    print(_summarize(result), file=sys.stderr)
    for warn in result.warnings[:20]:
        print(f"  warn: {warn}", file=sys.stderr)
    if len(result.warnings) > 20:
        print(f"  ... +{len(result.warnings) - 20} more warnings", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
