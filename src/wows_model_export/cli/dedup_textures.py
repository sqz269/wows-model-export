"""CLI for de-duplicating textures written byte-for-byte into many folders.

Default mode (store): routes every texture set that recurs byte-for-byte
in two or more export folders (ship, exterior hull, skin pack, accessory,
projectile and building raw-DDS folders) through the workspace's
content-addressed store ``libraries/textures_shared/<key-8>/<name>``,
repoints the sidecar and library-index texture paths at it, and removes
the per-folder copies whose content matches (see
:mod:`wows_model_export.compose.shared_texture_migration`). New exports
already write this layout; the command converts older exports.

``--hardlink`` keeps the older behaviour for arbitrary ``--target``
names: each copy stays at its path and is hardlinked to a shared
canonical (disk-only; paths and consumers unchanged).

Argv shape::

    wows-dedup-textures [--dry-run] [common flags ...]
    wows-dedup-textures --hardlink [--target NAME ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.dedup_textures import DedupResult, dedup_textures
from ..compose.shared_texture_migration import (
    SharedTextureMigrationResult,
    migrate_shared_textures,
)
from ..errors import ConfigError, StepError
from ..shared_textures import SHARED_TEXTURE_NAMES
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
        description="Route every texture set shared byte-for-byte by two or "
                    "more export folders (and the always-routed "
                    + ", ".join(SHARED_TEXTURE_NAMES)
                    + ") through the workspace's shared texture store and repoint "
                    "sidecars at it; or, with --hardlink, hardlink byte-identical "
                    "copies in place.",
    )
    ap.add_argument(
        "--hardlink",
        action="store_true",
        help="Hardlink byte-identical copies of --target names to shared "
             "canonicals instead (paths unchanged, disk only).",
    )
    ap.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="NAME",
        help="With --hardlink: texture filename to dedup (repeatable). "
             "Default: ship_atlas_detail.dds.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without modifying any export files "
             "(store mode still writes its log and hash cache under .cache/).",
    )
    add_common_args(ap)
    return ap


def _summarize_hardlink(r: DedupResult) -> str:
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


def _summarize_store(r: SharedTextureMigrationResult) -> str:
    lines = [
        ("DRY-RUN " if r.dry_run else "")
        + f"sets_scanned={r.sets_scanned} sets_shared={r.sets_shared} "
        f"set_copies_moved={r.set_copies_moved} files_hashed={r.files_hashed}",
        f"files_moved={r.copies_found} store_written={r.store_files_written} "
        f"store_present={r.store_files_present} store_entries={r.store_entries}",
        f"redirect_files={r.redirect_files_written} "
        f"documents_rewritten={r.documents_rewritten} "
        f"references_rewritten={r.references_rewritten}",
        f"copies_removed={r.copies_removed} copies_kept={r.copies_kept} "
        f"copies_deferred={r.copies_deferred} "
        f"legacy_canonicals_removed={r.legacy_canonicals_removed}",
        f"removed={r.bytes_removed / 1e9:.2f} GB (logical)  "
        f"freed={r.bytes_freed / 1e6:.1f} MB (on disk)",
    ]
    for ref in r.store_refs[:10]:
        lines.append(f"store: {ref}")
    if len(r.store_refs) > 10:
        lines.append(f"store: ... +{len(r.store_refs) - 10} more (see the log)")
    if r.log_path is not None:
        lines.append(f"log: {r.log_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)
    if args.target and not args.hardlink:
        print("--target applies to --hardlink only", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        if args.hardlink:
            targets = tuple(args.target) if args.target else ("ship_atlas_detail.dds",)
            hl = dedup_textures(
                workspace=cfg.workspace,
                config=cfg,
                targets=targets,
                dry_run=args.dry_run,
                on_event=printer,
            )
            summary, warnings = _summarize_hardlink(hl), hl.warnings
        else:
            mg = migrate_shared_textures(
                workspace=cfg.workspace,
                config=cfg,
                dry_run=args.dry_run,
                on_event=printer,
            )
            summary, warnings = _summarize_store(mg), mg.warnings
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

    print(summary, file=sys.stderr)
    for warn in warnings[:20]:
        print(f"  warn: {warn}", file=sys.stderr)
    if len(warnings) > 20:
        print(f"  ... +{len(warnings) - 20} more warnings", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
