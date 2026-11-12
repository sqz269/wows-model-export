"""CLI wrapper for :func:`wows_model_export.compose.ingest_ship.ingest_ship`.

Drives the one-shot per-ship pipeline composer end-to-end. Argv shape::

    wows-ingest-ship <ship>
        [--workspace P] [--label LBL]
        [--non-interactive]
        [--class-override CO] [--ship-key-suffix S]
        [--build-library] [--rebuild-library]
        [--and-publish --publish-target P] [--publish-force]
        [--variant-permoflage ID]
        [--toolkit-ship NAME] [--gameparams-ship-id ID]
        [--hull UPGRADE]
        [common flags ...]

List mode ingests several new ships in one process
(:func:`wows_model_export.compose.ingest_ships.ingest_ships`)::

    wows-ingest-ship <ship> <ship> ... [--jobs N] [shared options]
    wows-ingest-ship --ships-file FILE [<ship> ...] [--jobs N] [shared options]
        [--report JSON]

A ships-file line is one ship's own part of a single-ship command line:
``<ship> [--label L] [--toolkit-ship T] [--gameparams-ship-id G]
[--hull H] [--variant-permoflage P] [--exterior-hulls | --no-exterior-hulls]
[--class-override C] [--ship-key-suffix S]``; ``#`` starts a comment.
Options a line leaves out come from the command line.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import traceback
from pathlib import Path
from typing import Any

from ..compose.ingest_ship import ingest_ship
from ..compose.ingest_ships import (
    MAX_INGEST_JOBS,
    ShipIngestSpec,
    check_specs,
    ingest_ships,
)
from ..errors import ConfigError, StepError, ToolkitError
from ..types import IngestResult
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_STEP_ERROR,
    EXIT_UNEXPECTED,
    add_common_args,
    build_printer,
    resolve_config,
)

_LIST_MODE_HELP = f"""\
several ships at once (list mode):
  Give several ships, or --ships-file, to ingest them in one process,
  up to {MAX_INGEST_JOBS} at a time with --jobs. Each ingest is the one the
  single-ship command runs for that ship, except that it never prompts (an
  ambiguous ship name fails that ship) and the accessory library
  (--build-library / --rebuild-library) is built once after the group,
  followed by the post-library sidecar refresh of every ship ingested. A
  failed ship is reported and the others go on; the exit status is 1 when
  any ship or the library build failed.

  A --ships-file line holds one ship's own part of a single-ship command:

      <ship> [--label L] [--toolkit-ship T] [--gameparams-ship-id G]
             [--hull H] [--variant-permoflage P]
             [--exterior-hulls | --no-exterior-hulls]
             [--class-override C] [--ship-key-suffix S]

  Blank lines and text after '#' are ignored; values with spaces are
  quoted as in a shell. --hull, --variant-permoflage, --exterior-hulls,
  --class-override and --ship-key-suffix on the command line apply to
  every ship whose line does not set them. --label, --toolkit-ship and
  --gameparams-ship-id name one ship, so in list mode they go on its line.
  List mode does not publish: publish the group afterwards with
  wows-publish --target <root> --only <labels>.

  Run one group at a time. The ingests are threads of this process because
  the library build and the toolkit, camo and particle caches lock only
  inside one process: never start several ingest processes side by side.
