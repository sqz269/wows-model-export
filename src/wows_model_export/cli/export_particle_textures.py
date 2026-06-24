"""Extract WG particle textures into a consumer's Pipeline tree.

Phase 0 of the particle-asset publish. The shell-tracer trail sprites
(``particles/trails/*.dds``) referenced by ``ammo_profiles.json``'s
``visual.tracer.texture`` are plain VFS files — NOT the decoded ``.effect``
binary blobs — so they extract directly with the toolkit ``extract`` glob,
with no particle-binary decode and no per-ship sidecar dependency. This is
why the consumer's textured-tracer path is unblocked independently of the
(unmerged) particle-binary pipeline.

Output preserves the VFS layout under ``--dest``::

    <dest>/particles/trails/Trail_GK.dds

so a consumer that mirrors VFS paths under its pipeline root resolves them
by the same ``visual.tracer.texture`` string it already stores (the same
layout the other published libraries use under ``--dest``).

One glob (``particles/trails/*.dds``) pulls the whole trail set — the colour
strips, ``Trail_Shell_Hat`` head sprites, ``Trail_Smoke_*`` smoke, and
``Trail_Distort_*`` distortion maps, plus the ``_Own`` variants — so it also
seeds the smoke-streak / head-glow follow-on.

Argv::

    wows-export-particle-textures --dest <PIPELINE_ROOT>
        [--glob PATTERN ...]        # default: particles/trails/*.dds
        [--all-particle-textures]   # Phase 1: particles/**/*.dds
        [common flags ...]

NOTE: this extracts RAW WG DDS (no UV-normalize pass). For an additive
camera-facing tracer strip that's fine (V-orientation is invisible across the
ribbon); if a future textured effect needs the consumer's normalized layout,
route it through the publish/normalize pipeline instead.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from ..errors import ConfigError, ToolkitError
from ..toolkit import vfs as _vfs
from ._args import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_UNEXPECTED,
    add_common_args,
    resolve_config,
)

# Phase 0 default: the shell-tracer trail strips (+ their smoke/distort/hat siblings).
_DEFAULT_GLOBS: tuple[str, ...] = ("particles/trails/*.dds",)
# Phase 1 shorthand: every particle texture (gun / explosion / smoke / atlas sprites).
_ALL_PARTICLE_GLOBS: tuple[str, ...] = ("particles/**/*.dds",)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wows-export-particle-textures",
        description="Extract WG particle textures (default: tracer trails) into a "
                    "consumer Pipeline tree, preserving VFS layout.",
    )
    ap.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Consumer pipeline root (the same target the published libraries "
             "are written under). Files land at <dest>/particles/trails/... "
             "(VFS layout preserved).",
    )
    ap.add_argument(
        "--glob",
        action="append",
        default=None,
        metavar="PATTERN",
        help="VFS glob to extract (repeatable). Overrides the default set when "
             "given. Default: particles/trails/*.dds.",
    )
    ap.add_argument(
        "--all-particle-textures",
        action="store_true",
        help="Phase 1 shorthand: extract every particle texture "
             "(particles/**/*.dds), not just the tracer trails.",
    )
    add_common_args(ap)
    return ap


def _select_globs(args: argparse.Namespace) -> tuple[str, ...]:
    if args.glob:
        return tuple(args.glob)
    if args.all_particle_textures:
        return _ALL_PARTICLE_GLOBS
    return _DEFAULT_GLOBS


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    globs = _select_globs(args)
    dest = args.dest.resolve()

    try:
        _vfs.extract(list(globs), dest, config=cfg)
    except (ConfigError, ToolkitError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_UNEXPECTED

    # vfs.extract returns only the out_dir (the glob match size isn't known up
    # front), so count what actually landed for the summary.
    n = sum(1 for _ in dest.glob("particles/**/*.dds"))
    print(
        f"export-particle-textures -> {dest}  globs={list(globs)}  dds_on_disk={n}",
        file=sys.stderr,
    )
    return EXIT_OK


__all__ = ["main"]
