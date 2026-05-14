"""CLI wrapper for :func:`wows_model_export.compose.accessories_scan.scan_legacy_glb`.

Walk a legacy gamemodels3d.com GLB and emit the hardpoint / accessory
scan JSON used by legacy-mode decoratives resolution. Argv shape::

    wows-accessories-scan <legacy_glb> --output <P>
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.accessories_scan import scan_legacy_glb
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
        prog="wows-accessories-scan",
        description="Walk a legacy gamemodels3d.com <Ship>_visual.glb "
                    "and emit the accessory scan JSON.",
    )
    ap.add_argument(
        "legacy_glb",
        type=Path,
        help="Path to <Ship>_visual.glb (gamemodels3d.com export).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for the scan JSON (convention: "
             "<Ship>_accessories_scan.json beside the input GLB).",
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
        output_path = scan_legacy_glb(
            args.legacy_glb,
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

    print(f"scan -> {output_path}", file=sys.stderr)
    return EXIT_OK


__all__ = ["main"]
