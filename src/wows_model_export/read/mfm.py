"""`.mfm` MaterialPrototype reader.

Minimal Python parser for WG `.mfm` MaterialPrototype records. Mirrors
the relevant subset of the wows-toolkit Rust parser at
``crates/wowsunpack/src/models/material.rs``.

We only need the FIRST record's properties — that's the per-asset
material; subsequent records in disk-extracted .mfm files have OOL
pointers offset to data the toolkit expects to be contiguous in
`assets.bin`, and they don't decode cleanly from a flat file dump
(verified empirically 2026-04-30).

Use case: pull `emissivePower` from `*_emissive.mfm` files extracted
via ``wowsunpack extract``. The shader uniforms
`emissionAnimationMode`, `maskColor1`, `maskColor2`, `maskSmooth`
exposed by the compiled `ship_emissive_material.win.dx11.fxo` are NOT
in the .mfm — they're runtime/camo-system uniforms, not material
constants. The .mfm carries the per-material `emissivePower` scalar
plus texture references; the emissive mask comes from the `_mg` map's
B channel.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

# Item layout: see ``crates/wowsunpack/src/models/material.rs`` doc-comment.
MATERIAL_ITEM_SIZE = 0x78
_TYPE_ELEMENT_SIZES = (1, 4, 4, 4, 8, 8, 12, 16, 64)
_TYPE_NAMES = (
    "Bool", "Int32", "FloatA", "FloatB",
    "Texture", "Vec2", "Vec3", "Vec4", "Mat4",
)


# Murmur3-32 (seed=0). Same algorithm the toolkit uses to hash property names.
def _murmur3_32(key: bytes, seed: int = 0) -> int:
    length = len(key)
    nblocks = length // 4
    h1 = seed
    c1, c2 = 0xCC9E2D51, 0x1B873593
    for i in range(nblocks):
        k1 = struct.unpack_from("<I", key, i * 4)[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
        h1 = ((h1 << 13) | (h1 >> 19)) & 0xFFFFFFFF
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = key[nblocks * 4:]
    k1 = 0
    if len(tail) >= 3:
        k1 ^= (tail[2] << 16)
    if len(tail) >= 2:
        k1 ^= (tail[1] << 8)
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
    h1 ^= length
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1


# Subset of property names from the toolkit's hardcoded dictionary. We
# add a few names found in the compiled `ship_emissive_material.fxo`
# that the toolkit's own table doesn't include, so we can decode them
# when present (currently they only show up at the shader level, not
# the .mfm level — but kept here for future-proofing).
_KNOWN_NAMES = (
    "ambientOcclusionMap", "animMap", "animEmissionPower", "detailMap",
    "diffuseMap", "doubleSided", "emissionColor", "emissivePower",
    "emissionAnimationMode", "g_albedoMap", "g_detailAlbedoInfluence",
    "g_detailFadeDistance", "g_detailGlossInfluence",
    "g_detailNormalInfluence", "g_detailScale", "g_detailScaleU",
    "g_detailScaleV", "glowColor", "glowStrength", "incandescenceMap",
    "maskColor1", "maskColor2", "maskSmooth", "metallicGlossMap",
    "normalMap", "alphaReference", "alphaMul", "alphaPow",
    "camoMaskColor1", "camoMaskColor2", "camoMaskSpeed", "camoMaskOffset",
)
_NAMES_BY_HASH: dict[int, str] = {
    _murmur3_32(n.encode()): n for n in _KNOWN_NAMES
}


@dataclass(frozen=True)
class MaterialPrototype:
    """First-record contents of a `.mfm` file.

    ``properties`` is a dict keyed on resolved property name (or
    ``#XXXXXXXX`` for unknown name-hashes); values are typed Python
    values (bool / int / float / tuple-of-floats / texture-hash int).
    """

    shader_id:     int
    material_hash: int
    properties:    dict[str, object]


# Default emissive power across observed ARP / Azur Lane / Sabaton
# emissive ships. Returned by `get_emissive_power` when the .mfm
# can't be parsed or doesn't carry the property.
DEFAULT_EMISSIVE_POWER: float = 1.8


def parse_mfm(path: str | Path) -> MaterialPrototype | None:
    """Parse the first MaterialPrototype record from a disk `.mfm` file.

    Returns ``None`` if the file is too short or has an obviously-bad
    header (e.g. it's a packed atlas with a non-MFM prefix). Disk-
    extracted .mfm files from `wowsunpack extract` work; `assets.bin`-
    embedded blobs need the toolkit-side parser instead.
    """
    p = Path(path)
    data = p.read_bytes()
    if len(data) < MATERIAL_ITEM_SIZE:
        return None

    pcnt, _flags = struct.unpack_from("<HH", data, 0)
    shader_id = struct.unpack_from("<I", data, 4)[0]
    names_ptr, type_idx_ptr = struct.unpack_from("<QQ", data, 16)
    type_ptrs = struct.unpack_from("<9Q", data, 32)
    mat_hash = struct.unpack_from("<Q", data, 0x68)[0]

    if not (1 <= pcnt <= 50):
        return None
    if names_ptr + pcnt * 4 > len(data) or type_idx_ptr + pcnt * 2 > len(data):
        return None

    props: dict[str, object] = {}
    for i in range(pcnt):
        nh = struct.unpack_from("<I", data, names_ptr + i * 4)[0]
        ti = struct.unpack_from("<H", data, type_idx_ptr + i * 2)[0]
        ptype = ti & 0xF
        idx = ti >> 4

        if ptype >= 9:
            continue  # malformed type tag — skip silently

        type_ptr = type_ptrs[ptype]
        type_size = _TYPE_ELEMENT_SIZES[ptype]
        elem_off = type_ptr + idx * type_size
        if type_ptr == 0 or elem_off + type_size > len(data):
            value: object = None
        elif ptype == 0:
            value = bool(data[elem_off])
        elif ptype == 1:
            value = struct.unpack_from("<i", data, elem_off)[0]
        elif ptype == 2 or ptype == 3:
            value = struct.unpack_from("<f", data, elem_off)[0]
        elif ptype == 4:
            value = struct.unpack_from("<Q", data, elem_off)[0]  # texture hash
        elif ptype == 5:
            value = struct.unpack_from("<2f", data, elem_off)
        elif ptype == 6:
            value = struct.unpack_from("<3f", data, elem_off)
        elif ptype == 7:
            value = struct.unpack_from("<4f", data, elem_off)
        else:
            value = None  # mat4 — unused by us

        name = _NAMES_BY_HASH.get(nh, f"#{nh:08X}")
        props[name] = value

    return MaterialPrototype(
        shader_id=shader_id,
        material_hash=mat_hash,
        properties=props,
    )


def get_emissive_power(
    path: str | Path,
    default: float = DEFAULT_EMISSIVE_POWER,
) -> float:
    """Return ``emissivePower`` from a `.mfm`, or ``default`` if missing.

    ``default`` is the WG-typical 1.8 (observed across all tested ARP /
    Azur Lane / Sabaton emissive ships — Takao Arpeggio, Iowa AzurLane,
    Maya ARP, Maya Deck_house).
    """
    proto = parse_mfm(path)
    if proto is None:
        return default
    val = proto.properties.get("emissivePower")
    if isinstance(val, (int, float)):
        return float(val)
    return default


__all__ = [
    "MaterialPrototype",
    "MATERIAL_ITEM_SIZE",
    "DEFAULT_EMISSIVE_POWER",
    "parse_mfm",
    "get_emissive_power",
]
