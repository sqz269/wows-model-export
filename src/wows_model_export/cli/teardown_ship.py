"""CLI wrapper for :func:`wows_model_export.compose.teardown_ship.teardown_ship`.

Remove a ship's per-ship working directory + library bindings. Argv shape::

    wows-teardown-ship <ship>
        [--no-dry-run]
        [--prune-orphans]
        [common flags ...]

By default, runs in dry-run mode (emits events but doesn't touch disk).
Pass ``--no-dry-run`` to actually delete.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.teardown_ship import teardown_ship
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
        prog="wows-teardown-ship",
        description="Remove a ship's working dir + library bindings.",
    )
    ap.add_argument(
        "ship",
        help="Filesystem label (matches <workspace>/<Ship>/).",
    )
    ap.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually delete; without this, the composer only walks "
             "the inventory and emits events.",
    )
    ap.add_argument(
        "--prune-orphans",
        action="store_true",
        help="When combined with --no-dry-run, also delete library "
             "asset dirs whose used_by_ships becomes empty.",
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
        result = teardown_ship(
            args.ship,
            workspace=cfg.workspace,
            config=cfg,
            dry_run=not args.no_dry_run,
            prune_orphans=args.prune_orphans,
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

    mode = "DRY-RUN" if not args.no_dry_run else "DELETED"
    print(
        f"teardown {args.ship!r} {mode}  "
        f"ship_dir_exists={result.get('ship_dir_exists')}  "
        f"library_assets_unbound={len(result.get('library_assets_unbound') or [])}  "
        f"orphans_pruned={len(result.get('orphans_pruned') or [])}",
        file=sys.stderr,
    )
    return EXIT_OK


__all__ = ["main"]
