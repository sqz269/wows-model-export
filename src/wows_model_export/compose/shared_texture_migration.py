"""Convert an existing workspace to the shared texture store.

Exports written before the store existed carry their own copy of every
engine-global texture (``ship_atlas_detail.dds``) in each raw-DDS folder,
and their sidecars / library indexes name those local copies. This
composer moves a workspace to the layout new exports produce (see
:mod:`wows_model_export.shared_textures`):

1. **scan** — find every copy of each name in
   :data:`~wows_model_export.shared_textures.SHARED_TEXTURE_NAMES` under
   ``ships/``, ``libraries/`` and ``buildings/`` (the store itself is
   skipped) and hash it (SHA-1; hardlinked copies are hashed once).
2. **store** — write one store copy per distinct content to
   ``libraries/textures_shared/<sha1-8>/<name>`` (verified if present).
3. **redirects** — record ``name -> store path`` in each folder that held
   a copy, so a sidecar or index rebuilt later from disk resolves to the
   store too.
4. **rewrite** — repoint every texture path in the ship sidecars,
   building sidecars and the accessory / projectile library indexes that
   names a copy. Each path is resolved against the directory it is
   relative to and matched by content hash (or by the folder's redirect
   record); a path that matches neither is left alone. The file is
   rewritten by exact string substitution, so its formatting is unchanged.
5. **remove** — delete each copy whose content hash equals its verified
   store copy. Copies without one are kept and reported, and so is every
   copy under an export root (ship / building directory, library) whose
   document failed to rewrite or is a ``*.meta.json`` this composer does
   not know.
6. **legacy** — delete the flat hardlink canonicals
   ``<stem>.<sha256-8>.s<N><ext>`` that ``wows-dedup-textures
   --hardlink`` left at the store root, once nothing else links them and
   their content equals a store copy.

Every removal is logged to ``<workspace>/.cache/shared_textures/``.
Idempotent: a second run finds nothing to move and only repairs
references that still name a local copy.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import shared_textures as store
from ..config import PipelineConfig
from ..types import OnEvent
from ._step_runner import StepRunner

# Export trees the producer writes raw-DDS folders into.
_SCAN_ROOTS: tuple[str, ...] = ("ships", "libraries", "buildings")

# Library indexes whose per-asset texture paths are relative to the
# asset's GLB directory.
_LIBRARY_INDEXES: tuple[str, ...] = ("accessories", "projectiles")

_LEGACY_SHARD_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<hash>[0-9a-f]{8})\.s(?P<shard>\d+)(?P<ext>\.[^.]+)$",
    re.IGNORECASE,
)


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


@dataclass
class _Copy:
    path: Path
    size: int
    sha1: str
    inode: tuple[int, int]
    nlink: int


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _scan(ws: Path, names_lower: frozenset[str], warnings: list[str]) -> list[_Copy]:
    store_root = _norm(store.store_root(ws))
    by_inode: dict[tuple[int, int], str] = {}
    copies: list[_Copy] = []
    for root_name in _SCAN_ROOTS:
        root = ws / root_name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if _norm(Path(dirpath)) == store_root:
                dirnames[:] = []
                continue
            for fn in sorted(filenames):
                if fn.lower() not in names_lower:
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                    inode = (st.st_dev, st.st_ino)
                    sha = by_inode.get(inode) if st.st_ino else None
                    if sha is None:
                        sha = store.file_sha1(p)
                        if st.st_ino:
                            by_inode[inode] = sha
                except OSError as e:
                    warnings.append(f"stat/hash failed {p}: {e}")
                    continue
                copies.append(_Copy(p, st.st_size, sha, inode, st.st_nlink))
    return copies


def _stored_contents(ws: Path, names_lower: frozenset[str]) -> dict[str, set[str]]:
    """``{name (lower): {sha1}}`` for the files already in the store."""
    out: dict[str, set[str]] = defaultdict(set)
    sroot = store.store_root(ws)
    if not sroot.is_dir():
        return out
    for sub in sroot.iterdir():
        if not sub.is_dir():
            continue
        for f in sub.iterdir():
            if f.is_file() and f.name.lower() in names_lower:
                sha = store.cached_sha1(f)
                if sha and sha[: store.HASH_PREFIX_LEN] == sub.name.lower():
                    out[f.name.lower()].add(sha)
    return out


def _documents(ws: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """``(document, kind)`` for every JSON that names raw-DDS textures,
    plus the sidecar-like files (``*.meta.json`` next to a sidecar under
    another name) this migration does not know how to rewrite."""
    out: list[tuple[Path, str]] = []
    unvisited: list[Path] = []
    for top in ("ships", "buildings"):
        root = ws / top
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
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
    for lib in _LIBRARY_INDEXES:
        idx = ws / "libraries" / lib / "index.json"
        if idx.is_file():
            out.append((idx, "library_index"))
    return out, unvisited


def _under(path: Path, roots: dict[str, str]) -> str | None:
    """Reason recorded for the first of ``roots`` that contains ``path``."""
    p = _norm(path)
    for root, reason in roots.items():
        if p.startswith(root + os.sep):
            return reason
    return None


class _Rewriter:
    """Plan the texture-path replacements for one JSON document."""

    def __init__(
        self,
        sha_by_path: dict[str, str],
        stored: dict[str, set[str]],
        warnings: list[str],
        doc_path: Path,
    ) -> None:
        self.sha_by_path = sha_by_path
        self.stored = stored
        self.warnings = warnings
        self.doc_path = doc_path
        self.plan: dict[str, set[str]] = defaultdict(set)
        self.count: dict[str, int] = defaultdict(int)

    def _ref_for(self, value: str, base: Path) -> str | None:
        rel = value.replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]
        canon = store.canonical_name(name)
        target = base.joinpath(*rel.split("/"))
        sha = self.sha_by_path.get(_norm(target))
        if sha is not None and sha in self.stored.get(name.lower(), ()):
            return store.store_ref(canon, sha)
        # Folder migrated earlier: its redirect file knows.
        rec = store.read_redirect_records(target.parent).get(name)
        if rec is not None:
            return rec["ref"]
        # Anything else (a VFS / provenance string, a path to a file that
        # is gone) is not a reference to a scanned copy: never guess.
        self.warnings.append(f"{self.doc_path}: {value!r} (under {base}) left as is")
        return None

    def walk(self, node: Any, base: Path) -> Any:
        """Return ``node`` with references replaced; record the plan."""
        if isinstance(node, dict):
            return {k: self.walk(v, base) for k, v in node.items()}
        if isinstance(node, list):
            return [self.walk(v, base) for v in node]
        if (
            isinstance(node, str)
            and store.is_shared_name(node)
            and not store.is_workspace_path(node)
        ):
            ref = self._ref_for(node, base)
            if ref is not None:
                self.plan[node].add(ref)
                self.count[node] += 1
                return ref
        return node


def _rewrite_document(
    doc_path: Path,
    kind: str,
    sha_by_path: dict[str, str],
    stored: dict[str, set[str]],
    warnings: list[str],
    *,
    dry_run: bool,
) -> int:
    """Rewrite one document; returns the number of references replaced."""
    raw = doc_path.read_bytes()
    text = raw.decode("utf-8-sig")
    low = text.lower()
    if not any(n.lower() in low for n in store.SHARED_TEXTURE_NAMES):
        return 0
    doc = json.loads(text)
    rw = _Rewriter(sha_by_path, stored, warnings, doc_path)

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
    # text exactly as often as it was replaced; the result is re-parsed
    # and compared against the planned document either way.
    new_text = text
    exact = True
    for old, news in rw.plan.items():
        quoted = json.dumps(old, ensure_ascii=False)
        if len(news) != 1 or new_text.count(quoted) != rw.count[old]:
            exact = False
            break
        new_text = new_text.replace(quoted, json.dumps(next(iter(news)), ensure_ascii=False))
    if exact:
        try:
            exact = json.loads(new_text) == expected
        except ValueError:
            exact = False
    if not exact:
        indent = 2 if text.lstrip().startswith("{\n  ") else None
        new_text = json.dumps(expected, indent=indent, ensure_ascii=False)
        if text.endswith("\n"):
            new_text += "\n"
        warnings.append(f"{doc_path}: re-serialized (string substitution was ambiguous)")

    if not dry_run:
        bom = raw[:3] if raw.startswith(b"\xef\xbb\xbf") else b""
        tmp = doc_path.with_name(f".{doc_path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(bom + new_text.encode("utf-8"))
        os.replace(tmp, doc_path)
    return n_refs


def migrate_shared_textures(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    dry_run: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> SharedTextureMigrationResult:
    """Move the workspace's shared-texture copies into the store.

    See the module docstring for the steps. ``dry_run=True`` hashes and
    plans everything but writes nothing except the log.
    """
    cfg = config or PipelineConfig.load()
    ws = Path(workspace or cfg.workspace).resolve()
    names = store.SHARED_TEXTURE_NAMES
    names_lower = frozenset(n.lower() for n in names)
    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log: dict[str, Any] = {
        "workspace": str(ws),
        "dry_run": dry_run,
        "names": list(names),
        "started_at": stamp,
        "store_files": [],
        "redirects": [],
        "documents": [],
        "removed": [],
        "kept": [],
        "legacy_removed": [],
    }

    # ── 1. scan ───────────────────────────────────────────────────────
    with runner.step("scan", detail=", ".join(names)) as st:
        copies = _scan(ws, names_lower, warnings)
        groups: dict[tuple[str, str], list[_Copy]] = defaultdict(list)
        for c in copies:
            groups[(c.path.name.lower(), c.sha1)].append(c)
        st.annotate(f"{len(copies)} copies, {len(groups)} distinct content(s)")

    # ── 2. store ──────────────────────────────────────────────────────
    written = present = 0
    store_ok: dict[tuple[str, str], str] = {}
    with runner.step("store") as st:
        for (name_l, sha), members in sorted(groups.items()):
            canon = store.canonical_name(name_l)
            ref = store.store_ref(canon, sha)
            existed = store.resolve_ref(ws, ref).is_file()
            if not dry_run:
                try:
                    ref = store.put(ws, members[0].path, sha1=sha, name=canon)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"store write failed for {ref}: {e}")
                    continue
            store_ok[(name_l, sha)] = ref
            if existed:
                present += 1
            else:
                written += 1
            log["store_files"].append({
                "ref": ref, "sha1": sha, "size": members[0].size,
                "copies": len(members), "existed": existed,
            })
        st.annotate(f"{written} written, {present} already present")

    stored = _stored_contents(ws, names_lower)
    if dry_run:
        for (name_l, sha) in store_ok:
            stored[name_l].add(sha)
    sha_by_path = {_norm(c.path): c.sha1 for c in copies}

    # ── 3. redirects ──────────────────────────────────────────────────
    n_redirects = 0
    with runner.step("redirects") as st:
        by_dir: dict[Path, dict[str, dict[str, Any]]] = defaultdict(dict)
        for c in copies:
            ref = store_ok.get((c.path.name.lower(), c.sha1))
            if ref is not None:
                by_dir[c.path.parent][c.path.name] = store.redirect_record(ref, c.sha1, c.size)
        for d, recs in sorted(by_dir.items()):
            if not dry_run:
                try:
                    store.write_redirects(d, recs)
                except OSError as e:
                    warnings.append(f"redirect write failed in {d}: {e}")
                    continue
            n_redirects += 1
            log["redirects"].append(str(d))
        st.annotate(f"{n_redirects} folder(s)")

    # ── 4. rewrite references ─────────────────────────────────────────
    # Export roots (a ship / building directory, a library) whose
    # references may still name a local copy: their copies are kept.
    blocked: dict[str, str] = {}
    n_docs = n_refs = 0
    with runner.step("rewrite") as st:
        documents, unvisited = _documents(ws)
        for doc_path in unvisited:
            reason = f"{doc_path.name} is not a document this migration rewrites"
            blocked.setdefault(_norm(doc_path.parent), reason)
            warnings.append(f"{doc_path}: not rewritten; copies under {doc_path.parent} kept")
        for doc_path, kind in documents:
            try:
                n = _rewrite_document(
                    doc_path, kind, sha_by_path, stored, warnings, dry_run=dry_run,
                )
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

    # ── 5. remove copies ──────────────────────────────────────────────
    # Links left per inode (dry run simulates the unlinks), so a removal
    # that drops the last link counts its bytes as freed.
    links_left: dict[tuple[int, int], int] = {}
    for c in copies:
        links_left.setdefault(c.inode, c.nlink)
    removed = kept = 0
    bytes_removed = bytes_freed = 0
    with runner.step("remove") as st:
        for c in copies:
            reason = _under(c.path, blocked)
            if reason is not None:
                kept += 1
                log["kept"].append({"path": str(c.path), "sha1": c.sha1, "reason": reason})
                continue
            ref = store_ok.get((c.path.name.lower(), c.sha1))
            verified = ref is not None and (
                dry_run or store.cached_sha1(store.resolve_ref(ws, ref)) == c.sha1
            )
            if not verified:
                kept += 1
                log["kept"].append({"path": str(c.path), "sha1": c.sha1,
                                    "reason": "no verified store copy"})
                continue
            if dry_run:
                left = links_left[c.inode]
            else:
                try:
                    left = c.path.stat().st_nlink
                    c.path.unlink()
                except OSError as e:
                    warnings.append(f"remove failed {c.path}: {e}")
                    kept += 1
                    continue
            links_left[c.inode] = left - 1
            freed = left <= 1
            removed += 1
            bytes_removed += c.size
            if freed:
                bytes_freed += c.size
            log["removed"].append({"path": str(c.path), "size": c.size,
                                   "sha1": c.sha1, "store": ref, "freed": freed})
        st.annotate(f"{removed} removed, {kept} kept")

    # ── 6. legacy hardlink canonicals at the store root ───────────────
    n_legacy = 0
    sroot = store.store_root(ws)
    with runner.step("legacy") as st:
        for f in (sorted(sroot.iterdir()) if sroot.is_dir() else ()):
            m = _LEGACY_SHARD_RE.match(f.name) if f.is_file() else None
            if m is None:
                continue
            name_l = f"{m.group('stem')}{m.group('ext')}".lower()
            if name_l not in names_lower:
                continue
            try:
                st_f = f.stat()
                sha = store.file_sha1(f)
            except OSError as e:
                warnings.append(f"legacy canonical unreadable {f}: {e}")
                continue
            if sha not in stored.get(name_l, ()):
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

    log["finished_at"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log["warnings"] = warnings
    log_path: Path | None = ws / ".cache" / "shared_textures" / (
        f"migration-{stamp}{'-dry-run' if dry_run else ''}.json"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        warnings.append(f"could not write log {log_path}: {e}")
        log_path = None

    return SharedTextureMigrationResult(
        names=names,
        copies_found=len(copies),
        distinct_contents=len(groups),
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
        store_refs=tuple(sorted(set(store_ok.values()))),
        log_path=log_path,
        dry_run=dry_run,
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = ["migrate_shared_textures", "SharedTextureMigrationResult"]
