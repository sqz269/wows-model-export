"""Route shared texture sets through the content-addressed store.

One engine, two entry points:

* :func:`migrate_shared_textures` (``wows-dedup-textures``) converts a
  whole workspace: every texture set that recurs byte-for-byte in two or
  more in-scope export folders (or that the store already holds, or that
  is an always-routed name) moves into the store.
* :func:`converge_dirs` is the export-time hook behind
  :func:`wows_model_export.shared_textures.adopt_dirs`: the same steps,
  restricted to the sets of freshly written folders and every other
  folder's copy of those sets.

Claimed folders. A copy in a folder whose export root an open
:func:`wows_model_export.shared_textures.export_session` claims (an
export that is still computing the documents of that root, in this or
another process) is left in place, unless the run was asked to adopt
that folder; the folder is added to the pending list
(:data:`~wows_model_export.shared_textures.PENDING_REL`). Each run also
takes up the pending folders nobody claims any more, and
:func:`flush_pending` runs when a session ends. So no export ever has a
copy it named moved away by somebody else, and no copy stays behind once
the exports are done.

See :mod:`wows_model_export.shared_textures` for the store layout, the
texture-set model and the consumer path rule. Steps:

1. **scan** — list every texture set (``<stem>.dds`` / ``.dd<N>`` files of
   one stem in one folder) under the export trees
   :data:`~wows_model_export.shared_textures.SCOPE_ROOTS`, and the store's
   entries. Stat only.
2. **plan** — sets whose shape (member names + sizes) occurs in another
   folder or in the store are hashed (SHA-1, through the persistent hash
   cache); a set is shared when its key (:func:`~wows_model_export.shared_textures.set_key`)
   occurs in two or more folders, the store holds it, or it is an
   always-routed name. A set is shared only when every member matches.
3. **store** — one store copy per (key, file name), written atomically
   and verified, or verified when present.
4. **redirects** — every folder that holds a shared set records
   ``name -> store path`` for its members; stale records of sets that are
   local again are dropped.
5. **rewrite** — every texture path in the ship sidecars, building
   sidecars, the accessory / projectile library indexes and the building
   gun index (``buildings/_shared/guns/index.json``) that names a moved
   copy is repointed at the store path. Each path is resolved
   against the directory it is relative to and matched against the moved
   copies (or the folder's redirect record when the file is gone); a path
   that matches neither is left alone. The file is rewritten by exact
   string substitution (or, when that is ambiguous, re-serialized with the
   formatting it already had); the result is re-parsed and compared with
   the planned document, and the file must not have changed on disk since
   it was read.
6. **remove** — delete each copy whose content equals its verified store
   copy. Copies under an export root (ship / building directory, library)
   whose document failed to rewrite or is a ``*.meta.json`` this engine
   does not know are kept and reported.
7. **legacy** (migration only) — delete the flat hardlink canonicals
   ``<stem>.<sha256-8>.s<N><ext>`` that ``wows-dedup-textures
   --hardlink`` left at the store root, once nothing else links them and
   their content equals a store copy.

The migration logs to ``<workspace>/.cache/shared_textures/migration-*.json``;
export-time runs append one line each to ``adopt.jsonl`` there.
Idempotent: a second run finds nothing to move and only repairs
references that still name a local copy.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import shared_textures as store
from ..config import PipelineConfig
from ..types import OnEvent
from ._step_runner import StepRunner

# Library indexes whose per-asset texture paths are relative to the
# asset's GLB directory.
_LIBRARY_INDEXES: tuple[str, ...] = ("accessories", "projectiles", "aircraft")

# The building gun library (``buildings/_shared/guns/<stem>/``) and its
# index, which has the library-index shape. ``_shared`` is the export root
# its texture folders belong to (see :func:`_export_root`).
_BUILDING_SHARED = "_shared"
_BUILDING_GUN_INDEX: tuple[str, ...] = ("guns", "index.json")

_LEGACY_SHARD_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<hash>[0-9a-f]{8})\.s(?P<shard>\d+)(?P<ext>\.[^.]+)$",
    re.IGNORECASE,
)

_LOG_SUBDIR = (".cache", "shared_textures")

# One engine run at a time per process (threads) and per workspace
# (processes, through a lock file).
_RUN_LOCK = threading.Lock()
_LOCKFILE_NAME = "converge.lock"
_LOCK_WAIT_S = 300.0
_LOCK_STALE_S = 1800.0


@dataclass(frozen=True)
class SharedTextureMigrationResult:
    names: tuple[str, ...]
    copies_found: int = 0
    distinct_contents: int = 0
    store_files_written: int = 0
    store_files_present: int = 0
    redirect_files_written: int = 0
    documents_rewritten: int = 0
    references_rewritten: int = 0
    copies_removed: int = 0
    copies_kept: int = 0
    legacy_canonicals_removed: int = 0
    bytes_removed: int = 0
    bytes_freed: int = 0
    store_refs: tuple[str, ...] = ()
    log_path: Path | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()
    step_timings_ms: dict[str, float] = field(default_factory=dict)
    # Texture-set view (phase 2).
    sets_scanned: int = 0
    sets_shared: int = 0
    set_copies_moved: int = 0
    store_entries: int = 0
    files_hashed: int = 0
    # Files left in claimed folders (moved when their session ends).
    copies_deferred: int = 0


# ---------------------------------------------------------------------------
# Scan model
# ---------------------------------------------------------------------------


@dataclass
class _File:
    name: str
    path: Path
    size: int
    mtime_ns: int
    sha1: str | None = None


@dataclass
class _Set:
    folder: Path
    folder_key: str
    stem: str
    files: list[_File]
    key: str | None = None

    @property
    def shape(self) -> tuple[tuple[str, int], ...]:
        return tuple((f.name, f.size) for f in self.files)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def always_routed(self) -> bool:
        return len(self.files) == 1 and store.is_shared_name(self.files[0].name)


@dataclass
class _Scan:
    sets: list[_Set]
    folders: dict[str, Path]               # folder key -> path (texture folders)
    local_files: set[str]                  # normalized paths of every set member
    redirect_folders: set[str]             # folder keys holding a redirect file
    store_entries: dict[str, dict[str, _File]]  # key-8 -> {name: file}
    all_keys: set[str]                     # hash-cache keys seen (for pruning)


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _scan(ws: Path, cache: store.HashCache) -> _Scan:
    sroot = store.store_root(ws)
    sroot_key = _norm(sroot)
    sets: list[_Set] = []
    folders: dict[str, Path] = {}
    local_files: set[str] = set()
    redirect_folders: set[str] = set()
    all_keys: set[str] = set()
    for root_rel in store.SCOPE_ROOTS:
        root = ws.joinpath(*root_rel.split("/"))
        if not root.is_dir():
            continue
        stack = [str(root)]
        while stack:
            d = stack.pop()
            if _norm(d) == sroot_key:
                continue
            members: dict[str, list[_File]] = defaultdict(list)
            has_redirect = False
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                                continue
                            if not e.is_file():
                                continue
                        except OSError:
                            continue
                        if e.name == store.REDIRECT_FILENAME:
                            has_redirect = True
                            continue
                        stem = store.set_stem(e.name)
                        if stem is None:
                            continue
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        members[stem.lower()].append(
                            _File(e.name, Path(e.path), st.st_size, st.st_mtime_ns)
                        )
            except OSError:
                continue
            fk = _norm(d)
            if has_redirect:
                redirect_folders.add(fk)
                folders.setdefault(fk, Path(d))
            if not members:
                continue
            folders[fk] = Path(d)
            for stem_l, files in members.items():
                files.sort(key=lambda f: f.name)
                for f in files:
                    local_files.add(_norm(f.path))
                    all_keys.add(cache.rel(f.path))
                sets.append(_Set(Path(d), fk, stem_l, files))
    entries: dict[str, dict[str, _File]] = {}
    if sroot.is_dir():
        for sub in sroot.iterdir():
            if not sub.is_dir():
                continue
            n = sub.name.lower()
            if len(n) != store.HASH_PREFIX_LEN or any(c not in "0123456789abcdef" for c in n):
                continue
            files: dict[str, _File] = {}
            for f in sub.iterdir():
                if f.is_file() and store.is_set_member(f.name):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    files[f.name] = _File(f.name, f, st.st_size, st.st_mtime_ns)
                    all_keys.add(cache.rel(f))
            if files:
                entries[n] = files
    return _Scan(sets, folders, local_files, redirect_folders, entries, all_keys)


def _store_name(name: str) -> str:
    return store.canonical_name(name) if store.is_shared_name(name) else name


def _hash_files(files: list[_File], cache: store.HashCache, workers: int = 8) -> None:
    todo = [f for f in files if f.sha1 is None]
    if not todo:
        return
    if len(todo) == 1:
        todo[0].sha1 = cache.sha1(todo[0].path, todo[0].size, todo[0].mtime_ns)
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f, sha in zip(todo, ex.map(lambda x: cache.sha1(x.path, x.size, x.mtime_ns), todo)):
            f.sha1 = sha


def _store_holds(scan: _Scan, key: str, s: _Set, cache: store.HashCache) -> bool:
    entry = scan.store_entries.get(key[: store.HASH_PREFIX_LEN].lower())
    if not entry:
        return False
    for f in s.files:
        sf = entry.get(_store_name(f.name))
        if sf is None or sf.size != f.size:
            return False
        if sf.sha1 is None:
            try:
                sf.sha1 = cache.sha1(sf.path, sf.size, sf.mtime_ns)
            except OSError:
                return False
        if sf.sha1 != f.sha1:
            return False
    return True


@dataclass
class _Shared:
    key: str
    copies: list[_Set]
    held: bool


def _plan(
    scan: _Scan,
    targets: set[str] | None,
    cache: store.HashCache,
) -> tuple[dict[str, _Shared], int]:
    """Shared keys among ``targets`` (folder keys; None = every folder)."""
    by_shape: dict[tuple, list[_Set]] = defaultdict(list)
    for s in scan.sets:
        by_shape[s.shape].append(s)
    store_shapes: set[tuple] = set()
    for files in scan.store_entries.values():
        ordered = sorted(files.values(), key=lambda f: f.name)
        store_shapes.add(tuple((f.name, f.size) for f in ordered))
        for f in ordered:
            store_shapes.add(((f.name, f.size),))
    cand_shapes: set[tuple] = set()
    for s in scan.sets:
        if targets is not None and s.folder_key not in targets:
            continue
        sh = s.shape
        if len(by_shape[sh]) >= 2 or sh in store_shapes or s.always_routed:
            cand_shapes.add(sh)
    to_hash = [s for sh in cand_shapes for s in by_shape[sh]]
    files = [f for s in to_hash for f in s.files]
    _hash_files(files, cache)
    groups: dict[str, list[_Set]] = defaultdict(list)
    for s in to_hash:
        s.key = store.set_key({f.name: f.sha1 for f in s.files})  # type: ignore[misc]
        groups[s.key].append(s)
    shared: dict[str, _Shared] = {}
    for key, copies in groups.items():
        if targets is not None and not any(c.folder_key in targets for c in copies):
            continue
        folders = {c.folder_key for c in copies}
        held = all(_store_holds(scan, key, c, cache) for c in copies)
        if len(folders) >= 2 or held or any(c.always_routed for c in copies):
            copies.sort(key=lambda c: c.folder_key)
            shared[key] = _Shared(key, copies, held)
    return shared, len(files)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _export_root(ws: Path, folder: Path) -> Path | None:
    """The export root a texture folder belongs to: ``ships/<S>``,
    ``buildings/<B>`` or ``libraries/<lib>``."""
    try:
        rel = Path(folder).resolve().relative_to(ws.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] in ("ships", "buildings"):
        return ws / parts[0] / parts[1]
    if len(parts) >= 2 and parts[0] == "libraries":
        return ws / "libraries" / parts[1]
    return None


def _documents(ws: Path, roots: Iterable[Path] | None) -> tuple[list[tuple[Path, str]], list[Path]]:
    """``(document, kind)`` for every JSON that names raw-DDS textures under
    ``roots`` (None = the whole workspace), plus the sidecar-like files
    (``*.meta.json`` next to a sidecar under another name) this engine does
    not know how to rewrite."""
    out: list[tuple[Path, str]] = []
    unvisited: list[Path] = []
    if roots is None:
        root_list: list[Path] = []
        for top in ("ships", "buildings"):
            r = ws / top
            if r.is_dir():
                root_list.extend(d for d in sorted(r.iterdir()) if d.is_dir())
        root_list.extend(ws / "libraries" / lib for lib in _LIBRARY_INDEXES)
    else:
        root_list = sorted({Path(r) for r in roots}, key=lambda p: str(p).lower())
    for d in root_list:
        if d.parent.name == "libraries":
            idx = d / "index.json"
            if d.name in _LIBRARY_INDEXES and idx.is_file():
                out.append((idx, "library_index"))
            continue
        if d.parent.name == "buildings" and d.name == _BUILDING_SHARED:
            # The building gun library: one index, library-index shaped
            # (each asset's texture paths relative to its GLB directory).
            idx = d.joinpath(*_BUILDING_GUN_INDEX)
            if idx.is_file():
                out.append((idx, "library_index"))
            continue
        if not d.is_dir():
            continue
        expected = f"{d.name}.meta.json"
        for sc in sorted(d.glob("*.meta.json")):
            if not sc.is_file():
                continue
            if sc.name == expected:
                out.append((sc, "sidecar"))
            else:
                unvisited.append(sc)
    return out, unvisited


class _Rewriter:
    """Plan the texture-path replacements for one JSON document."""

    def __init__(
        self,
        moved: dict[str, str],
        scan: _Scan,
        doc_path: Path,
    ) -> None:
        self.moved = moved
        self.scan = scan
        self.doc_path = doc_path
        self.plan: dict[str, set[str]] = defaultdict(set)
        self.count: dict[str, int] = defaultdict(int)
        self._redirects: dict[str, dict[str, dict[str, Any]]] = {}

    def _redirect(self, folder: Path) -> dict[str, dict[str, Any]]:
        k = _norm(folder)
        hit = self._redirects.get(k)
        if hit is None:
            hit = store.read_redirect_records(folder) if k in self.scan.redirect_folders else {}
            self._redirects[k] = hit
        return hit

    def _ref_for(self, value: str, base: Path) -> str | None:
        rel = value.replace("\\", "/")
        target = base.joinpath(*rel.split("/"))
        tk = _norm(target)
        ref = self.moved.get(tk)
        if ref is not None:
            return ref
        if tk in self.scan.local_files:
            return None  # the local copy stays
        # Folder converted earlier: its redirect file knows the store path.
        rec = self._redirect(target.parent).get(target.name)
        if rec is not None:
            return rec["ref"]
        return None

    def walk(self, node: Any, base: Path) -> Any:
        """Return ``node`` with references replaced; record the plan."""
        if isinstance(node, dict):
            return {k: self.walk(v, base) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v, base) for v in node]
        if (
            isinstance(node, str)
            and store.is_set_member(node)
            and not store.is_workspace_path(node)
        ):
            ref = self._ref_for(node, base)
            if ref is not None and ref != node:
                self.plan[node].add(ref)
                self.count[node] += 1
                return ref
        return node


def _detect_indent(text: str) -> int | None:
    m = re.match(r"\s*[\[{]\r?\n( +)", text)
    return len(m.group(1)) if m else None


def _rewrite_document(
    doc_path: Path,
    kind: str,
    moved: dict[str, str],
    scan: _Scan,
    warnings: list[str],
    *,
    dry_run: bool,
) -> int:
    """Rewrite one document; returns the number of references replaced."""
    raw = doc_path.read_bytes()
    text = raw.decode("utf-8-sig")
    doc = json.loads(text)
    rw = _Rewriter(moved, scan, doc_path)

    if kind == "sidecar":
        # Ship / building texture paths are relative to `models/`.
        expected = rw.walk(doc, doc_path.parent / "models")
    else:
        # Library index: paths are relative to each asset's GLB dir.
        lib_root = doc_path.parent
        expected = dict(doc)
        assets = doc.get("assets")
        if isinstance(assets, dict):
            new_assets: dict[str, Any] = {}
            for aid, entry in assets.items():
                glb = entry.get("glb") if isinstance(entry, dict) else None
                if not isinstance(glb, str) or not glb:
                    new_assets[aid] = entry
                    continue
                base = lib_root.joinpath(*glb.replace("\\", "/").split("/")[:-1])
                new_assets[aid] = rw.walk(entry, base)
            expected["assets"] = new_assets

    n_refs = sum(rw.count.values())
    if n_refs == 0:
        return 0

    # Exact string substitution keeps the file's formatting. It is only
    # used when each old string has one replacement and occurs in the
    # text exactly as often as it was replaced.
    new_text: str | None = text
    for old, news in rw.plan.items():
        quoted = json.dumps(old, ensure_ascii=False)
        if len(news) != 1 or new_text.count(quoted) != rw.count[old]:  # type: ignore[union-attr]
            new_text = None
            break
        new_text = new_text.replace(quoted, json.dumps(next(iter(news)), ensure_ascii=False))  # type: ignore[union-attr]
    if new_text is not None:
        try:
            if json.loads(new_text) != expected:
                new_text = None
        except ValueError:
            new_text = None
    if new_text is None:
        # Re-serialize, but only with the exact formatting the file
        # already has (re-serializing the parsed original must reproduce
        # it byte for byte).
        # (json.dumps never emits a raw newline inside a string, so the
        # line-ending swap below only touches the layout.)
        indent = _detect_indent(text)
        nl = "\r\n" if "\r\n" in text else "\n"
        tail = nl if text.endswith(nl) else ""

        def _dump(obj: Any, ensure_ascii: bool) -> str:
            out = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
            return (out.replace("\n", nl) if nl != "\n" else out) + tail

        for ensure_ascii in (False, True):
            if _dump(doc, ensure_ascii) == text:
                new_text = _dump(expected, ensure_ascii)
                break
        if new_text is None:
            raise RuntimeError(
                "references are ambiguous for string substitution and the "
                "file's formatting cannot be reproduced"
            )
        warnings.append(f"{doc_path}: re-serialized (string substitution was ambiguous)")

    if not dry_run:
        if doc_path.read_bytes() != raw:
            raise RuntimeError("document changed on disk while it was being rewritten")
        bom = raw[:3] if raw.startswith(b"\xef\xbb\xbf") else b""
        tmp = doc_path.with_name(f".{doc_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(bom + new_text.encode("utf-8"))
        os.replace(tmp, doc_path)
    return n_refs


def _under(path: Path, roots: dict[str, str]) -> str | None:
    """Reason recorded for the first of ``roots`` that contains ``path``."""
    p = _norm(path)
    for root, reason in roots.items():
        if p.startswith(root + os.sep):
            return reason
    return None


# ---------------------------------------------------------------------------
# Inter-process run lock
# ---------------------------------------------------------------------------


class _RunLock:
    """Serialize engine runs on one workspace (threads and processes)."""

    def __init__(self, ws: Path) -> None:
        self.path = ws.joinpath(*_LOG_SUBDIR) / _LOCKFILE_NAME
        self.fd: int | None = None

    def __enter__(self) -> "_RunLock":
        _RUN_LOCK.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + _LOCK_WAIT_S
            while True:
                try:
                    self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(self.fd, f"{os.getpid()} {time.time():.0f}\n".encode())
                    return self
                except FileExistsError:
                    try:
                        age = time.time() - self.path.stat().st_mtime
                    except OSError:
                        continue
                    if age > _LOCK_STALE_S:
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        continue
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{self.path} is held by another run")
                    time.sleep(0.5)
        except BaseException:
            _RUN_LOCK.release()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
            try:
                self.path.unlink()
            except OSError:
                pass
        finally:
            _RUN_LOCK.release()


def wait_for_engine(workspace: Path) -> None:
    """Return once no engine run is in progress on ``workspace``. An export
    session calls it after registering its claims, so a run that read the
    claims before they existed finishes before the export goes on."""
    with _RunLock(Path(workspace).resolve()):
        pass


# ---------------------------------------------------------------------------
# Pending folders (copies left in claimed folders)
# ---------------------------------------------------------------------------

_PENDING_SCHEMA_VERSION = 1


def _pending_path(ws: Path) -> Path:
    return ws.joinpath(*store.PENDING_REL.split("/"))


def _read_pending(ws: Path) -> dict[str, dict[str, Any]]:
    """``{folder path: info}`` from the pending list."""
    try:
        doc = json.loads(_pending_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    folders = (doc or {}).get("folders") if isinstance(doc, dict) else None
    if not isinstance(folders, dict):
        return {}
    return {k: (v if isinstance(v, dict) else {}) for k, v in folders.items() if isinstance(k, str)}


def _write_pending(ws: Path, folders: dict[str, dict[str, Any]]) -> None:
    path = _pending_path(ws)
    if not folders:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": _PENDING_SCHEMA_VERSION,
           "folders": {k: folders[k] for k in sorted(folders)}}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _run(
    ws: Path,
    *,
    targets: list[Path] | None,
    dry_run: bool,
    runner: StepRunner,
    log: dict[str, Any],
    warnings: list[str],
) -> tuple[SharedTextureMigrationResult, dict[Path, dict[str, str]]]:
    cache = store.HashCache(ws)
    target_keys = None if targets is None else {_norm(t) for t in targets}

    # Export roots that open sessions are still writing documents for, and
    # the folders earlier runs left alone because of them.
    claims = store.active_claims(ws)
    pending = _read_pending(ws)
    pending_free = {_norm(p) for p in pending if not store.is_claimed(p, claims)}
    if target_keys is not None:
        target_keys |= pending_free

    def _movable(c: _Set) -> bool:
        if target_keys is not None and c.folder_key in target_keys:
            return True
        return not store.is_claimed(c.folder_key, claims)

    # ── 1-2. scan + plan ──────────────────────────────────────────────
    with runner.step("scan", detail=", ".join(store.SCOPE_ROOTS)) as st:
        scan = _scan(ws, cache)
        shared, n_hashed = _plan(scan, target_keys, cache)
        movable: dict[str, list[_Set]] = {}
        deferred: list[_Set] = []
        for key, sh in shared.items():
            movable[key] = [c for c in sh.copies if _movable(c)]
            deferred.extend(c for c in sh.copies if not _movable(c))
        n_copies = sum(len(sh.copies) for sh in shared.values())
        n_files = sum(len(c.files) for sh in shared.values() for c in sh.copies)
        st.annotate(
            f"{len(scan.sets)} set(s) in {len(scan.folders)} folder(s); "
            f"{len(shared)} shared ({n_copies} copies, {n_files} files; "
            f"{len(deferred)} copies in claimed folders left in place); "
            f"{n_hashed} file(s) hashed ({cache.misses} new)"
        )
    for c in deferred:
        log["deferred"].append({"path": str(c.folder), "stem": c.stem, "key": c.key})

    # ── 3. store ──────────────────────────────────────────────────────
    written = present = 0
    refs_by_key: dict[str, dict[str, str]] = {}  # key -> {copy name: ref}
    with runner.step("store") as st:
        for key, sh in sorted(shared.items()):
            if not movable[key]:
                continue  # every copy is in a claimed folder
            refs: dict[str, str] = {}
            ok = True
            by_name: dict[str, _File] = {}
            # Read from a copy that is moving (a claimed folder's copy may
            # be in the middle of being rewritten by its export).
            moving_ids = {id(c) for c in movable[key]}
            for c in movable[key] + [c for c in sh.copies if id(c) not in moving_ids]:
                for f in c.files:
                    by_name.setdefault(f.name, f)
            entry_log = {"key": key, "copies": len(sh.copies),
                         "bytes": sh.copies[0].size, "files": []}
            for name, f in sorted(by_name.items()):
                sname = _store_name(name)
                ref = store.store_ref(sname, key)
                existed = store.resolve_ref(ws, ref).is_file()
                if not dry_run:
                    try:
                        ref = store.put(ws, f.path, sha1=f.sha1, name=sname, key=key)
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"store write failed for {ref}: {e}")
                        ok = False
                        break
                refs[name] = ref
                if existed:
                    present += 1
                else:
                    written += 1
                entry_log["files"].append({"ref": ref, "sha1": f.sha1, "size": f.size,
                                           "existed": existed})
            if ok:
                refs_by_key[key] = refs
                log["store_entries"].append(entry_log)
        st.annotate(f"{written} file(s) written, {present} already present")

    moved_sets = [c for key in shared if key in refs_by_key for c in movable[key]]
    moved: dict[str, str] = {}
    for c in moved_sets:
        refs = refs_by_key[c.key]  # type: ignore[index]
        for f in c.files:
            moved[_norm(f.path)] = refs[f.name]

    # ── 4. redirects ──────────────────────────────────────────────────
    n_redirects = 0
    n_dropped = 0
    with runner.step("redirects") as st:
        by_dir: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for c in moved_sets:
            for f in c.files:
                by_dir[c.folder_key][f.name] = store.redirect_record(
                    moved[_norm(f.path)], f.sha1 or "", f.size,
                )
        for fk, recs in sorted(by_dir.items()):
            if not dry_run:
                try:
                    store.write_redirects(scan.folders[fk], recs)
                except OSError as e:
                    warnings.append(f"redirect write failed in {scan.folders[fk]}: {e}")
                    continue
            n_redirects += 1
            log["redirects"].append(str(scan.folders[fk]))
        # Sets that stay local: a redirect record of the same name is stale.
        moved_names: dict[str, set[str]] = defaultdict(set)
        for c in moved_sets:
            moved_names[c.folder_key].update(f.name.lower() for f in c.files)
        for s in scan.sets:
            if s.folder_key not in scan.redirect_folders:
                continue
            if target_keys is not None and s.folder_key not in target_keys:
                continue
            names = [f.name for f in s.files if f.name.lower() not in moved_names[s.folder_key]]
            if not names:
                continue
            if dry_run:
                recs = store.read_redirect_records(s.folder)
                n_dropped += sum(1 for n in names if n in recs)
            else:
                n_dropped += store.drop_redirects(s.folder, names)
        st.annotate(f"{n_redirects} folder(s); {n_dropped} stale record(s) dropped")

    # ── 5. rewrite references ─────────────────────────────────────────
    blocked: dict[str, str] = {}
    n_docs = n_refs = 0
    with runner.step("rewrite") as st:
        if targets is None:
            roots = None
        else:
            roots = {r for r in (_export_root(ws, c.folder) for c in moved_sets) if r}
            roots |= {r for r in (_export_root(ws, t) for t in targets) if r}
        documents, unvisited = _documents(ws, roots)
        if targets is None and claims:
            # A claimed root's copies stay (deferred) and its export is
            # writing its documents: leave them to the flush.
            documents = [(p, k) for p, k in documents if not store.is_claimed(p.parent, claims)]
            unvisited = [p for p in unvisited if not store.is_claimed(p.parent, claims)]
        for doc_path in unvisited:
            reason = f"{doc_path.name} is not a document this engine rewrites"
            blocked.setdefault(_norm(doc_path.parent), reason)
            warnings.append(f"{doc_path}: not rewritten; copies under {doc_path.parent} kept")
        for doc_path, kind in documents:
            try:
                n = _rewrite_document(doc_path, kind, moved, scan, warnings, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                warnings.append(
                    f"rewrite failed for {doc_path}: {e}; copies under "
                    f"{doc_path.parent} kept"
                )
                blocked[_norm(doc_path.parent)] = f"{doc_path.name} failed to rewrite: {e}"
                continue
            if n:
                n_docs += 1
                n_refs += n
                log["documents"].append({"path": str(doc_path), "references": n})
        st.annotate(f"{n_refs} reference(s) in {n_docs} document(s)")

    # ── 6. remove copies ──────────────────────────────────────────────
    removed = kept = 0
    bytes_removed = bytes_freed = 0
    links_left: dict[tuple[int, int], int] = {}
    removed_by_dir: dict[Path, dict[str, str]] = defaultdict(dict)
    with runner.step("remove") as st:
        for c in moved_sets:
            reason = _under(c.folder / "x", blocked)
            for f in c.files:
                ref = moved[_norm(f.path)]
                if reason is not None:
                    kept += 1
                    log["kept"].append({"path": str(f.path), "sha1": f.sha1, "reason": reason})
                    continue
                if not dry_run and store.cached_sha1(store.resolve_ref(ws, ref)) != f.sha1:
                    kept += 1
                    log["kept"].append({"path": str(f.path), "sha1": f.sha1,
                                        "reason": "no verified store copy"})
                    continue
                try:
                    stf = f.path.stat()
                except OSError as e:
                    warnings.append(f"stat failed {f.path}: {e}")
                    kept += 1
                    continue
                inode = (stf.st_dev, stf.st_ino)
                left = links_left.get(inode, stf.st_nlink) if stf.st_ino else 1
                if not dry_run:
                    try:
                        f.path.unlink()
                    except OSError as e:
                        warnings.append(f"remove failed {f.path}: {e}")
                        kept += 1
                        continue
                if stf.st_ino:
                    links_left[inode] = left - 1
                freed = left <= 1
                removed += 1
                bytes_removed += f.size
                if freed:
                    bytes_freed += f.size
                removed_by_dir[c.folder][f.name] = ref
                log["removed"].append({"path": str(f.path), "size": f.size,
                                       "sha1": f.sha1, "store": ref, "freed": freed})
        st.annotate(f"{removed} removed, {kept} kept")

    # ── 7. legacy hardlink canonicals at the store root ───────────────
    n_legacy = 0
    sroot = store.store_root(ws)
    if targets is None:
        legacy = [
            (f, _LEGACY_SHARD_RE.match(f.name))
            for f in (sorted(sroot.iterdir()) if sroot.is_dir() else ())
            if f.is_file()
        ]
        legacy = [(f, m) for f, m in legacy if m is not None]
        stored_by_name: dict[str, set[str]] = defaultdict(set)
        if legacy:
            for files in scan.store_entries.values():
                for sf in files.values():
                    if sf.sha1 is None:
                        try:
                            sf.sha1 = cache.sha1(sf.path, sf.size, sf.mtime_ns)
                        except OSError:
                            continue
                    stored_by_name[sf.name.lower()].add(sf.sha1)
            for key, refs in refs_by_key.items():
                for c in shared[key].copies:
                    for cf in c.files:
                        if cf.sha1:
                            stored_by_name[_store_name(cf.name).lower()].add(cf.sha1)
        with runner.step("legacy") as st:
            for f, m in legacy:
                name_l = f"{m.group('stem')}{m.group('ext')}".lower()
                try:
                    st_f = f.stat()
                    sha = store.file_sha1(f)
                except OSError as e:
                    warnings.append(f"legacy canonical unreadable {f}: {e}")
                    continue
                if sha not in stored_by_name.get(name_l, ()):
                    warnings.append(f"legacy canonical {f.name} has no store copy; kept")
                    continue
                inode = (st_f.st_dev, st_f.st_ino)
                left = links_left.get(inode, st_f.st_nlink) if dry_run else st_f.st_nlink
                if left > 1:
                    warnings.append(
                        f"legacy canonical {f.name} still has {left - 1} other "
                        f"link(s) outside the scanned trees; kept"
                    )
                    continue
                if not dry_run:
                    try:
                        f.unlink()
                    except OSError as e:
                        warnings.append(f"remove failed {f}: {e}")
                        continue
                n_legacy += 1
                bytes_freed += st_f.st_size
                log["legacy_removed"].append({"path": str(f), "size": st_f.st_size, "sha1": sha})
            st.annotate(f"{n_legacy} removed")

    # ── pending list: folders left alone because a session claims them ─
    if not dry_run:
        # Folders this run took up (its targets, which include the free
        # pending folders; or, for the whole workspace, every free one)
        # are done; claimed ones wait.
        done_keys = target_keys if target_keys is not None else pending_free
        new_pending = {
            p: info for p, info in pending.items()
            if _norm(p) not in done_keys and Path(p).is_dir()
        }
        for c in deferred:
            new_pending.setdefault(str(c.folder), {"since": log["started_at"], "by": log["mode"]})
        if new_pending != pending:
            try:
                _write_pending(ws, new_pending)
            except OSError as e:
                warnings.append(f"could not write the pending list: {e}")

    if targets is None:
        cache.forget_missing(scan.all_keys | {cache.rel(store.resolve_ref(ws, r))
                                              for refs in refs_by_key.values()
                                              for r in refs.values()})
    cache.save()

    entries_after = set(scan.store_entries)
    for key in refs_by_key:
        entries_after.add(key[: store.HASH_PREFIX_LEN].lower())

    result = SharedTextureMigrationResult(
        names=store.SHARED_TEXTURE_NAMES,
        copies_found=len(moved),
        distinct_contents=len(refs_by_key),
        store_files_written=written,
        store_files_present=present,
        redirect_files_written=n_redirects,
        documents_rewritten=n_docs,
        references_rewritten=n_refs,
        copies_removed=removed,
        copies_kept=kept,
        legacy_canonicals_removed=n_legacy,
        bytes_removed=bytes_removed,
        bytes_freed=bytes_freed,
        store_refs=tuple(sorted({r for refs in refs_by_key.values() for r in refs.values()})),
        dry_run=dry_run,
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
        sets_scanned=len(scan.sets),
        sets_shared=len(refs_by_key),
        set_copies_moved=len(moved_sets),
        store_entries=len(entries_after),
        files_hashed=cache.misses,
        copies_deferred=sum(len(c.files) for c in deferred),
    )
    return result, dict(removed_by_dir)


def _new_log(ws: Path, *, dry_run: bool, mode: str) -> dict[str, Any]:
    return {
        "workspace": str(ws),
        "mode": mode,
        "dry_run": dry_run,
        "scope": list(store.SCOPE_ROOTS),
        "always_routed": list(store.SHARED_TEXTURE_NAMES),
        "started_at": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "store_entries": [],
        "redirects": [],
        "documents": [],
        "removed": [],
        "kept": [],
        "deferred": [],
        "legacy_removed": [],
    }


def migrate_shared_textures(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    dry_run: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> SharedTextureMigrationResult:
    """Route every shared texture set of the workspace through the store.

    See the module docstring for the steps. ``dry_run=True`` hashes and
    plans everything but writes nothing except the log and the hash cache.
    """
    cfg = config or PipelineConfig.load()
    ws = Path(workspace or cfg.workspace).resolve()
    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    log = _new_log(ws, dry_run=dry_run, mode="migration")
    with _RunLock(ws):
        result, _ = _run(ws, targets=None, dry_run=dry_run, runner=runner,
                         log=log, warnings=warnings)
    log["finished_at"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log["summary"] = {
        "sets_scanned": result.sets_scanned,
        "sets_shared": result.sets_shared,
        "set_copies_moved": result.set_copies_moved,
        "files_moved": result.copies_found,
        "store_files_written": result.store_files_written,
        "store_files_present": result.store_files_present,
        "store_entries": result.store_entries,
        "redirect_folders": result.redirect_files_written,
        "documents_rewritten": result.documents_rewritten,
        "references_rewritten": result.references_rewritten,
        "copies_removed": result.copies_removed,
        "copies_kept": result.copies_kept,
        "copies_deferred": result.copies_deferred,
        "bytes_removed": result.bytes_removed,
        "bytes_freed": result.bytes_freed,
        "files_hashed": result.files_hashed,
    }
    log["warnings"] = warnings
    log_path: Path | None = ws.joinpath(*_LOG_SUBDIR) / (
        f"migration-{log['started_at']}{'-dry-run' if dry_run else ''}.json"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        log_path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")  # type: ignore[union-attr]
    except OSError as e:
        warnings.append(f"could not write log {log_path}: {e}")
        log_path = None
    return dataclasses.replace(result, log_path=log_path, warnings=tuple(warnings))


def converge_dirs(
    workspace: Path,
    dirs: list[Path],
    *,
    reason: str = "export",
) -> dict[Path, dict[str, str]]:
    """Export-time adoption: route the shared sets of ``dirs`` (and every
    other folder's copy of them, except copies in folders an open export
    session claims) through the store. The pending folders nobody claims
    any more are taken up as well.

    Returns ``{folder: {name: store path}}`` for the files removed from the
    given folders. Appends one line to ``.cache/shared_textures/adopt.jsonl``.
    """
    ws = Path(workspace).resolve()
    runner = StepRunner(None)
    warnings: list[str] = []
    log = _new_log(ws, dry_run=False, mode=reason)
    log["dirs"] = [str(d) for d in dirs]
    with _RunLock(ws):
        result, removed = _run(ws, targets=[Path(d) for d in dirs], dry_run=False,
                               runner=runner, log=log, warnings=warnings)
    for w in warnings:
        print(f"[shared_textures] {w}", file=sys.stderr)
    if result.copies_removed or result.copies_kept or result.copies_deferred or warnings:
        line = {
            "at": log["started_at"],
            "reason": reason,
            "dirs": log["dirs"],
            "sets_shared": result.sets_shared,
            "set_copies_moved": result.set_copies_moved,
            "store_files_written": result.store_files_written,
            "copies_removed": result.copies_removed,
            "copies_kept": result.copies_kept,
            "copies_deferred": result.copies_deferred,
            "bytes_removed": result.bytes_removed,
            "documents": log["documents"],
            "removed": [r["path"] for r in log["removed"]],
            "kept": log["kept"],
            "deferred": sorted({d["path"] for d in log["deferred"]}),
            "warnings": warnings,
            "timings_ms": result.step_timings_ms,
        }
        try:
            p = ws.joinpath(*_LOG_SUBDIR) / "adopt.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            pass
    wanted = {_norm(d) for d in dirs}
    return {d: m for d, m in removed.items() if _norm(d) in wanted}


def flush_pending(workspace: Path) -> int:
    """Move the copies of the pending folders that no export session
    claims any more (called when a session ends). Returns the number of
    folders taken up; 0 without a run when there is nothing to do."""
    ws = Path(workspace).resolve()
    pending = _read_pending(ws)
    if not pending:
        return 0
    claims = store.active_claims(ws)
    free = [p for p in pending if not store.is_claimed(p, claims)]
    if not free:
        return 0
    converge_dirs(ws, [], reason="flush")
    return len(free)


__all__ = [
    "converge_dirs",
    "flush_pending",
    "migrate_shared_textures",
    "wait_for_engine",
    "SharedTextureMigrationResult",
]