"""


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-ingest-ship",
        description=ingest_ship.__doc__.splitlines()[0] if ingest_ship.__doc__ else None,
        epilog=_LIST_MODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "ship",
        nargs="*",
        help="Display name (Montana), model_dir (ASB017_Montana_1945), "
             "or GameParams Vehicle id. Several ships: list mode (below).",
    )
    ap.add_argument(
        "--label",
        default=None,
        help="Force a filesystem folder label (e.g. Baltimore_Old). "
             "Default: the input name, or the user's choice on ambiguity.",
    )
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="Don't prompt. Fail on ambiguous identity. (List mode never "
             "prompts.)",
    )
    ap.add_argument(
        "--class-override",
        default=None,
        help="Override toolkit species mapping (CA/CL/BB/DD/CV/SS/...).",
    )
    ap.add_argument(
        "--ship-key-suffix",
        default=None,
        help="Trailing ship_key segment (e.g. 'B' for hull variants).",
    )
    ap.add_argument(
        "--build-library",
        action="store_true",
        help="After ingest, refresh the fleet-wide accessory library. "
             "Purely additive; existing assets are kept. In list mode it "
             "is built once, after every ship.",
    )
    ap.add_argument(
        "--rebuild-library",
        action="store_true",
        help="Implies --build-library. Forces a full regenerate of "
             "every asset GLB + DDS.",
    )
    ap.add_argument(
        "--and-publish",
        action="store_true",
        help="After ingest (and library build, if any), publish this "
             "ship's outputs. Requires --publish-target. Single ship only.",
    )
    ap.add_argument(
        "--publish-target",
        type=Path,
        default=None,
        help="Consumer-side destination root. Required with "
             "--and-publish.",
    )
    ap.add_argument(
        "--publish-force",
        action="store_true",
        help="Implies --and-publish. Bypasses the mtime+size cache.",
    )
    ap.add_argument(
        "--variant-permoflage",
        default="auto",
        help="Mesh-swap permoflage routing: 'auto' (default) picks "
             "Vehicle.nativePermoflage when an Exterior carries a full "
             "hull mesh swap; pass an Exterior id (e.g. "
             "PJES478_RED_TAKAO) for a non-default variant; 'none' to "
             "disable.",
    )
    ap.add_argument(
        "--exterior-hulls",
        action="store_true",
        help="HullDelta: export each hull-swap exterior's variant hull "
             "GLB-only into models/exteriors/<exterior_id>_hull.glb and "
             "stamp the exteriors[] hull field. One extra export-ship "
             "per hull-swap exterior; idempotent (skip-on-existence). "
             "Base scaffolds only.",
    )
    ap.add_argument(
        "--toolkit-ship",
        default=None,
        help="Override the name passed to wowsunpack export-ship / "
             "armor-json / ammo. Useful when disambiguation forced a "
             "model_dir like ASC017_Baltimore_1944 but the folder "
             "should stay friendly.",
    )
    ap.add_argument(
        "--gameparams-ship-id",
        default=None,
        help="Override the GameParams Vehicle id used for autofill + "
             "permoflage discovery. Required when multiple Vehicles "
             "share one model_dir.",
    )
    ap.add_argument(
        "--hull",
        default=None,
        help="Hull upgrade to render: a _Hull upgrade id (e.g. "
             "PJUH432_Furutaka_1930) or a hull component name (B_Hull) "
             "of the ship named by --gameparams-ship-id, or else by the "
             "ship name as a GameParams id, a param index or a model_dir "
             "only one Vehicle uses; any other ship name, or a value that "
             "names none of its hulls, fails the ingest. The mount "
             "placements and the sidecar's top-level mounts follow it; "
             "the armor table and the hull mesh stay the lowest upgrade "
             "id's. Default: the lowest upgrade id (normally the stock "
             "hull).",
    )
    g = ap.add_argument_group("list mode")
    g.add_argument(
        "--ships-file",
        type=Path,
        default=None,
        help="Ingest the ships listed in this file, one per line (format "
             "below), after any ships given on the command line.",
    )
    g.add_argument(
        "--jobs", "-j",
        type=int,
        default=1,
        help=f"Ingests to run at once in list mode, 1 to {MAX_INGEST_JOBS} "
             "(default 1). Each is a thread of this process. A single "
             "ship is one ingest whatever the value.",
    )
    g.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the list-mode report (per ship: label, ok, error, "
             "warnings, step timings, wall time; the library build; the "
             "group's wall time) to this JSON file.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: IngestResult) -> str:
    bits = [f"ingest {result.label!r} ({result.ship_id}) ok"]
    if result.scaffold and result.scaffold.sidecar_path:
        bits.append(f"sidecar={result.scaffold.sidecar_path.name}")
    if result.library_refreshed:
        bits.append("library_refreshed")
    if result.published_to is not None:
        bits.append(f"published->{result.published_to}")
    if result.warnings:
        bits.append(f"warnings={len(result.warnings)}")
    total = result.step_timings_ms.get("total")
    if total is not None:
        bits.append(f"total={total:.0f}ms")
    return "  ".join(bits)


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------


class _LineError(ValueError):
    pass


class _LineParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _LineError(message)


def _line_parser() -> argparse.ArgumentParser:
    """Parser for one ships-file line: the per-ship options of the
    single-ship command. ``None`` means "take the command line's"."""
    lp = _LineParser(prog="ships-file line", add_help=False)
    lp.add_argument("ship")
    lp.add_argument("--label", default=None)
    lp.add_argument("--toolkit-ship", default=None)
    lp.add_argument("--gameparams-ship-id", default=None)
    lp.add_argument("--hull", default=None)
    lp.add_argument("--variant-permoflage", default=None)
    lp.add_argument("--exterior-hulls", action=argparse.BooleanOptionalAction, default=None)
    lp.add_argument("--class-override", default=None)
    lp.add_argument("--ship-key-suffix", default=None)
    return lp


def _spec(args: argparse.Namespace, ship: str, line: argparse.Namespace | None = None) -> ShipIngestSpec:
    def pick(name: str) -> Any:
        value = getattr(line, name, None) if line is not None else None
        return getattr(args, name) if value is None else value

    return ShipIngestSpec(
        ship_input=ship,
        label=getattr(line, "label", None),
        toolkit_ship=getattr(line, "toolkit_ship", None),
        gameparams_ship_id=getattr(line, "gameparams_ship_id", None),
        hull=pick("hull"),
        variant_permoflage=pick("variant_permoflage"),
        export_exterior_hulls=bool(pick("exterior_hulls")),
        class_override=pick("class_override"),
        ship_key_suffix=pick("ship_key_suffix"),
    )


def _read_ships_file(path: Path, args: argparse.Namespace) -> list[ShipIngestSpec]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ValueError(f"cannot read --ships-file {path}: {e}") from e
    lp = _line_parser()
    specs: list[ShipIngestSpec] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError as e:
            raise ValueError(f"{path}:{n}: {e}") from e
        if not tokens:
            continue
        try:
            line = lp.parse_args(tokens)
        except _LineError as e:
            raise ValueError(f"{path}:{n}: {e}") from e
        specs.append(_spec(args, line.ship, line))
    return specs


def _list_specs(args: argparse.Namespace) -> list[ShipIngestSpec]:
    """The group's ships, or :class:`ValueError` for a bad command line."""
    if args.and_publish or args.publish_force or args.publish_target is not None:
        raise ValueError(
            "list mode does not publish; publish the group afterwards "
            "with wows-publish --target <root> --only <labels>"
        )
    one_ship = [
        flag for flag, value in (
            ("--label", args.label),
            ("--toolkit-ship", args.toolkit_ship),
            ("--gameparams-ship-id", args.gameparams_ship_id),
        ) if value is not None
    ]
    if one_ship:
        raise ValueError(
            f"{', '.join(one_ship)} "
            f"{'names' if len(one_ship) == 1 else 'name'} one ship; in list "
            f"mode put {'it' if len(one_ship) == 1 else 'them'} on that "
            f"ship's --ships-file line"
        )
    specs = [_spec(args, ship) for ship in args.ship]
    if args.ships_file is not None:
        specs += _read_ships_file(args.ships_file, args)
    if not specs:
        raise ValueError(f"--ships-file {args.ships_file} lists no ships")
    return specs


