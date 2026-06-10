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
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.ingest_ship import ingest_ship
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


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-ingest-ship",
        description=ingest_ship.__doc__.splitlines()[0] if ingest_ship.__doc__ else None,
    )
    ap.add_argument(
        "ship",
        help="Display name (Montana), model_dir (ASB017_Montana_1945), "
             "or GameParams Vehicle id.",
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
        help="Don't prompt. Fail on ambiguous identity.",
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
             "Purely additive; existing assets are kept.",
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
             "ship's outputs. Requires --publish-target.",
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    printer = build_printer(args)

    try:
        result = ingest_ship(
            args.ship,
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
