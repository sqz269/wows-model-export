"""CLI wrapper for :func:`wows_model_export.compose.map_model_library.build_map_model_library`.

Export WoWS map models (buildings, props, rocks, coast pieces) by stem or
model path into ``<workspace>/libraries/map_models/`` with every LOD and its
switch distance (``reference/schemas/MAP_MODEL_LIBRARY.md``). Argv shape::

    wows-build-map-model-library MODEL [MODEL ...] [--models-file FILE] [--force] [common flags ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from ..compose.map_model_library import MapModelLibraryError, build_map_model_library
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
        prog="wows-build-map-model-library",
        description="Export map models into libraries/map_models/ (GLB with every LOD, the .visual LOD table, "
                    "raw DDS through the shared texture store) and index them.",
    )
    ap.add_argument("models", nargs="*", help="Model file stems (LBT013) or model paths "
                                              "(content/location/building/tribal/LBT013/LBT013).")
    ap.add_argument("--models-file", type=Path, default=None,
                    help="A file with one model per line (blank lines and # comments skipped).")
    ap.add_argument("--force", action="store_true", help="Re-export models whose index entry is current.")
    add_common_args(ap)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    models = list(args.models)
    if args.models_file:
        for line in args.models_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                models.append(line)
    if not models:
        print("error: name at least one model", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    quiet = getattr(args, "quiet", False) or getattr(args, "json_events", False)

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr)

    try:
        res = build_map_model_library(models, config=cfg, force=args.force, log=log)
    except (MapModelLibraryError, StepError, ToolkitError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_UNEXPECTED
    summary = {"library": str(res.library_root), "index": str(res.index_path), "models": res.models,
               "written": res.written, "skipped": res.skipped, "failed": res.failed, "warnings": res.warnings}
    if getattr(args, "json_events", False):
        print(json.dumps(summary))
    else:
        for w in res.warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(f"map-model library {res.library_root}: {len(res.written)} exported, {len(res.skipped)} current, "
              f"{len(res.failed)} failed")
    return EXIT_STEP_ERROR if res.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
