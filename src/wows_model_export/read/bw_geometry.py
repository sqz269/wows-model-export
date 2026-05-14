"""Minimal Python reader for BigWorld ``.geometry`` files.

Lifted from ``tools/shared/bw_geometry.py`` (private I:-side repo). Layer 1
(read): pure parser, no subprocess, no file writes — read-only on disk.

Produces enough data for *mesh-comparison* tasks: per primitive group, return
positions + UV0 + indices. Skips collision/armor blocks, normals, tangents,
binormals, and skinning data — none of which are needed to determine whether
a mod's mesh shares topology + UV layout with the pipeline output.

Format reference: ``J:/PROG/test/wows-toolkit/crates/wowsunpack/src/models/geometry.rs``
and ``vertex_format.rs``. Compression: meshoptimizer (``ENCD`` magic) for
buffers carrying that prefix; raw little-endian otherwise. The skin mods
under inspection ship raw buffers, so the meshopt path is implemented as a
pass-through to the ``meshoptimizer`` Python package when the magic is set.

Coordinate scale: native WG units (1 unit ≈ 15 m). Caller is responsible for
scaling to metres if comparing against the pipeline's pre-scaled GLBs.

This module is intentionally not re-exported via ``read/__init__.py`` yet;
the parent agent will wire it in alongside the rest of the read surface.
Consumers must import the submodule directly:

    from wows_model_export.read import bw_geometry
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ENCD_MAGIC = 0x44434E45  # "ENCD"


# ---------------------------------------------------------------------------
# Vertex format
# ---------------------------------------------------------------------------


@dataclass
class VertexAttribute:
    semantic: str            # 'position' | 'normal' | 'uv0' | 'uv1' | 'tangent' | 'binormal' | 'bones' | 'extra'
    fmt: str                 # 'f32x3' | 'packed_normal' | 'packed_uv' | 'raw4'
    offset: int              # byte offset within a vertex


@dataclass
class VertexFormat:
    attributes: list[VertexAttribute]
    stride: int


def parse_vertex_format(format_name: str) -> VertexFormat:
    """Port of toolkit's ``parse_vertex_format``. Returns attribute table +
    computed stride. Format strings look like ``set3/xyznuvtbpc`` — we strip
    the ``set?/`` prefix and walk the suffix character by character.
    """
    code = format_name.rsplit('/', 1)[-1]
    attrs: list[VertexAttribute] = []
    offset = 0
    uv_count = 0

    chars = list(code)
    i = 0
    while i < len(chars):
        ch = chars[i]
        if ch == 'x':                                    # xyz → POSITION (12 bytes)
            i += 1
            if i < len(chars) and chars[i] == 'y':
                i += 1
            if i < len(chars) and chars[i] == 'z':
                i += 1
            attrs.append(VertexAttribute('position', 'f32x3', offset))
            offset += 12
        elif ch == 'n':                                  # n → packed normal (4 bytes)
            i += 1
            attrs.append(VertexAttribute('normal', 'packed_normal', offset))
            offset += 4
        elif ch == 'u':                                  # uv | uv2
            i += 1
            if i < len(chars) and chars[i] == 'v':
                i += 1
            if i < len(chars) and chars[i] == '2':
                # "uv2" → two UV channels back-to-back
                i += 1
                attrs.append(VertexAttribute('uv0', 'packed_uv', offset))
                offset += 4
                attrs.append(VertexAttribute('uv1', 'packed_uv', offset))
                offset += 4
                uv_count = 2
            else:
                sem = 'uv0' if uv_count == 0 else 'uv1'
                attrs.append(VertexAttribute(sem, 'packed_uv', offset))
                offset += 4
                uv_count += 1
        elif ch == 't':                                  # tb → tangent + binormal
            i += 1
            if i < len(chars) and chars[i] == 'b':
                i += 1
                attrs.append(VertexAttribute('tangent',  'packed_normal', offset))
                offset += 4
                attrs.append(VertexAttribute('binormal', 'packed_normal', offset))
                offset += 4
        elif ch == 'i':                                  # iiiww → bones (8 bytes); lone i → 4 bytes
            i += 1
            if i < len(chars) and chars[i] == 'i':
                i += 1
                if i < len(chars) and chars[i] == 'i':
                    i += 1
                if i < len(chars) and chars[i] == 'w':
                    i += 1
                    if i < len(chars) and chars[i] == 'w':
                        i += 1
                attrs.append(VertexAttribute('bones',   'raw4', offset))
                offset += 4
                attrs.append(VertexAttribute('weights', 'raw4', offset))
                offset += 4
            else:
                attrs.append(VertexAttribute('bones', 'raw4', offset))
                offset += 4
        elif ch == 'r':                                  # r → extra 4 bytes
            i += 1
            attrs.append(VertexAttribute('extra', 'raw4', offset))
            offset += 4
        elif ch == 'p':                                  # pc — flag, no bytes
            i += 1
            if i < len(chars) and chars[i] == 'c':
                i += 1
        elif ch == 'o':                                  # oi — instance flag, no bytes
            i += 1
            if i < len(chars) and chars[i] == 'i':
                i += 1
        else:
            i += 1                                       # unknown, skip

    return VertexFormat(attributes=attrs, stride=offset)


def _unpack_packed_uv(packed_bytes: np.ndarray) -> np.ndarray:
    """Decode N×4 bytes (each a packed UV: u_half, v_half little-endian)
    into N×2 float32 UVs. Engine stores ``actual - 0.5`` to centre around
    zero where float16 has best precision; we add 0.5 back after decode.
    """
    n = len(packed_bytes)
    raw = packed_bytes.view(np.uint8).reshape(n, 4)
    u_bits = raw[:, 0:2].copy().view(np.uint16).reshape(n)
    v_bits = raw[:, 2:4].copy().view(np.uint16).reshape(n)
    u = u_bits.view(np.float16).astype(np.float32) + 0.5
    v = v_bits.view(np.float16).astype(np.float32) + 0.5
    return np.stack([u, v], axis=1)


# ---------------------------------------------------------------------------
# File-level parser
# ---------------------------------------------------------------------------


@dataclass
class MappingEntry:
    mapping_id: int
    merged_buffer_index: int
    items_offset: int        # vertex (or index) start in the merged array
    items_count: int


@dataclass
class MergedVertices:
    format_name: str
    stride: int
    is_skinned: bool
    is_bumped: bool
    raw_bytes: bytes         # decoded if originally meshopt-encoded


@dataclass
class MergedIndices:
    index_size: int          # 2 or 4 bytes
    raw_bytes: bytes


@dataclass
class GeometryFile:
    path: Path
    merged_vertices: list[MergedVertices]
    merged_indices: list[MergedIndices]
    vertices_mapping: list[MappingEntry]
    indices_mapping: list[MappingEntry]


def _parse_packed_string(buf: bytes, struct_base: int) -> str:
    char_count, _padding, text_relptr = struct.unpack_from("<IIq", buf, struct_base)
    if char_count == 0:
        return ""
    text_offset = struct_base + text_relptr
    s = bytes(buf[text_offset:text_offset + char_count])
    return s.rstrip(b"\x00").decode("utf-8", errors="replace")


def _decode_buffer(blob: bytes, stride: int, *, is_index: bool, index_size: int = 0) -> bytes:
    """Pass through raw buffers; meshopt-decode buffers prefixed with ENCD."""
    if len(blob) >= 8:
        magic = struct.unpack_from("<I", blob, 0)[0]
        if magic == ENCD_MAGIC:
            element_count = struct.unpack_from("<I", blob, 4)[0]
            payload = blob[8:]
            try:
                import meshoptimizer as mo  # lazy — only needed for compressed buffers
            except ImportError as e:
                raise RuntimeError(
                    "File ships meshopt-compressed buffers; install via "
                    "`pip install meshoptimizer`"
                ) from e
            if is_index:
                arr = mo.decode_index_buffer(element_count, index_size, payload)
                if arr.dtype != (np.uint16 if index_size == 2 else np.uint32):
                    arr = arr.astype(np.uint16 if index_size == 2 else np.uint32)
                return arr.tobytes()
            else:
                # Avoid `dtype=np.uint8` overload — it returns a shortened
                # buffer in this binding version (only `count` bytes instead of
                # `count*stride`). Default float32 reshape is correct, and
                # `.tobytes()` preserves the underlying byte layout.
                arr = mo.decode_vertex_buffer(element_count, stride, payload)
                buf = arr.tobytes()
                if len(buf) != element_count * stride:
                    raise RuntimeError(
                        f"meshopt decode size mismatch: got {len(buf)} bytes, "
                        f"expected {element_count * stride}"
                    )
                return buf
    return blob


def parse_geometry(path: str | Path) -> GeometryFile:
    """Parse a ``.geometry`` file into mappings + merged buffers.

    Buffers are decompressed if originally meshopt-encoded. Caller slices the
    merged buffers via ``MappingEntry.items_offset`` + ``items_count``.
    """
    path = Path(path)
    data = path.read_bytes()

    # Header: 6×u32 then 6×i64 = 72 bytes. Pointers are RELATIVE to the file
    # start (header_base = 0).
    (mvc, mic, vmc, imc, _cmc, _amc,
     vmp, imp, mvp, mip, _cmp, _amp) = struct.unpack_from("<6I6q", data, 0)

    # Mapping arrays. Each entry is 16 bytes:
    #   u32 mapping_id, u16 merged_buffer_index, u16 packed_texel_density,
    #   u32 items_offset, u32 items_count
    def parse_mappings(base: int, count: int) -> list[MappingEntry]:
        out = []
        for i in range(count):
            ofs = base + i * 16
            mid, mbi, _ptd, io, ic = struct.unpack_from("<IHHII", data, ofs)
            out.append(MappingEntry(mapping_id=mid, merged_buffer_index=mbi,
                                    items_offset=io, items_count=ic))
        return out

    vertices_mapping = parse_mappings(vmp, vmc)
    indices_mapping  = parse_mappings(imp, imc)

    # MergedVertices (0x20 each):
    #   i64 data_relptr, 16-byte packed_string (format_name), u32 size, u16 stride,
    #   u8 skinned, u8 bumped
    merged_vertices: list[MergedVertices] = []
    for i in range(mvc):
        sb = mvp + i * 0x20
        data_relptr, = struct.unpack_from("<q", data, sb)
        size, stride, skinned, bumped = struct.unpack_from("<IHBB", data, sb + 0x18)
        format_name = _parse_packed_string(data, sb + 0x08)
        data_offset = sb + data_relptr
        blob = bytes(data[data_offset:data_offset + size])
        decoded = _decode_buffer(blob, stride, is_index=False)
        merged_vertices.append(MergedVertices(
            format_name=format_name, stride=stride,
            is_skinned=bool(skinned), is_bumped=bool(bumped),
            raw_bytes=decoded,
        ))

    # MergedIndices (0x10 each):
    #   i64 data_relptr, u32 size, u16 reserved, u16 index_size
    merged_indices: list[MergedIndices] = []
    for i in range(mic):
        sb = mip + i * 0x10
        data_relptr, size, _resv, index_size = struct.unpack_from("<qIHH", data, sb)
        data_offset = sb + data_relptr
        blob = bytes(data[data_offset:data_offset + size])
        decoded = _decode_buffer(blob, stride=index_size, is_index=True, index_size=index_size)
        merged_indices.append(MergedIndices(index_size=index_size, raw_bytes=decoded))

    return GeometryFile(
        path=path,
        merged_vertices=merged_vertices,
        merged_indices=merged_indices,
        vertices_mapping=vertices_mapping,
        indices_mapping=indices_mapping,
    )


# ---------------------------------------------------------------------------
# Primitive extraction
# ---------------------------------------------------------------------------


@dataclass
class PrimitiveGroup:
    """One render-set's worth of mesh data, sliced from the merged arrays."""
    vertices_mapping_id: int
    indices_mapping_id: int
    vertex_count: int
    triangle_count: int
    positions: np.ndarray    # (V, 3) float32, native units (1 unit = 15 m)
    uvs:       np.ndarray    # (V, 2) float32 — UV0
    indices:   np.ndarray    # (3T,) uint32 — flat triangle list
    format_name: str