def _ship_line(entry: dict[str, Any]) -> str:
    if not entry["ok"]:
        where = f" at step {entry['step']!r}" if entry.get("step") else ""
        return f"FAILED {entry['label']!r} ({entry['ship']}){where}: {entry['error']}"
    bits = [f"ingest {entry['label']!r} ({entry['toolkit_ship']}) ok"]
    if entry.get("sidecar"):
        bits.append(f"sidecar={Path(entry['sidecar']).name}")
    if entry["warnings"]:
        bits.append(f"warnings={len(entry['warnings'])}")
    if entry.get("wall_s") is not None:
        bits.append(f"wall={entry['wall_s']:.0f}s")
    return "  ".join(bits)


def _run_list(args: argparse.Namespace, cfg) -> int:
    try:
        specs = _list_specs(args)
        check_specs(specs)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)
    jobs = min(args.jobs, len(specs))
    print(
        f"ingesting {len(specs)} ship(s), {jobs} at a time"
        + ("; accessory library once at the end" if args.build_library or args.rebuild_library else ""),
        file=sys.stderr,
    )

    def on_start(i: int, total: int, spec: ShipIngestSpec) -> None:
        print(f"[{i + 1}/{total}] ingest {spec.folder_label} ({spec.ship_input})", file=sys.stderr)

    def on_done(entry: dict[str, Any]) -> None:
        print(_ship_line(entry), file=sys.stderr)
        for warn in entry.get("warnings") or ():
            print(f"  warn: {warn}", file=sys.stderr)

    try:
        report = ingest_ships(
            specs,
            workspace=cfg.workspace,
            config=cfg,
            jobs=jobs,
            build_library=args.build_library or args.rebuild_library,
            rebuild_library=args.rebuild_library,
            on_event=printer,
            on_start=on_start,
            on_done=on_done,
        )
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    ships = report["ships"]
    ok = [e for e in ships if e["ok"]]
    print("", file=sys.stderr)
    for entry in ships:
        print(_ship_line(entry), file=sys.stderr)
        # The ship's ingest warnings were printed as it ended; these came later.
        for warn in entry.get("library_warnings") or ():
            print(f"  warn (post-library refresh): {warn}", file=sys.stderr)
    lib = report["library"]
    lib_failed = False
    if lib is not None:
        if "skipped" in lib:
            print(f"accessory library: skipped ({lib['skipped']})", file=sys.stderr)
        elif lib["ok"]:
            print(
                f"accessory library: ok ({'rebuild' if lib['rebuild'] else 'additive'}, "
                f"{lib['ships']} ship(s) refreshed, {lib['wall_s']:.0f}s)",
                file=sys.stderr,
            )
            for warn in lib.get("warnings") or ():
                print(f"  warn: {warn}", file=sys.stderr)
        else:
            lib_failed = True
            print(f"accessory library: FAILED {lib['error']}", file=sys.stderr)
    wall = report["wall_s"]
    print(
        f"ingested {len(ok)}/{len(ships)} ship(s), {report['jobs']} at a time"
        + (" (cancelled)" if report["cancelled"] else "")
        + f"; wall {wall['total']:.0f}s (ingests {wall['ingests']:.0f}s, "
          f"library {wall['library']:.0f}s)",
        file=sys.stderr,
    )
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"warn: could not write --report {args.report}: {e}", file=sys.stderr)
    if len(ok) != len(ships) or lib_failed or report["cancelled"]:
        return EXIT_STEP_ERROR
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    if not args.ship and args.ships_file is None:
        ap.error("give a ship, several ships, or --ships-file")
    list_mode = args.ships_file is not None or len(args.ship) > 1
    if not list_mode and args.report is not None:
        ap.error("--report is for list mode (several ships or --ships-file)")
    if not 1 <= args.jobs <= MAX_INGEST_JOBS:
        ap.error(f"--jobs must be 1 to {MAX_INGEST_JOBS}, not {args.jobs}")
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if list_mode:
        return _run_list(args, cfg)

    printer = build_printer(args)

    try:
        result = ingest_ship(
            args.ship[0],
            workspace=cfg.workspace,
            config=cfg,
            forced_label=args.label,
            interactive=not args.non_interactive,
            class_override=args.class_override,
            ship_key_suffix=args.ship_key_suffix,
            build_library=args.build_library or args.rebuild_library,
            rebuild_library=args.rebuild_library,
            and_publish=args.and_publish or args.publish_force,
            publish_target=args.publish_target,
            publish_force=args.publish_force,
            variant_permoflage=args.variant_permoflage,
            export_exterior_hulls=args.exterior_hulls,
            toolkit_ship_override=args.toolkit_ship,
            gameparams_ship_id=args.gameparams_ship_id,
            hull=args.hull,
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
