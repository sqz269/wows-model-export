"""Per-weather sky / IBL asset extraction for map exports.

The engine ships one prefiltered IBL cubemap per map per weather preset:
``content/location/skybox/<map>/lightcube/<Weather>/main_probe.dds`` — a
legacy (pre-DX10) DDS cubemap, D3DFMT_A16B16G16R16F (RGBA half-float),
typically 256², full 9-mip PMREM ladder. The GLB's ``weathers[]`` scene
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
from pathlib import Path
from typing import Any

import numpy as np

_DDPF_FOURCC = 0x4
_D3DFMT_A16B16G16R16F = 113
_DDSCAPS2_CUBEMAP = 0x200


def parse_rgba16f_cube_dds(data: bytes) -> np.ndarray:
    """Parse a legacy RGBA16F cubemap DDS into mip-0 faces.

    Returns ``(6, size, size, 4)`` float32 in DDS face order
    (+X, -X, +Y, -Y, +Z, -Z).
    """
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = struct.unpack_from("<I", data, 84)[0]
    caps2 = struct.unpack_from("<I", data, 112)[0]
    if not (pf_flags & _DDPF_FOURCC) or fourcc != _D3DFMT_A16B16G16R16F:
        raise ValueError(f"expected D3DFMT_A16B16G16R16F (113), got fourcc={fourcc}")
    if not caps2 & _DDSCAPS2_CUBEMAP:
        raise ValueError("not a cubemap DDS")
    if width != height:
        raise ValueError(f"non-square cube face {width}x{height}")

    # Face-major layout: each face stores its full mip chain contiguously.
    face_bytes = 0
    for i in range(mip_count):
        m = max(width >> i, 1)
        face_bytes += m * m * 8
    mip0_bytes = width * width * 8

    faces = np.empty((6, height, width, 4), dtype=np.float32)
    for f in range(6):
        off = 128 + f * face_bytes
        raw = np.frombuffer(data, dtype=np.float16, count=mip0_bytes // 2, offset=off)
        faces[f] = raw.reshape(height, width, 4).astype(np.float32)
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


def extract_sky_assets(extras: dict[str, Any], res_unpack: Path, out_dir: Path) -> dict[str, Any]:
    """Extract per-weather IBL probes named by the GLB's ``weathers[]`` extras.

    Writes ``sky/<weather>/env_cube.hdr`` under ``out_dir`` and returns the
    sky manifest (``wows.map.sky_manifest.v1``).
    """
    weathers = extras.get("weathers") or []
    probes = extras.get("probes") or []
    probe_name = "main_probe"
    if probes and isinstance(probes[0], dict) and probes[0].get("name"):
        probe_name = str(probes[0]["name"])

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
            dds_path = res_unpack / Path(cubemaps_path.strip("/")) / f"{probe_name}.dds"
            if dds_path.is_file():
                try:
                    faces = parse_rgba16f_cube_dds(dds_path.read_bytes())
                    equirect = cube_to_equirect(faces)
                    rel = f"sky/{_safe_weather_dir(name)}/env_cube.hdr"
                    out_path = out_dir / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    write_hdr_rgbe(out_path, equirect)
                    entry["env_hdr"] = rel
                except Exception as exc:  # noqa: BLE001 — per-weather isolation
                    entry["error"] = str(exc)
            else:
                entry["error"] = f"probe not found: {dds_path}"
        entries.append(entry)

    return {
        "schema": "wows.map.sky_manifest.v1",
        "probe_name": probe_name,
        "weather_count": len(entries),
        "extracted_count": sum(1 for e in entries if e["env_hdr"]),
        "weathers": entries,
    }
