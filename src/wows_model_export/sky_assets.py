"""Per-weather sky / IBL asset extraction for map exports.

The engine ships one prefiltered IBL cubemap per map per weather preset:
``content/location/skybox/<map>/lightcube/<Weather>/main_probe.dds`` — a
DDS cubemap, typically 256², full 9-mip PMREM ladder: legacy header +
D3DFMT_A16B16G16R16F (RGBA half-float), or DX10 header + BC6H for probes
re-authored since 15.9. The GLB's ``weathers[]`` scene
extras carry each weather block's ``pbs.cubemapsPath``; this module joins
the two and converts each probe's mip-0 faces into a small equirectangular
Radiance ``.hdr`` the webview can feed straight to three.js
(``RGBELoader`` + ``EquirectangularReflectionMapping``) and Unity can
import natively.

Grounding: reference/maps/grounding_2026_07_03/probe_pmrem.md (file format,
naming chain) and weather_sky_tod.md (weather-block schema). The 4096² BC6H
sky-dome panorama is NOT handled here yet — that needs the
software-decode path (Unity mis-decodes BC6H) and is tracked as follow-up.
"""

from __future__ import annotations

import re
import struct
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig
from .toolkit import vfs as _vfs

_DDPF_FOURCC = 0x4
_D3DFMT_A16B16G16R16F = 113
_DDSCAPS2_CUBEMAP = 0x200
_DXGI_R16G16B16A16_FLOAT = 10
_DXGI_BC6H_UF16 = 95
_DXGI_BC6H_SF16 = 96
_DDS_RESOURCE_MISC_TEXTURECUBE = 0x4


def parse_cube_dds(data: bytes) -> np.ndarray:
    """Parse a cubemap probe DDS into its mip-0 faces.

    Returns ``(6, size, size, 4)`` float32 in DDS face order
    (+X, -X, +Y, -Y, +Z, -Z). Accepts the legacy header with
    D3DFMT_A16B16G16R16F and the DX10 header with R16G16B16A16_FLOAT or
    BC6H (UF16/SF16): probes WG re-authors (Ocean's in 15.9, new weathers)
    ship as BC6H with a DX10 header.

    Faces are stored one after another, each with its full mip chain, so
    face ``f`` starts at ``f * <bytes of one face's mips>``.
    ``imagecodecs.dds_decode`` assumes mip-0-sized faces and returns
    garbage for faces 1-5 of a mipped cube, so BC6H faces are decoded one
    at a time with ``bcn_decode``.
    """
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]
    caps2 = struct.unpack_from("<I", data, 112)[0]
    if width != height:
        raise ValueError(f"non-square cube face {width}x{height}")

    if pf_flags & _DDPF_FOURCC and fourcc == b"DX10":
        if len(data) < 148:
            raise ValueError("truncated DX10 header")
        dxgi, _dim, misc = struct.unpack_from("<III", data, 128)
        if not (misc & _DDS_RESOURCE_MISC_TEXTURECUBE or caps2 & _DDSCAPS2_CUBEMAP):
            raise ValueError("not a cubemap DDS")
        offset = 148
        if dxgi == _DXGI_R16G16B16A16_FLOAT:
            return _rgba16f_faces(data, offset, width, mip_count)
        if dxgi in (_DXGI_BC6H_UF16, _DXGI_BC6H_SF16):
            return _bc6h_faces(data, offset, width, mip_count, signed=dxgi == _DXGI_BC6H_SF16)
        raise ValueError(f"unsupported cube DXGI format {dxgi}")

    if not (pf_flags & _DDPF_FOURCC) or struct.unpack_from("<I", data, 84)[0] != _D3DFMT_A16B16G16R16F:
        raise ValueError(
            f"expected D3DFMT_A16B16G16R16F (113) or a DX10 header, got fourcc={fourcc!r}"
        )
    if not caps2 & _DDSCAPS2_CUBEMAP:
        raise ValueError("not a cubemap DDS")
    return _rgba16f_faces(data, 128, width, mip_count)


