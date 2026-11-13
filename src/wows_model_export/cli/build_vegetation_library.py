"""CLI wrapper for :func:`wows_model_export.compose.vegetation_library.build_vegetation_library`.

Export every SpeedTree species of the exported client maps once into
``<workspace>/libraries/vegetation/`` (``reference/schemas/VEGETATION_LIBRARY.md``).
Each map is read from the game build it was exported from (its
``build_deps.json``), so the command takes no ``--build``. Argv shape::

    wows-build-vegetation-library [SPACE ...] [--force] [--quality PRESET] [common flags ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from ..compose.vegetation_library import VegetationLibraryError, build_vegetation_library
from ..errors import ConfigError, StepError, ToolkitError
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
        prog="wows-build-vegetation-library",
        description="Export the SpeedTree species of the exported client maps into libraries/vegetation/: "
                    "one GLB per species with every LOD and its switch distances, named from the space's "
                    "forest.bin, textures through the shared texture store.",
    )
    ap.add_argument("spaces", nargs="*", help="Map folders under <workspace>/maps/ (default: every exported "
                                              "map with vegetation).")
    ap.add_argument("--force", action="store_true", help="Rewrite species whose index entry is current.")
    ap.add_argument("--quality", default=None, help="Forest quality preset whose LOD profile sets the switch "
                                                     "distances (default: the client's default preset).")
    add_common_args(ap, build_option=False)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args, pin_build=False)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    quiet = getattr(args, "quiet", False) or getattr(args, "json_events", False)

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr)

    try:
        res = build_vegetation_library(config=cfg, spaces=args.spaces or None, force=args.force,
                                       quality=args.quality, log=log)
    except (VegetationLibraryError, StepError, ToolkitError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_UNEXPECTED
    summary = {
        "library": str(res.library_root), "index": str(res.index_path), "species": len(res.species),
        "written": res.written, "skipped": res.skipped, "spaces": res.spaces, "warnings": res.warnings,
    }
    if getattr(args, "json_events", False):
        print(json.dumps(summary))
    else:
        for w in res.warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"vegetation library {res.library_root}: {len(res.species)} species from {len(res.spaces)} "
              f"map(s), {len(res.written)} written, {len(res.skipped)} current")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
