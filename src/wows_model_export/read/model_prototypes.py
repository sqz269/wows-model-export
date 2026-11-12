"""Model prototypes (``.model`` records) of ``content/assets.bin``.

Layer 1 reader over the PrototypeDatabase container (the walker in
:mod:`.particles`). A model record is 0x28 bytes in the model blob
(magic ``0xA9576F28``)::

    +0x00  u64  visual path id
    +0x08  u8   skeleton-extender count
    +0x09  u8   misc type
    +0x0A  u8   animation count
    +0x0B  u8   dye count
    +0x10  i64  relptr -> u64[skeleton-extender count]   (path ids)
    +0x18  i64  relptr -> u64[animation count]           (path ids of .ssm files)
    +0x20  i64  relptr -> dyes

Relative pointers are from the record's start. The animation list is the
state machines the client loads with the model (``Model`` resources,
``getModelAnimations``): one path id per entry, 8 bytes each. On build
13533378, 1,332 of the 37,055 models list at least one (1,227 one, 105 two
to twelve).
"""

from __future__ import annotations

import mmap
import struct
from collections.abc import Iterable
from pathlib import Path

from . import particles as _db

#: murmur3_32("ModelPrototype"): the model blob of the PrototypeDatabase.
MODEL_BLOB_MAGIC = 0xA9576F28
_RECORD_SIZE = 0x28


def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("/")


def model_state_machines(assets_bin: Path | str, models: Iterable[str]) -> dict[str, list[str]]:
    """``{model VFS path: [state machine VFS paths]}`` for each requested
    ``.model`` path (no leading slash). A model without a record maps to an
    empty list; so does one that lists no state machines."""
    wanted = {_norm(m) for m in models}
    leaves = {w.rsplit("/", 1)[-1] for w in wanted}
    out: dict[str, list[str]] = {w: [] for w in wanted}
    if not wanted:
        return out
    with open(assets_bin, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        hdr = _db._read_header(buf)
        by_id = _db._build_paths_by_self_id(hdr.paths)

        def full(pid: int) -> str | None:
            e = by_id.get(pid)
            return _db._reconstruct_full_path(by_id, e) if e else None

        blob = next((b for b in hdr.blobs if b.prototype_magic == MODEL_BLOB_MAGIC), None)
        if blob is None:
            raise ValueError("assets.bin has no model blob")
        blob_index = hdr.blobs.index(blob)
        for p in hdr.paths:
            if p.name not in leaves:
                continue
            path = full(p.self_id)
            if path not in wanted:
                continue
            loc = _db._r2p_lookup(buf, hdr, p.self_id)
            if loc is None or loc[0] != blob_index:
                continue
            rec = blob.data_offset + 0x10 + loc[1] * _RECORD_SIZE
            count = buf[rec + 0x0A]
            if not count:
                continue
            at = rec + struct.unpack_from("<q", buf, rec + 0x18)[0]
            ids = struct.unpack_from(f"<{count}Q", buf, at)
            out[path] = [s for s in (full(i) for i in ids) if s]
    return out


def model_state_machine_census(assets_bin: Path | str) -> dict[str, list[str | None]]:
    """``{model VFS path: [state machine paths]}`` for every model record
    that lists at least one; a path id missing from the path table reads as
    None."""
    out: dict[str, list[str | None]] = {}
    with open(assets_bin, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        hdr = _db._read_header(buf)
        by_id = _db._build_paths_by_self_id(hdr.paths)

        def full(pid: int) -> str | None:
            e = by_id.get(pid)
            return _db._reconstruct_full_path(by_id, e) if e else None

        blob = next((b for b in hdr.blobs if b.prototype_magic == MODEL_BLOB_MAGIC), None)
        if blob is None:
            raise ValueError("assets.bin has no model blob")
        blob_index = hdr.blobs.index(blob)
        for p in hdr.paths:
            if not p.name.endswith(".model"):
                continue
            loc = _db._r2p_lookup(buf, hdr, p.self_id)
            if loc is None or loc[0] != blob_index:
                continue
            rec = blob.data_offset + 0x10 + loc[1] * _RECORD_SIZE
            count = buf[rec + 0x0A]
            if not count:
                continue
            at = rec + struct.unpack_from("<q", buf, rec + 0x18)[0]
            path = full(p.self_id)
            if path:
                out[path] = [full(i) for i in struct.unpack_from(f"<{count}Q", buf, at)]
    return out


__all__ = ["MODEL_BLOB_MAGIC", "model_state_machine_census", "model_state_machines"]
