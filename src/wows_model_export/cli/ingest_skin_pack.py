"""CLI wrapper for :func:`wows_model_export.compose.skin_pack.ingest_skin_pack`.

Ingest a skin pack (loose-mod folder or VFS-variant slice). Argv shape::

    wows-ingest-skin-pack <source>
        --ship <ship_id>
        [--skin-id ID]
        [--display-name NAME]
        [--source-kind loose_mod|vfs_variant|auto]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.skin_pack import ingest_skin_pack
from ..errors import ConfigError, StepError, ToolkitError
from ..types import SkinPackResult
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
        prog="wows-ingest-skin-pack",
        description="Ingest a skin pack and append it to the ship's sidecar.",
    )
    ap.add_argument(
        "source",
        help="Loose-mod folder path OR VFS-variant identifier (variant "
             "asset_id like ASC080_Baltimore_1944_Azur, GameParams id "
             "like PJSC705, or Exterior id like PJES477_ARP_TAKAO).",
    )
    ap.add_argument(
        "--ship",
        required=True,
        dest="ship_id",
        help="Filesystem label of the ship whose sidecar gets the new "
             "skins[] entry.",
    )
    ap.add_argument(
        "--skin-id",
        default=None,
        help="Stable identifier for the skin entry. Auto-derived when "
             "not given (loose mod folder name / exterior id).",
    )
    ap.add_argument(
        "--display-name",
        default=None,
        help="Human-friendly label shown in consumers. Defaults to the "
             "skin id when unset.",
    )
    ap.add_argument(
        "--source-kind",
        choices=("loose_mod", "vfs_variant", "auto"),
        default="auto",
        help="Force the source type. 'auto' (default) sniffs based on "
             "whether the source is an existing directory.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: SkinPackResult) -> str:
    bits = [
        f"skin pack {result.skin_id!r} -> {result.ship_id}",
        f"source={result.source}",
    ]
    if result.swizzled:
        bits.append("swizzled")
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    bits.append(f"sidecar={result.sidecar_path.name}")
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
        result = ingest_skin_pack(
            args.source,
            ship_id=args.ship_id,
            workspace=cfg.workspace,
            config=cfg,
            skin_id=args.skin_id,
            display_name=args.display_name,
            source_kind=args.source_kind,
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
