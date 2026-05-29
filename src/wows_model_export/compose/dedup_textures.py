"""Reclaim disk by hardlinking byte-identical duplicate textures.

Some engine-global textures — notably ``ship_atlas_detail.dds``, a 16.8 MB
detail atlas — are dumped verbatim into every accessory + ship
``textures_dds/`` dir by the toolkit's raw-DDS dumper (it dedups only by
filename WITHIN one export session, and each accessory gets its own
session). On a full workspace that's ~1223 byte-identical copies ≈ 20.5 GB.

This composer replaces those duplicates with hardlinks to shared canonical
copies under ``<workspace>/libraries/textures_shared/``, reclaiming the
space with **zero consumer impact**: each file stays at its original path
with identical bytes; only the physical on-disk allocation is shared
(webview / Unity / Blender read the same path + bytes as before — no
sidecar URI change needed).

Safety model:

* **Content-verified** — a copy is linked only when its size + SHA-256
  match the canonical. Differing content is left untouched + reported
  (a future per-ship atlas variant could never be silently aliased).
* **Atomic link** — each link is created at a unique temp name then
  ``os.replace``-d over the copy, so a crash mid-dedup never loses a file.
* **Idempotent** — a copy already sharing a canonical's inode (same
  ``st_ino``) is skipped; re-running is cheap.
* **NTFS-aware** — NTFS caps hardlinks at 1024 per inode, so canonicals
  are sharded (a new canonical per 1023 links); arbitrarily many copies
  are supported.
* **Cross-volume-safe** — a copy on a different volume than the canonical
  (``os.link`` ``EXDEV`` / WinError 17) is left untouched + reported.

Re-extraction interaction: pair with the toolkit's atomic raw-DDS write
(tmp + rename → fresh inode) so a later re-export over a linked path breaks
that one link cleanly instead of aliasing the shared content; re-run this
afterwards to re-link the freshly-written copies.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PipelineConfig
from ..types import OnEvent
from ._step_runner import StepRunner

# NTFS allows at most 1024 hardlinks per inode (the canonical counts as
# one), i.e. 1023 ADDITIONAL links. Shard canonicals below that.
_MAX_EXTRA_LINKS = 1023

# Engine-global textures dumped byte-identically into every accessory/ship
# dir. `ship_atlas_detail.dds` is ~99% of the waste.
_DEFAULT_TARGETS = ("ship_atlas_detail.dds",)

_SHARED_SUBDIR = "libraries/textures_shared"


@dataclass(frozen=True)
class DedupResult:
    targets: tuple[str, ...]
    files_scanned: int = 0
    files_linked: int = 0
    already_linked: int = 0
    skipped_mismatch: int = 0
    skipped_error: int = 0
    canonicals_created: int = 0
    bytes_reclaimed: int = 0
    dry_run: bool = False
    warnings: tuple[str, ...] = ()
    step_timings_ms: dict[str, float] = field(default_factory=dict)


def _sha256(path: Path, _buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


class _CanonStore:
    """Sharded canonical files for one (filename, content-hash). Hands out
    a canonical with link room, creating a new shard (seeded by copying a
    same-content source) when the current one is full."""

    def __init__(self, shared_dir: Path, stem: str, ext: str, sha: str) -> None:
        self._dir = shared_dir
        self._stem = stem
        self._ext = ext
        self._sha8 = sha[:8]
        self.by_ino: dict[int, Path] = {}
        self._shards: list[Path] = []
        for p in sorted(shared_dir.glob(f"{stem}.{self._sha8}.s*{ext}")):
            try:
                self.by_ino[p.stat().st_ino] = p
                self._shards.append(p)
            except OSError:
                pass

    def acquire(self, src: Path) -> tuple[Path, bool]:
        """Return ``(canonical, created)`` for a same-content ``src``."""
        for shard in self._shards:
            try:
                # st_nlink counts the canonical + its links; room while < 1024.
                if shard.stat().st_nlink <= _MAX_EXTRA_LINKS:
                    return shard, False
            except OSError:
                continue
        self._dir.mkdir(parents=True, exist_ok=True)
        new_path = self._dir / f"{self._stem}.{self._sha8}.s{len(self._shards)}{self._ext}"
        tmp = new_path.with_name(new_path.name + f".tmp{os.getpid()}")
        shutil.copyfile(src, tmp)
        os.replace(tmp, new_path)
        self._shards.append(new_path)
        self.by_ino[new_path.stat().st_ino] = new_path
        return new_path, True


def _atomic_link(canonical: Path, copy: Path) -> None:
    """Replace ``copy`` with a hardlink to ``canonical`` atomically."""
    tmp = copy.with_name(copy.name + f".deduptmp{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    os.link(canonical, tmp)   # may raise EXDEV / WinError 1142 (link cap)
    os.replace(tmp, copy)     # atomic swap; frees copy's old inode


def dedup_textures(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    targets: tuple[str, ...] = _DEFAULT_TARGETS,
    dry_run: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> DedupResult:
    """Hardlink byte-identical copies of ``targets`` across the workspace
    to shared canonicals. See the module docstring for the safety model.

    Scans ``<workspace>/ships`` + ``<workspace>/libraries`` (excluding the
    shared sink). ``dry_run=True`` reports what would be reclaimed (treating
    each not-yet-linked copy as reclaimable; the ~16 MB-per-shard canonical
    cost it ignores is negligible) without touching the filesystem.
    """
    cfg = config or PipelineConfig.load()
    ws = (workspace or cfg.workspace).resolve()
    shared_dir = ws / _SHARED_SUBDIR

    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    scanned = linked = already = mismatch = errored = canon_created = 0
    bytes_freed = 0
    bytes_canon = 0

    scan_roots = [ws / "ships", ws / "libraries"]

    for target in targets:
        stem, _, ext_raw = target.rpartition(".")
        ext = f".{ext_raw}" if ext_raw else ""
        stores: dict[str, _CanonStore] = {}

        with runner.step("dedup", detail=target) as st:
            copies: list[Path] = []
            for root in scan_roots:
                if not root.is_dir():
                    continue
                for p in root.rglob(target):
                    if shared_dir in p.parents:
                        continue  # never touch the shared sink itself
                    if p.is_file():
                        copies.append(p)
            scanned += len(copies)
            t_linked = t_already = t_canon = 0

            for i, copy in enumerate(copies):
                if i % 100 == 0:
                    runner.progress("dedup", detail=f"{i}/{len(copies)} {target}")
                try:
                    sstat = copy.stat()
                    size, ino = sstat.st_size, sstat.st_ino
                    sha = _sha256(copy)
                except OSError as e:
                    errored += 1
                    warnings.append(f"stat/hash failed {copy}: {e}")
                    continue

                store = stores.get(sha)
                if store is None:
                    store = _CanonStore(shared_dir, stem, ext, sha)
                    stores[sha] = store

                if ino in store.by_ino:
                    already += 1
                    t_already += 1
                    continue

                if dry_run:
                    linked += 1
                    t_linked += 1
                    bytes_freed += size
                    continue

                try:
                    canon, created = store.acquire(copy)
                except OSError as e:
                    errored += 1
                    warnings.append(f"canonical create failed for {copy}: {e}")
                    continue
                if created:
                    canon_created += 1
                    t_canon += 1
                    bytes_canon += size

                try:
                    if canon.stat().st_size != size:
                        mismatch += 1
                        warnings.append(f"size mismatch vs canonical, skipped {copy}")
                        continue
                    _atomic_link(canon, copy)
                    linked += 1
                    t_linked += 1
                    bytes_freed += size
                except OSError as e:
                    errored += 1
                    warnings.append(f"link failed {copy}: {e}")

            st.annotate(
                f"{target}: {len(copies)} copies → linked {t_linked}, "
                f"already {t_already}, +{t_canon} canonical(s)"
            )

    reclaimed = max(0, bytes_freed - bytes_canon)
    return DedupResult(
        targets=targets,
        files_scanned=scanned,
        files_linked=linked,
        already_linked=already,
        skipped_mismatch=mismatch,
        skipped_error=errored,
        canonicals_created=canon_created,
        bytes_reclaimed=reclaimed,
        dry_run=dry_run,
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = ["dedup_textures", "DedupResult"]
