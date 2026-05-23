"""CLI wrapper for :func:`wows_model_export.compose.teardown_ship.teardown_ship`
and the workspace-wide variants in :mod:`compose.clean_workspace`.

Three modes selected by the first positional / ``--all`` flag::

    wows-teardown-ship <ship>           # per-ship teardown
        [--no-dry-run]
        [--prune-orphans]
        [common flags ...]

    wows-teardown-ship --all            # workspace-wide teardown
        [--no-dry-run]
        [--no-prune-library]
        [common flags ...]

    wows-teardown-ship --all --reextract  # clean + replay every ship
        [--no-dry-run]
        [--no-prune-library]
        [--no-replay-skins]
        [common flags ...]

By default, every mode runs as dry-run (no disk writes; the workspace
mode prints the scan inventory + intended actions). Pass ``--no-dry-run``
to actually perform the operation.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from ..compose.clean_workspace import (
    clean_and_reextract,
    clean_workspace,
    scan_extracted_ships,
)
from ..compose.teardown_ship import teardown_ship
from ..errors import CancelledError, ConfigError, StepError, ToolkitError
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
        description=(
            "Remove a ship's working dir + library bindings, OR (with "
            "--all) every extracted ship under the workspace, "
            "optionally re-running each ship's ingest after the clean."
        ),
    )
    ap.add_argument(
        "ship",
        nargs="?",
        default=None,
        help="Filesystem label (matches <workspace>/ships/<Ship>/). "
             "Omit when passing --all.",
    )
    ap.add_argument(
        "--all",
        dest="all_ships",
        action="store_true",
        help="Workspace-wide mode: tear down every ship under "
             "<workspace>/ships/. Mutually exclusive with a positional "
             "<ship>.",
    )
    ap.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually perform the operation. Without this, the command "
             "only walks the inventory and emits events (per-ship mode) "
             "or prints the planned actions (--all mode).",
    )
    # Per-ship flag.
    ap.add_argument(
        "--prune-orphans",
        action="store_true",
        help="Per-ship mode only: when combined with --no-dry-run, also "
             "delete library asset dirs whose used_by_ships becomes empty.",
    )
    # --all flags.
    ap.add_argument(
        "--reextract",
        action="store_true",
        help="--all mode: after the cleanup, replay each ship's ingest "
             "using args captured in the sidecar (provenance.extract_args, "
             "or a fallback synthesized from ship.wg_ship_full_id).",
    )
    ap.add_argument(
        "--no-prune-library",
        dest="prune_library",
        action="store_false",
        default=True,
        help="--all mode: keep <workspace>/libraries/accessories/. "
             "Default is to rmtree it so the next re-extract rebuilds "
             "shared assets from scratch.",
    )
    ap.add_argument(
        "--no-replay-skins",
        dest="replay_skins",
        action="store_false",
        default=True,
        help="--all --reextract mode: skip the skin-pack replay phase. "
             "Default replays each ship's loose-mod + VFS-variant skins "
             "after the base ingest finishes.",
    )
    add_common_args(ap)
    return ap


def _validate_mode(args: argparse.Namespace, ap: argparse.ArgumentParser) -> None:
    if args.all_ships and args.ship is not None:
        ap.error("--all is mutually exclusive with a positional <ship>")
    if not args.all_ships and args.ship is None:
        ap.error("either provide a <ship> label or pass --all")
    if not args.all_ships and args.reextract:
        ap.error("--reextract requires --all")


def _print_dry_run_inventory(plans: list, on_disk: list[str], *, reextract: bool) -> None:
    """Render the inventory the actual --all run would touch."""
    print("DRY RUN — pass --no-dry-run to actually clean.", file=sys.stderr)
    print(f"  ships on disk:           {len(on_disk)}", file=sys.stderr)
    if reextract:
        print(f"  replay plans recovered:  {len(plans)}", file=sys.stderr)
        unrecoverable = sorted(set(on_disk) - {p.label for p in plans})
        if unrecoverable:
            print(
                f"  unrecoverable (would clean but cannot replay):  "
                f"{len(unrecoverable)}",
                file=sys.stderr,
            )
            for label in unrecoverable[:5]:
                print(f"    - {label}", file=sys.stderr)
            if len(unrecoverable) > 5:
                print(
                    f"    ... and {len(unrecoverable) - 5} more",
                    file=sys.stderr,
                )
        stamped = sum(1 for p in plans if p.provenance_source == "stamped")
        fallback = len(plans) - stamped
        if plans:
            print(
                f"  provenance:              {stamped} stamped, "
                f"{fallback} fallback (auto permoflage)",
                file=sys.stderr,
            )
        total_skins = sum(len(p.skins) for p in plans)
        if total_skins:
            print(
                f"  skin-pack replays:       {total_skins} (across "
                f"{sum(1 for p in plans if p.skins)} ship(s))",
                file=sys.stderr,
            )
        print("", file=sys.stderr)
        print("Replay plans (first 10):", file=sys.stderr)
        for p in plans[:10]:
            marker = "*" if p.provenance_source == "fallback" else " "
            print(
                f"  {marker} {p.label:35s}  vehicle={p.vehicle:30s} "
                f"perm={p.permoflage}  skins={len(p.skins)}",
                file=sys.stderr,
            )
        if len(plans) > 10:
            print(f"  ... and {len(plans) - 10} more", file=sys.stderr)
        if any(p.provenance_source == "fallback" for p in plans):
            print(
                "\n  * = no stamped provenance; will replay with "
                "permoflage=\"auto\". Run the ship's extract once "
                "after stamping (any post-this-build ingest) for "
                "lossless replay.",
                file=sys.stderr,
            )


def _format_all_report(report: dict[str, Any], *, with_reextract: bool) -> str:
    bits: list[str] = []
    bits.append(f"torn_down={len(report.get('ships_torn_down') or [])}")
    if report.get("library_wiped"):
        bits.append("library_wiped=yes")
    elif report.get("library_existed"):
        bits.append("library_wiped=skipped")
    if with_reextract:
        bits.append(
            f"reextracted={len(report.get('ships_reextracted') or [])}"
        )
        bits.append(
            f"skins_replayed={len(report.get('skins_replayed') or [])}"
        )
    errors = report.get("errors") or []
    if errors:
        bits.append(f"errors={len(errors)}")
    return "  ".join(bits)


def _run_per_ship(args: argparse.Namespace, cfg, printer) -> int:
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
        print(
            f"\nerror: step {e.step!r} failed: {e.detail or e}",
            file=sys.stderr,
        )
        return EXIT_STEP_ERROR
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(
            f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr,
        )
        return EXIT_UNEXPECTED

    mode = "DRY-RUN" if not args.no_dry_run else "DELETED"
    print(
        f"teardown {args.ship!r} {mode}  "
        f"ship_dir_existed={result.get('ship_dir_existed')}  "
        f"exclusive={len(result.get('assets_exclusive') or [])}  "
        f"shared_pruned={len(result.get('assets_shared') or [])}  "
        f"orphans_pruned={len(result.get('orphans_pruned') or [])}",
        file=sys.stderr,
    )
    return EXIT_OK


def _run_all(args: argparse.Namespace, cfg, printer) -> int:
    if not args.no_dry_run:
        plans = scan_extracted_ships(cfg.workspace)
        ships_root = cfg.workspace / "ships"
        on_disk = (
            sorted(
                p.name for p in ships_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
            if ships_root.is_dir()
            else []
        )
        _print_dry_run_inventory(plans, on_disk, reextract=args.reextract)
        return EXIT_OK

    try:
        if args.reextract:
            report = clean_and_reextract(
                workspace=cfg.workspace,
                config=cfg,
                prune_library=args.prune_library,
                replay_skins=args.replay_skins,
                on_event=printer,
            )
        else:
            report = clean_workspace(
                workspace=cfg.workspace,
                config=cfg,
                prune_library=args.prune_library,
                on_event=printer,
            )
    except CancelledError as e:
        print(f"\ncancelled at step {e.step!r}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except StepError as e:
        print(
            f"\nerror: step {e.step!r} failed: {e.detail or e}",
            file=sys.stderr,
        )
        return EXIT_STEP_ERROR
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(
            f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr,
        )
        return EXIT_UNEXPECTED

    print(
        f"\nteardown --all done  "
        f"{_format_all_report(report, with_reextract=args.reextract)}",
        file=sys.stderr,
    )
    for err in report.get("errors") or ():
        print(
            f"  ! {err['phase']:>15s}  {err['label']}: {err['message']}",
            file=sys.stderr,
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    _validate_mode(args, ap)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    if args.all_ships:
        return _run_all(args, cfg, printer)
    return _run_per_ship(args, cfg, printer)


__all__ = ["main"]
