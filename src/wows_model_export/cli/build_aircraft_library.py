"""CLI wrapper for :func:`wows_model_export.compose.aircraft_library.build_aircraft_library`.

Build / refresh the aircraft library. Argv shape::

    wows-build-aircraft-library [MODEL_OR_KEY ...]
        [--list FILE] [--all]
        [--library-root P] [--rebuild] [--no-gameparams]
        [--manifest P] [--refresh-manifest]
        [--validate] [--stats-only]
        [common flags ...]

Each MODEL_OR_KEY is an aircraft model folder name
(``AAB702_Grumman_TBF_proto``) or a GameParams aircraft key
(``PAAB508_Enterprise_stock``, resolved to its model). ``--list`` reads more
of them from a file (one per line, ``#`` comments allowed). With no
selection and no ``--all`` the index is rewritten from the airframes
already on disk. ``--validate`` only checks the existing index and its
stats sidecar. ``--stats-only`` rewrites only the stats sidecar
(``stats.json``: per-plane stats and loadouts, from GameParams) for the
airframes of the existing index.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.aircraft_library import build_aircraft_library, validate_index
from ..compose.aircraft_stats import STATS_FILE, build_aircraft_stats, validate_stats
from ..errors import ConfigError, StepError, ToolkitError
from ..types import AircraftLibraryResult
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
        prog="wows-build-aircraft-library",
        description="Build / refresh the aircraft library (libraries/aircraft).",
    )
    ap.add_argument(
        "select",
        nargs="*",
        metavar="MODEL_OR_KEY",
        help="Aircraft model folder names and/or GameParams aircraft keys.",
    )
    ap.add_argument(
        "--list",
        type=Path,
        default=None,
        dest="list_file",
        help="File with more models / keys, one per line ('#' starts a comment).",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Export every aircraft model in the game files.",
    )
    ap.add_argument(
        "--library-root",
        type=Path,
        default=None,
        help="Where to build the library (default: <workspace>/libraries/aircraft).",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Re-export selected airframes that are already on disk.",
    )
    ap.add_argument(
        "--no-gameparams",
        action="store_true",
        help="Skip the GameParams join (no aircraft entries per model; keys "
             "in the selection then do not resolve).",
    )
    ap.add_argument("--manifest", type=Path, default=None, help="VFS manifest JSON path.")
    ap.add_argument("--refresh-manifest", action="store_true", help="Rebuild the VFS manifest first.")
    ap.add_argument(
        "--validate",
        action="store_true",
        help="Only validate the existing index against its files, and the stats sidecar.",
    )
    ap.add_argument(
        "--stats-only",
        action="store_true",
        help="Only rewrite stats.json (per-plane stats and loadouts from GameParams) "
             "for the airframes of the existing index.",
    )
    add_common_args(ap)
    return ap


def _read_list(path: Path) -> list[str]:
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _summarize(result: AircraftLibraryResult) -> str:
    bits = [
        f"library {result.library_root}",
        f"built={result.aircraft_built}",
        f"indexed={result.aircraft_indexed}",
        f"available={result.aircraft_available}",
    ]
    if result.stats_entries is not None:
        bits.append(f"stats={result.stats_entries}")
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    total = result.step_timings_ms.get("total")
    if total is not None:
        bits.append(f"total={total:.0f}ms")
    return "  ".join(bits)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    lib_root = args.library_root or (Path(cfg.workspace) / "libraries" / "aircraft")
    if args.validate:
        problems = validate_index(Path(lib_root), workspace=Path(cfg.workspace))
        for p in problems:
            print(f"  problem: {p}", file=sys.stderr)
        print(f"{lib_root / 'index.json'}: {len(problems)} problem(s)", file=sys.stderr)
        stats_problems = validate_stats(Path(lib_root))
        for p in stats_problems:
            print(f"  problem: {p}", file=sys.stderr)
        print(f"{lib_root / STATS_FILE}: {len(stats_problems)} problem(s)", file=sys.stderr)
        return EXIT_OK if not (problems or stats_problems) else EXIT_STEP_ERROR

    if args.stats_only:
        try:
            stats = build_aircraft_stats(
                workspace=cfg.workspace, config=cfg, library_root=args.library_root,
                on_event=build_printer(args),
            )
        except StepError as e:
            print(f"\nerror: step {e.step!r} failed: {e.detail or e}", file=sys.stderr)
            return EXIT_STEP_ERROR
        print(f"{stats.stats_path}: {stats.entries} entr(ies) over {stats.airframes} airframe(s)",
              file=sys.stderr)
        for warn in stats.warnings:
            print(f"  warn: {warn}", file=sys.stderr)
        return EXIT_OK if not stats.warnings else EXIT_STEP_ERROR

    select = list(args.select)
    if args.list_file:
        select += _read_list(args.list_file)

    printer = build_printer(args)
    try:
        result = build_aircraft_library(
            select=select,
            all_aircraft=args.all,
            workspace=cfg.workspace,
            config=cfg,
            library_root=args.library_root,
            rebuild=args.rebuild,
            gameparams=not args.no_gameparams,
            manifest_path=args.manifest,
            refresh_manifest=args.refresh_manifest,
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


if __name__ == "__main__":
    raise SystemExit(main())
