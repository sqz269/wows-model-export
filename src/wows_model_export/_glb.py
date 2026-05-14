"""Internal GLB byte-level helpers.

Private module — single-underscore prefix — used by both `toolkit/` (for
the post-export armor/hitbox winding flip) and (future) `resolve/winding`
once the auto-detect heuristic ships.

This file carries only the **byte-level core** of the GLB toolchain:
parse, reassemble, and reverse triangle winding. The 400+ lines of
auto-detect scoring + CLI in `tools/shared/glb_flip_winding.py` (in the
I:/Models/warships private repo) will land in `resolve/winding.py`
when that lift happens.

Why a top-level `_glb` instead of `toolkit/_glb`?
  Both `toolkit` and (eventually) `resolve` need these byte helpers.
  The layer scheme says toolkit can't depend on resolve, but neither
  blocks them from sharing a private internal module that has no layer
  semantics of its own.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

# ── glTF / GLB constants ──────────────────────────────────────────────

GLB_MAGIC  = b"glTF"
CHUNK_JSON = b"JSON"
CHUNK_BIN  = b"BIN\x00"

MODE_TRIANGLES = 4

COMP_UNSIGNED_BYTE  = 5121
COMP_UNSIGNED_SHORT = 5123
COMP_UNSIGNED_INT   = 5125

COMP_BYTES = {
    COMP_UNSIGNED_BYTE:  1,
    COMP_UNSIGNED_SHORT: 2,
    COMP_UNSIGNED_INT:   4,
}

COMP_FLOAT = 5126


# ── GLB parse / reassemble ────────────────────────────────────────────


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Return (gltf_json, bin_chunk_bytes). Raises on malformed GLB."""
    if len(data) < 12 or data[:4] != GLB_MAGIC:
        raise ValueError("not a GLB (magic mismatch)")
    version, total_len = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError(f"unsupported GLB version {version}")
    # Note: some writers lie about total_len; we don't validate it.

    pos = 12
    gltf: dict | None = None
    bin_data = b""
    while pos + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, pos)
        pos += 8
        if pos + chunk_len > len(data):
            raise ValueError(f"chunk at {pos - 8} extends past end of file")
        payload = data[pos:pos + chunk_len]
        pos += chunk_len
        if chunk_type == CHUNK_JSON:
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == CHUNK_BIN:
            bin_data = bytes(payload)
        # Other chunk types are legal per spec but not expected; skip.
    if gltf is None:
        raise ValueError("no JSON chunk found")
    return gltf, bin_data


def write_glb(gltf: dict, bin_data: bytes, path: Path) -> None:
    """Reassemble GLB + write atomically (sibling `.tmp` → rename)."""
    json_bytes = json.dumps(gltf, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    bin_padded = bytearray(bin_data)
    while len(bin_padded) % 4 != 0:
        bin_padded.append(0x00)

    chunks = struct.pack("<I4s", len(json_bytes), CHUNK_JSON) + json_bytes
    if bin_padded:
        chunks += struct.pack("<I4s", len(bin_padded), CHUNK_BIN) + bytes(bin_padded)

    total_len = 12 + len(chunks)
    header = struct.pack("<4sII", GLB_MAGIC, 2, total_len)
    out = header + chunks

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(out)
    tmp.replace(path)


# ── Winding reversal ──────────────────────────────────────────────────


def _swap(ba: bytearray, a_off: int, b_off: int, size: int) -> None:
    a = bytes(ba[a_off:a_off + size])
    b = bytes(ba[b_off:b_off + size])
    ba[a_off:a_off + size] = b
    ba[b_off:b_off + size] = a


def flip_winding(
    gltf: dict,
    bin_data: bytes,
    mesh_filter=None,
) -> tuple[bytes, dict]:
    """Reverse triangle winding in every TRIANGLES-mode indexed primitive.

    Optional ``mesh_filter`` is a callable ``(mesh_dict) -> bool``; only
    meshes for which it returns ``True`` get their primitives flipped.
    Default (``None``) processes every mesh.

    Returns (new_bin, report).
    """
    bin_ba = bytearray(bin_data)
    accessors = gltf.get("accessors", [])
    buffer_views = gltf.get("bufferViews", [])

    seen_keys: set[tuple[int, int, int]] = set()
    report = dict(
        primitives_seen=0,
        buffer_views_flipped=0,
        skipped_no_indices=0,
        skipped_non_tri=0,
        skipped_by_filter=0,
    )

    for mesh in gltf.get("meshes", []):
        if mesh_filter is not None and not mesh_filter(mesh):
            report["skipped_by_filter"] += 1
            continue
        for prim in mesh.get("primitives", []):
            mode = prim.get("mode", MODE_TRIANGLES)
            if mode != MODE_TRIANGLES:
                report["skipped_non_tri"] += 1
                continue
            indices_idx = prim.get("indices")
            if indices_idx is None:
                report["skipped_no_indices"] += 1
                continue
            report["primitives_seen"] += 1

            acc = accessors[indices_idx]
            ct = acc["componentType"]
            n = acc["count"]
            if n % 3 != 0:
                continue
            size = COMP_BYTES.get(ct)
            if size is None:
                continue

            bv = buffer_views[acc["bufferView"]]
            start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)

            key = (start, ct, n)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            report["buffer_views_flipped"] += 1

            triangle_stride = size * 3
            for tri in range(n // 3):
                tri_off = start + tri * triangle_stride
                _swap(bin_ba, tri_off + size, tri_off + size * 2, size)

    return bytes(bin_ba), report


__all__ = [
    "parse_glb",
    "write_glb",
    "flip_winding",
    "GLB_MAGIC",
    "CHUNK_JSON",
    "CHUNK_BIN",
    "MODE_TRIANGLES",
    "COMP_UNSIGNED_BYTE",
    "COMP_UNSIGNED_SHORT",
    "COMP_UNSIGNED_INT",
    "COMP_BYTES",
    "COMP_FLOAT",
]
