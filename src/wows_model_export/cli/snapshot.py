"""CLI wrapper for :func:`wows_model_export.compose.snapshot.snapshot`.

Build the Vehicles + permoflages picker payload (consumed by the Extract
webview). Argv shape::

    wows-snapshot --output <P>
        [--refresh]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.snapshot import snapshot
from ..errors import ConfigError, StepError, ToolkitError
from ..types import SnapshotResult
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
        prog="wows-snapshot",
        description="Build the Vehicles + permoflages picker payload.",
    )
    ap.add_argument(
        "--output",
        type=Path,
        required=True,
        dest="output_path",
        help="JSON output destination.",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Force a fresh GameParams ensure_dump (skip cache). Use "
             "after a game patch.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: SnapshotResult) -> str:
    bits = [
        f"snapshot -> {result.output_path}",
        f"vehicles={result.vehicles_count}",
        f"permoflages={result.permoflages_count}",
    ]
    if result.cache_refreshed:
        bits.append("cache_refreshed")
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
        result = snapshot(
            output_path=args.output_path,
            config=cfg,
            refresh=args.refresh,
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
    return EXIT_OK


__all__ = ["main"]
