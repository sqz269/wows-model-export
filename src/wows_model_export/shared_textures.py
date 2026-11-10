"""Content-addressed store for textures shared byte-for-byte across exports.

Some engine-global textures are bound by materials in every export folder.
``ship_atlas_detail.dds`` (the 2048² detail atlas, 16.8 MB) is the main
one: the toolkit's raw-DDS dumper writes it into every ship, accessory,
projectile and building ``textures_dds/`` directory it touches, so a full
workspace held well over a thousand identical copies, and every consumer
that loaded each folder's copy loaded the same texture hundreds of times.

The pipeline keeps ONE copy per (file name, content) instead::

    <workspace>/libraries/textures_shared/<sha1-8>/<file name>

and every sidecar / library-index texture path that would have named a
local copy names the store copy by its workspace-relative path::

    libraries/textures_shared/54b48161/ship_atlas_detail.dds

Path rule for consumers (the same rule the camo libraries already use):
a texture path that starts with ``libraries/`` is relative to the
workspace root; every other texture path is relative to the directory of
the model that owns it (``models/`` for a ship, the asset directory for a
library entry).

How the producer gets there:

* :func:`adopt_dir` runs after every toolkit export that dumped raw DDS
  into a workspace directory (see :mod:`wows_model_export.toolkit.ship`).
  It hashes each file whose name is in :data:`SHARED_TEXTURE_NAMES`,
  makes sure the store holds that content, deletes the local copy and
  records ``name -> store path`` in the directory's redirect file
  :data:`REDIRECT_FILENAME`.
* :func:`dds_dir_entries` lists a raw-DDS directory for the sidecar and
  library resolvers. Redirected names come back with their store path, so
  every texture slot that would have pointed at the local copy points at
  the store instead, also when a sidecar is rebuilt later from disk
  without a new export.
* :mod:`wows_model_export.compose.shared_textures` converts an existing
  workspace (store copy, reference rewrite, removal of local copies).

Only names in :data:`SHARED_TEXTURE_NAMES` are routed through the store.
The store layout is generic: adding a name here routes that texture too.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Workspace-relative root of the store (POSIX form, as written in
#: sidecars). Shares its directory with the legacy hardlink canonicals of
#: ``wows-dedup-textures --hardlink`` (flat ``<stem>.<sha256-8>.s<N><ext>``
#: files); store entries are the ``<sha1-8>/<name>`` subdirectories.
STORE_REL = "libraries/textures_shared"

#: Prefix that marks a texture path as workspace-relative.
WORKSPACE_PATH_PREFIX = "libraries/"

#: File names (compared case-insensitively) routed into the store at
#: export time. Each one is engine-global: bound unchanged by materials in
#: many export folders.
SHARED_TEXTURE_NAMES: tuple[str, ...] = ("ship_atlas_detail.dds",)

#: Redirect file written into a raw-DDS directory whose shared textures
#: were moved to the store. Pipeline-internal: never published.
REDIRECT_FILENAME = "shared_textures.json"

REDIRECT_SCHEMA_VERSION = 1

#: Length of the hex hash prefix that names a store subdirectory.
HASH_PREFIX_LEN = 8

_SHARED_LOWER = frozenset(n.lower() for n in SHARED_TEXTURE_NAMES)

# Store files verified in this process: path -> (size, mtime_ns, sha1).
_verified: dict[str, tuple[int, int, str]] = {}
_lock = threading.Lock()


class SharedTextureConflict(RuntimeError):
    """A store path already holds different content (hash-prefix clash)."""


# ---------------------------------------------------------------------------
# Names, paths, hashes
# ---------------------------------------------------------------------------


def is_shared_name(name: str) -> bool:
    """Whether ``name`` (a file name or path) is routed through the store."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return base.lower() in _SHARED_LOWER


