"""Murmur3 hash-table builder + placement-string parser for `.skel_ext`.

Lifted from ``tools/shared/skel_ext_hashes.py``.

The toolkit's ``--skel-ext-candidates-json`` emits per-placement
``p0_hash`` values for every decorative prop on a ship. As of 2026-05-06
we know:

    p0_hash = Murmur3_x86_32(seed=0, "MP_<asset_id>[<suffix>]")

where the suffix is one of:
    ""                  - single-instance placement
    "_INDEX_<n>"        - N-th of multiple instances (1-based)
    ".<NNN>"            - Maya/Blender duplicate-naming convention
                          (alternative to _INDEX_, observed in WG data)

This module builds an exhaustive lookup table by walking every ``MP_*``
literal in the game's ``assets.bin`` (35k+ strings on the current
client), parsing each into ``(asset_id, suffix)``, hashing it with
Murmur3-32, and indexing by hash. Then any ``p0_hash`` from any ship's
``.skel_ext`` resolves directly via dict lookup.

Layer 3 fit: pure transforms (no subprocess, no writes beyond the cache
file the caller hands us). The only disk I/O is reading
``assets.bin`` / the cache JSON when building or loading the table -
both inputs the caller resolves.

The original module also exposed a CLI; that has not been lifted - CLI
wrappers live in the ``cli`` layer.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Murmur3_32 (seed=0) - verified against "Scene Root" -> 0x10C30510
# ---------------------------------------------------------------------------


def murmur3_32(key: str | bytes, seed: int = 0) -> int:
    """MurmurHash3 x86 32-bit. Matches the toolkit's `p1_hash` derivation
    (verified: Murmur3_32("Scene Root") = 0x10C30510)."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    length = len(key)
    nblocks = length // 4
    h = seed & 0xFFFFFFFF
    c1, c2 = 0xCC9E2D51, 0x1B873593

    def rotl(x: int, r: int) -> int:
        return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF

    for i in range(nblocks):
        k = struct.unpack_from("<I", key, i * 4)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = rotl(k, 15)
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = rotl(h, 13)
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF

    tail = key[nblocks * 4:]
    k1 = 0
    if len(tail) >= 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = rotl(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h ^= k1

    h ^= length
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


# ---------------------------------------------------------------------------
# Asset_id parsing
# ---------------------------------------------------------------------------

# `_INDEX_<digits>` at end (1-based; saw 1..hundreds on Scharnhorst etc.)
_INDEX_RE = re.compile(r"^(.*)_INDEX_(\d+)$")
# `.<NNN>` Maya/Blender duplicate suffix (3+ digits at end)
_DOT_RE = re.compile(r"^(.*)\.(\d{3,})$")

# Filter strings like "MP_3_HDR.dds" - these are texture filenames, not
# placement IDs. Asset_ids always start with a 1-3 letter scope prefix +
# optional digits (e.g. "AM001", "JM501", "AGM034", "JD611"). Reject
# anything that doesn't match this pattern post-strip.
_ASSET_ID_RE = re.compile(r"^[A-Z]{1,3}\d+_[A-Za-z0-9_]+$")


def parse_placement_string(s: str) -> dict | None:
    """Parse a "MP_<...>" or "SP_<...>" string into
    ``{'prefix', 'asset_id', 'index', 'suffix'}``.

    Returns ``None`` if the string isn't a recognisable placement id
    (e.g. ``MP_3_HDR.dds`` - a stray texture name that happens to start
    with ``MP_``, or ``SP_1MShape.geometry`` - a mesh-name reference)."""
    if s.startswith("MP_"):
        prefix = "MP_"
    elif s.startswith("SP_"):
        prefix = "SP_"
    else:
        return None
    body = s[3:]

    # Strip _INDEX_<n> first (highest priority - explicit numbering).
    if (m := _INDEX_RE.match(body)) is not None:
        return {
            "prefix": prefix,
            "asset_id": m.group(1),
            "index": int(m.group(2)),
            "suffix": f"_INDEX_{m.group(2)}",
        } if _ASSET_ID_RE.match(m.group(1)) else None

    # Maya/Blender `.NNN` form.
    if (m := _DOT_RE.match(body)) is not None:
        return {
            "prefix": prefix,
            "asset_id": m.group(1),
            "index": int(m.group(2)),
            "suffix": f".{m.group(2)}",
        } if _ASSET_ID_RE.match(m.group(1)) else None

    # Bare placement (single instance).
    if _ASSET_ID_RE.match(body):
        return {"prefix": prefix, "asset_id": body, "index": None, "suffix": ""}

    return None


# Backwards-compat alias.
parse_mp_string = parse_placement_string


# ---------------------------------------------------------------------------
# assets.bin extraction
# ---------------------------------------------------------------------------

# Match every printable ASCII string starting with "MP_" or "SP_",
# null-terminated, 4..200 chars long.
#
# Empirically:
#   "MP_" - 35k strings, vast majority. Standard "Mesh Placement" prefix
#           used for HP_-bound mounts AND decorative props.
#   "SP_" - 1.5k strings, with asset_id-shaped tails (`SP_YMS001_YHBM_M_1_INDEX_3`
#           etc.). Likely "Skinned Placement" - covers skinned-mesh
#           placements like Yamato's `YMS*` rope/rigging meshes.
#
# Other observed prefixes in assets.bin (`AN_`, `R_`, `M_`, `L_`, `T_`,
# `LAZE_`, `PBS_`) don't follow the asset_id placement pattern at scale
# and are excluded.
_PLACEMENT_RE = re.compile(rb"(?:MP|SP)_[\x20-\x7E]{2,200}\x00")


def extract_placement_strings(assets_bin_path: Path | str) -> set[str]:
    """Return every distinct `MP_*` or `SP_*` ASCII string in assets.bin."""
    data = Path(assets_bin_path).read_bytes()
    out: set[str] = set()
    for m in _PLACEMENT_RE.finditer(data):
        out.add(m.group(0)[:-1].decode("ascii", errors="ignore"))
    return out


# Backwards-compat alias.
extract_mp_strings = extract_placement_strings


# ---------------------------------------------------------------------------
# Build / load lookup table
# ---------------------------------------------------------------------------

def build_table(assets_bin_path: Path | str) -> dict[int, dict]:
    """Build ``hash_u32 -> {asset_id, index, suffix, string}`` from
    every ``MP_*`` string in the supplied assets.bin file.

    Hash collisions: theoretically possible (32-bit Murmur3), but on the
    35k-string corpus we've seen zero collisions in practice. If two
    strings hash to the same value, the second one overwrites the first
    in this table; production callers should detect this if they care."""
    table: dict[int, dict] = {}
    collisions = 0
    skipped = 0
    for s in extract_placement_strings(assets_bin_path):
        parsed = parse_placement_string(s)
        if parsed is None:
            skipped += 1
            continue
        h = murmur3_32(s)
        if h in table and table[h]["string"] != s:
            collisions += 1
        table[h] = {**parsed, "string": s}
    if collisions:
        print(
            f"[skel_ext_hashes] WARNING: {collisions} hash collision(s) on this "
            f"corpus - last-write-wins.",
            file=sys.stderr,
        )
    return table


def save_table(table: dict[int, dict], path: Path | str) -> None:
    """Serialise the table to JSON. Hash keys become 8-char hex strings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = {
        f"0x{h:08X}": entry for h, entry in sorted(table.items())
    }
    path.write_text(
        json.dumps(serialised, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_table(path: Path | str) -> dict[int, dict]:
    """Inverse of `save_table`. Hex keys -> int."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(k, 16): v for k, v in raw.items()}


def load_or_build(
    *,
    cache: Path | str,
    assets_bin_path: Path | str | None = None,
    refresh: bool = False,
) -> dict[int, dict]:
    """Return the table, building (and caching) on first use.

    `cache` is the on-disk path the table is read from / written to;
    callers typically pass ``PipelineConfig.require_cache_dir() /
    "skel_ext_hashes.json"``.

    `assets_bin_path` defaults to ``$WOWS_ASSETS_BIN`` then a
    ``$TEMP/assets_bin/content/assets.bin`` fallback (the typical
    CWD-output of ``wowsunpack metadata-json``). Re-extract via
    ``wowsunpack extract --out-dir <somewhere> '**/assets.bin'`` if
    missing.
    """
    cache = Path(cache)
    if cache.is_file() and not refresh:
        return load_table(cache)
    if assets_bin_path is None:
        # Conventional locations the toolkit tends to write to. Override
        # with WOWS_ASSETS_BIN for non-default workflows.
        env_path = os.environ.get("WOWS_ASSETS_BIN")
        candidates: list[Path] = []
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(
            Path(os.environ.get("TEMP", r"C:\Windows\Temp"))
            / "assets_bin" / "content" / "assets.bin"
        )
        for c in candidates:
            if c.is_file():
                assets_bin_path = c
                break
        else:
            raise RuntimeError(
                "assets.bin not found in any standard location. Pass "
                "`assets_bin_path=...` or extract via "
                "`wowsunpack extract --out-dir <dir> '**/assets.bin'`."
            )
    table = build_table(assets_bin_path)
    save_table(table, cache)
    return table


# ---------------------------------------------------------------------------
# High-level resolver - consume the toolkit's skel_ext_candidates JSON
# ---------------------------------------------------------------------------

def resolve_candidates(
    candidates: list[dict] | dict,
    table: dict[int, dict] | None = None,
    *,
    cache: Path | str | None = None,
) -> dict:
    """Resolve a list of toolkit-emitted skel_ext candidates against the
    lookup table.

    Input shape (matches `wowsunpack export-ship --skel-ext-candidates-json`):
        [
            {"p0_hash": "0xCA841EE4", "p1_hash": "...", "matrix_native": [...],
             "matrix_metric": [...], "position_metric": [x, y, z], ...},
            ...
        ]

    Or the wrapper dict ``{"placements": [...]}`` produced by some
    callers - both supported.

    Returns:
        {
            "summary": {"total": N, "resolved": M,
                        "unique_asset_ids": K, "coverage_pct": ...},
            "resolved":   [{...candidate, asset_id, instance_index, prefix}],
            "unresolved": [{...candidate}],   # p0 not in table
            "asset_counts": {asset_id: count, ...}  # placements per asset
        }

    When ``table`` is None the function calls ``load_or_build`` with the
    supplied ``cache`` path (required in that case).
    """
    if isinstance(candidates, dict):
        candidates = candidates.get("placements") or candidates.get("candidates") or []
    if table is None:
        if cache is None:
            raise ValueError(
                "resolve_candidates requires either `table=` or `cache=` so "
                "load_or_build has somewhere to read/write the hash table."
            )
        table = load_or_build(cache=cache)

    resolved = []
    unresolved = []
    asset_counts: dict[str, int] = {}
    for c in candidates:
        h = c.get("p0_hash")
        if isinstance(h, str):
            h = int(h, 16) if h.lower().startswith("0x") else int(h, 16)
        if h is None:
            unresolved.append(c)
            continue
        entry = table.get(h)
        if entry is None:
            unresolved.append(c)
            continue
        out = dict(c)
        out["asset_id"] = entry["asset_id"]
        out["instance_index"] = entry["index"]
        out["prefix"] = entry["prefix"]
        resolved.append(out)
        asset_counts[entry["asset_id"]] = asset_counts.get(entry["asset_id"], 0) + 1

    total = len(candidates)
    return {
        "summary": {
            "total":            total,
            "resolved":         len(resolved),
            "unresolved":       len(unresolved),
            "coverage_pct":     (100.0 * len(resolved) / total) if total else 0.0,
            "unique_asset_ids": len(asset_counts),
        },
        "resolved":     resolved,
        "unresolved":   unresolved,
        "asset_counts": asset_counts,
    }


__all__ = [
    "murmur3_32",
    "parse_placement_string",
    "parse_mp_string",
    "extract_placement_strings",
    "extract_mp_strings",
    "build_table",
    "save_table",
    "load_table",
    "load_or_build",
    "resolve_candidates",
]