def _rgba16f_faces(data: bytes, offset: int, size: int, mip_count: int) -> np.ndarray:
    face_bytes = sum(max(size >> i, 1) ** 2 * 8 for i in range(mip_count))
    mip0_bytes = size * size * 8
    faces = np.empty((6, size, size, 4), dtype=np.float32)
    for f in range(6):
        raw = np.frombuffer(
            data, dtype=np.float16, count=mip0_bytes // 2, offset=offset + f * face_bytes
        )
        faces[f] = raw.reshape(size, size, 4).astype(np.float32)
    return faces


def _bc6h_faces(
    data: bytes, offset: int, size: int, mip_count: int, *, signed: bool
) -> np.ndarray:
    import imagecodecs  # noqa: PLC0415 - only BC6H probes need it

    def blocks(m: int) -> int:
        return max((m + 3) // 4, 1) ** 2 * 16

    face_bytes = sum(blocks(max(size >> i, 1)) for i in range(mip_count))
    mip0_bytes = blocks(size)
    if len(data) < offset + 6 * face_bytes:
        raise ValueError("truncated BC6H cubemap")
    faces = np.ones((6, size, size, 4), dtype=np.float32)  # BC6H has no alpha
    for f in range(6):
        start = offset + f * face_bytes
        rgb = imagecodecs.bcn_decode(
            data[start:start + mip0_bytes], -6 if signed else 6, shape=(size, size, 3)
        )
        faces[f, ..., :3] = np.asarray(rgb, dtype=np.float32)
    return faces


def cube_to_equirect(faces: np.ndarray, out_w: int = 512, out_h: int = 256) -> np.ndarray:
    """Resample cube faces to an equirectangular RGB image (float32).

    Uses the D3D cubemap face conventions. World frame: +Y up; longitude 0
    looks down +Z. Nearest-texel sampling — the source is a 256² prefiltered
    probe, so there is no high-frequency content worth filtering.
    """
    py, px = np.mgrid[0:out_h, 0:out_w]
    lon = (px + 0.5) / out_w * 2.0 * np.pi - np.pi
    lat = np.pi / 2.0 - (py + 0.5) / out_h * np.pi
    x = np.cos(lat) * np.sin(lon)
    y = np.sin(lat)
    z = np.cos(lat) * np.cos(lon)

    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    face = np.zeros(x.shape, dtype=np.int8)
    sc = np.zeros_like(x)
    tc = np.zeros_like(x)
    ma = np.zeros_like(x)

    # D3D face selection: +X,-X,+Y,-Y,+Z,-Z with per-face (sc, tc) axes.
    m = (ax >= ay) & (ax >= az) & (x > 0)
    face[m], sc[m], tc[m], ma[m] = 0, -z[m], -y[m], ax[m]
    m = (ax >= ay) & (ax >= az) & (x <= 0)
    face[m], sc[m], tc[m], ma[m] = 1, z[m], -y[m], ax[m]
    m = (ay > ax) & (ay >= az) & (y > 0)
    face[m], sc[m], tc[m], ma[m] = 2, x[m], z[m], ay[m]
    m = (ay > ax) & (ay >= az) & (y <= 0)
    face[m], sc[m], tc[m], ma[m] = 3, x[m], -z[m], ay[m]
    m = (az > ax) & (az > ay) & (z > 0)
    face[m], sc[m], tc[m], ma[m] = 4, x[m], -y[m], az[m]
    m = (az > ax) & (az > ay) & (z <= 0)
    face[m], sc[m], tc[m], ma[m] = 5, -x[m], -y[m], az[m]

    size = faces.shape[1]
    ma = np.maximum(ma, 1e-9)
    u = np.clip(((sc / ma + 1.0) * 0.5 * size).astype(np.int32), 0, size - 1)
    v = np.clip(((tc / ma + 1.0) * 0.5 * size).astype(np.int32), 0, size - 1)
    return faces[face, v, u, :3]


def write_hdr_rgbe(path: Path, rgb: np.ndarray) -> None:
    """Write a float32 RGB image as an uncompressed Radiance RGBE ``.hdr``.

    Flat (non-RLE) scanlines — three.js `RGBELoader` and PIL both accept
    them, and the only byte pattern that could alias the RLE scanline
    marker requires an exponent byte of 0, which this encoder emits only
    for fully black pixels (0,0,0,0).
    """
    h, w, _ = rgb.shape
    maxc = rgb.max(axis=2)
    nz = maxc >= 1e-32
    mant, exp = np.frexp(np.where(nz, maxc, 1.0))
    scale = np.where(nz, mant * 256.0 / np.where(nz, maxc, 1.0), 0.0)
    rgbe = np.zeros((h, w, 4), dtype=np.uint8)
    rgbe[..., :3] = np.clip(rgb * scale[..., None], 0.0, 255.0).astype(np.uint8)
    rgbe[..., 3] = np.where(nz, exp + 128, 0).astype(np.uint8)
    header = b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n" + f"-Y {h} +X {w}\n".encode("ascii")
    path.write_bytes(header + rgbe.tobytes())


def _safe_weather_dir(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "Default"


def _probe_name(extras: dict[str, Any]) -> str:
    probes = extras.get("probes") or []
    if probes and isinstance(probes[0], dict) and probes[0].get("name"):
        return str(probes[0]["name"])
    return "main_probe"


def _probe_vfs_path(cubemaps_path: str, probe_name: str) -> str:
    return f"{cubemaps_path.strip().strip('/')}/{probe_name}.dds"


def extract_sky_assets(extras: dict[str, Any], probe_root: Path, out_dir: Path) -> dict[str, Any]:
    """Extract per-weather IBL probes named by the GLB's ``weathers[]`` extras.

    ``probe_root`` is a directory laid out like the VFS (the probe for a
    weather is read from ``<probe_root>/<cubemapsPath>/<probe>.dds``). Writes
    ``sky/<weather>/env_cube.hdr`` under ``out_dir`` and returns the sky
    manifest (``wows.map.sky_manifest.v1``). Exports use
    :func:`extract_sky_assets_from_vfs`, which fills ``probe_root`` from the
    current game build.
    """
    weathers = extras.get("weathers") or []
    probe_name = _probe_name(extras)

    entries: list[dict[str, Any]] = []
    for w in weathers:
        if not isinstance(w, dict):
            continue
        name = str(w.get("name") or "Default")
        cubemaps_path = str(((w.get("pbs") or {}).get("cubemapsPath") or "")).strip()
        entry: dict[str, Any] = {
            "name": name,
            "cubemaps_path": cubemaps_path or None,
            "env_hdr": None,
            "error": None,
        }
        if cubemaps_path:
            vfs_path = _probe_vfs_path(cubemaps_path, probe_name)
            dds_path = probe_root / vfs_path
            if dds_path.is_file():
                try:
                    faces = parse_cube_dds(dds_path.read_bytes())
                    equirect = cube_to_equirect(faces)
                    rel = f"sky/{_safe_weather_dir(name)}/env_cube.hdr"
                    out_path = out_dir / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    write_hdr_rgbe(out_path, equirect)
                    entry["env_hdr"] = rel
                except Exception as exc:  # noqa: BLE001 — per-weather isolation
                    entry["error"] = str(exc)
            else:
                entry["error"] = f"probe not found: {vfs_path}"
        entries.append(entry)

    return {
        "schema": "wows.map.sky_manifest.v1",
        "probe_name": probe_name,
        "weather_count": len(entries),
        "extracted_count": sum(1 for e in entries if e["env_hdr"]),
        "weathers": entries,
    }


def extract_sky_assets_from_vfs(
    extras: dict[str, Any], out_dir: Path, *, config: PipelineConfig
) -> dict[str, Any]:
    """:func:`extract_sky_assets` with the probes read from the current game
    build. Probes change when a map's lighting is reworked (Ocean in 15.9)
    and new weathers add new ones, so a one-off unpack goes stale."""
    probe_name = _probe_name(extras)
    patterns = sorted({
        "/" + _probe_vfs_path(str(cp), probe_name)
        for w in extras.get("weathers") or []
        if isinstance(w, dict)
        and (cp := ((w.get("pbs") or {}).get("cubemapsPath") or "").strip())
    })
    with tempfile.TemporaryDirectory(prefix="wms-sky-") as td:
        probe_root = Path(td)
        if patterns:
            _vfs.extract(patterns, out_dir=probe_root, config=config)
        return extract_sky_assets(extras, probe_root, out_dir)