def extract_primitive(
    geom: GeometryFile,
    vertices_mapping_id: int,
    indices_mapping_id: int,
) -> PrimitiveGroup:
    """Pull a (vertex_buffer_slice, index_buffer_slice) pair by mapping_id."""
    vmap = next(
        (m for m in geom.vertices_mapping if m.mapping_id == vertices_mapping_id),
        None,
    )
    imap = next(
        (m for m in geom.indices_mapping  if m.mapping_id == indices_mapping_id),
        None,
    )
    if vmap is None or imap is None:
        raise KeyError(
            f"mapping not found: v=0x{vertices_mapping_id:X} i=0x{indices_mapping_id:X}"
        )
    return _extract_primitive_from_mappings(geom, vmap, imap)


def extract_primitives(geom: GeometryFile) -> list[PrimitiveGroup]:
    """Pair (vmap, imap) by inferred vertex coverage.

    Indices in each ``imap`` are LOCAL to a primitive group's vmap range
    (verified empirically: index values never exceed the paired vmap's
    ``items_count``). But the maximum index is often *less than*
    ``items_count - 1`` because seam-split duplicate vertices at the tail
    of the vmap range are unused at this LOD/group. So we can't rely on
    exact ``items_count == max(indices)+1``.

    Pairing strategy (largest-first):
      1. Sort imaps DESC by ``items_count`` (LOD0's index buffer is the
         biggest one, in practice).
      2. For each imap, compute ``unique_verts = max(indices) + 1`` and
         find the smallest available vmap with ``items_count >= unique_verts``.
         Prefer exact matches first (handles the common case); fall back to
         smallest-fit on miss.
      3. Tie-breaker for equal vmap counts: lowest items_offset (deterministic).
    """
    imap_order = sorted(
        range(len(geom.indices_mapping)),
        key=lambda i: -geom.indices_mapping[i].items_count,
    )

    out: list[PrimitiveGroup] = []
    used_vmap: set[int] = set()
    for ii in imap_order:
        imap = geom.indices_mapping[ii]
        if imap.items_count == 0:
            continue
        mi = geom.merged_indices[imap.merged_buffer_index]
        idx_dtype = np.uint16 if mi.index_size == 2 else np.uint32
        idx_arr = np.frombuffer(mi.raw_bytes, dtype=idx_dtype)
        sl = idx_arr[imap.items_offset:imap.items_offset + imap.items_count]
        if len(sl) == 0:
            continue
        unique_verts = int(sl.max()) + 1

        # Candidate vmaps: items_count >= unique_verts, not already used.
        candidates = [
            vi for vi, vmap in enumerate(geom.vertices_mapping)
            if vi not in used_vmap and vmap.items_count >= unique_verts
        ]
        if not candidates:
            continue
        # Pick smallest-fit (smallest items_count that holds the indices).
        # Ties broken by lowest items_offset for determinism.
        best_v = min(candidates, key=lambda vi: (
            geom.vertices_mapping[vi].items_count,
            geom.vertices_mapping[vi].items_offset,
        ))
        used_vmap.add(best_v)
        out.append(
            _extract_primitive_from_mappings(geom, geom.vertices_mapping[best_v], imap)
        )
    return out