def canonical_name(name: str) -> str:
    """The registered spelling of a shared texture name (store file name)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    for n in SHARED_TEXTURE_NAMES:
        if n.lower() == base.lower():
            return n
    return base


def is_workspace_path(path: str) -> bool:
    """Whether a texture path is workspace-relative (``libraries/...``)."""
    return path.replace("\\", "/").startswith(WORKSPACE_PATH_PREFIX)


def file_sha1(path: Path, _buf: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


def store_ref(name: str, sha1: str) -> str:
    """Workspace-relative path of the store copy of ``name`` with ``sha1``."""
    return f"{STORE_REL}/{sha1[:HASH_PREFIX_LEN].lower()}/{name}"


def store_root(workspace: Path) -> Path:
    return Path(workspace).joinpath(*STORE_REL.split("/"))


def store_path(workspace: Path, name: str, sha1: str) -> Path:
    return Path(workspace).joinpath(*store_ref(name, sha1).split("/"))


def resolve_ref(workspace: Path, ref: str) -> Path:
    """Absolute path of a workspace-relative texture path."""
    return Path(workspace).joinpath(*ref.replace("\\", "/").split("/"))


def is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Store writes
# ---------------------------------------------------------------------------


def cached_sha1(path: Path) -> str | None:
    """SHA-1 of a store file, cached per (size, mtime) for this process."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    with _lock:
        hit = _verified.get(key)
    if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    sha = file_sha1(path)
    with _lock:
        _verified[key] = (st.st_size, st.st_mtime_ns, sha)
    return sha


def put(
    workspace: Path,
    src: Path,
    *,
    sha1: str | None = None,
    name: str | None = None,
) -> str:
    """Make sure the store holds ``src``'s content; return its store path.

    ``name`` is the file name in the store (default: ``src``'s name).
    The store copy is written once (temp file + atomic rename, a fresh
    file rather than a link to ``src``). A store file that already exists
    is verified by full SHA-1; different content under the same 8-hex
    prefix raises :class:`SharedTextureConflict`.
    """
    src = Path(src)
    name = name or src.name
    sha = sha1 or file_sha1(src)
    dst = store_path(workspace, name, sha)
    existing = cached_sha1(dst) if dst.exists() else None
    if existing is not None:
        if existing != sha:
            raise SharedTextureConflict(
                f"{dst} holds sha1 {existing}, not {sha} (from {src})"
            )
        return store_ref(name, sha)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                fo.write(chunk)
        try:
            os.replace(tmp, dst)
        except OSError:
            # A concurrent writer got there first (Windows refuses to
            # replace a file another process has open). Accept its copy
            # when the content matches.
            if not dst.exists() or cached_sha1(dst) != sha:
                raise
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if cached_sha1(dst) != sha:
        raise SharedTextureConflict(f"{dst} does not hold sha1 {sha} after write")
    return store_ref(name, sha)


# ---------------------------------------------------------------------------
# Redirect files
# ---------------------------------------------------------------------------


def read_redirect_records(dds_dir: Path) -> dict[str, dict[str, Any]]:
    """``{name: {"ref", "sha1", "size"}}`` from a directory's redirect file."""
    p = Path(dds_dir) / REDIRECT_FILENAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, rec in ((doc or {}).get("textures") or {}).items():
        if isinstance(rec, dict) and isinstance(rec.get("ref"), str):
            out[name] = rec
    return out


def read_redirects(dds_dir: Path) -> dict[str, str]:
    """``{name: store path}`` for the textures redirected out of ``dds_dir``."""
    return {name: rec["ref"] for name, rec in read_redirect_records(dds_dir).items()}


