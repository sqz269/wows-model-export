"""CLI wrapper for :func:`wows_model_export.compose.rig_normalize_bones.normalize_file`.

Strip Blender's bone-axis bake from one or more ``.rig.glb`` files so
``yaw.localRotation = Quaternion.Euler(0, deg, 0)`` rotates the gun
around the world up axis exactly as the turret rig spec promises. Run
this after every Blender rig export; ``compose.turret_rig.build_rig``
invokes it automatically on success.

Argv shape::

    wows-rig-normalize-bones <rig.glb> [<rig.glb> ...] [-o PATH]

Without ``-o`` each input is rewritten in place (idempotent). With
``-o`` exactly one input may be given and the result is written to the
specified path.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..compose.rig_normalize_bones import normalize_file
from ._args import EXIT_OK, EXIT_STEP_ERROR, EXIT_UNEXPECTED


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-rig-normalize-bones",
        description="Strip Blender's bone-axis bake from a turret rig.glb.",
    )
    ap.add_argument(
        "rig_glb",
        nargs="+",
        type=Path,
        help="One or more <asset_id>.rig.glb files. Modified in place.",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="If given (only with one input), write to this path instead "
             "of overwriting in place.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.output and len(args.rig_glb) != 1:
        print("--output requires exactly one input", file=sys.stderr)
        return EXIT_STEP_ERROR

    n_ok = 0
    n_fail = 0
    for path in args.rig_glb:
        if not path.is_file():
            print(f"  {path}: not found", file=sys.stderr)
            n_fail += 1
            continue
        try:
            stats = normalize_file(path, output=args.output)
        except NotImplementedError as e:
            print(f"  {path.name}: unsupported — {e}", file=sys.stderr)
            n_fail += 1
            continue
        except ValueError as e:
            print(f"  {path.name}: invalid input — {e}", file=sys.stderr)
            n_fail += 1
            continue
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(
                f"  {path.name}: unexpected error: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            n_fail += 1
            continue
        out_path = args.output or path
        print(
            f"  {path.name}: "
            f"{stats.joints_normalised}/{stats.joints} joints, "
            f"{stats.non_joints_propagated} helper(s) propagated, "
            f"{stats.skins_updated} skin(s) → {out_path}",
            file=sys.stderr,
        )
        n_ok += 1

    if n_fail:
        return EXIT_STEP_ERROR if n_ok else EXIT_UNEXPECTED
    return EXIT_OK


__all__ = ["main"]
