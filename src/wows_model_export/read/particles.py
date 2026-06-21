"""WoWS particle binary reader.

Decodes the Effect blob (magic ``0xEB23E0AF``) of ``content/assets.bin``
— the on-disk form of WG's particle authoring data, used to drive every
particle system the game spawns. Replaces the prior "particle data is
opaque" assumption.

Two readers, layered:

- :func:`parse_assets_bin` — minimal container parser. Walks the
  PrototypeDatabase header so we can pull out the Effect blob and the
  resource-path lookup table.

- :class:`ParticleStore` — high-level reader.  Holds the parsed
  container plus a name index (``"particles/vehicles/Fire_small.xml" →
  record_index``) and lazily decodes :class:`Effect` records on demand.

The full byte-level schema is in
``reference/investigations/particle_work/particle_format_spec.md``. This
module mirrors that spec; deviations are bugs.

Status: 2026-05-16. All structures byte-for-byte verified across the
full 3329-record corpus (0 decode errors). Source: ported from
``tmp/particle_stage_d/e7_endtoend_v5_stagef2.py`` with the container
walk lifted from ``crates/wowsunpack/src/models/assets_bin.rs``.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Magic numbers + sizing constants
# ---------------------------------------------------------------------------

# PrototypeDatabase outer container
ASSETS_BIN_MAGIC = 0x42574442      # "BWDB"
ASSETS_BIN_VERSION = 0x01010000

# Particle data lives in the Effect blob (magic = murmur3_32("EffectPrototype")).
EFFECT_BLOB_MAGIC = 0xEB23E0AF
EFFECT_PRESET_BLOB_MAGIC = 0x42E15336
EFFECT_METADATA_BLOB_MAGIC = 0xDFC8F8E0

# Fixed struct sizes inside the Effect blob.
SYSTEM_SIZE = 0x1c8
COMPONENT_SIZE = 0x10
SYSTEM_DISTANCE_CONFIG_SIZE = 0x20
SYSTEM_INTENSITY_CHANNEL_SIZE = 0x10
SYSTEM_INTENSITY_CONFIG_SIZE = 0x20
EFFECT_METADATA_CHANNEL_SIZE = 0x20

# Item sizes per database blob (index 0..9 in PrototypeDatabase.databases).
# Sourced from the toolkit's `assets_bin` subcommand. Not all are used here.
PROTOTYPE_ITEM_SIZES: tuple[int, ...] = (
    0x78, 0x70, 0x20, 0x28, 0x70, 0x10, 0x18, 0x10, 0x10, 0x10,
)

# ---------------------------------------------------------------------------
# Enum vocabularies
# ---------------------------------------------------------------------------

# Component kinds (Component.kind, +0x00 of each 16-byte Component slot).
COMP_KIND = {-1: "empty", 0: "PCAT", 1: "light", 2: "decal", 3: "PSAT"}

# PCAT action table — per-particle actions. Index = action_idx in body header.
PCAT: dict[int, str] = {
    0: "dampfer", 1: "stream", 2: "jitter", 3: "force",
    4: "resizer", 5: "orbitor", 6: "scaler", 7: "tint",
    8: "cylinder", 9: "alphaSetter", 10: "sphere", 11: "magnet",
    12: "velocityField", 13: "box",
}

# PSAT action table — system-level actions.
PSAT: dict[int, str] = {
    0: "dampfer", 1: "spawner", 2: "stream", 3: "jitter", 4: "force",
    5: "plane", 6: "cylinder", 7: "orbitor", 8: "sphere", 9: "magnet",
    10: "velocityField", 11: "box", 12: "creator",
}

# PS_VGT — volume-generator prototype types used by creator.initialPosition /
# initialVelocity. The schema doc previously listed a "cone" entry; that does
# not exist in the binary dispatcher.
PS_VGT = {-1: "empty", 0: "box", 1: "point", 2: "cylinder", 3: "sphere", 4: "line"}

# PS_VALG ramp parameter / sampling — interpretation of the ramp lookup axis +
# wrap mode for ValueGenerator type=2. Stored as u32 enum indices in the
# payload; we keep the names mirror-readable so JSON consumers don't have to
# carry the enum tables.
# Order CORRECTED 2026-06-04 (build 12506899): the prior maps were ALPHABETICAL
# guesses and WRONG. Binary truth = the runtime sampler switch (FUN_14071a440 /
# FUN_140718650) + the .rdata enum tables @0x1420e34e0 / @0x1420e3660. ANY prebuilt
# library/particles/records.json built before this MUST be regenerated — its
# parameterType/samplingType strings are mislabeled corpus-wide. See RE doc
# findings_2026_06_04/62_fx_runtime_eval_size_model.md.
PS_VALG_RAMP_PARAMETER = {
    0: "systemAge", 1: "particleAge", 2: "systemVelocity",
    3: "particleVelocity", 4: "systemActiveTime", 5: "particleIndex",
}
PS_VALG_RAMP_SAMPLING = {0: "loop", 1: "pingPong", 2: "once"}

# PS_RBT — Render Blend Type. Renderer +0x88 (i32, 10 values). Value
# order confirmed against the binary enum table @ 0x1420befc0 (WoWS
# build 12267945) — the 2026-05-22 statistical probe had it right.
PS_RBT = {
    0: "BLENDED_WATER_SURFACE",
    1: "DEFORM_WATER_SURFACE",
    2: "ADDITIVE",
    3: "BLENDED_UNDERWATER",
    4: "ADDITIVE_WATER_SURFACE",
    5: "UNDERWATER_GRADIENT_MAP",
    6: "BLENDED_GLOW",
    7: "GRADIENT_MAP",
    8: "SHIMMER",
    9: "BLENDED",
}

# PS_RLT — Renderer Lighting Type. Renderer +0x84 (i32, 3 values).
# Labels recovered from the binary enum table @ 0x1420bf490 (WoWS build
# 12267945, value-ordered). This is the +0x84 slot the 2026-05-22 probe
# mislabeled as a "blendFlag84" gradient sub-mode flag.
PS_RLT = {0: "lambert", 1: "lightmapping4Way", 2: "lightmappingHL2"}

# PS_RRC — Rotation Center reference. Renderer +0x80 (i32, 4 values).
# Labels recovered from the binary enum table @ 0x1420bf0d0 (WoWS build
# 12267945, value-ordered); supersedes the earlier tentative guess
# {center/topLeft/topRight/bottomLeft}, which was wrong.
PS_RRC = {0: "bottom", 1: "corner", 2: "center", 3: "custom"}

# PS_PAT — Particle Animation Type. Animation +0x38 (u32, 3 values).
# Labels recovered from the binary enum table @ 0x1420bf430 (WoWS build
# 12267945, value-ordered).
PS_PAT = {0: "noAnimation", 1: "framesPlayback", 2: "motionVectors"}

# PS_IC — runtime intensity-channel target IDs. Recovered from the build
# 12506899 sorted enum table at 0x1420bf640; system intensity configs store
# these integer IDs in their `flags[]` array.
PS_IC = {
    0: "PARTICLE_TILING_U",
    1: "LIGHT_TINT_R",
    2: "PARTICLE_STREAMER_X",
    3: "PARTICLE_SCALE_X",
    4: "PARTICLE_VEL_Z",
    5: "LIGHT_RADIUS",
    6: "LIGHT_TINT_B",
    7: "PARTICLE_COLOR_R",
    8: "AGE_SCALE",
    9: "PARTICLE_COLOR_B",
    10: "PARTICLE_VEL_Y",
    11: "PARTICLE_TILING_V",
    12: "PARTICLE_COLOR_A",
    13: "PARTICLE_TINT_G",
    14: "AGE_AUX_SCALE",
    15: "PARTICLE_TINT_B",
    16: "PARTICLE_SCALE_Y",
    17: "PARTICLE_STREAMER_Y",
    18: "PARTICLE_TINT_R",
    19: "EMITTER_RATE",
    20: "PARTICLE_VEL_X",
    21: "PARTICLE_STREAMER_Z",
    22: "PARTICLE_COLOR_G",
    23: "PARTICLE_SIZE",
    24: "PARTICLE_TINT_A",
    25: "LIGHT_TINT_G",
}


# ---------------------------------------------------------------------------
# Dataclasses (parser output)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PathEntry:
    """One entry from the PrototypeDatabase ``pathsStorage`` array.

    Forms a parent-linked tree; :class:`ResourceIndex.full_path` walks
    the chain back to the root.
    """
    self_id: int
    parent_id: int
    name: str


@dataclass(frozen=True)
class BlobInfo:
    """One ``PrototypeDatabase`` blob descriptor (the per-type record store)."""
    prototype_magic: int
    prototype_checksum: int
    size: int
    data_offset: int    # file-absolute byte offset of the blob's data
    record_count: int   # parsed from blob header at data_offset+0


@dataclass(frozen=True)
class _AssetsBinHeader:
    """Surface-level container info needed to read prototype records."""
    blobs: list[BlobInfo]
    paths: list[PathEntry]
    r2p_capacity: int
    r2p_buckets_offset: int
    r2p_values_offset: int


# ---------------------------------------------------------------------------
# Container walker — mirrors the toolkit's assets_bin.rs
# ---------------------------------------------------------------------------

def _read_header(buf: bytes | mmap.mmap) -> _AssetsBinHeader:
    """Parse the PrototypeDatabase container, return the metadata we need
    to read individual prototype blobs and resolve path → record lookups.

    Mirrors ``parse_assets_bin`` in ``wows-toolkit``'s
    ``crates/wowsunpack/src/models/assets_bin.rs``. We stop before
    parsing per-blob fixed records; those are blob-specific (Effect /
    Visual / Material / …) and walked separately by their callers.
    """
    magic, version, _ck, _arch, _end = struct.unpack_from("<IIIHH", buf, 0)
    if magic != ASSETS_BIN_MAGIC:
        raise ValueError(f"assets.bin: bad magic 0x{magic:08X}")
    if version != ASSETS_BIN_VERSION:
        raise ValueError(f"assets.bin: unsupported version 0x{version:08X}")

    body_base = 0x10
    # body header — see the Rust parse_body_header.
    (offsets_cap, _p1, offsets_buckets_rp, offsets_values_rp,
     string_data_size, _p2, string_data_rp,
     r2p_cap, _p3, r2p_buckets_rp, r2p_values_rp,
     paths_count, _p4, paths_data_rp,
     databases_count, _p5, databases_rp) = struct.unpack_from(
        "<IIqqIIqIIqqIIqIIq", buf, body_base,
    )
    # strings/offsets section is at body_base; r2p at body_base+0x28; paths
    # at body_base+0x40; databases entries at body_base + databases_rp.
    paths_base = body_base + 0x40
    paths_data_offset = paths_base + paths_data_rp
    paths: list[PathEntry] = []
    for i in range(paths_count):
        entry_base = paths_data_offset + i * 32
        self_id, parent_id = struct.unpack_from("<QQ", buf, entry_base)
        name_base = entry_base + 0x10
        name_size, _p, name_relptr = struct.unpack_from("<IIq", buf, name_base)
        if name_size > 0:
            name_off = name_base + name_relptr
            raw = bytes(buf[name_off:name_off + name_size]).rstrip(b"\x00")
            name = raw.decode("utf-8", errors="replace")
        else:
            name = ""
        paths.append(PathEntry(self_id=self_id, parent_id=parent_id, name=name))

    db_entries_offset = body_base + databases_rp
    blobs: list[BlobInfo] = []
    for i in range(databases_count):
        entry_base = db_entries_offset + i * 0x18
        pmagic, pcheck, psize, _pad, data_relptr = struct.unpack_from(
            "<IIIIq", buf, entry_base,
        )
        if psize > 0:
            blobs.append(BlobInfo(
                prototype_magic=pmagic,
                prototype_checksum=pcheck,
                size=psize,
                data_offset=entry_base + data_relptr,
                record_count=struct.unpack_from("<Q", buf, entry_base + data_relptr)[0],
            ))
        else:
            blobs.append(BlobInfo(
                prototype_magic=pmagic, prototype_checksum=pcheck,
                size=0, data_offset=0, record_count=0,
            ))

    r2p_base = body_base + 0x28
    return _AssetsBinHeader(
        blobs=blobs,
        paths=paths,
        r2p_capacity=r2p_cap,
        r2p_buckets_offset=r2p_base + r2p_buckets_rp,
        r2p_values_offset=r2p_base + r2p_values_rp,
    )


# ---------------------------------------------------------------------------
# Resource-path → prototype location lookup
# ---------------------------------------------------------------------------

def _build_paths_by_self_id(paths: list[PathEntry]) -> dict[int, PathEntry]:
    return {p.self_id: p for p in paths}


def _reconstruct_full_path(
    paths_by_self_id: dict[int, PathEntry],
    leaf: PathEntry,
    *,
    depth_cap: int = 100,
) -> str:
    """Walk the parent_id chain back to root and join the segment names."""
    parts: list[str] = []
    cur: PathEntry | None = leaf
    for _ in range(depth_cap):
        if cur is None:
            break
        if cur.name:
            parts.append(cur.name)
        if cur.parent_id == 0:
            break
        cur = paths_by_self_id.get(cur.parent_id)
    parts.reverse()
    return "/".join(parts)


def _r2p_lookup(
    buf: bytes | mmap.mmap, hdr: _AssetsBinHeader, self_id: int,
) -> tuple[int, int] | None:
    """Look up the prototype location for ``self_id`` in the
    resource_to_prototype_map hashmap.

    Returns ``(blob_index, record_index)`` or ``None`` if not found.
    Mirrors ``PrototypeDatabase::lookup_r2p`` + ``decode_r2p_value``.
    """
    cap = hdr.r2p_capacity
    if cap == 0:
        return None
    start = self_id % cap
    for probe in range(cap):
        slot = (start + probe) % cap
        # 16-byte bucket: (u64 key, u64 sentinel). sentinel=0 + key=0 → empty.
        boff = hdr.r2p_buckets_offset + slot * 16
        key = struct.unpack_from("<Q", buf, boff)[0]
        sentinel = struct.unpack_from("<Q", buf, boff + 8)[0]
        if sentinel == 0 and key == 0:
            return None
        if key == self_id:
            value = struct.unpack_from("<I", buf, hdr.r2p_values_offset + slot * 4)[0]
            type_tag = value & 0xFF
            record_index = value >> 8
            if type_tag % 4 != 0:
                return None
            blob_index = type_tag // 4
            return (blob_index, record_index)
    return None


# ---------------------------------------------------------------------------
# Effect-blob decoder — per-record walker
# ---------------------------------------------------------------------------

def _decode_ramp(buf: bytes | mmap.mmap, ramp_addr: int, file_end: int) -> dict:
    """16-byte Ramp header + RampKey[count] (8B each: value f32, time f32)."""
    if ramp_addr + 16 > file_end:
        return {"_err": "ramp_hdr_oob"}
    count, _pad, points_rp = struct.unpack_from("<IIq", buf, ramp_addr)
    if count == 0:
        return {"count": 0, "points": []}
    if count > 256:
        return {"count": count, "_err": "huge_count"}
    pts_addr = ramp_addr + points_rp
    if pts_addr + count * 8 > file_end:
        return {"count": count, "_err": "points_oob"}
    points = []
    for i in range(count):
        v, t = struct.unpack_from("<2f", buf, pts_addr + i * 8)
        points.append({"value": v, "time": t})
    return {"count": count, "points": points}


def _decode_color(buf: bytes | mmap.mmap, color_addr: int, file_end: int) -> dict:
    """16-byte Color header + ColorKey[count] (20B each: r,g,b,a,time f32)."""
    if color_addr + 16 > file_end:
        return {"_err": "color_hdr_oob"}
    count, _pad, points_rp = struct.unpack_from("<IIq", buf, color_addr)
    if count == 0:
        return {"count": 0, "points": []}
    if count > 256:
        return {"count": count, "_err": "huge_count"}
    pts_addr = color_addr + points_rp
    if pts_addr + count * 20 > file_end:
        return {"count": count, "_err": "points_oob"}
    points = []
    for i in range(count):
        r, g, b, a, t = struct.unpack_from("<5f", buf, pts_addr + i * 20)
        points.append({"r": r, "g": g, "b": b, "a": a, "time": t})
    return {"count": count, "points": points}


def _decode_light_color_animation(
    buf: bytes | mmap.mmap, color_addr: int, file_end: int,
) -> dict:
    """16-byte light-color curve header + time-first ColorKey[count].

    Effect component kind=light uses the same 16-byte count/relptr
    container as ``Color``, but the key payload order is
    ``time,r,g,b,a`` rather than tint's ``r,g,b,a,time``.
    """
    if color_addr + 16 > file_end:
        return {"_err": "light_color_hdr_oob"}
    count, _pad, points_rp = struct.unpack_from("<IIq", buf, color_addr)
    if count == 0:
        return {"count": 0, "points": []}
    if count > 256:
        return {"count": count, "_err": "huge_count"}
    pts_addr = color_addr + points_rp
    if pts_addr + count * 20 > file_end:
        return {"count": count, "_err": "points_oob"}
    points = []
    for i in range(count):
        t, r, g, b, a = struct.unpack_from("<5f", buf, pts_addr + i * 20)
        points.append({"r": r, "g": g, "b": b, "a": a, "time": t})
    return {"count": count, "points": points}


def _decode_time_value_ramp(
    buf: bytes | mmap.mmap, ramp_addr: int, file_end: int,
) -> dict:
    """16-byte curve header + key[count] stored as ``time,value`` pairs."""
    if ramp_addr + 16 > file_end:
        return {"_err": "time_value_ramp_hdr_oob"}
    count, _pad, points_rp = struct.unpack_from("<IIq", buf, ramp_addr)
    if count == 0:
        return {"count": 0, "points": []}
    if count > 256:
        return {"count": count, "_err": "huge_count"}
    pts_addr = ramp_addr + points_rp
    if pts_addr + count * 8 > file_end:
        return {"count": count, "_err": "points_oob"}
    points = []
    for i in range(count):
        t, v = struct.unpack_from("<2f", buf, pts_addr + i * 8)
        points.append({"value": v, "time": t})
    return {"count": count, "points": points}


def _decode_scalar_vg(
    buf: bytes | mmap.mmap, slot_addr: int, file_end: int,
) -> dict:
    """Decode a 16-byte scalar ValueGenerator slot.

    ``type`` ∈ {-1=none, 0=linear, 1=constant, 2=ramp}. Payload follows
    the relptr; see :func:`_decode_ramp` for the ramp payload shape.
    """
    if slot_addr + 16 > file_end:
        return {"_err": "slot_oob"}
    vg_type, _pad, payload_rp = struct.unpack_from("<iIq", buf, slot_addr)
    if vg_type == -1:
        return {"type": "none"}
    payload = slot_addr + payload_rp
    if payload < 0 or payload > file_end:
        return {"type": int(vg_type), "_err": "payload_oob"}
    if vg_type == 0:  # linearGenerator
        if payload + 8 > file_end:
            return {"type": "linear", "_err": "linear_oob"}
        a, b = struct.unpack_from("<2f", buf, payload)
        return {"type": "linear", "from": a, "to": b}
    if vg_type == 1:  # constantGenerator
        if payload + 4 > file_end:
            return {"type": "constant", "_err": "const_oob"}
        return {"type": "constant", "value": struct.unpack_from("<f", buf, payload)[0]}
    if vg_type == 2:  # rampValueGenerator
        if payload + 24 > file_end:
            return {"type": "ramp", "_err": "ramp_oob"}
        ramp = _decode_ramp(buf, payload, file_end)
        param_type, sampling_type = struct.unpack_from("<II", buf, payload + 0x10)
        return {
            "type": "ramp",
            "ramp": ramp,
            "parameterType": PS_VALG_RAMP_PARAMETER.get(param_type, int(param_type)),
            "samplingType": PS_VALG_RAMP_SAMPLING.get(sampling_type, int(sampling_type)),
        }
    return {"type": int(vg_type), "_err": "unknown_vg_type"}


def _decode_vgt_body(
    buf: bytes | mmap.mmap, body_addr: int, vgt_type: int, file_end: int,
) -> dict:
    """Decode one PS_VGT body (box / point / cylinder / sphere / line)."""
    if vgt_type == 0:  # box (24B)
        if body_addr + 24 > file_end:
            return {"_err": "box_oob"}
        corner = struct.unpack_from("<3f", buf, body_addr)
        opposite = struct.unpack_from("<3f", buf, body_addr + 0x0c)
        return {"corner": list(corner), "opposite": list(opposite)}
    if vgt_type == 1:  # point (12B)
        if body_addr + 12 > file_end:
            return {"_err": "point_oob"}
        return {"position": list(struct.unpack_from("<3f", buf, body_addr))}
    if vgt_type == 2:  # cylinder (0x40B; confirmed via FUN_1407149d0)
        if body_addr + 0x40 > file_end:
            return {"_err": "cyl_oob"}
        origin = struct.unpack_from("<3f", buf, body_addr)
        max_r = struct.unpack_from("<f", buf, body_addr + 0x0c)[0]
        basis_u = struct.unpack_from("<3f", buf, body_addr + 0x10)
        min_r = struct.unpack_from("<f", buf, body_addr + 0x1c)[0]
        basis_v = struct.unpack_from("<3f", buf, body_addr + 0x20)
        diff = struct.unpack_from("<3f", buf, body_addr + 0x2c)
        scale = struct.unpack_from("<2f", buf, body_addr + 0x38)  # Vector2 @+0x38 (confirmed fx::Vector2; body ends at 0x40)
        return {
            "origin": list(origin), "maxRadius": max_r,
            "basisU": list(basis_u), "minRadius": min_r,
            "basisV": list(basis_v), "difference": list(diff),
            "scale": list(scale),
        }
    if vgt_type == 3:  # sphere (20B)
        if body_addr + 20 > file_end:
            return {"_err": "sph_oob"}
        center = struct.unpack_from("<3f", buf, body_addr)
        min_r, max_r = struct.unpack_from("<2f", buf, body_addr + 0x0c)
        return {"center": list(center), "minRadius": min_r, "maxRadius": max_r}
    if vgt_type == 4:  # line (24B)
        if body_addr + 24 > file_end:
            return {"_err": "line_oob"}
        corner = struct.unpack_from("<3f", buf, body_addr)
        diff = struct.unpack_from("<3f", buf, body_addr + 0x0c)
        return {"corner": list(corner), "difference": list(diff)}
    return {"_err": f"unknown_vgt_{vgt_type}"}


def _decode_variant_vg(
    buf: bytes | mmap.mmap, slot_addr: int, file_end: int,
) -> dict:
    """Decode a 16-byte variant ValueGenerator slot.

    Variant VGs carry PS_VGT volume-generator prototypes. They are used
    by creator initial position/velocity, jitter velocity/position, and
    the Emitter initial position/velocity slots in the live corpus.
    """
    if slot_addr + 16 > file_end:
        return {"_err": "slot_oob"}
    outer_type, count, protos_rp = struct.unpack_from("<IIq", buf, slot_addr)
    if count == 0:
        return {"outer_type": int(outer_type), "count": 0, "prototypes": []}
    if count > 16:
        return {"_err": "huge_count", "count": int(count)}
    proto_arr = slot_addr + protos_rp
    if proto_arr + count * 16 > file_end:
        return {"_err": "proto_arr_oob"}
    protos: list[dict] = []
    for i in range(count):
        proto_off = proto_arr + i * 16
        vgt_type, _pad, body_rp = struct.unpack_from("<iIq", buf, proto_off)
        p = {"vgt_type": PS_VGT.get(int(vgt_type), f"unk_{vgt_type}")}
        if vgt_type == -1:
            protos.append(p)
            continue
        body_addr = proto_off + body_rp
        if body_addr < 0 or body_addr > file_end:
            p["_err"] = "body_oob"
        else:
            p["body"] = _decode_vgt_body(buf, body_addr, int(vgt_type), file_end)
        protos.append(p)
    return {"outer_type": int(outer_type), "count": int(count), "prototypes": protos}


def _read_cstr(
    buf: bytes | mmap.mmap, addr: int, file_end: int, max_len: int = 1024,
) -> str | None:
    """Read a NUL-terminated ASCII printable string at ``addr``.

    Used to deref pool-form ResourceRefs into the OOL string pool. Bails
    on any non-printable byte before the NUL — pool addresses computed
    from junk relptrs frequently land in noise; the strict ASCII filter
    keeps false positives out of the parser output.
    """
    if addr < 0 or addr >= file_end:
        return None
    end = min(file_end, addr + max_len)
    raw = bytes(buf[addr:end])
    nul = raw.find(b"\x00")
    if nul <= 0:
        return None
    body = raw[:nul]
    if not all(0x20 <= c < 0x7f for c in body):
        return None
    return body.decode("ascii")


def _read_resource_ref(
    buf: bytes | mmap.mmap, addr: int, file_end: int,
) -> str | None:
    """Decode a 16-byte ResourceRef.

    Two encodings are observed for action-body resource refs.

    Inline encoding:

        u64  length      // +0x00 — string length incl. trailing null
        u32  tag         // +0x08 — 0x10 (inline discriminant)
        u32  pad         // +0x0c — 0
        [bytes follow immediately: length ASCII + NUL]

    Pool encoding:

        i64  relptr      // +0x00 — target = addr + relptr - 8
        u32  tag         // +0x08 — non-0x10 discriminator
        u32  pad         // +0x0c — 0

    Renderer/animation texture refs use a third texture-specific shape;
    callers for those fields must use :func:`_read_texture_ref`.
    """
    if addr + 16 > file_end:
        return None
    # Most action effectName refs use the same length/pad/relptr shape as
    # renderer texture refs. Inline refs also satisfy this form: the low u32 is
    # the length, the high u32 is zero, and the inline tag at +0x08 is a
    # relptr-like 0x10 to the bytes immediately after the 16-byte header.
    length, len_pad, relptr = struct.unpack_from("<IIq", buf, addr)
    if 0 < length < 2048 and len_pad == 0 and relptr != 0:
        s = _read_cstr(buf, addr + relptr, file_end, max_len=length)
        if s is not None and len(s.encode("ascii")) + 1 == length:
            return s
    a, tag, pad = struct.unpack_from("<qII", buf, addr)
    if pad != 0:
        return None
    if tag == 0x10 and 0 < a < 1024 and (addr + 16 + a) <= file_end:
        body = bytes(buf[addr + 16:addr + 16 + a])
        if body and body[-1] == 0:
            try:
                return body[:-1].decode("ascii")
            except UnicodeDecodeError:
                return None
    if tag != 0x10 and a != 0:
        return _read_cstr(buf, addr + a - 8, file_end)
    return None


def _read_texture_ref(
    buf: bytes | mmap.mmap, addr: int, file_end: int,
) -> str | None:
    """Decode the 16-byte renderer/animation texture-name reference.

    On-disk layout (empirically verified against Fire_small.xml/high
    System[0] on WoWS build 12267945):

        u32  length      // +0x00 — string length incl. trailing null
        u32  pad         // +0x04 — 0
        i64  relptr      // +0x08 — target = addr + relptr (NO -8 quirk)

    Resolves to the ASCII path stored elsewhere in the OOL pool. The
    pad MUST be zero, length MUST be 1..2047, and the string MUST be
    NUL-terminated ASCII printable — anything else means the slot is
    empty or the field offset is wrong. This shape is distinct from the
    other two ResourceRef encodings the format spec describes
    (:func:`_read_resource_ref` for inline; pool-form not implemented).

    Verified live: Fire_small.xml System[0] +0x00 reads
    `length=36, relptr=0x0e40` which deref's to
    ``"particles/animated/Sparkles_8x8.dds"``.
    """
    if addr + 16 > file_end:
        return None
    length, pad, relptr = struct.unpack_from("<IIq", buf, addr)
    if pad != 0 or length == 0 or length > 2047:
        return None
    target = addr + relptr
    if target < 0 or target >= file_end:
        return None
    end = min(file_end, target + length)
    raw = bytes(buf[target:end])
    if len(raw) < length:
        return None
    # The recorded length includes the trailing NUL — last byte must be 0.
    if raw[length - 1] != 0:
        return None
    body = raw[: length - 1]
    if not body:
        return None
    if not all(0x20 <= c < 0x7f for c in body):
        return None
    return body.decode("ascii")


def _decode_light_body(
    buf: bytes | mmap.mmap, body_addr: int, file_end: int,
) -> dict:
    """Decode a component kind=light body.

    Native schema field emitter at 0x1406fec20 names these fields as
    colorAnimation, radiusAnimation, color, localPosition, radius,
    minQuality, animatedColor, animatedRadius. The body has the same
    16-byte header shape as PCAT/PSAT action bodies; fields start at
    ``body + fields_relptr``.
    """
    if body_addr + 16 > file_end:
        return {"_err": "light_hdr_oob"}
    _kind_idx, _pad, fields_rp = struct.unpack_from("<iIq", buf, body_addr)
    fa = body_addr + fields_rp
    if fa + 0x6a > file_end:
        return {"_err": "light_fields_oob"}
    color = list(struct.unpack_from("<4f", buf, fa + 0x40))
    local_position = list(struct.unpack_from("<3f", buf, fa + 0x50))
    radius = struct.unpack_from("<f", buf, fa + 0x60)[0]
    min_quality = struct.unpack_from("<I", buf, fa + 0x64)[0]
    return {
        "colorAnimationPeriod": struct.unpack_from("<f", buf, fa + 0x00)[0],
        "colorAnimation": _decode_light_color_animation(buf, fa + 0x10, file_end),
        "radiusAnimationPeriod": struct.unpack_from("<f", buf, fa + 0x20)[0],
        "radiusAnimation": _decode_time_value_ramp(buf, fa + 0x30, file_end),
        "color": color,
        "localPosition": local_position,
        "radius": radius,
        "minQuality": int(min_quality),
        "animatedColor": bool(buf[fa + 0x68]),
        "animatedRadius": bool(buf[fa + 0x69]),
    }


# Per-action decoders. Each returns a dict of named fields for the action
# body's typed payload (everything past the 16-byte header).
def _decode_action_body(
    buf: bytes | mmap.mmap, body_addr: int, kind: str, action_name: str, file_end: int,
) -> dict:
    """Decode one PCAT / PSAT action body. ``kind`` ∈ {'PCAT','PSAT'}."""
    if body_addr + 16 > file_end:
        return {"_err": "hdr_oob"}
    _aidx, _pad, fields_rp = struct.unpack_from("<iIq", buf, body_addr)
    fa = body_addr + fields_rp
    out: dict[str, Any] = {}

    if action_name == "dampfer":
        out["velocityGenerator"] = _decode_scalar_vg(buf, fa + 0x00, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x10)[0]
    elif action_name == "spawner":
        out["spawnRamp"] = _decode_ramp(buf, fa + 0x00, file_end)
        eff = _read_resource_ref(buf, fa + 0x10, file_end)
        if eff is not None:
            out["effectName"] = eff
    elif action_name == "stream":
        out["vector"] = list(struct.unpack_from("<3f", buf, fa + 0x00))
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x0c)[0]
        out["halfLife"] = struct.unpack_from("<f", buf, fa + 0x10)[0]
        out["switchCoordinateStyle"] = bool(buf[fa + 0x14])
    elif action_name == "jitter":
        out["velocityGenerator"] = _decode_variant_vg(buf, fa + 0x00, file_end)
        out["positionGenerator"] = _decode_variant_vg(buf, fa + 0x10, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x20)[0]
        out["affectPosition"] = bool(buf[fa + 0x24])
        out["affectVelocity"] = bool(buf[fa + 0x25])
    elif action_name == "force":
        out["forceXGenerator"] = _decode_scalar_vg(buf, fa + 0x00, file_end)
        out["forceYGenerator"] = _decode_scalar_vg(buf, fa + 0x10, file_end)
        out["forceZGenerator"] = _decode_scalar_vg(buf, fa + 0x20, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x30)[0]
    elif action_name == "resizer":
        # 12-byte inline body (corpus-verified, 310/310 systems). Layout:
        #   +0x00 i32 type tag (==0 in all 310; mirrors a linear-VG type)
        #   +0x04 f32 sizeFrom   +0x08 f32 sizeTo
        # Either endpoint may be larger (e.g. 1000->250 shrink, 5->1000 grow),
        # so these are interpolation endpoints, not (rate, target). Raw BW units
        # (same space as emitter sizeGenerator; consumer applies NATIVE_TO_METRES
        # at draw). NOTE the i32 tag name is unconfirmed (==0 everywhere) and the
        # per-particle APPLY semantics (lerp axis / overwrite vs multiply) are
        # NOT yet RE-resolved — consumer keeps the field inert until then.
        out["_typeTag"] = struct.unpack_from("<i", buf, fa + 0x00)[0]
        out["sizeFrom"] = struct.unpack_from("<f", buf, fa + 0x04)[0]
        out["sizeTo"] = struct.unpack_from("<f", buf, fa + 0x08)[0]
    elif action_name == "plane":
        eff = _read_resource_ref(buf, fa + 0x00, file_end)
        if eff is not None:
            out["effectName"] = eff
        out["planeEquation"] = list(struct.unpack_from("<4f", buf, fa + 0x10))
        out["reaction"] = int(buf[fa + 0x20])
        out["strength"] = struct.unpack_from("<f", buf, fa + 0x24)[0]
        out["stopAge"] = struct.unpack_from("<f", buf, fa + 0x28)[0]
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x2c)[0]
        out["useWorldSpace"] = bool(buf[fa + 0x30])
    elif action_name == "orbitor":
        out["angularVelocityGenerator"] = _decode_scalar_vg(buf, fa + 0x00, file_end)
        out["point"] = list(struct.unpack_from("<3f", buf, fa + 0x10))
        out["axis"] = list(struct.unpack_from("<3f", buf, fa + 0x1c))
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x28)[0]
        out["affectPosition"] = bool(buf[fa + 0x2c])
        out["affectVelocity"] = bool(buf[fa + 0x2d])
    elif action_name == "scaler":
        out["sizeGenerator"] = _decode_scalar_vg(buf, fa + 0x00, file_end)
        out["scaleXGenerator"] = _decode_scalar_vg(buf, fa + 0x10, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x20)[0]
    elif action_name == "tint":
        out["tint"] = _decode_color(buf, fa + 0x00, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x10)[0]
        out["period"] = struct.unpack_from("<f", buf, fa + 0x14)[0]
        out["repeat"] = bool(buf[fa + 0x18])
        out["useVelocity"] = bool(buf[fa + 0x19])
    elif action_name in ("sphere", "cylinder"):
        eff = _read_resource_ref(buf, fa + 0x00, file_end)
        if eff is not None:
            out["effectName"] = eff
        out["position"] = list(struct.unpack_from("<3f", buf, fa + 0x10))
        out["reaction"] = int(buf[fa + 0x1c])
        out["strength"] = struct.unpack_from("<f", buf, fa + 0x20)[0]
        out["stopAge"] = struct.unpack_from("<f", buf, fa + 0x24)[0]
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x28)[0]
        out["radius"] = struct.unpack_from("<f", buf, fa + 0x2c)[0]
    elif action_name == "alphaSetter":
        out["ramp"] = _decode_ramp(buf, fa + 0x00, file_end)
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x10)[0]
    elif action_name == "magnet":
        out["attractorPoint"] = list(struct.unpack_from("<3f", buf, fa + 0x00))
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x0c)[0]
        out["minimalDistance"] = struct.unpack_from("<f", buf, fa + 0x10)[0]
        out["strength"] = struct.unpack_from("<f", buf, fa + 0x14)[0]
    elif action_name == "velocityField":
        out["topLeftFront"] = list(struct.unpack_from("<3f", buf, fa + 0x00))
        out["bottomRightBack"] = list(struct.unpack_from("<3f", buf, fa + 0x0c))
        out["stopAge"] = struct.unpack_from("<f", buf, fa + 0x18)[0]
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x1c)[0]
        out["velocityScale"] = struct.unpack_from("<f", buf, fa + 0x20)[0]
        out["influence"] = struct.unpack_from("<f", buf, fa + 0x24)[0]
        fld = _read_resource_ref(buf, fa + 0x28, file_end)
        if fld is not None:
            out["fieldSourceName"] = fld
    elif action_name == "box":
        eff = _read_resource_ref(buf, fa + 0x00, file_end)
        if eff is not None:
            out["effectName"] = eff
        out["opposite"] = list(struct.unpack_from("<3f", buf, fa + 0x10))
        out["corner"] = list(struct.unpack_from("<3f", buf, fa + 0x1c))
        out["strength"] = struct.unpack_from("<f", buf, fa + 0x28)[0]
        out["stopAge"] = struct.unpack_from("<f", buf, fa + 0x2c)[0]
        out["reaction"] = int(buf[fa + 0x30])
        out["delay"] = struct.unpack_from("<f", buf, fa + 0x34)[0]
    elif action_name == "creator":
        out["rateRamp"] = _decode_ramp(buf, fa + 0x00, file_end)
        out["initialPositionGenerator"] = _decode_variant_vg(buf, fa + 0x10, file_end)
        out["initialVelocityGenerator"] = _decode_variant_vg(buf, fa + 0x20, file_end)
        (out["systemAgeLimitMin"], out["systemAgeLimitMax"]) = \
            struct.unpack_from("<2f", buf, fa + 0x30)
        # +0x38 velocityInheritanceFactor is an IEEE-754 f32, NOT an i32: the
        # native descriptor tags it 'i' = 4-byte slot, but every corpus value is
        # a canonical float bit-pattern (0x3F800000=1.0, 0x3E99999A=0.3; reading
        # as int yields 1065353216 garbage). It mirrors the emitter sibling
        # inheritVelocityFactor (+0x84, f32). +0x3c minRandomRateBound IS a
        # genuine i32 (sentinel -1 = unbounded).
        out["velocityInheritanceFactor"] = struct.unpack_from("<f", buf, fa + 0x38)[0]
        out["minRandomRateBound"] = struct.unpack_from("<i", buf, fa + 0x3c)[0]
        out["repeated"] = bool(buf[fa + 0x40])
        out["useSmoothRate"] = bool(buf[fa + 0x41])
        out["useWorldCoordinates"] = bool(buf[fa + 0x42])
    return out


def _decode_renderer(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """Decode the Renderer block at System +0x000 (0xa0 bytes).

    Field offsets confirmed against the WoWS binary (build 12506899,
    FUN_1406f0c30) — supersedes the 2026-05-22 statistical probe.

    Surfaced fields:
      +0x00 ``textureName0`` (16 B ResourceRef)
      +0x10 ``textureName1`` (16 B ResourceRef)
      +0x20 ``yawRateRamp``  (16 B Ramp)
      +0x30 ``explicitOrientation`` (3-float Vec3)
      +0x3c ``customCenterOffset`` (2-float Vec2)
      +0x44 ``spinRateRange`` f32, +0x48 ``spinRateBase`` f32
      +0x4c ``lightingShineness`` f32
      +0x50 ``initialOrientationRange`` f32
      +0x54 ``lightingAmbient`` f32
      +0x58 ``initialOrientationBase`` f32
      +0x5c ``hideStartCos`` f32
      +0x60 ``scaleX`` f32
      +0x64 ``lightingDiffuse`` f32, +0x68 ``lightingTransmission`` f32
      +0x6c ``lightWrapAmount`` f32, +0x70 ``shadowsStrength`` f32
      +0x74 ``opacityMultiplier`` f32
      +0x78 ``hideSpeed`` f32, +0x7c ``softParticleDepthScale`` f32
      +0x80 ``rotationCenter`` i32 -> PS_RRC label
      +0x84 ``lightingType``  i32 -> PS_RLT label
      +0x88 ``blendType``     i32 -> PS_RBT label
      +0x8c ``sortType``      i32 (raw; enum fx::RendererSortType, labels TBD)
      +0x90 ``tilingU`` f32, +0x94 ``tilingV`` f32
      +0x98..+0x9b ``explicitOrientationLocal`` / ``billboard`` /
             ``velocityOriented`` / ``background`` bools
      +0x9c ``flipTexcoordU`` bool, +0x9d ``flipTexcoordV`` bool
    """
    base = sys_off  # Renderer is the first sub-struct
    out: dict[str, Any] = {}
    t0 = _read_texture_ref(buf, base + 0x00, file_end)
    if t0:
        out["textureName0"] = t0
    t1 = _read_texture_ref(buf, base + 0x10, file_end)
    if t1:
        out["textureName1"] = t1
    out["yawRateRamp"] = _decode_ramp(buf, base + 0x20, file_end)
    out["explicitOrientation"] = [
        float(v) for v in struct.unpack_from("<3f", buf, base + 0x30)
    ]
    out["customCenterOffset"] = [
        float(v) for v in struct.unpack_from("<2f", buf, base + 0x3c)
    ]
    (
        spin_rate_range,
        spin_rate_base,
        lighting_shineness,
        initial_orientation_range,
        lighting_ambient,
        initial_orientation_base,
        hide_start_cos,
        scale_x,
        lighting_diffuse,
        lighting_transmission,
        light_wrap_amount,
        shadows_strength,
        opacity_multiplier,
        hide_speed,
        soft_particle_depth_scale,
    ) = struct.unpack_from("<15f", buf, base + 0x44)
    out["spinRateRange"] = float(spin_rate_range)
    out["spinRateBase"] = float(spin_rate_base)
    out["lightingShineness"] = float(lighting_shineness)
    out["initialOrientationRange"] = float(initial_orientation_range)
    out["lightingAmbient"] = float(lighting_ambient)
    out["initialOrientationBase"] = float(initial_orientation_base)
    out["hideStartCos"] = float(hide_start_cos)
    out["scaleX"] = float(scale_x)
    out["lightingDiffuse"] = float(lighting_diffuse)
    out["lightingTransmission"] = float(lighting_transmission)
    out["lightWrapAmount"] = float(light_wrap_amount)
    out["shadowsStrength"] = float(shadows_strength)
    out["opacityMultiplier"] = float(opacity_multiplier)
    out["hideSpeed"] = float(hide_speed)
    out["softParticleDepthScale"] = float(soft_particle_depth_scale)
    rotation_center, lighting_type, blend_type, sort_type = struct.unpack_from(
        "<4i", buf, base + 0x80,
    )
    tiling_u, tiling_v = struct.unpack_from("<2f", buf, base + 0x90)
    explicit_orientation_local = bool(buf[base + 0x98])
    billboard = bool(buf[base + 0x99])
    velocity_oriented = bool(buf[base + 0x9a])
    background = bool(buf[base + 0x9b])
    flip_u = bool(buf[base + 0x9c])
    flip_v = bool(buf[base + 0x9d])
    out["rotationCenter"] = PS_RRC.get(int(rotation_center), str(rotation_center))
    # +0x84 is fx::RendererLightingType (PS_RLT), NOT a blend sub-mode flag.
    # Confirmed via Ghidra (FUN_1406f2150, type_info 0x142a81bb8); the earlier
    # "blendFlag84 co-varies with GRADIENT_MAP" reading was coincidental.
    out["lightingType"] = PS_RLT.get(int(lighting_type), str(lighting_type))
    out["blendType"] = PS_RBT.get(int(blend_type), str(blend_type))
    out["sortType"] = int(sort_type)
    out["tilingU"] = float(tiling_u)
    out["tilingV"] = float(tiling_v)
    out["explicitOrientationLocal"] = explicit_orientation_local
    out["billboard"] = billboard
    out["velocityOriented"] = velocity_oriented
    out["background"] = background
    out["flipTexcoordU"] = flip_u
    out["flipTexcoordV"] = flip_v
    return out


def _decode_animation(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """Decode the Animation block at System +0x130 (0x40 bytes).

    Field offsets confirmed against the WoWS binary (build 12267945,
    FUN_1406f37a0) — supersedes the 2026-05-22 statistical probe, which
    had the two trailing bools (+0x3c / +0x3d) swapped.

      +0x00 ``frameRateRamp``           (16 B Ramp)
      +0x10 ``motionVectorsTexture``    (16 B ResourceRef)
      +0x20 ``framesPerY`` u32          \\
      +0x24 ``framesPerX`` u32           |  sprite atlas grid
      +0x28 ``framesRangeBegin`` u32     |  (animationType=0 + grid=1x1
      +0x2c ``framesRangeEnd`` u32      /   means "no animation")
      +0x30 ``animationPeriod`` f32
      +0x34 ``motionVectorsDistortion`` f32
      +0x38 ``animationType`` u32 -> PS_PAT label
      +0x3c ``randomFrameOnly`` u8 bool
      +0x3d ``useEmissionAlphaFromMV`` u8 bool
    """
    base = sys_off + 0x130
    out: dict[str, Any] = {}
    out["frameRateRamp"] = _decode_ramp(buf, base + 0x00, file_end)
    mv = _read_texture_ref(buf, base + 0x10, file_end)
    if mv:
        out["motionVectorsTexture"] = mv
    fp_y, fp_x, rng_begin, rng_end = struct.unpack_from(
        "<4I", buf, base + 0x20,
    )
    anim_period, mv_distortion = struct.unpack_from("<2f", buf, base + 0x30)
    anim_type = struct.unpack_from("<I", buf, base + 0x38)[0]
    out["framesPerY"] = int(fp_y)
    out["framesPerX"] = int(fp_x)
    out["framesRangeBegin"] = int(rng_begin)
    out["framesRangeEnd"] = int(rng_end)
    out["animationPeriod"] = float(anim_period)
    out["motionVectorsDistortion"] = float(mv_distortion)
    out["animationType"] = PS_PAT.get(int(anim_type), str(anim_type))
    out["randomFrameOnly"] = bool(buf[base + 0x3c])
    out["useEmissionAlphaFromMV"] = bool(buf[base + 0x3d])
    return out


def _decode_emitter(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """Decode the System's Emitter sub-struct (+0x0a0, 0x90 bytes).

    Six slots use scalar ValueGenerators. The initial position/velocity
    slots use PS_VGT variant generators in the live corpus, matching the
    creator/jitter volume-generator shape rather than scalar VG.
    """
    base = sys_off + 0x0a0
    out: dict[str, Any] = {
        "rateGenerator":           _decode_scalar_vg(buf, base + 0x00, file_end),
        "initialPositionGenerator": _decode_variant_vg(buf, base + 0x10, file_end),
        "initialVelocityGenerator": _decode_variant_vg(buf, base + 0x20, file_end),
        "sizeGenerator":           _decode_scalar_vg(buf, base + 0x30, file_end),
        "ageScaleGenerator":       _decode_scalar_vg(buf, base + 0x40, file_end),
        "ageScaleAuxGenerator":    _decode_scalar_vg(buf, base + 0x50, file_end),
        "delayGenerator":          _decode_scalar_vg(buf, base + 0x60, file_end),
        "sleepPeriodGenerator":    _decode_scalar_vg(buf, base + 0x70, file_end),
        "activePeriod":            struct.unpack_from("<f", buf, base + 0x80)[0],
        "inheritVelocityFactor":   struct.unpack_from("<f", buf, base + 0x84)[0],
        "particleDistributionStrength": struct.unpack_from("<f", buf, base + 0x88)[0],
        "snapToSeaLevel":          bool(buf[base + 0x8c]),
    }
    return out


def _decode_general(buf: bytes | mmap.mmap, sys_off: int) -> dict:
    """GeneralSection (+0x170, 0x18 bytes): capacity / max-instances / age."""
    base = sys_off + 0x170
    capacity, max_instances = struct.unpack_from("<II", buf, base)
    max_age, camera_attach = struct.unpack_from("<2f", buf, base + 0x08)
    coord_style = struct.unpack_from("<i", buf, base + 0x10)[0]
    refl = bool(buf[base + 0x14])
    prewarm = bool(buf[base + 0x15])
    return {
        "capacity": int(capacity),
        "maxInstancesCount": int(max_instances),
        "maxParticleAge": float(max_age),
        "cameraAttachOffset": float(camera_attach),
        "coordinateStyle": int(coord_style),
        "reflectionVisible": refl,
        "prewarm": prewarm,
    }


def _decode_system_distance(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """Decode System.DistanceSection (+0x188).

    Native distance application (`FUN_1406c9c40`) consumes the config array as
    the same 0x20 `{Ramp, flags[]}` shape used by intensity configs, keyed by
    camera distance and folded into the runtime PS_IC multiplier block.
    """
    base = sys_off + 0x188
    max_distance = struct.unpack_from("<f", buf, base)[0]
    configs_count = struct.unpack_from("<I", buf, base + 0x04)[0]
    configs_rp = struct.unpack_from("<q", buf, base + 0x08)[0]
    out: dict[str, Any] = {
        "maxDistance": float(max_distance),
        "configsCount": int(configs_count),
        "configs": [],
    }
    if configs_count == 0:
        return out
    if configs_count > 32 or configs_rp == 0:
        out["_err"] = "configs_invalid"
        return out
    configs_addr = base + configs_rp
    if (
        configs_addr < 0
        or configs_addr + configs_count * SYSTEM_DISTANCE_CONFIG_SIZE > file_end
    ):
        out["_err"] = "configs_oob"
        return out

    configs: list[dict[str, Any]] = []
    for i in range(int(configs_count)):
        cfg_off = configs_addr + i * SYSTEM_DISTANCE_CONFIG_SIZE
        flags_count = struct.unpack_from("<I", buf, cfg_off + 0x10)[0]
        flags_rp = struct.unpack_from("<q", buf, cfg_off + 0x18)[0]
        cfg: dict[str, Any] = {
            "ramp": _decode_ramp(buf, cfg_off, file_end),
            "flagsCount": int(flags_count),
            "flags": [],
        }
        if flags_count == 0:
            configs.append(cfg)
            continue
        if flags_count > 32 or flags_rp == 0:
            cfg["_err"] = "flags_invalid"
            configs.append(cfg)
            continue
        flags_addr = cfg_off + flags_rp
        if flags_addr < 0 or flags_addr + flags_count * 4 > file_end:
            cfg["_err"] = "flags_oob"
            configs.append(cfg)
            continue
        flags = [
            int(struct.unpack_from("<I", buf, flags_addr + j * 4)[0])
            for j in range(int(flags_count))
        ]
        cfg["flags"] = flags
        cfg["flagNames"] = [PS_IC.get(flag, f"unk_{flag}") for flag in flags]
        configs.append(cfg)
    out["configs"] = configs
    return out


def _decode_system_intensities(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """Decode System.Intensities (+0x1a8).

    Each channel owns zero or more configs. A config is a scalar ramp sampled
    by the live EffectManager channel value (usually 0..10), plus a compact
    `flags[]` array of PS_IC target IDs. The flags relptr is relative to the
    config start (`config + flags_relptr`), not the nested count field.
    """
    base = sys_off + 0x1a8
    channel_count, _pad, channels_rp = struct.unpack_from("<IIq", buf, base)
    out: dict[str, Any] = {
        "channelCount": int(channel_count),
        "channels": [],
    }
    if channel_count == 0:
        return out
    if channel_count > 32 or channels_rp == 0:
        out["_err"] = "channels_invalid"
        return out
    channels_addr = base + channels_rp
    if (
        channels_addr < 0
        or channels_addr + channel_count * SYSTEM_INTENSITY_CHANNEL_SIZE > file_end
    ):
        out["_err"] = "channels_oob"
        return out

    channels: list[dict[str, Any]] = []
    for c in range(int(channel_count)):
        ch_off = channels_addr + c * SYSTEM_INTENSITY_CHANNEL_SIZE
        configs_count, _pad, configs_rp = struct.unpack_from("<IIq", buf, ch_off)
        ch: dict[str, Any] = {
            "configsCount": int(configs_count),
            "configs": [],
        }
        if configs_count == 0:
            channels.append(ch)
            continue
        if configs_count > 32 or configs_rp == 0:
            ch["_err"] = "configs_invalid"
            channels.append(ch)
            continue
        configs_addr = ch_off + configs_rp
        if (
            configs_addr < 0
            or configs_addr + configs_count * SYSTEM_INTENSITY_CONFIG_SIZE > file_end
        ):
            ch["_err"] = "configs_oob"
            channels.append(ch)
            continue

        configs: list[dict[str, Any]] = []
        for i in range(int(configs_count)):
            cfg_off = configs_addr + i * SYSTEM_INTENSITY_CONFIG_SIZE
            flags_count = struct.unpack_from("<I", buf, cfg_off + 0x10)[0]
            flags_rp = struct.unpack_from("<q", buf, cfg_off + 0x18)[0]
            cfg: dict[str, Any] = {
                "ramp": _decode_ramp(buf, cfg_off, file_end),
                "flagsCount": int(flags_count),
                "flags": [],
            }
            if flags_count == 0:
                configs.append(cfg)
                continue
            if flags_count > 32 or flags_rp == 0:
                cfg["_err"] = "flags_invalid"
                configs.append(cfg)
                continue
            flags_addr = cfg_off + flags_rp
            if flags_addr < 0 or flags_addr + flags_count * 4 > file_end:
                cfg["_err"] = "flags_oob"
                configs.append(cfg)
                continue
            flags = [
                int(struct.unpack_from("<I", buf, flags_addr + j * 4)[0])
                for j in range(int(flags_count))
            ]
            cfg["flags"] = flags
            cfg["flagNames"] = [PS_IC.get(flag, f"unk_{flag}") for flag in flags]
            configs.append(cfg)
        ch["configs"] = configs
        channels.append(ch)

    out["channels"] = channels
    return out


def _decode_system(
    buf: bytes | mmap.mmap, sys_off: int, file_end: int,
) -> dict:
    """One 0x1c8-byte System slot. Includes Emitter, General, and
    Component[componentsCount] but skips Renderer/Animation internals
    (per-field byte offsets not mapped — see spec doc's "open items").
    """
    comp_count = struct.unpack_from("<i", buf, sys_off + 0x1b8)[0]
    comp_rp = struct.unpack_from("<q", buf, sys_off + 0x1c0)[0]
    components: list[dict] = []
    if 0 < comp_count <= 256 and comp_rp != 0:
        comp_arr = sys_off + comp_rp
        if comp_arr + comp_count * COMPONENT_SIZE <= file_end:
            for c in range(comp_count):
                comp_off = comp_arr + c * COMPONENT_SIZE
                kind_raw, _p, body_rp = struct.unpack_from("<iIq", buf, comp_off)
                kname = COMP_KIND.get(int(kind_raw), f"unk_{kind_raw}")
                rec: dict[str, Any] = {"kind": kname}
                if kname in ("PCAT", "PSAT"):
                    body_addr = comp_off + body_rp
                    if body_addr + 16 <= file_end:
                        aidx = struct.unpack_from("<i", buf, body_addr)[0]
                        table = PSAT if kname == "PSAT" else PCAT
                        aname = table.get(int(aidx), f"unk_{aidx}")
                        rec["action"] = aname
                        rec["body"] = _decode_action_body(
                            buf, body_addr, kname, aname, file_end,
                        )
                elif kname == "light":
                    body_addr = comp_off + body_rp
                    if body_addr + 16 <= file_end:
                        rec["body"] = _decode_light_body(buf, body_addr, file_end)
                components.append(rec)

    return {
        "renderer": _decode_renderer(buf, sys_off, file_end),
        "animation": _decode_animation(buf, sys_off, file_end),
        "emitter": _decode_emitter(buf, sys_off, file_end),
        "general": _decode_general(buf, sys_off),
        "distance": _decode_system_distance(buf, sys_off, file_end),
        "intensities": _decode_system_intensities(buf, sys_off, file_end),
        "components": components,
    }


def _decode_effect_record(
    buf: bytes | mmap.mmap, blob_data_off: int, record_index: int, file_end: int,
) -> dict:
    """Decode the per-Effect record at ``blob_data_off + 0x10 +
    record_index*16`` and its Systems / Components chain.

    Returns a JSON-serialisable dict — the shape this module exports to
    the rest of the pipeline.
    """
    rec_off = blob_data_off + 0x10 + record_index * 16
    max_emit, sys_count, sys_rp = struct.unpack_from("<fIq", buf, rec_off)
    out: dict[str, Any] = {
        "record_index": record_index,
        "maxEmittingDuration": float(max_emit),
        "systemsCount": int(sys_count),
        "systems": [],
    }
    if sys_count == 0:
        return out
    sys_addr = rec_off + sys_rp
    if sys_addr + sys_count * SYSTEM_SIZE > file_end:
        out["_err"] = "systems_oob"
        return out
    systems: list[dict] = []
    for s in range(int(sys_count)):
        systems.append(_decode_system(buf, sys_addr + s * SYSTEM_SIZE, file_end))
    out["systems"] = systems
    return out


def _read_null_terminated(
    buf: bytes | mmap.mmap,
    start: int,
    file_end: int,
    *,
    max_len: int = 256,
) -> str | None:
    if start < 0 or start >= file_end:
        return None
    stop = min(file_end, start + max_len)
    end = start
    while end < stop and buf[end] != 0:
        end += 1
    if end == start:
        return ""
    try:
        return bytes(buf[start:end]).decode("utf-8", errors="replace")
    except Exception:
        return None


def _decode_effect_metadata_record(
    buf: bytes | mmap.mmap,
    blob_data_off: int,
    record_index: int,
    file_end: int,
) -> dict:
    """Decode blob-8 EffectMetadata intensity channel metadata.

    Blob 8 is parallel to blob 7 EffectPreset, so callers pass the preset
    record index. Each 32-byte channel entry carries a name string and the
    per-channel min/max/default scalar authored for EffectManager.
    """
    rec_off = blob_data_off + 0x10 + record_index * 16
    if rec_off < 0 or rec_off + 16 > file_end:
        return {"_err": "metadata_record_oob", "intensityChannels": []}
    count, channels_rp = struct.unpack_from("<Qq", buf, rec_off)
    out: dict[str, Any] = {
        "intensityChannelCount": int(count),
        "intensityChannels": [],
    }
    if count == 0:
        return out
    if count > 64 or channels_rp == 0:
        out["_err"] = "metadata_channels_invalid"
        return out
    channels_addr = rec_off + channels_rp
    if (
        channels_addr < 0
        or channels_addr + count * EFFECT_METADATA_CHANNEL_SIZE > file_end
    ):
        out["_err"] = "metadata_channels_oob"
        return out

    channels: list[dict[str, Any]] = []
    for i in range(int(count)):
        entry = channels_addr + i * EFFECT_METADATA_CHANNEL_SIZE
        name_len = struct.unpack_from("<I", buf, entry)[0]
        name_rp = struct.unpack_from("<q", buf, entry + 0x08)[0]
        min_i, max_i, default_i = struct.unpack_from("<3f", buf, entry + 0x10)
        kind = struct.unpack_from("<I", buf, entry + 0x1c)[0]
        name_addr = entry + name_rp
        name = _read_null_terminated(
            buf,
            name_addr,
            file_end,
            max_len=max(1, min(int(name_len), 256)),
        )
        channels.append(
            {
                "index": i,
                "name": name if name is not None else f"Intensity {i}",
                "nameLength": int(name_len),
                "minIntensity": float(min_i),
                "maxIntensity": float(max_i),
                "defaultIntensity": float(default_i),
                "channelKind": int(kind),
            },
        )
    out["intensityChannels"] = channels
    return out


# ---------------------------------------------------------------------------
# High-level reader
# ---------------------------------------------------------------------------

@dataclass
class ParticleStore:
    """Lazy reader over a single ``assets.bin`` file.

    Owns the parsed header, an mmap view of the file, and an index of
    ``"particles/vehicles/<name>.xml" → effect_record_index``. Effects
    are decoded on demand via :meth:`get`.

    Typical use::

        store = ParticleStore.open(assets_bin_path)
        eff = store.get("particles/vehicles/Fire_small.xml")
    """

    _buf: mmap.mmap | bytes
    _hdr: _AssetsBinHeader
    _effect_blob: BlobInfo
    _effect_metadata_blob: BlobInfo | None = None
    _name_index: dict[str, int] = field(default_factory=dict)
    """Full per-quality path → record_index map (`.../Fire_small.xml/high`)."""
    _base_index: dict[str, dict[str, int]] = field(default_factory=dict)
    """Base-path → {quality: record_index} map (`.../Fire_small.xml`)."""
    _base_metadata_index: dict[str, int] = field(default_factory=dict)
    """Base-path → EffectMetadata record_index map from blob 7/8."""
    _decoded_cache: dict[int, dict] = field(default_factory=dict)
    _metadata_cache: dict[int, dict] = field(default_factory=dict)
    _mmap_close: Any = None
    _file_end: int = 0

    # ------ Factory ----------------------------------------------------

    @classmethod
    def open(cls, assets_bin_path: Path | str | os.PathLike) -> ParticleStore:
        """Open ``assets.bin`` (mmap) and build the path → record index.

        The mmap stays alive for the lifetime of the store; call
        :meth:`close` (or use the context-manager form) when done. Index
        build is one linear pass over the path table — sub-second on
        a typical assets.bin.
        """
        path = Path(assets_bin_path).resolve()
        f = open(path, "rb")
        try:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            f.close()
            raise
        try:
            hdr = _read_header(mm)
            effect = next(
                (b for b in hdr.blobs if b.prototype_magic == EFFECT_BLOB_MAGIC),
                None,
            )
            if effect is None:
                raise ValueError("assets.bin has no Effect blob (magic 0xEB23E0AF)")
            preset = next(
                (b for b in hdr.blobs if b.prototype_magic == EFFECT_PRESET_BLOB_MAGIC),
                None,
            )
            metadata = next(
                (b for b in hdr.blobs if b.prototype_magic == EFFECT_METADATA_BLOB_MAGIC),
                None,
            )

            # Build name index. Walk every PathEntry; for those that
            # resolve to the Effect blob index, reconstruct the full path
            # and bind it to (record_index). The WG storage form keys
            # particle records by quality variant — ``Fire_small.xml/high``
            # and ``Fire_small.xml/low`` are distinct entries with
            # different system counts; we expose both the per-quality
            # lookup (`name_index`) and the convenience base-path lookup
            # (`base_index`) so callers can say ``"particles/vehicles/
            # Fire_small.xml"`` and get the highest-quality variant.
            effect_blob_index = hdr.blobs.index(effect)
            preset_blob_index = hdr.blobs.index(preset) if preset is not None else None
            paths_by_self_id = _build_paths_by_self_id(hdr.paths)
            name_index: dict[str, int] = {}
            base_index: dict[str, dict[str, int]] = {}
            base_metadata_index: dict[str, int] = {}
            for entry in hdr.paths:
                if entry.self_id == 0 or not entry.name:
                    continue
                loc = _r2p_lookup(mm, hdr, entry.self_id)
                if loc is None:
                    continue
                blob_index, record_index = loc
                full = _reconstruct_full_path(paths_by_self_id, entry)
                if not full:
                    continue
                if blob_index == effect_blob_index:
                    name_index[full] = record_index
                    # Strip the trailing quality suffix ("/high", "/low",
                    # "/shared") to derive the .xml base path.
                    if "/" in full:
                        base, _, quality = full.rpartition("/")
                        if base.endswith(".xml") and quality in ("high", "low", "shared"):
                            base_index.setdefault(base, {})[quality] = record_index
                elif (
                    preset_blob_index is not None
                    and blob_index == preset_blob_index
                    and full.endswith(".xml")
                ):
                    base_metadata_index[full] = record_index
        except Exception:
            mm.close()
            f.close()
            raise

        store = cls(
            _buf=mm,
            _hdr=hdr,
            _effect_blob=effect,
            _effect_metadata_blob=metadata,
            _name_index=name_index,
            _base_index=base_index,
            _base_metadata_index=base_metadata_index,
            _mmap_close=(mm, f),
            _file_end=len(mm),
        )
        return store

    def close(self) -> None:
        """Release the mmap + file handle."""
        if self._mmap_close is None:
            return
        mm, f = self._mmap_close
        try:
            mm.close()
        finally:
            try:
                f.close()
            finally:
                self._mmap_close = None

    def __enter__(self) -> ParticleStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------ Accessors --------------------------------------------------

    def __len__(self) -> int:
        return len(self._base_index)

    def __contains__(self, name: str) -> bool:
        key = _canonicalize_name(name)
        return key in self._base_index or key in self._name_index

    def names(self) -> list[str]:
        """Sorted list of every known particle base path (``.xml`` form,
        no quality suffix). Use :meth:`qualified_names` for the raw
        ``…/high`` / ``…/low`` paths."""
        return sorted(self._base_index.keys())

    def qualified_names(self) -> list[str]:
        """Sorted list of every per-quality particle path (``…/high``
        and ``…/low`` siblings, plus ``…/shared`` where present)."""
        return sorted(self._name_index.keys())

    def qualities_for(self, name: str) -> list[str]:
        """The quality suffixes available for ``name`` (sorted; one of
        ``["high", "low"]`` for the typical case)."""
        return sorted(self._base_index.get(_canonicalize_name(name), {}).keys())

    def record_count(self) -> int:
        """Total Effect records in the blob (including unreachable)."""
        return self._effect_blob.record_count

    def _metadata_for_base(self, base: str) -> dict | None:
        if self._effect_metadata_blob is None:
            return None
        meta_idx = self._base_metadata_index.get(base)
        if meta_idx is None:
            return None
        if meta_idx in self._metadata_cache:
            return self._metadata_cache[meta_idx]
        if meta_idx < 0 or meta_idx >= self._effect_metadata_blob.record_count:
            return None
        decoded = _decode_effect_metadata_record(
            self._buf,
            self._effect_metadata_blob.data_offset,
            meta_idx,
            self._file_end,
        )
        self._metadata_cache[meta_idx] = decoded
        return decoded

    def _attach_metadata(self, decoded: dict, base: str | None) -> None:
        if not base:
            return
        metadata = self._metadata_for_base(base)
        if not metadata:
            return
        decoded["intensityChannelCount"] = metadata.get("intensityChannelCount", 0)
        decoded["intensityChannels"] = metadata.get("intensityChannels", [])

    def get(self, name: str, *, quality: str | None = None) -> dict | None:
        """Decode the Effect for ``name``. Returns ``None`` if not in the
        index.

        ``name`` may be either:

        * the base ``.xml`` path (``"particles/vehicles/Fire_small.xml"``)
          — preferred; the parser picks the requested quality variant,
          falling back to the next-best available.
        * the fully-qualified path
          (``"particles/vehicles/Fire_small.xml/high"``) — only the
          listed record is returned.

        ``quality`` is one of ``"high"`` (default), ``"low"``,
        ``"shared"``. Ignored when ``name`` is fully-qualified.

        Result is cached per record_index; subsequent calls are O(1).
        """
        key = _canonicalize_name(name)
        # Direct fully-qualified lookup wins (preserves backwards-compat
        # for callers that pass the WG-form path verbatim).
        idx = self._name_index.get(key)
        metadata_base: str | None = None
        if idx is None:
            # Try the base-path lookup with quality preference. The "high"
            # variant is the canonical authoring artefact; "low" is the
            # low-spec fallback (fewer systems) and "shared" is rare /
            # never-resolvable in this corpus.
            variants = self._base_index.get(key)
            if variants is None:
                return None
            preferred = (quality or "high", "high", "low", "shared")
            for q in preferred:
                if q in variants:
                    idx = variants[q]
                    break
            if idx is None:
                return None
            metadata_base = key
        elif "/" in key:
            base, _, suffix = key.rpartition("/")
            if base.endswith(".xml") and suffix in ("high", "low", "shared"):
                metadata_base = base
        if idx in self._decoded_cache:
            decoded = self._decoded_cache[idx]
            self._attach_metadata(decoded, metadata_base)
            return decoded
        decoded = _decode_effect_record(
            self._buf, self._effect_blob.data_offset, idx, self._file_end,
        )
        decoded["name"] = key
        self._attach_metadata(decoded, metadata_base)
        self._decoded_cache[idx] = decoded
        return decoded

    def get_by_index(self, record_index: int) -> dict | None:
        """Decode the Effect record at ``record_index`` directly.

        Bypasses the name index — useful when iterating the whole corpus
        (e.g. for batch dumps).
        """
        if record_index < 0 or record_index >= self._effect_blob.record_count:
            return None
        if record_index in self._decoded_cache:
            return self._decoded_cache[record_index]
        decoded = _decode_effect_record(
            self._buf, self._effect_blob.data_offset, record_index, self._file_end,
        )
        self._decoded_cache[record_index] = decoded
        return decoded


def _canonicalize_name(name: str) -> str:
    """Normalize a particle path for index lookup (slashes + case).

    The Effect blob's path table stores entries as-recorded by WG (mixed
    forward / back-slash on some builds). Consumers pass VFS paths in
    either form, so we normalize both sides to forward-slash lowercase.

    Note: Path entries in the corpus are *case-sensitive* in WG's source
    data; we keep the lookup case-sensitive too (lowercase-only would
    drop Hellcarrier-style filenames). What we DO normalize is the
    separator.
    """
    return name.replace("\\", "/").strip("/")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ASSETS_BIN_MAGIC",
    "EFFECT_BLOB_MAGIC",
    "EFFECT_PRESET_BLOB_MAGIC",
    "EFFECT_METADATA_BLOB_MAGIC",
    "PCAT",
    "PSAT",
    "PS_IC",
    "PS_VGT",
    "PS_VALG_RAMP_PARAMETER",
    "PS_VALG_RAMP_SAMPLING",
    "ParticleStore",
    "PathEntry",
    "BlobInfo",
]
