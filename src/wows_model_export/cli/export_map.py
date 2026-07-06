"""CLI wrapper for :func:`wows_model_export.maps_export.run_export`.

Headless space export — the same artifact set the webview route
``POST /api/maps/{name}/export`` produces (GLB + every sidecar manifest
+ decal textures + sky assets + export.json), without a running
backend. Argv shape::

    wows-export-map SPACE [SPACE ...]
        [--lod N] [--terrain-step N] [--lightmap-density N]
        [--vegetation-density M] [--max-texture-size N]
        [--no-terrain] [--no-water] [--no-vegetation] [--no-textures]
        [--collision-manifest]
        [--skip-glb] [--skip-decal-textures]
        [common flags ...]

``--skip-glb`` re-runs only the sidecar post-pass over an
already-exported GLB (supersedes the scratchpad
``postprocess_ops_maps.py`` driver from the 2026-07-04 ops round).

Spaces may be given bare (``40_Okinawa``) or VFS-form
(``spaces/40_Okinawa``). Failures are per-space: a hard GLB-export
failure on one space is reported and the remaining spaces still run;
the exit code is non-zero if ANY space failed hard. Soft failures
(individual manifests, decal textures, sky) never fail the run — they
are recorded as ``*_error`` keys in that space's ``export.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from ..errors import ConfigError, ToolkitError
from ..maps_export import run_export
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
        prog="wows-export-map",
        description="Export spaces (battle maps / docks / ops) to "
                    "<workspace>/maps/<name>/ with every sidecar manifest.",
    )
    ap.add_argument(
        "spaces",
        nargs="+",
        help="Space names (e.g. 40_Okinawa s01_NavalBase). The "
             "'spaces/' VFS prefix is accepted and stripped.",
    )

    tk = ap.add_argument_group("toolkit export flags")
    tk.add_argument("--lod", type=int, default=0, help="LOD level (0 = highest detail).")
    tk.add_argument(
        "--terrain-step", type=int, default=4,
        help="Terrain decimation step (1=full, 4=default, 8=coarse).",
    )
    tk.add_argument(
        "--lightmap-density", type=int, default=8,
        help="Baked terrain-lightmap texels per 3.125 m block edge "
             "(toolkit default is 4; the published-map convention is 8).",
    )
    tk.add_argument(
        "--vegetation-density", type=float, default=0.0,
        help="Per-species one-tree-per-cell decimation in metres (0 = all).",
    )
    tk.add_argument(
        "--max-texture-size", type=int, default=None,
        help="Cap any texture dimension (box filter). Default: keep sizes.",
    )
    tk.add_argument("--no-terrain", action="store_true", help="Skip terrain mesh.")
    tk.add_argument("--no-water", action="store_true", help="Skip water plane.")
    tk.add_argument("--no-vegetation", action="store_true", help="Skip vegetation.")
    tk.add_argument("--no-textures", action="store_true", help="Skip model textures.")
    tk.add_argument(
        "--collision-manifest", action="store_true",
        help="Also write the map collision manifest sidecar.",
    )

    pp = ap.add_argument_group("post-pass control")
    pp.add_argument(
        "--skip-glb", action="store_true",
        help="Do not run the toolkit export; re-run the sidecar post-pass "
             "over the already-exported GLB.",
    )
    pp.add_argument(
        "--skip-decal-textures", action="store_true",
        help="Skip the decal texture payload export.",
    )

    add_common_args(ap)
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    quiet = bool(getattr(args, "quiet", False))
    json_events = bool(getattr(args, "json_events", False))

    try:
        config = resolve_config(args)
    except ConfigError as err:
        print(f"config error: {err}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    flags = {
        "lod": args.lod,
        "terrain_step": args.terrain_step,
        "lightmap_density": args.lightmap_density,
        "vegetation_density": args.vegetation_density,
    }
    if args.max_texture_size is not None:
        flags["max_texture_size"] = args.max_texture_size
    for key in ("no_terrain", "no_water", "no_vegetation", "no_textures"):
        if getattr(args, key):
            flags[key] = True

    hard_failures: list[str] = []
    for raw in args.spaces:
        name = raw.removeprefix("spaces/")
        try:
            meta = run_export(
                name,
                config=config,
                flags=flags,
                collision_manifest=args.collision_manifest,
                skip_glb=args.skip_glb,
                skip_decal_textures=args.skip_decal_textures,
            )
        except (ToolkitError, FileNotFoundError, ValueError) as err:
            hard_failures.append(name)
            if json_events:
                print(json.dumps({"name": name, "ok": False, "error": str(err)}))
            else:
                print(f"{name}: FAILED — {err}", file=sys.stderr)
            continue
        except ConfigError as err:
            print(f"config error: {err}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

        soft_errors = sorted(
            k.removesuffix("_error") for k in meta if k.endswith("_error")
        )
        if json_events:
            print(json.dumps({
                "name": name,
                "ok": True,
                "glb_size": meta.get("glb_size"),
                "elapsed_ms": meta.get("elapsed_ms"),
                "soft_errors": soft_errors,
            }))
        elif not quiet:
            mi = meta.get("model_instance_manifest") or {}
            sd = meta.get("static_decal_manifest") or {}
            pl = meta.get("point_light_manifest") or {}
            dt = meta.get("decal_textures") or {}
            print(
                f"{name}: glb={meta.get('glb_size')} "
                f"instances={mi.get('instance_count')} "
                f"decals={sd.get('decal_count')} "
                f"decal_textures={dt.get('texture_count')} "
                f"lights={pl.get('light_count')} "
                f"soft_errors={soft_errors or 'none'}"
            )

    if hard_failures:
        if not json_events:
            print(
                f"{len(hard_failures)} space(s) failed: {', '.join(hard_failures)}",
                file=sys.stderr,
            )
        return EXIT_STEP_ERROR
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(EXIT_UNEXPECTED)
