"""Complete DDS mip chains that stop before 1x1.

Some WG textures carry a truncated mip chain. Two shapes occur:

* uncompressed fields cut short on purpose, e.g.
  ``particles/deform16f/water_spray_lens_noise.dds`` (G16R16F 512x1024,
  6 of 11 levels);
* block-compressed, non-square textures whose chain stops when the SHORT
  side reaches 1 pixel, e.g. a 2048x512 DXT1 with 10 of 12 levels (the
  last two, 2x1 and 1x1, are missing). One BC7 1024x1024 normal map also
  stops at 2x2.

Direct3D samples all of these fine, but some loaders reject them outright:
Unity's built-in DDS importer logs ``Invalid mipCount: 10 provided, 12
expected`` and produces no texture, so every material or effect using the
file silently renders without it.

:func:`complete_partial_mip_chain` rewrites such a file in place with the
full chain. The authored levels are kept byte for byte. Missing levels are
derived from the last authored one:

* uncompressed float / RGBA8 formats: 2x2 box filter;
* block-compressed formats (BC1-BC7 / DXT*): each new 4x4 block copies the
  nearest block of the previous level. The added levels are tiny (their
  short side is already below one block), so nearest-block is close enough
  and avoids re-encoding.

Formats it doesn't know are left alone. Idempotent.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

_FOURCC = 84
_DX10_DXGI = 128

# Uncompressed: (channels, numpy dtype name). Legacy D3DFMT codes are stored
# as a number in the FourCC field.
_D3DFMT_UNCOMPRESSED = {
    111: (1, "float16"),  # R16F
    112: (2, "float16"),  # G16R16F
    113: (4, "float16"),  # A16B16G16R16F
    114: (1, "float32"),  # R32F
    115: (2, "float32"),  # G32R32F
    116: (4, "float32"),  # A32B32G32R32F
}
_DXGI_UNCOMPRESSED = {
    2: (4, "float32"),   # R32G32B32A32_FLOAT
    10: (4, "float16"),  # R16G16B16A16_FLOAT
    28: (4, "uint8"),    # R8G8B8A8_UNORM
    34: (2, "float16"),  # R16G16_FLOAT
}

# Block-compressed: bytes per 4x4 block.
_FOURCC_BLOCK = {
    b"DXT1": 8, b"DXT2": 16, b"DXT3": 16, b"DXT4": 16, b"DXT5": 16,
    b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
    b"ATI2": 16, b"BC5U": 16, b"BC5S": 16,
}
_DXGI_BLOCK = {
    **{f: 8 for f in (70, 71, 72, 79, 80, 81)},                       # BC1, BC4
    **{f: 16 for f in (73, 74, 75, 76, 77, 78, 82, 83, 84,             # BC2, BC3, BC5
                       94, 95, 96, 97, 98, 99)},                       # BC6H, BC7
}


def _level_dims(width: int, height: int, count: int) -> list[tuple[int, int]]:
    dims = []
    w, h = width, height
    for _ in range(count):
        dims.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return dims


def complete_partial_mip_chain(path: Path) -> bool:
    """Extend ``path``'s mip chain down to 1x1 if it stops early.

    Returns ``True`` when the file was rewritten.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 128 or data[:4] != b"DDS ":
        return False
    height, width = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    full = int(math.floor(math.log2(max(width, height, 1)))) + 1
    if mips <= 1 or mips >= full:
        return False

    fourcc = data[_FOURCC:_FOURCC + 4]
    if fourcc == b"DX10":
        header_len = 148
        dxgi = struct.unpack_from("<I", data, _DX10_DXGI)[0]
        uncompressed = _DXGI_UNCOMPRESSED.get(dxgi)
        block_bytes = _DXGI_BLOCK.get(dxgi)
    else:
        header_len = 128
        uncompressed = _D3DFMT_UNCOMPRESSED.get(struct.unpack_from("<I", data, _FOURCC)[0])
        block_bytes = _FOURCC_BLOCK.get(bytes(fourcc))

    if uncompressed is not None:
        levels = _complete_uncompressed(data, header_len, width, height, mips, full, *uncompressed)
    elif block_bytes is not None:
        levels = _complete_blocks(data, header_len, width, height, mips, full, block_bytes)
    else:
        return False
    if levels is None:
        return False  # payload size doesn't match the header: not the layout we assumed

    header = bytearray(data[:header_len])
    struct.pack_into("<I", header, 28, full)
    path.write_bytes(bytes(header) + b"".join(levels))
    return True


def _complete_uncompressed(data, offset, width, height, mips, full, channels, dtype_name):
    import numpy as np  # noqa: PLC0415

    dtype = np.dtype(dtype_name)
    arrays = []
    for w, h in _level_dims(width, height, mips):
        count = w * h * channels
        if offset + count * dtype.itemsize > len(data):
            return None
        arrays.append(np.frombuffer(data, dtype=dtype, count=count, offset=offset)
                      .reshape(h, w, channels))
        offset += count * dtype.itemsize
    if offset != len(data):
        return None

    cur = arrays[-1].astype(np.float32)
    while len(arrays) < full:
        if cur.shape[0] % 2:
            cur = np.concatenate([cur, cur[-1:]], axis=0)
        if cur.shape[1] % 2:
            cur = np.concatenate([cur, cur[:, -1:]], axis=1)
        cur = 0.25 * (cur[0::2, 0::2] + cur[1::2, 0::2] + cur[0::2, 1::2] + cur[1::2, 1::2])
        level = np.rint(cur) if dtype.kind == "u" else cur
        arrays.append(level.astype(dtype))
    return [a.tobytes() for a in arrays]


def _complete_blocks(data, offset, width, height, mips, full, block_bytes):
    levels: list[bytes] = []
    grids: list[tuple[int, int]] = []
    for w, h in _level_dims(width, height, mips):
        bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
        size = bw * bh * block_bytes
        if offset + size > len(data):
            return None
        levels.append(data[offset:offset + size])
        grids.append((bw, bh))
        offset += size
    if offset != len(data):
        return None

    dims = _level_dims(width, height, full)
    while len(levels) < full:
        pbw, pbh = grids[-1]
        prev = levels[-1]
        w, h = dims[len(levels)]
        bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
        out = bytearray()
        for by in range(bh):
            sy = min(2 * by, pbh - 1)
            for bx in range(bw):
                sx = min(2 * bx, pbw - 1)
                start = (sy * pbw + sx) * block_bytes
                out += prev[start:start + block_bytes]
        levels.append(bytes(out))
        grids.append((bw, bh))
    return levels
