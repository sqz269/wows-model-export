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

The redirect files and the hash cache (:data:`HASH_CACHE_REL`) are
pipeline-internal and never published.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping
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
    Folders outside :data:`SCOPE_ROOTS` are left alone. Idempotent.

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


__all__ = [
    "HASH_CACHE_REL",
    "HASH_PREFIX_LEN",
    "REDIRECT_FILENAME",
    "SCOPE_ROOTS",
    "SHARED_TEXTURE_NAMES",
    "STORE_REL",
    "WORKSPACE_PATH_PREFIX",
    "HashCache",
    "SharedTextureConflict",
    "SharedTextureIncomplete",
    "adopt_dir",
    "adopt_dirs",
    "cached_sha1",
    "canonical_name",
    "dds_dir_entries",
    "dds_is_complete",
    "dir_file",
    "dir_files",
    "dir_names",
    "drop_redirects",
    "file_sha1",
    "find_workspace",
    "in_scope",
    "is_inside",
    "is_set_member",
    "is_shared_name",
    "is_workspace_path",
    "put",
    "read_redirect_records",
    "read_redirects",
    "redirect_record",
    "resolve_ref",
    "set_key",
    "set_stem",
    "store_path",
    "store_ref",
    "store_root",
    "write_redirects",
]
