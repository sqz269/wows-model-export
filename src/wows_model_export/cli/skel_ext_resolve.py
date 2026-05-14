"""CLI wrapper for :func:`wows_model_export.compose.skel_ext_resolve.resolve_decorative_placements`.

Merge HP_ placements + decorative candidates into a unified
accessories JSON. Argv shape::

    wows-skel-ext-resolve <placements_json>
        --candidates <P> --output <P>
        [--record-offsets 0x0,0x...]
        [--manifest P] [--hull-glb P]
        [--accessories-lib P]
        [--include-dock] [--keep-skinned]
        [--hull-margin-m F] [--ship-nation N]
        [--extra-scopes common,...]
        [--keep-degenerate] [--origin-threshold-m F]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.skel_ext_resolve import resolve_decorative_placements
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
        prog="wows-skel-ext-resolve",
        description="Merge HP_ placements + skel_ext decorative candidates "
                    "into a unified accessories JSON.",
    )
    ap.add_argument(
        "placements_json",
        type=Path,
        help="Toolkit-emitted <Ship>_placements.json (HP_-only mounts).",
    )
    ap.add_argument(
        "--candidates",
        type=Path,
        dest="candidates_json",
        required=True,
        help="Toolkit-emitted <Ship>_skel_ext.json (decorative candidates).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output <Ship>_accessories.json path.",
    )
    ap.add_argument(
        "--record-offsets",
        type=str,
        default="0x0",
        help="Comma-separated list of record-offset filters (default: "
             "0x0, the base ship). Pass an empty string to keep every "
             "variant record block.",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to the toolkit's accessories manifest (hash mode).",
    )
    ap.add_argument(
        "--hull-glb",
        type=Path,
        default=None,
        help="Path to the hull GLB; used by the degenerate-origin "
             "filter to score candidates against the hull bounds.",
    )
    ap.add_argument(
        "--accessories-lib",
        type=Path,
        default=None,
        help="Override accessory library root (default: "
             "<workspace>/libraries/accessories).",
    )
    ap.add_argument(
        "--include-dock",
        action="store_true",
        help="Keep dock-only placements (default: drop).",
    )
    ap.add_argument(
        "--keep-skinned",
        action="store_true",
        help="Keep SP_* skinned-mesh placements (default: drop). Enable "
             "when feeding a per-permoflage composer downstream.",
    )
    ap.add_argument(
        "--hull-margin-m",
        type=float,
        default=5.0,
        help="Outside-hull rejection margin in metres (default: 5.0).",
    )
    ap.add_argument(
        "--ship-nation",
        default=None,
        help="ISO-style nation tag (usa/jpn/uk/...) for library lookup "
             "scoping. Auto-derived when unset.",
    )
    ap.add_argument(
        "--extra-scopes",
        default="common",
        help="Comma-separated additional asset scopes for library "
             "lookup beyond ship_nation (default: 'common').",
    )
    ap.add_argument(
        "--keep-degenerate",
        action="store_true",
        help="Keep candidates with degenerate origins (default: drop).",
    )
    ap.add_argument(
        "--origin-threshold-m",
        type=float,
        default=0.001,
        help="Distance-from-origin threshold for the degenerate filter "
             "(default: 0.001m).",
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

    record_offsets: tuple[str, ...] | None
    if args.record_offsets.strip() == "":
        record_offsets = None
    else:
        record_offsets = tuple(
            s.strip() for s in args.record_offsets.split(",") if s.strip()
        )

    extra_scopes = tuple(
        s.strip() for s in args.extra_scopes.split(",") if s.strip()
    )

    try:
        output_path = resolve_decorative_placements(
            args.placements_json,
            candidates_json=args.candidates_json,
            output_json=args.output,
            keep_record_offsets=record_offsets,
            manifest_path=args.manifest,
            hull_glb=args.hull_glb,
            accessories_lib=args.accessories_lib,
            include_dock=args.include_dock,
            drop_skinned=not args.keep_skinned,
            hull_margin_m=args.hull_margin_m,
            ship_nation=args.ship_nation,
            extra_scopes=extra_scopes,
            drop_degenerate=not args.keep_degenerate,
            origin_threshold_m=args.origin_threshold_m,
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

    print(f"resolved decoratives -> {output_path}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
