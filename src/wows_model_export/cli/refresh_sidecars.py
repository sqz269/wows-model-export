"""CLI wrapper for :mod:`wows_model_export.compose.refresh_sidecar_passes`.

Re-runs single GameParams passes over ship sidecars already in the workspace,
without a toolkit call or a re-scaffold. Argv shape::

    wows-refresh-sidecars (<ship label> ... | --all)
        [--pass NAME ...] [--dry-run] [common flags ...]

``--pass`` defaults to ``air_defense``. ``--dry-run`` reports which sidecars
would change and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.refresh_sidecar_passes import PASSES, refresh_sidecar, workspace_ship_labels
from ..errors import ConfigError
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STEP_ERROR,
    EXIT_UNEXPECTED,
    add_common_args,
    resolve_config,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-refresh-sidecars",
        description="Re-run single GameParams passes over existing ship sidecars.",
    )
    ap.add_argument("ships", nargs="*", help="Ship folder labels under <workspace>/ships/.")
    ap.add_argument("--all", action="store_true", help="Every ship folder that holds a sidecar.")
    ap.add_argument(
        "--pass",
        dest="passes",
        action="append",
        choices=sorted(PASSES),
        help="Pass to re-run (repeatable). Default: air_defense.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Report changes, write nothing.")
    add_common_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    workspace = cfg.workspace
    if workspace is None:
        print("config error: no workspace configured", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    labels = workspace_ship_labels(workspace) if args.all else list(args.ships)
    if not labels:
        print("nothing to do: name ship labels or pass --all", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    passes = tuple(args.passes or ["air_defense"])
    failed = 0
    counts: dict[str, int] = {}
    for label in labels:
        try:
            r = refresh_sidecar(label, workspace=workspace, passes=passes, config=cfg, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            failed += 1
            traceback.print_exc(file=sys.stderr)
            print(f"{label}: failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        counts[r.status] = counts.get(r.status, 0) + 1
        extra = f" {r.changed_sections}" if r.changed_sections else ""
        detail = f" ({r.detail})" if r.detail else ""
        print(f"{label}: {r.status}{extra}{detail}")
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"{len(labels)} sidecars: {summary}" + (f", failed {failed}" if failed else ""), file=sys.stderr)
    if failed:
        return EXIT_STEP_ERROR
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(EXIT_UNEXPECTED)


__all__ = ["main"]
