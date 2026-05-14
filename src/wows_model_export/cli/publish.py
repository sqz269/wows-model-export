"""CLI wrapper for :func:`wows_model_export.compose.publish.publish`.

Copy pipeline artifacts to a consumer target (Unity Assets/Ships/
Pipeline/ historically, but any destination works). Argv shape::

    wows-publish --target <dir>
        [--only <ship> ...]
        [--domains ships,library,projectiles,decals]
        [--force]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.publish import publish
from ..errors import ConfigError, StepError, ToolkitError
from ..types import PublishResult
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
        prog="wows-publish",
        description="Copy pipeline artifacts to a consumer target.",
    )
    ap.add_argument(
        "--target",
        type=Path,
        required=True,
        dest="target_dir",
        help="Consumer-side destination root (e.g. Unity "
             "Assets/Ships/Pipeline/).",
    )
    ap.add_argument(
        "--only",
        nargs="*",
        default=None,
        metavar="SHIP",
        help="Restrict the ships domain to this subset. Default: every "
             "in-tree ship.",
    )
    ap.add_argument(
        "--domains",
        default="ships,library,projectiles,decals",
        help="Comma-separated list of domains to publish (default: "
             "ships,library,projectiles,decals).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Copy every file regardless of mtime/size compare.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: PublishResult) -> str:
    bits = [f"published -> {result.target_dir}"]
    for name in ("ships", "library", "projectiles", "decals"):
        counts = getattr(result, name)
        bits.append(
            f"{name}=copied:{counts.copied}/skipped:{counts.skipped}"
            + (f"/deleted:{counts.deleted}" if counts.deleted else "")
        )
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    return "  ".join(bits)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    only_ships: tuple[str, ...] | None
    only_ships = tuple(args.only) if args.only else None

    domains = tuple(s.strip() for s in args.domains.split(",") if s.strip())

    try:
        result = publish(
            target_dir=args.target_dir,
            workspace=cfg.workspace,
            config=cfg,
            only_ships=only_ships,
            domains=domains,
            force=args.force,
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
