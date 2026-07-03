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
        [--library]                 # decoded records.json/index.json + .vfd fields
        [--full]                    # everything: --all-particle-textures --library
        [--refresh-assets]          # re-dump assets.bin from the client first
        [common flags ...]

``--library`` delivers the decoded particle library (the shared
``library/particles/records.json`` + ``index.json`` built by
``compose.library_particles.ensure_built``) to
``<dest>/particles/library/``, plus the velocity-field ``.vfd`` resources
referenced by ``velocityField.fieldSourceName`` at their VFS-layout
``<dest>/content/particles/velocity_fields/`` paths — so a consumer can
resolve ``fieldSourceName`` verbatim under its pipeline root, the same way
the webview fetches ``repoUrl(fieldSourceName)``. A ``--library``-only run
skips the texture extraction step.

The library build is mtime+schema gated against the CACHED ``assets.bin``;
that cache is never refreshed on its own, so after a game-client update pass
``--refresh-assets`` to re-dump ``assets.bin`` (and thereby rebuild the
library against the current client) before delivering.

Many WG particle textures (incl. the main tracer strips) are stored as
LEGACY uncompressed bitmask DDS (FourCC=0, R8G8B8A8). Some loaders reject
that variant (Unity's IHV importer reports "Unsupported file"). After
extraction this command rewrites those headers IN PLACE to the equivalent
DX10 ``R8G8B8A8_UNORM`` (or ``B8G8R8A8_UNORM``) form — a header-only,
lossless transform that every modern DDS loader accepts; compressed /
already-DX10 files are left untouched.

The HDR fire ramps (``particles/ramps/*_HDR.dds``) ship as BC6H_UF16, which
Unity's native importer MIS-DECODES (constant garbage -> particle glow renders
pink). This command software-decodes those to a TRUE-HDR ``<name>.exr`` sibling
(float16, full HDR range) the consumer loads instead — or an 8-bit ``<name>.png``
when ``imagecodecs`` is absent (the webview software-decodes BC6H for the same
reason).

NOTE: this extracts RAW WG DDS (no UV-normalize pass). For an additive
camera-facing tracer strip that's fine (V-orientation is invisible across the
ribbon); if a future textured effect needs the consumer's normalized layout,
route it through the publish/normalize pipeline instead.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import traceback
from pathlib import Path
from typing import Any

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
    ap.add_argument(
        "--library",
        action="store_true",
        help="Deliver the decoded particle library (records.json + index.json "
             "via compose.library_particles.ensure_built) to "
             "<dest>/particles/library/, plus the referenced velocity-field "
             ".vfd resources at <dest>/content/particles/velocity_fields/. "
             "Given alone, skips the texture extraction step.",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Deliver the whole particle asset set: "
             "--all-particle-textures + --library.",
    )
    ap.add_argument(
        "--refresh-assets",
        action="store_true",
        help="Re-dump assets.bin from the game client before building the "
             "library (the cache is otherwise kept forever, so a library "
             "delivery after a client update needs this to pick up the "
             "current corpus).",
    )
    add_common_args(ap)
    return ap


# DDS pixel-format field offsets within the 4-byte magic + 124-byte header.
_PF_FLAGS = 80
_PF_FOURCC = 84
_PF_BITCOUNT = 88
_PF_MASKS = 92  # R, G, B, A — four uint32
_DX10_DXGI = 128  # dxgiFormat: first u32 of the DDS_HEADER_DXT10 (after the 128-byte header)
_DXGI_BC6H_UF16 = 95  # DXGI_FORMAT_BC6H_UF16 (unsigned HDR)


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


def _transcode_bc6h_ramp(path: Path) -> bool:
    """Decode a BC6H HDR ramp to a TRUE-HDR ``.exr`` sibling (lossless float16).

    Unity's native ``.dds`` importer MIS-DECODES BC6H_UF16 — it returns constant
    garbage, so the WG fire/HDR ramps (``particles/ramps/*_HDR.dds``) render
    wrong (a particle GRADIENT_MAP glow goes constant-red -> pink smoke). The
    webview already software-decodes BC6H (``lib/dds/bc6h.ts``); do the same here
    so the ``--dest`` tree carries a decode-correct sibling the consumer loads
    instead of the raw ``.dds``.

    ``imagecodecs.dds_decode`` returns float16 RGB carrying the FULL HDR range
    (bit-exact vs the webview ``bcdec_bc6h_half`` reference; one ramp peaks at
    ~240), and ``imagecodecs.exr_encode`` writes an ``.exr`` Unity imports as a
    linear-HDR RGBAHalf texture (via the real TextureImporter, not the IHV one).
    Falls back to the legacy 8-bit ``.png`` (texture2ddecoder — colours correct,
    HDR clamped to 1.0) when ``imagecodecs`` is absent. Idempotent; returns
    ``True`` when a sibling was written.
    """
    data = path.read_bytes()
    if len(data) < 148 or data[:4] != b"DDS ":
        return False
    if data[_PF_FOURCC:_PF_FOURCC + 4] != b"DX10":
        return False
    if struct.unpack_from("<I", data, _DX10_DXGI)[0] not in (95, 96):  # BC6H UF16/SF16
        return False

    # Primary: TRUE-HDR EXR (decode float16 + encode EXR, one dependency).
    try:
        import numpy as np  # noqa: PLC0415
        import imagecodecs as ic  # noqa: PLC0415
    except ImportError:
        ic = None
    if ic is not None:
        out = path.with_suffix(".exr")
        if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
            return False
        rgb = np.asarray(ic.dds_decode(data))             # (h, w, 3) float16, HDR
        h, w = rgb.shape[:2]
        rgba = np.ones((h, w, 4), dtype=np.float16)        # BC6H has no alpha -> 1.0
        rgba[..., :3] = rgb[..., :3]
        out.write_bytes(ic.exr_encode(rgba, compression="zip"))
        return True

    # Fallback: legacy 8-bit PNG (colours correct, HDR clamped to 1.0).
    try:
        import texture2ddecoder  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        print(
            f"  BC6H transcode skipped (imagecodecs/texture2ddecoder absent): {path.name}",
            file=sys.stderr,
        )
        return False
    out = path.with_suffix(".png")
    if out.is_file() and out.stat().st_mtime >= path.stat().st_mtime:
        return False
    height, width = struct.unpack_from("<II", data, 12)
    bgra = bytes(texture2ddecoder.decode_bc6(data[148:], width, height))
    rgba = bytearray(len(bgra))
    rgba[0::4] = bgra[2::4]  # R
    rgba[1::4] = bgra[1::4]  # G
    rgba[2::4] = bgra[0::4]  # B
    rgba[3::4] = bgra[3::4]  # A
    Image.frombytes("RGBA", (width, height), bytes(rgba)).save(out)
    return True


def _select_globs(args: argparse.Namespace) -> tuple[str, ...]:
    if args.glob:
        return tuple(args.glob)
    if args.all_particle_textures or args.full:
        return _ALL_PARTICLE_GLOBS
    return _DEFAULT_GLOBS


def _atomic_copy(src: Path, target: Path) -> None:
    """Copy ``src`` over ``target`` atomically (tmp sibling + ``os.replace``).

    A consumer may hot-reload the library keyed on the file's write time;
    a plain overwrite of a ~160 MB records.json would expose a truncated
    window. The tmp file lives next to the target (same volume).
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, target)


def _deliver_library(dest: Path, cfg) -> dict[str, Any]:
    """Deliver the decoded particle library + velocity fields under ``dest``.

    Ensures the workspace library is built (mtime+schema gated), then copies
    ``records.json`` + ``index.json`` to ``<dest>/particles/library/`` and the
    extracted ``.vfd`` velocity fields to their VFS-layout
    ``<dest>/content/particles/velocity_fields/`` paths, so consumers resolve
    ``velocityField.fieldSourceName`` verbatim under their pipeline root.
    """
    from ..compose import library_particles as _lib  # noqa: PLC0415

    build_info = _lib.ensure_built(config=cfg)
    ws = cfg.workspace.resolve()
    paths = _lib.library_paths(ws)

    out_root = dest / "particles" / "library"
    out_root.mkdir(parents=True, exist_ok=True)
    for src in (paths["records"], paths["index"]):
        _atomic_copy(src, out_root / src.name)

    vfd_src = ws / "content" / "particles" / "velocity_fields"
    vfd_copied = 0
    if vfd_src.is_dir():
        vfd_out = dest / "content" / "particles" / "velocity_fields"
        vfd_out.mkdir(parents=True, exist_ok=True)
        for f in sorted(vfd_src.glob("*.vfd")):
            _atomic_copy(f, vfd_out / f.name)
            vfd_copied += 1

    return {
        "status": build_info.get("status"),
        "records_bytes": (out_root / "records.json").stat().st_size,
        "velocity_fields_copied": vfd_copied,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    deliver_library = args.library or args.full
    # A --library-only invocation delivers just the decoded records + fields;
    # texture extraction still runs whenever globs were selected (explicitly
    # or by default) or --full asked for everything.
    extract_textures = (
        args.full or args.all_particle_textures or bool(args.glob)
        or not deliver_library
    )

    globs = _select_globs(args)
    dest = args.dest.resolve()

    if args.refresh_assets:
        from ..toolkit import assets_bin as _assets_bin  # noqa: PLC0415

        try:
            _assets_bin.ensure_dump(refresh=True, config=cfg)
        except (ConfigError, ToolkitError) as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    converted = bc6h = 0
    n = 0
    if extract_textures:
        try:
            _vfs.extract(list(globs), dest, config=cfg)
        except (ConfigError, ToolkitError) as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
            return EXIT_UNEXPECTED

        # Post-extract passes (idempotent):
        #  - modernize legacy uncompressed DDS headers -> DX10 so loaders accept them
        #  - transcode BC6H_UF16 HDR ramps -> EXR/PNG sibling (loaders mis-decode BC6H)
        for dds in dest.glob("particles/**/*.dds"):
            if _modernize_legacy_dds(dds):
                converted += 1
            if _transcode_bc6h_ramp(dds):
                bc6h += 1

        # vfs.extract returns only the out_dir (the glob match size isn't known
        # up front), so count what actually landed for the summary.
        n = sum(1 for _ in dest.glob("particles/**/*.dds"))

    lib_summary = ""
    if deliver_library:
        try:
            lib = _deliver_library(dest, cfg)
        except (ConfigError, ToolkitError) as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"\nunexpected error: {type(e).__name__}: {e}", file=sys.stderr)
            return EXIT_UNEXPECTED
        lib_summary = (
            f"  library={lib['status']} "
            f"records_bytes={lib['records_bytes']} "
            f"vfd_copied={lib['velocity_fields_copied']}"
        )

    tex_summary = (
        f"globs={list(globs)}  dds_on_disk={n}  "
        f"dx10_rewritten={converted}  bc6h_transcoded={bc6h}"
        if extract_textures
        else "textures=skipped"
    )
    print(
        f"export-particle-textures -> {dest}  {tex_summary}{lib_summary}",
        file=sys.stderr,
    )
    return EXIT_OK


__all__ = ["main"]