def _extract_primitive_from_mappings(
    geom: GeometryFile,
    vmap: MappingEntry,
    imap: MappingEntry,
) -> PrimitiveGroup:
    mv = geom.merged_vertices[vmap.merged_buffer_index]
    mi = geom.merged_indices[imap.merged_buffer_index]

    fmt = parse_vertex_format(mv.format_name)
    pos_attr = next(a for a in fmt.attributes if a.semantic == 'position')
    uv_attr  = next((a for a in fmt.attributes if a.semantic == 'uv0'), None)

    # Vertex slice: (count, stride) bytes
    v_start = vmap.items_offset * mv.stride
    v_end   = v_start + vmap.items_count * mv.stride
    vbuf = np.frombuffer(
        mv.raw_bytes[v_start:v_end],
        dtype=np.uint8,
    ).reshape(vmap.items_count, mv.stride)

    # Positions as f32x3 starting at pos_attr.offset
    pos_bytes = vbuf[:, pos_attr.offset:pos_attr.offset + 12].copy()
    positions = pos_bytes.view(np.float32).reshape(vmap.items_count, 3)

    if uv_attr is not None:
        uv_bytes = vbuf[:, uv_attr.offset:uv_attr.offset + 4].copy()
        uvs = _unpack_packed_uv(uv_bytes.reshape(vmap.items_count, 4))
    else:
        uvs = np.zeros((vmap.items_count, 2), dtype=np.float32)

    # Index slice
    idx_dtype = np.uint16 if mi.index_size == 2 else np.uint32
    idx_arr = np.frombuffer(mi.raw_bytes, dtype=idx_dtype)
    indices = idx_arr[
        imap.items_offset:imap.items_offset + imap.items_count
    ].astype(np.uint32)

    return PrimitiveGroup(
        vertices_mapping_id=vmap.mapping_id,
        indices_mapping_id=imap.mapping_id,
        vertex_count=vmap.items_count,
        triangle_count=imap.items_count // 3,
        positions=positions,
        uvs=uvs,
        indices=indices,
        format_name=mv.format_name,
    )


__all__ = [
    "ENCD_MAGIC",
    "VertexAttribute",
    "VertexFormat",
    "parse_vertex_format",
    "MappingEntry",
    "MergedVertices",
    "MergedIndices",
    "GeometryFile",
    "PrimitiveGroup",
    "parse_geometry",
    "extract_primitive",
    "extract_primitives",
]