def write_redirects(dds_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    """Merge ``records`` into ``dds_dir``'s redirect file (atomic write)."""
    dds_dir = Path(dds_dir)
    merged = read_redirect_records(dds_dir)
    merged.update(records)
    doc = {
        "schema_version": REDIRECT_SCHEMA_VERSION,
        "store": STORE_REL,
        "textures": {k: merged[k] for k in sorted(merged)},
    }
    text = json.dumps(doc, indent=2) + "\n"
    target = dds_dir / REDIRECT_FILENAME
    try:
        if target.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{REDIRECT_FILENAME}.", suffix=".tmp", dir=str(dds_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def redirect_record(ref: str, sha1: str, size: int) -> dict[str, Any]:
    return {"ref": ref, "sha1": sha1, "size": int(size)}


# ---------------------------------------------------------------------------
# Producer hook: move shared textures out of a fresh raw-DDS dump
# ---------------------------------------------------------------------------


def adopt_dir(dds_dir: Path, *, workspace: Path) -> dict[str, str]:
    """Move every shared texture in ``dds_dir`` into the store.

    For each file named in :data:`SHARED_TEXTURE_NAMES`: hash it, make sure
    the store holds the content (:func:`put`), record ``name -> store
    path`` in the directory's redirect file, then delete the local copy.
    Idempotent; returns ``{name: store path}`` for the files moved.
    """
    dds_dir = Path(dds_dir)
    if not dds_dir.is_dir():
        return {}
    moved: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    for f in sorted(dds_dir.iterdir()):
        if not f.is_file() or not is_shared_name(f.name):
            continue
        size = f.stat().st_size
        sha = file_sha1(f)
        ref = put(workspace, f, sha1=sha, name=canonical_name(f.name))
        records[f.name] = redirect_record(ref, sha, size)
        moved[f.name] = ref
    if records:
        # Redirect first: a crash between the two steps leaves a stale
        # local copy (re-adopted next time), never a dangling reference.
        write_redirects(dds_dir, records)
        for name in records:
            try:
                (dds_dir / name).unlink()
            except OSError as e:
                print(
                    f"[shared_textures] could not remove {dds_dir / name}: {e}",
                    file=sys.stderr,
                )
    return moved


def adopt_dirs(dirs: Iterable[Path | str | None], *, workspace: Path) -> dict[Path, dict[str, str]]:
    """:func:`adopt_dir` over the given raw-DDS directories.

    Directories outside ``workspace`` are left alone (their consumers
    could not resolve a workspace-relative path). Never raises: a failure
    is reported on stderr and that directory keeps its local copy, which
    still resolves through the owning-directory rule.
    """
    out: dict[Path, dict[str, str]] = {}
    seen: set[str] = set()
    ws = Path(workspace)
    for d in dirs:
        if not d:
            continue
        p = Path(d)
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if not is_inside(p, ws):
            continue
        try:
            moved = adopt_dir(p, workspace=ws)
        except Exception as e:  # noqa: BLE001 - never fail an export over this
            print(f"[shared_textures] adopt failed for {p}: {e}", file=sys.stderr)
            continue
        if moved:
            out[p] = moved
    return out


# ---------------------------------------------------------------------------
# Resolver view of a raw-DDS directory
# ---------------------------------------------------------------------------


def dds_dir_entries(dds_dir: Path, *, dds_uri_prefix: str = "textures_dds/") -> list[tuple[str, str]]:
    """``(file name, texture path)`` for every file in a raw-DDS directory.

    Local files map to ``<dds_uri_prefix><name>`` (relative to the owning
    model directory). Names redirected to the store and no longer present
    locally map to their workspace-relative store path. A local file wins
    over a redirect of the same name. Ordered like ``sorted(dir.iterdir())``
    (path order, case-insensitive on Windows), the order the resolvers
    used before redirects existed.
    """
    d = Path(dds_dir)
    if not d.is_dir():
        return []
    entries: dict[Path, str] = {}
    local: set[str] = set()
    for f in d.iterdir():
        if f.is_file() and f.name != REDIRECT_FILENAME:
            entries[f] = f"{dds_uri_prefix}{f.name}"
            local.add(f.name.lower())
    for name, ref in read_redirects(d).items():
        if name.lower() not in local:
            entries[d / name] = ref
    return [(p.name, entries[p]) for p in sorted(entries)]


__all__ = [
    "HASH_PREFIX_LEN",
    "REDIRECT_FILENAME",
    "SHARED_TEXTURE_NAMES",
    "STORE_REL",
    "WORKSPACE_PATH_PREFIX",
    "SharedTextureConflict",
    "adopt_dir",
    "adopt_dirs",
    "cached_sha1",
    "canonical_name",
    "dds_dir_entries",
    "file_sha1",
    "is_inside",
    "is_shared_name",
    "is_workspace_path",
    "put",
    "read_redirect_records",
    "read_redirects",
    "redirect_record",
    "resolve_ref",
    "store_path",
    "store_ref",
    "store_root",
    "write_redirects",
]
