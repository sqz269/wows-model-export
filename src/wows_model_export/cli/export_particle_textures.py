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

Many WG particle textures (incl. the main tracer strips) are stored as
LEGACY uncompressed bitmask DDS (FourCC=0, R8G8B8A8). Some loaders reject
that variant (Unity's IHV importer reports "Unsupported file"). After
extraction this command rewrites those headers IN PLACE to the equivalent
DX10 ``R8G8B8A8_UNORM`` (or ``B8G8R8A8_UNORM``) form — a header-only,
lossless transform that every modern DDS loader accepts; compressed /
already-DX10 files are left untouched.

NOTE: this extracts RAW WG DDS (no UV-normalize pass). For an additive
camera-facing tracer strip that's fine (V-orientation is invisible across the
ribbon); if a future textured effect needs the consumer's normalized layout,
route it through the publish/normalize pipeline instead.
"""
from __future__ import annotations

import argparse
import struct
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
# VFS file-tree paths carry a leading slash — the extract glob must match it (a
# bare "particles/..." matches nothing). Output still lands at <dest>/particles/...
# (the leading slash collapses).
_DEFAULT_GLOBS: tuple[str, ...] = ("/particles/trails/*.dds",)
# Phase 1 shorthand: every particle texture (gun / explosion / smoke / atlas sprites).
_ALL_PARTICLE_GLOBS: tuple[str, ...] = ("/particles/**/*.dds",)


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


# DDS pixel-format field offsets within the 4-byte magic + 124-byte header.
_PF_FLAGS = 80
_PF_FOURCC = 84
_PF_BITCOUNT = 88
_PF_MASKS = 92  # R, G, B, A — four uint32


def _modernize_legacy_dds(path: Path) -> bool:
    """Rewrite a legacy uncompressed 32-bit DDS header to a DX10 header.

    Legacy bitmask-format DDS (``FourCC=0``) is rejected by some loaders
    (Unity's IHV importer, etc.); the DX10 variant carries the same pixel
    data with an explicit DXGI format every modern loader accepts. The
    transform is header-only and lossless. Idempotent: compressed and
    already-DX10 files are skipped. Returns ``True`` when a file was rewritten.
    """
    data = path.read_bytes()
    if len(data) < 128 or data[:4] != b"DDS ":
        return False
    if data[_PF_FOURCC:_PF_FOURCC + 4].strip(b"\x00"):
        return False  # FourCC set (DXT*/DX10/etc.) — already loadable
    bits = struct.unpack_from("<I", data, _PF_BITCOUNT)[0]
    if bits != 32:
        return False
    rm, gm, bm, am = struct.unpack_from("<IIII", data, _PF_MASKS)
    if (rm, gm, bm, am) == (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000):
        swap_rb = False            # R8G8B8A8 already
    elif (rm, gm, bm, am) == (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000):
        swap_rb = True             # B8G8R8A8 -> swizzle to R8G8B8A8
    else:
        return False               # unrecognised 32-bit layout — leave as-is

    # Always emit R8G8B8A8_UNORM (DXGI 28): it's the only uncompressed 32-bit
    # form some loaders accept (notably NOT B8G8R8A8/87), so BGRA pixels are
    # swizzled R<->B in place across all mips (uncompressed, 4-byte aligned).
    pixels = bytearray(data[128:])
    if swap_rb:
        pixels[0::4], pixels[2::4] = bytes(pixels[2::4]), bytes(pixels[0::4])

    hdr = bytearray(data[:128])
    struct.pack_into("<I", hdr, _PF_FLAGS, 0x4)        # DDPF_FOURCC
    hdr[_PF_FOURCC:_PF_FOURCC + 4] = b"DX10"
    # DDS_HEADER_DXT10: R8G8B8A8_UNORM(28), TEXTURE2D(3), miscFlag, arraySize=1, miscFlags2
    dx10 = struct.pack("<IIIII", 28, 3, 0, 1, 0)
    path.write_bytes(bytes(hdr) + dx10 + bytes(pixels))
    return True


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

    # Modernize legacy uncompressed DDS headers -> DX10 so downstream loaders
    # accept them (idempotent; compressed / already-DX10 files are skipped).
    converted = 0
    for dds in dest.glob("particles/**/*.dds"):
        if _modernize_legacy_dds(dds):
            converted += 1

    # vfs.extract returns only the out_dir (the glob match size isn't known up
    # front), so count what actually landed for the summary.
    n = sum(1 for _ in dest.glob("particles/**/*.dds"))
    print(
        f"export-particle-textures -> {dest}  globs={list(globs)}  "
        f"dds_on_disk={n}  dx10_rewritten={converted}",
        file=sys.stderr,
    )
    return EXIT_OK


__all__ = ["main"]
