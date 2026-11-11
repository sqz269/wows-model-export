"""Content-addressed store for textures shared byte-for-byte across exports.

The toolkit's raw-DDS dumper writes every texture a model binds into that
model's own folder. Many textures are bound by models in several folders:
the engine-global detail atlas ``ship_atlas_detail.dds`` (every ship,
accessory, projectile and building), and also whole texture sets that one
asset family shares (a turret's maps copied into its alive / dead / fired /
variant model folders, a camo pattern in every ship, a skin that repeats
its ship's hull maps, ``default_ao.dds`` in a thousand accessories). The
pipeline keeps ONE copy of each such set instead::

    <workspace>/libraries/textures_shared/<key-8>/<file name>

and every sidecar / library-index texture path that would have named a
folder copy names the store copy by its workspace-relative path::

    libraries/textures_shared/54b48161/ship_atlas_detail.dds

Path rule for consumers (the same rule the camo libraries already use):
a texture path that starts with ``libraries/`` is relative to the
workspace root; every other texture path is relative to the directory of
the model that owns it (``models/`` for a ship, the asset directory for a
library entry).

**Texture sets.** The unit the store handles is a *texture set*: every
file of one texture in one folder, i.e. the split-mip files
``<stem>.dds`` / ``<stem>.dd0`` / ``<stem>.dd1`` / ``<stem>.dd2`` / ...
that one ``dds_mips`` list names. A set moves as a whole and keeps its
file names; it is shared only when another folder (or the store) holds
the same file names with byte-identical content for every member. The
store folder is keyed by :func:`set_key` (first 8 hex): the SHA-1 of the
file for a one-file set (so the phase-1 atlas entry ``54b48161`` keeps
its path), the SHA-1 of the sorted ``name<TAB>sha1`` manifest otherwise.

**Which sets move.** A set in a raw-DDS folder under one of
:data:`SCOPE_ROOTS` moves into the store when

* another in-scope folder holds a byte-identical set, or
* the store already holds it, or
* it is one file named in :data:`SHARED_TEXTURE_NAMES` (engine-global
  textures; routed even with a single copy).

How the producer gets there:

* :func:`adopt_dirs` runs after every producer step that writes raw DDS
  into a workspace folder (the toolkit wrappers in
  :mod:`wows_model_export.toolkit.ship`, and the composers after their
  own post-processing such as emissive synthesis). It routes the sets in
  those folders through the store, together with every other folder's
  copy of the same sets, rewrites the documents that named the moved
  copies and records ``name -> store path`` in each folder's redirect
  file :data:`REDIRECT_FILENAME` (see
  :mod:`wows_model_export.compose.shared_texture_migration`).
* :func:`export_session` brackets every export that computes documents
  in memory and writes them later (ship ingest and scaffold, skin packs,
  the accessory / projectile libraries, buildings). While it runs, its
  export roots are *claimed*: adoption by anyone else leaves the copies
  in a claimed root where they are (recorded in :data:`PENDING_REL`), so
  a document that export is still computing never names a file that is
  gone. When the session ends the pending copies move. The claims are
  visible to other processes through :data:`SESSIONS_REL`.
* :func:`repair_refs` repoints, in a document about to be written, the
  paths whose folder copy has moved to the store (a safety net for the
  writers that compute documents before their own last adoption).
* :func:`dds_dir_entries` lists a raw-DDS directory for the sidecar and
  library resolvers. Redirected names come back with their store path, so
  every texture slot that would have pointed at the local copy points at
  the store instead, also when a sidecar is rebuilt later from disk
  without a new export.
* :func:`dir_files` / :func:`dir_file` give producer code that READS a
  raw-DDS folder (emissive synthesis, diffuse recovery) the file behind
  each name, local or in the store.
* ``wows-dedup-textures`` converts an existing workspace
  (:func:`wows_model_export.compose.shared_texture_migration.migrate_shared_textures`).

The redirect files, the hash cache (:data:`HASH_CACHE_REL`), the session
files and the pending list are pipeline-internal and never published.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

#: Workspace-relative root of the store (POSIX form, as written in
#: sidecars). Shares its directory with the legacy hardlink canonicals of
#: ``wows-dedup-textures --hardlink`` (flat ``<stem>.<sha256-8>.s<N><ext>``
#: files); store entries are the ``<key-8>/<name>`` subdirectories.
STORE_REL = "libraries/textures_shared"

#: Prefix that marks a texture path as workspace-relative.
WORKSPACE_PATH_PREFIX = "libraries/"

#: File names (compared case-insensitively) always routed into the store,
#: even when only one folder holds a copy. Each one is engine-global:
#: bound unchanged by materials in many export folders.
SHARED_TEXTURE_NAMES: tuple[str, ...] = ("ship_atlas_detail.dds",)

#: Workspace-relative export trees whose raw-DDS folders the store serves
#: (ship incl. exterior hulls and skin packs, accessory, projectile and
#: building exports). Map exports and the camo / decal libraries are not
#: part of it; neither is anything under ``.cache/``.
SCOPE_ROOTS: tuple[str, ...] = (
    "ships",
    "libraries/accessories",
    "libraries/projectiles",
    "buildings",
)

#: Redirect file written into a raw-DDS directory whose textures were moved
#: to the store. Pipeline-internal: never published.
REDIRECT_FILENAME = "shared_textures.json"

REDIRECT_SCHEMA_VERSION = 1

#: Persistent SHA-1 cache of the in-scope texture files, keyed by
#: workspace-relative path and validated by (size, mtime). Pipeline-internal.
HASH_CACHE_REL = ".cache/shared_textures/hashes.json"

HASH_CACHE_SCHEMA_VERSION = 1

#: Directory of the per-process export-session files (one ``<pid>.json``
#: per process with open sessions, listing the export roots it claims).
#: Pipeline-internal.
SESSIONS_REL = ".cache/shared_textures/sessions"

#: Folders whose copies of shared sets were left in place because their
#: export root was claimed; moved when no session claims them any more.
#: Written by the engine under its run lock. Pipeline-internal.
PENDING_REL = ".cache/shared_textures/pending.json"

#: A session file older than this is ignored (guards against a reused pid).
SESSION_STALE_S = 48 * 3600.0

#: Length of the hex hash prefix that names a store subdirectory.
HASH_PREFIX_LEN = 8

_SHARED_LOWER = frozenset(n.lower() for n in SHARED_TEXTURE_NAMES)

#: A texture-set member: ``<stem>.dds`` or ``<stem>.dd<N>``.
_SET_MEMBER_RE = re.compile(r"^(?P<stem>.+)\.(?:dds|dd\d+)$", re.IGNORECASE)

# Store files verified in this process: path -> (size, mtime_ns, sha1).
_verified: dict[str, tuple[int, int, str]] = {}
_lock = threading.Lock()


class SharedTextureConflict(RuntimeError):
    """A store path already holds different content (hash-prefix clash)."""


class SharedTextureIncomplete(RuntimeError):
    """A source file is not a complete texture (e.g. an interrupted write)."""


# ---------------------------------------------------------------------------
# Names, paths, hashes
# ---------------------------------------------------------------------------


def _basename(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def is_shared_name(name: str) -> bool:
    """Whether ``name`` (a file name or path) is always routed through the store."""
    return _basename(name).lower() in _SHARED_LOWER


def canonical_name(name: str) -> str:
    """The registered spelling of an always-routed name (store file name)."""
    base = _basename(name)
    for n in SHARED_TEXTURE_NAMES:
        if n.lower() == base.lower():
            return n
    return base


def set_stem(name: str) -> str | None:
    """The texture-set stem of a file name (``X_a.dd0`` -> ``X_a``), else None."""
    m = _SET_MEMBER_RE.match(_basename(name))
    return m.group("stem") if m else None


def is_set_member(name: str) -> bool:
    """Whether ``name`` is a split-mip DDS file (``.dds`` / ``.dd<N>``)."""
    return set_stem(name) is not None


def set_key(members: Mapping[str, str]) -> str:
    """Store key of a texture set given ``{file name: sha1}`` of its members.

    A one-file set is keyed by that file's SHA-1; a larger set by the SHA-1
    of its manifest, the lines ``<name>\\t<sha1>\\n`` sorted by name.
    """
    if not members:
        raise ValueError("empty texture set")
    items = sorted((n, s.lower()) for n, s in members.items())
    if len(items) == 1:
        return items[0][1]
    h = hashlib.sha1()
    for name, sha in items:
        h.update(f"{name}\t{sha}\n".encode("utf-8"))
    return h.hexdigest()


def is_workspace_path(path: str) -> bool:
    """Whether a texture path is workspace-relative (``libraries/...``)."""
    return path.replace("\\", "/").startswith(WORKSPACE_PATH_PREFIX)


# DDS payload sizes, for spotting a truncated file before it is stored.
# Block-compressed formats: bytes per 4x4 block.
_DDS_FOURCC_BLOCK = {
    b"DXT1": 8, b"DXT2": 16, b"DXT3": 16, b"DXT4": 16, b"DXT5": 16,
    b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
    b"ATI2": 16, b"BC5U": 16, b"BC5S": 16,
}
_DXGI_BLOCK = {
    **{f: 8 for f in (70, 71, 72, 79, 80, 81)},                       # BC1, BC4
    **{f: 16 for f in (73, 74, 75, 76, 77, 78, 82, 83, 84,             # BC2, BC3, BC5
                       94, 95, 96, 97, 98, 99)},                       # BC6H, BC7
}
# Uncompressed formats: bytes per pixel.
_D3DFMT_BPP = {111: 2, 112: 4, 113: 8, 114: 4, 115: 8, 116: 16}
_DXGI_BPP = {
    **{f: 16 for f in (1, 2, 3, 4)},                                   # R32G32B32A32
    **{f: 8 for f in (9, 10, 11, 12, 13, 14, 15, 16, 17, 18)},         # R16G16B16A16, R32G32
    **{f: 4 for f in (23, 24, 25, 26, 27, 28, 29, 30, 31, 32,          # R10G10B10A2, R11G11B10, RGBA8
                      33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,      # R16G16, R32
                      87, 88, 90, 91, 92, 93)},                        # BGRA8 / BGRX8
    **{f: 2 for f in (48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59)}, # R8G8, R16
    **{f: 1 for f in (60, 61, 62, 63, 64, 65)},                        # R8, A8
}


def dds_is_complete(path: Path) -> bool:
    """Whether ``path`` is a DDS file whose payload is all there.

    Checks the header and, for the 2D / cube formats whose layout is
    known, that the file holds every mip level of every face. A file of an
    unknown format (or a volume texture) passes on a valid header alone.
    Split-mip files (``.dd0`` / ``.dd1`` / ...) carry their own DDS header
    and are checked the same way.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(148)
        size = os.path.getsize(path)
    except OSError:
        return False
    if len(head) < 128 or head[:4] != b"DDS " or int.from_bytes(head[4:8], "little") != 124:
        return False

    def u32(off: int) -> int:
        return int.from_bytes(head[off:off + 4], "little")

    height, width, mips = u32(12), u32(16), u32(28) or 1
    pf_flags, fourcc, rgb_bits, caps2 = u32(80), head[84:88], u32(88), u32(112)
    header_len, faces = 128, 1
    block = bpp = None
    if fourcc == b"DX10":
        if len(head) < 148:
            return False
        header_len = 148
        dxgi, dim, misc, array = u32(128), u32(132), u32(136), u32(140) or 1
        if dim == 4:  # volume
            return True
        faces = array * (6 if misc & 0x4 else 1)
        block, bpp = _DXGI_BLOCK.get(dxgi), _DXGI_BPP.get(dxgi)
    else:
        if caps2 & 0x200000:  # volume
            return True
        if caps2 & 0x200:  # cube: one bit per face present
            faces = bin(caps2 & 0xFC00).count("1") or 6
        if pf_flags & 0x4:  # DDPF_FOURCC
            block = _DDS_FOURCC_BLOCK.get(bytes(fourcc))
            bpp = _D3DFMT_BPP.get(u32(84))
        elif rgb_bits and rgb_bits % 8 == 0:
            bpp = rgb_bits // 8
    if block is None and bpp is None:
        return True
    payload = 0
    w, h = max(width, 1), max(height, 1)
    for _ in range(mips):
        if block is not None:
            payload += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
        else:
            payload += w * h * bpp
        w, h = max(1, w // 2), max(1, h // 2)
    return size >= header_len + payload * faces


def file_sha1(path: Path, _buf: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


def store_ref(name: str, key: str) -> str:
    """Workspace-relative path of ``name`` in the store entry ``key``.

    ``key`` is a set key (:func:`set_key`); for a one-file set it is the
    file's SHA-1.
    """
    return f"{STORE_REL}/{key[:HASH_PREFIX_LEN].lower()}/{name}"


def store_root(workspace: Path) -> Path:
    return Path(workspace).joinpath(*STORE_REL.split("/"))


def store_path(workspace: Path, name: str, key: str) -> Path:
    return Path(workspace).joinpath(*store_ref(name, key).split("/"))


def resolve_ref(workspace: Path, ref: str) -> Path:
    """Absolute path of a workspace-relative texture path."""
    return Path(workspace).joinpath(*ref.replace("\\", "/").split("/"))


def is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def in_scope(path: Path, workspace: Path) -> bool:
    """Whether ``path`` lies in one of the export trees the store serves
    (:data:`SCOPE_ROOTS`) and outside the store itself."""
    p = Path(path)
    ws = Path(workspace)
    if is_inside(p, store_root(ws)):
        return False
    return any(is_inside(p, ws.joinpath(*r.split("/"))) for r in SCOPE_ROOTS)


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
    key: str | None = None,
) -> str:
    """Make sure the store holds ``src``'s content; return its store path.

    ``name`` is the file name in the store (default: ``src``'s name);
    ``key`` the store entry (default: the file's SHA-1, i.e. a one-file
    set). The store copy is written once (temp file + atomic rename, a
    fresh file rather than a link to ``src``). A store file that already
    exists is verified by full SHA-1; different content at the same path
    raises :class:`SharedTextureConflict`. A DDS source that is cut short
    (:func:`dds_is_complete`) raises :class:`SharedTextureIncomplete` and
    is not stored.
    """
    src = Path(src)
    name = name or src.name
    if is_set_member(src.name) and not dds_is_complete(src):
        raise SharedTextureIncomplete(f"{src} is not a complete DDS file; not stored")
    sha = sha1 or file_sha1(src)
    dst = store_path(workspace, name, key or sha)
    existing = cached_sha1(dst) if dst.exists() else None
    if existing is not None:
        if existing != sha:
            raise SharedTextureConflict(
                f"{dst} holds sha1 {existing}, not {sha} (from {src})"
            )
        return store_ref(name, key or sha)
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
    return store_ref(name, key or sha)


# ---------------------------------------------------------------------------
# Persistent hash cache
# ---------------------------------------------------------------------------


class HashCache:
    """SHA-1 of workspace files, keyed by workspace-relative path and
    validated by (size, mtime_ns). Saved to :data:`HASH_CACHE_REL`.

    Thread-safe for :meth:`sha1`; one instance per run. A lost update
    (two processes saving at once) only costs a re-hash later.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace.joinpath(*HASH_CACHE_REL.split("/"))
        self._files: dict[str, list[Any]] = {}
        self._dirty = False
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if doc.get("schema_version") == HASH_CACHE_SCHEMA_VERSION:
                files = doc.get("files")
                if isinstance(files, dict):
                    self._files = files
        except (OSError, ValueError, AttributeError):
            pass

    def rel(self, path: Path) -> str:
        r = os.path.relpath(str(path), str(self.workspace))
        return os.path.normcase(r).replace("\\", "/")

    def sha1(self, path: Path, size: int | None = None, mtime_ns: int | None = None) -> str:
        if size is None or mtime_ns is None:
            st = Path(path).stat()
            size, mtime_ns = st.st_size, st.st_mtime_ns
        k = self.rel(path)
        with self._lock:
            hit = self._files.get(k)
        if hit and hit[0] == size and hit[1] == mtime_ns:
            with self._lock:
                self.hits += 1
            return hit[2]
        sha = file_sha1(Path(path))
        with self._lock:
            self._files[k] = [size, mtime_ns, sha]
            self._dirty = True
            self.misses += 1
        return sha

    def forget_missing(self, present: set[str]) -> None:
        """Drop entries under the scanned trees whose files are gone.
        ``present`` holds :meth:`rel` keys of every file seen by a full scan."""
        with self._lock:
            gone = [k for k in self._files if k not in present]
            for k in gone:
                del self._files[k]
            if gone:
                self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            doc = {"schema_version": HASH_CACHE_SCHEMA_VERSION,
                   "files": dict(sorted(self._files.items()))}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".hashes.", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[shared_textures] could not save hash cache: {e}", file=sys.stderr)


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


def _write_redirect_doc(dds_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    target = Path(dds_dir) / REDIRECT_FILENAME
    if not records:
        try:
            target.unlink()
        except OSError:
            pass
        return
    doc = {
        "schema_version": REDIRECT_SCHEMA_VERSION,
        "store": STORE_REL,
        "textures": {k: records[k] for k in sorted(records)},
    }
    text = json.dumps(doc, indent=2) + "\n"
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


def write_redirects(dds_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    """Merge ``records`` into ``dds_dir``'s redirect file (atomic write)."""
    merged = read_redirect_records(dds_dir)
    merged.update(records)
    _write_redirect_doc(Path(dds_dir), merged)


def drop_redirects(dds_dir: Path, names: Iterable[str]) -> int:
    """Remove the redirect records of ``names`` (a set that is local again).
    Deletes the redirect file when nothing is left. Returns records removed."""
    current = read_redirect_records(dds_dir)
    lower = {n.lower() for n in names}
    kept = {n: r for n, r in current.items() if n.lower() not in lower}
    if len(kept) == len(current):
        return 0
    _write_redirect_doc(Path(dds_dir), kept)
    return len(current) - len(kept)


def redirect_record(ref: str, sha1: str, size: int) -> dict[str, Any]:
    return {"ref": ref, "sha1": sha1, "size": int(size)}


# ---------------------------------------------------------------------------
# Export sessions: the export roots whose documents are being written
# ---------------------------------------------------------------------------
#
# An export reads its folders, computes documents in memory (a ship
# sidecar, the library index) and writes them later. Adoption by another
# export (another thread or process) in between would move a copy those
# documents still name. A session claims the export's roots for that
# time; the engine leaves the copies in claimed folders alone (except the
# folders it was asked to adopt) and lists them in PENDING_REL, and they
# move once no session claims them.


class _Session:
    __slots__ = ("ws", "ws_key", "claims", "raw")

    def __init__(self, ws: Path, ws_key: str) -> None:
        self.ws = ws
        self.ws_key = ws_key
        self.claims: list[str] = []   # normcased resolved paths
        self.raw: list[str] = []      # resolved paths as written


_sessions: dict[int, _Session] = {}
_sessions_lock = threading.Lock()
_session_ids = itertools.count(1)
_tls = threading.local()


def _path_key(p: Path | str) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def _sessions_dir(ws: Path) -> Path:
    return Path(ws).joinpath(*SESSIONS_REL.split("/"))


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid is running (best effort)."""
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _sync_session_file(ws: Path, ws_key: str) -> None:
    """Rewrite this process's session file for ``ws`` (caller holds
    ``_sessions_lock``)."""
    claims = sorted({c for s in _sessions.values() if s.ws_key == ws_key for c in s.raw})
    path = _sessions_dir(ws) / f"{os.getpid()}.json"
    if not claims:
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "claims": claims,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[shared_textures] could not write session file {path}: {e}", file=sys.stderr)


def _wait_for_engine(ws: Path) -> None:
    """Let an engine run that may have read the claims before ours were
    registered finish before the export goes on."""
    try:
        from .compose.shared_texture_migration import wait_for_engine
        wait_for_engine(ws)
    except Exception as e:  # noqa: BLE001 - never fail an export over this
        print(f"[shared_textures] could not wait for a running adoption: {e}", file=sys.stderr)


def active_claims(workspace: Path) -> list[str]:
    """Export roots claimed by open sessions on ``workspace``, in this
    process and in every other live process (normcased resolved paths).
    Session files of processes that are gone are removed."""
    ws = Path(workspace).resolve()
    ws_key = os.path.normcase(str(ws))
    with _sessions_lock:
        out = [c for s in _sessions.values() if s.ws_key == ws_key for c in s.claims]
    d = _sessions_dir(ws)
    try:
        files = list(d.glob("*.json"))
    except OSError:
        files = []
    me = os.getpid()
    for f in files:
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if pid == me:
            continue
        try:
            age = time.time() - f.stat().st_mtime
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if age > SESSION_STALE_S or not _pid_alive(pid):
            try:
                f.unlink()
            except OSError:
                pass
            continue
        for c in (doc or {}).get("claims") or []:
            if isinstance(c, str):
                out.append(os.path.normcase(os.path.normpath(c)))
    return out


def is_claimed(folder: Path | str, claims: Iterable[str]) -> bool:
    """Whether ``folder`` lies in (or is) one of ``claims``
    (as returned by :func:`active_claims`)."""
    k = os.path.normcase(os.path.normpath(str(folder)))
    return any(k == c or k.startswith(c.rstrip("\\/") + os.sep) for c in claims)


def _open_session(ws: Path, claims: Iterable[Path | str | None]) -> int:
    ws = Path(ws).resolve()
    s = _Session(ws, os.path.normcase(str(ws)))
    for c in claims:
        if c:
            r = str(Path(c).resolve())
            k = os.path.normcase(r)
            if k not in s.claims:
                s.claims.append(k)
                s.raw.append(r)
    with _sessions_lock:
        sid = next(_session_ids)
        _sessions[sid] = s
        if s.claims:
            _sync_session_file(s.ws, s.ws_key)
    if s.claims:
        _wait_for_engine(s.ws)
    return sid


def _close_session(sid: int) -> Path | None:
    with _sessions_lock:
        s = _sessions.pop(sid, None)
        if s is None:
            return None
        if s.claims:
            _sync_session_file(s.ws, s.ws_key)
    return s.ws


@contextlib.contextmanager
def export_session(
    workspace: Path | str | os.PathLike | None,
    claims: Iterable[Path | str | None] = (),
) -> Iterator[None]:
    """Claim export roots while an export computes and writes documents.

    ``claims`` are directories (an export root such as ``ships/<S>``,
    ``libraries/accessories`` or ``buildings``); :func:`claim` adds more
    from inside the session. While the session is open, adoption leaves
    the copies in claimed folders in place unless it was asked to adopt
    that very folder, and records them as pending. When the session
    closes, the pending copies whose roots nobody claims any more move to
    the store (:func:`flush_pending`). Sessions nest; a ``None``
    workspace makes this a no-op. Never raises on its own account.
    """
    if workspace is None:
        yield
        return
    try:
        sid = _open_session(Path(workspace), claims)
    except Exception as e:  # noqa: BLE001
        print(f"[shared_textures] export session not opened: {e}", file=sys.stderr)
        yield
        return
    stack: list[int] = getattr(_tls, "stack", None) or []
    _tls.stack = stack
    stack.append(sid)
    try:
        yield
    finally:
        try:
            stack.remove(sid)
        except ValueError:
            pass
        ws = _close_session(sid)
        if ws is not None:
            flush_pending(ws)


def claim(path: Path | str) -> bool:
    """Add ``path`` to the innermost export session open in this thread.
    Returns False (and claims nothing) when no session is open."""
    stack = getattr(_tls, "stack", None)
    if not stack:
        return False
    with _sessions_lock:
        s = _sessions.get(stack[-1])
        if s is None:
            return False
        r = str(Path(path).resolve())
        k = os.path.normcase(r)
        if k in s.claims:
            return True
        s.claims.append(k)
        s.raw.append(r)
        _sync_session_file(s.ws, s.ws_key)
    _wait_for_engine(s.ws)
    return True


def with_export_session(
    roots: Callable[..., tuple[Path | str | None, Iterable[Path | str | None]]],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: run the function inside :func:`export_session`.

    ``roots(*args, **kwargs)`` returns ``(workspace, claims)`` for a call;
    when it raises, the call runs without a session.
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                ws, claims = roots(*args, **kwargs)
            except Exception:  # noqa: BLE001
                ws, claims = None, ()
            with export_session(ws, claims):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def flush_pending(workspace: Path | str | os.PathLike) -> None:
    """Move the pending copies whose export roots no session claims any
    more (see :data:`PENDING_REL`). Never raises."""
    try:
        from .compose.shared_texture_migration import flush_pending as _flush
        _flush(Path(workspace))
    except Exception as e:  # noqa: BLE001
        print(f"[shared_textures] pending flush failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Producer hook: route shared sets out of freshly written raw-DDS folders
# ---------------------------------------------------------------------------


def adopt_dirs(
    dirs: Iterable[Path | str | os.PathLike | None],
    *,
    workspace: Path,
    reason: str = "export",
) -> dict[Path, dict[str, str]]:
    """Route the shared texture sets of freshly written raw-DDS folders
    through the store.

    For each set in ``dirs`` that another in-scope folder or the store
    also holds byte-for-byte (or that is an always-routed name), the store
    takes one copy, every folder holding the set gets a redirect record,
    the documents naming any of those copies are rewritten to the store
    path and the copies are removed (see
    :func:`wows_model_export.compose.shared_texture_migration.converge_dirs`).
    Exception: a copy in a folder whose export root an open
    :func:`export_session` claims stays where it is (unless that folder
    is in ``dirs``) and moves when the session ends. Folders outside
    :data:`SCOPE_ROOTS` are left alone. Idempotent.

    A caller that computed a document from ``dirs`` BEFORE this call must
    re-resolve it (or pass it through :func:`repair_refs`) before writing
    it: the call can move copies out of ``dirs`` that the document names.

    Never raises: a failure is reported on stderr and the folders keep
    their copies, which still resolve through the owning-directory rule.
    Returns ``{folder: {name: store path}}`` for the given folders' moved
    files.
    """
    ws = Path(workspace)
    todo: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        if not d:
            continue
        p = Path(d)
        key = os.path.normcase(os.path.abspath(str(p)))
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir() and in_scope(p, ws):
            todo.append(p)
    if not todo:
        return {}
    try:
        from .compose.shared_texture_migration import converge_dirs
        return converge_dirs(ws, todo, reason=reason)
    except Exception as e:  # noqa: BLE001 - never fail an export over this
        print(f"[shared_textures] adopt failed for {len(todo)} folder(s): {e}", file=sys.stderr)
        return {}


def adopt_dir(dds_dir: Path, *, workspace: Path) -> dict[str, str]:
    """:func:`adopt_dirs` for one folder; returns ``{name: store path}``."""
    out = adopt_dirs([dds_dir], workspace=workspace)
    for d, moved in out.items():
        return moved
    return {}


# ---------------------------------------------------------------------------
# Resolver / reader view of a raw-DDS directory
# ---------------------------------------------------------------------------


def find_workspace(start: Path) -> Path | None:
    """The workspace root above ``start``: the nearest ancestor that holds
    the store directory."""
    p = Path(start).resolve()
    for anc in (p, *p.parents):
        if anc.joinpath(*STORE_REL.split("/")).is_dir():
            return anc
    return None


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


def dir_files(dds_dir: Path, *, workspace: Path | None = None) -> dict[str, Path]:
    """``{file name: absolute path}`` for every file a raw-DDS folder holds,
    locally or through its redirect file (the store copy).

    For producer code that reads the textures of a folder (emissive
    synthesis, diffuse recovery): a name moved to the store still counts
    as present and its path points at the store file. A local file wins
    over a redirect of the same name; a redirect whose store file is
    missing is skipped. Excludes the redirect file itself.
    """
    d = Path(dds_dir)
    if not d.is_dir():
        return {}
    out: dict[str, Path] = {}
    local: set[str] = set()
    for f in d.iterdir():
        if f.is_file() and f.name != REDIRECT_FILENAME:
            out[f.name] = f
            local.add(f.name.lower())
    redirects = read_redirects(d)
    if redirects:
        ws = Path(workspace) if workspace is not None else find_workspace(d)
        if ws is not None:
            for name, ref in redirects.items():
                if name.lower() in local:
                    continue
                p = resolve_ref(ws, ref)
                if p.is_file():
                    out[name] = p
    return out


def dir_file(dds_dir: Path, name: str, *, workspace: Path | None = None) -> Path | None:
    """The file behind ``name`` in a raw-DDS folder (local or store), else None."""
    local = Path(dds_dir) / name
    if local.is_file():
        return local
    rec = read_redirects(dds_dir)
    ref = rec.get(name)
    if ref is None:
        low = name.lower()
        ref = next((r for n, r in rec.items() if n.lower() == low), None)
    if ref is None:
        return None
    ws = Path(workspace) if workspace is not None else find_workspace(Path(dds_dir))
    if ws is None:
        return None
    p = resolve_ref(ws, ref)
    return p if p.is_file() else None


def dir_names(dds_dir: Path) -> list[str]:
    """File names a raw-DDS folder holds, locally or through its redirect file."""
    return [name for name, _uri in dds_dir_entries(Path(dds_dir))]


def repair_refs(node: Any, base: Path) -> int:
    """Repoint, in place, the texture paths in ``node`` whose folder copy
    has moved to the store.

    ``node`` is a document or a part of one (dicts and lists, mutated in
    place): a ship / building sidecar with ``base`` = its ``models/``
    directory, or one library-index entry with ``base`` = the directory of
    its GLB. A string is repointed only when it names a texture-set member
    by a folder-relative path, that file is missing from its folder, and
    the folder's redirect file records it; everything else stays as it is.
    For writers that computed a document before an adoption that may have
    moved copies it names. Returns the number of paths repointed.
    """
    base = Path(base)
    folders: dict[str, tuple[set[str], dict[str, str]]] = {}

    def lookup(value: str) -> str | None:
        target = base.joinpath(*value.replace("\\", "/").split("/"))
        folder = target.parent
        k = os.path.normcase(str(folder))
        hit = folders.get(k)
        if hit is None:
            try:
                names = {n.lower() for n in os.listdir(folder)}
            except OSError:
                names = set()
            redirects: dict[str, str] = {}
            if REDIRECT_FILENAME.lower() in names:
                redirects = {n.lower(): r for n, r in read_redirects(folder).items()}
            hit = (names, redirects)
            folders[k] = hit
        names, redirects = hit
        low = target.name.lower()
        if low in names:
            return None
        return redirects.get(low)

    count = 0

    def fix(value: Any) -> Any:
        nonlocal count
        if isinstance(value, str) and is_set_member(value) and not is_workspace_path(value):
            ref = lookup(value)
            if ref is not None and ref != value:
                count += 1
                return ref
            return value
        walk(value)
        return value

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for k, v in list(n.items()):
                nv = fix(v)
                if nv is not v:
                    n[k] = nv
        elif isinstance(n, list):
            for i, v in enumerate(n):
                nv = fix(v)
                if nv is not v:
                    n[i] = nv

    walk(node)
    return count


__all__ = [
    "HASH_CACHE_REL",
    "HASH_PREFIX_LEN",
    "PENDING_REL",
    "REDIRECT_FILENAME",
    "SCOPE_ROOTS",
    "SESSIONS_REL",
    "SHARED_TEXTURE_NAMES",
    "STORE_REL",
    "WORKSPACE_PATH_PREFIX",
    "HashCache",
    "SharedTextureConflict",
    "SharedTextureIncomplete",
    "active_claims",
    "adopt_dir",
    "adopt_dirs",
    "cached_sha1",
    "canonical_name",
    "claim",
    "dds_dir_entries",
    "dds_is_complete",
    "dir_file",
    "dir_files",
    "dir_names",
    "drop_redirects",
    "export_session",
    "file_sha1",
    "find_workspace",
    "flush_pending",
    "in_scope",
    "is_claimed",
    "is_inside",
    "is_set_member",
    "is_shared_name",
    "is_workspace_path",
    "put",
    "read_redirect_records",
    "read_redirects",
    "redirect_record",
    "repair_refs",
    "resolve_ref",
    "set_key",
    "set_stem",
    "store_path",
    "store_ref",
    "store_root",
    "with_export_session",
    "write_redirects",
]
