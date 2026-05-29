"""Atomic text-file writer shared by the library composers.

Several library writers (``accessory_library._write_index``,
``dead_variant_audit``, ``attached_accessories_library``) emit JSON that
is read back by sibling composers in the same build. Under concurrent
builds (reachable today: ``/api/extract/run`` + ``/api/bootstrap/build``
both dispatch into the ``jobs`` ThreadPoolExecutor, max_workers=4, with
only a per-LABEL lock) a bare ``write_text`` lets a reader observe a
torn / truncated file mid-write -> ``JSONDecodeError`` -> silently
missing metadata.

``atomic_write_text`` writes to a PROCESS-UNIQUE temp file in the same
directory, then ``os.replace`` (atomic rename on the same volume) swaps
it in. Two properties matter:

* readers see all-or-nothing — never a partial write;
* the temp name is unique per writer (``mkstemp`` random suffix), so two
  concurrent writers to the same target don't collide on a shared
  ``<name>.tmp`` (which on Windows raises ``PermissionError`` / WinError
  32 when the second open hits the first's still-open handle).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically via a unique temp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = ["atomic_write_text"]
