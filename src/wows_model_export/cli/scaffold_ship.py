"""CLI wrapper for :func:`wows_model_export.compose.scaffold_ship.scaffold_ship`.

Drives the per-ship scaffolder: ``export-ship`` + ``armor`` + ``ammo`` +
sidecar build. Argv shape::

    wows-scaffold-ship <ship>
        [--toolkit-ship NAME] [--gameparams-ship-id ID]
        [--class-override CO] [--ship-key-suffix S]
        [--variant-permoflage ID]
        [--skip-export] [--skip-armor] [--skip-ammo] [--skip-sidecar]
        [--skip-native-skin] [--skip-gameparams-autofill]
        [--skip-materials-skins] [--skip-geometry-hitbox]
        [common flags ...]
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ..compose.scaffold_ship import scaffold_ship
from ..errors import ConfigError, StepError, ToolkitError
from ..types import ScaffoldResult
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
        prog="wows-scaffold-ship",
        description="Scaffold a fresh ship working dir: hull GLB + sidecar + side files.",
    )
    ap.add_argument(
        "ship",
        help="Display name (e.g. Fletcher) or model-dir name (e.g. ASD048_Fletcher_1945).",
    )
    ap.add_argument(
        "--toolkit-ship",
        default=None,
        help="Override the name passed to wowsunpack. Useful when "
             "disambiguating (folder=Baltimore_Old toolkit="
             "ASC022_Baltimore_1941).",
    )
    ap.add_argument(
        "--gameparams-ship-id",
        default=None,
        help="Override the GameParams Vehicle id used for autofill + "
             "permoflage discovery (required when multiple Vehicles "
             "share one model_dir).",
    )
    ap.add_argument(
        "--class-override",
        default=None,
        help="2-letter class code (CA/CL/BB/DD/CV/SS/...) to override "
             "toolkit species mapping.",
    )
    ap.add_argument(
        "--ship-key-suffix",
        default=None,
        help="Trailing segment for ship_key (e.g. 'B' for a hull variant).",
    )
    ap.add_argument(
        "--variant-permoflage",
        default="auto",
        help="Mesh-swap permoflage routing: 'auto' (default), an "
             "Exterior id (e.g. PJES478_RED_TAKAO), or 'none'.",
    )
    ap.add_argument(
        "--skip-export",
        action="store_true",
        help="Reuse existing hull GLB + placements JSON.",
    )
    ap.add_argument(
        "--skip-armor",
        action="store_true",
        help="Reuse existing armor JSON.",
    )
    ap.add_argument(
        "--skip-ammo",
        action="store_true",
        help="Reuse existing ballistics JSON.",
    )
    ap.add_argument(
        "--skip-sidecar",
        action="store_true",
        help="Don't build/update the sidecar.",
    )
    ap.add_argument(
        "--skip-native-skin",
        action="store_true",
        help="Don't auto-ingest the Vehicle's nativePermoflage as a "
             "Skin entry.",
    )
    ap.add_argument(
        "--skip-gameparams-autofill",
        action="store_true",
        help="Skip the v3.1 GameParams autofill (kinematic fields, "
             "metadata extras, variants block).",
    )
    ap.add_argument(
        "--skip-materials-skins",
        action="store_true",
        help="Skip the materials + skins emit pass (camo / permoflage "
             "topology classifier).",
    )
    ap.add_argument(
        "--skip-geometry-hitbox",
        action="store_true",
        help="Skip the geometry + hitbox synthesis pass.",
    )
    add_common_args(ap)
    return ap


def _summarize(result: ScaffoldResult) -> str:
    bits = [f"scaffold {result.ship_id!r} ok"]
    if result.sidecar_path:
        bits.append(f"sidecar={result.sidecar_path.name}")
    if result.variant_routed:
        bits.append(f"variant={result.variant_permoflage}")
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
        result = scaffold_ship(
            args.ship,
            workspace=cfg.workspace,
            config=cfg,
            class_override=args.class_override,
            ship_key_suffix=args.ship_key_suffix,
            toolkit_ship=args.toolkit_ship,
            gameparams_ship_id=args.gameparams_ship_id,
            skip_export=args.skip_export,
            skip_armor=args.skip_armor,
            skip_ammo=args.skip_ammo,
            skip_sidecar=args.skip_sidecar,
            skip_native_skin=args.skip_native_skin,
            skip_gameparams_autofill=args.skip_gameparams_autofill,
            skip_materials_skins=args.skip_materials_skins,
            skip_geometry_hitbox=args.skip_geometry_hitbox,
            variant_permoflage=args.variant_permoflage,
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
