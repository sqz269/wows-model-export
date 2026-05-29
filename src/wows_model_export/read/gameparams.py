"""GameParams JSON cache reader.

Lifted from ``tools/shared/gameparams.py`` — the read side of the
fleet-wide GameParams JSON cache.

The toolkit's ``wowsunpack game-params --full`` writes a wrapper dict
``{"": {entity_id: {...}, ...}}`` — the empty string is the realm tag
(only one realm present in our installs). Everything in this module
operates on the *unwrapped* dict (``load_full`` returns the inner map).

Two access patterns:

1. **Flat load** (:func:`load_full`) — returns the entire unwrapped
   dict. Cached per-process, so multiple absorb passes share one parse.
   Costs ~3-4 GB Python memory; acceptable for the typical
   ``ingest_ship`` flow that calls into GameParams from several layers
   of the same process.

2. **Streaming key scan** (:func:`iter_top_level_keys` /
   :func:`resolve_ship_id`) — walks just the JSON keys via ``ijson``
   without loading the value tree. Used when we only need the entity
   ID (e.g. resolving ``PASB018`` -> ``PASB018_Iowa_1944``) and don't
   want to pay the flat-load cost.

The module also exposes per-entity slicers (:func:`get_entity`,
:func:`get_ship`, :func:`get_exterior`, :func:`get_projectile`).

Layer 1 read module: pure data access, no subprocess. The single
side-effecting path is :func:`load_full`, which delegates to
:func:`wows_model_export.toolkit.gameparams.ensure_dump` if the cache
file is missing — that's the one bridge into Layer 2 the read side
needs in order to bootstrap on a fresh install.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..toolkit.gameparams import ensure_dump

if TYPE_CHECKING:
    from ..config import PipelineConfig

# In-process flat-load cache. Keyed by the cache file's (mtime, size) so
# a refresh (which rewrites the file) invalidates automatically without
# callers having to think about cache busting. Size is included because
# Windows NTFS rounds mtimes coarsely and a same-second re-dump of a
# different-sized cache would otherwise be served stale.
#
# This global is intentional and load-bearing: ``load_full`` is called
# repeatedly across autofill passes, variant resolution, and ship
# scaffolding within a single process. Re-reading the 2.8 GB JSON each
# call would be prohibitive. The (mtime, size) key means concurrent
# refreshes from another process invalidate the cached parse
# automatically; ``unload_full`` lets long-running batch jobs free the
# ~3-4 GB residency after GameParams work is done.
_CACHE_KEY_T = tuple[float, int]
_FULL_DATA: tuple[_CACHE_KEY_T, dict[str, Any]] | None = None

# Serializes the cold 2.8 GB ``json.load`` so two concurrent first-loads
# (the jobs ThreadPoolExecutor is max_workers=4, and FastAPI
# ``/api/gameparams/*`` handlers call ``load_full`` on the anyio thread
# pool) can't each build a ~12 GB resident set in parallel (OOM on
# low-RAM boxes) or silently overwrite ``_FULL_DATA`` mid-parse. The warm
# fast-path stays lock-free via a snapshot read; only the cold parse and
# ``unload_full`` take the lock.
_load_lock = threading.Lock()


def _cache_key(p: Path) -> _CACHE_KEY_T:
    st = p.stat()
    return (st.st_mtime, st.st_size)


def load_full(
    *,
    refresh: bool = False,
    path: Path | None = None,
    config: "PipelineConfig | None" = None,
) -> dict[str, Any]:
    """Return the unwrapped GameParams dict, caching the parse per-process.

    The on-disk file is ``{"<realm>": {entity_id: {...}, ...}}``; this
    helper unwraps the realm and returns the inner map. Subsequent calls
    in the same process reuse the parsed dict unless ``refresh=True``.

    When ``path`` is ``None`` and the default cache file is missing,
    :func:`ensure_dump` is invoked to build it before reading. Passing
    ``config`` threads a pre-resolved :class:`PipelineConfig` into
    ``ensure_dump`` so the warm-cache hit path doesn't re-run
    ``PipelineConfig.load()`` (a user-settings file read) on every call —
    matters for hot loops that touch many entities.
    """
    global _FULL_DATA
    if path is None:
        cache_path = ensure_dump(refresh=refresh, config=config)
    else:
        cache_path = Path(path)
        if not cache_path.is_file():
            raise FileNotFoundError(f"gameparams: {cache_path} not found")
    key = _cache_key(cache_path)
    # Warm fast-path: snapshot the global into a local first so a
    # concurrent ``unload_full`` (which sets ``_FULL_DATA = None``) can't
    # turn the check-then-index into a torn read.
    snap = _FULL_DATA
    if not refresh and snap is not None and snap[0] == key:
        return snap[1]
    # Cold path: serialize the parse (double-checked locking).
    with _load_lock:
        snap = _FULL_DATA
        if not refresh and snap is not None and snap[0] == key:
            return snap[1]
        with open(cache_path, encoding="utf-8") as f:
            wrapped = json.load(f)
        if isinstance(wrapped, dict) and "" in wrapped:
            flat = wrapped[""] or {}
        else:
            flat = wrapped or {}
        _FULL_DATA = (key, flat)
        return flat


def unload_full() -> None:
    """Release the in-process GameParams cache (~3-4 GB).

    Long-running batch jobs (``build_accessory_library --all``,
    ``find_ship_variants --refresh``) keep this resident even after the
    GameParams data is no longer needed. Call this once GameParams work
    is complete to free memory before downstream steps.
    """
    global _FULL_DATA
    with _load_lock:
        _FULL_DATA = None


def _resolve_from_loaded(prefix: str) -> str | None:
    """Resolve a param_index ``prefix`` against the warm in-process dict.

    Returns the first TOP-LEVEL entity key matching ``prefix`` (the same
    predicate :func:`iter_top_level_keys` uses), or ``None`` when the
    flat dict isn't resident / doesn't match the default cache file's
    ``(mtime, size)`` key — in which case the caller falls back to the
    ``ijson`` stream.

    This is both ~4 orders of magnitude faster than re-streaming the
    2.8 GB file (~0.2 ms vs ~5-9 s) AND more correct for prefix-collision
    families: :func:`iter_top_level_keys` yields ``map_key`` events at
    *every* nesting depth, so the streaming path can return a nested
    sub-key (e.g. ``PRSC990_RC``) that happens to appear first in the
    byte stream, whereas this top-level scan returns the real entity
    (``PRSC990_Petrozavodsk``).
    """
    snap = _FULL_DATA
    if snap is None:
        return None
    # Validate the resident dict is the current default cache. ensure_dump
    # is a cheap is_file() check here (the file must exist — _FULL_DATA was
    # populated from it) and never triggers a dump on a warm cache.
    try:
        cache_path = ensure_dump()
    except Exception:
        return None
    if not cache_path.is_file() or snap[0] != _cache_key(cache_path):
        return None
    needle = prefix + "_"
    for key in snap[1]:
        if key == prefix or key.startswith(needle):
            return key
    return None


# ---------------------------------------------------------------------------
# Streaming helpers (no flat-load cost)
# ---------------------------------------------------------------------------


def iter_top_level_keys(
    predicate: Callable[[str], bool] | None = None,
    *,
    path: Path | None = None,
) -> Iterable[str]:
    """Yield top-level entity IDs, optionally filtered by ``predicate``.

    Uses ``ijson`` to walk the file without loading values. The wrapper
    realm key is skipped automatically. When ``path`` is ``None`` the
    default cache path is read via :func:`ensure_dump`; if the cache
    file does not exist no entries are yielded.
    """
    import ijson  # local import — only callers that need it pay the cost

    if path is None:
        try:
            cache_path = ensure_dump()
        except Exception:
            return
    else:
        cache_path = Path(path)
    if not cache_path.is_file():
        return
    with open(cache_path, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if event != "map_key":
                continue
            # Skip the realm wrapper key (depth 0). Top-level entities live
            # at depth 1 — prefix == "" for the realm key, prefix == "<realm>"
            # for entity keys (typically prefix == "" since the realm tag is
            # the empty string in our installs).
            if prefix == "":
                continue
            if not isinstance(value, str):
                continue
            if predicate is None or predicate(value):
                yield value


def resolve_ship_id(prefix_or_full: str, *, path: Path | None = None) -> str | None:
    """Resolve a Vehicle param_index prefix (``"PASB018"``) to the full
    GameParams entity key (``"PASB018_Iowa_1944"``).

    Returns the input unchanged if it already contains an underscore (the
    full-ID convention). Returns ``None`` if no entity matches the prefix.

    Streams the JSON via :func:`iter_top_level_keys`, so the cost is
    proportional to the file size up to the first match — typically ~1 s.
    """
    if not prefix_or_full:
        return None
    if "_" in prefix_or_full:
        return prefix_or_full
    # Warm fast-path (default cache only): resolve against the resident
    # parsed dict instead of re-streaming the 2.8 GB file via ijson.
    # Orders of magnitude faster on a warm cache, and more correct for
    # prefix-collision families (see ``_resolve_from_loaded``). A match
    # wins; a miss falls through to the ijson stream so behaviour is
    # unchanged when the dict isn't resident or has no top-level match.
    if path is None:
        hit = _resolve_from_loaded(prefix_or_full)
        if hit is not None:
            return hit
    needle = prefix_or_full + "_"
    for key in iter_top_level_keys(
        lambda k: k == prefix_or_full or k.startswith(needle),
        path=path,
    ):
        return key
    return None


# ---------------------------------------------------------------------------
# Block-level entity slicing
# ---------------------------------------------------------------------------


def get_entity(entity_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Return the entire dict for a single GameParams entity, or ``None``.

    Uses the flat-load cache. Pass ``entity_id`` as either the full key
    (``"PASB018_Iowa_1944"``) or a Vehicle param_index prefix
    (``"PASB018"``) — the prefix form is auto-resolved.
    """
    if not entity_id:
        return None
    flat = load_full(refresh=refresh)
    if entity_id in flat:
        return flat[entity_id]
    if "_" not in entity_id:
        # Prefix-form lookup. Linear over keys; cheap on a parsed dict.
        needle = entity_id + "_"
        for key in flat:
            if key == entity_id or key.startswith(needle):
                return flat[key]
    return None


def get_ship(ship_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Return a Vehicle GameParams entry, or ``None`` if not found / not a ship."""
    entity = get_entity(ship_id, refresh=refresh)
    if entity is None:
        return None
    typeinfo = entity.get("typeinfo") or {}
    if typeinfo.get("type") != "Ship":
        return None
    return entity


def get_exterior(exterior_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Return an Exterior GameParams entry (PCEM/PAES/etc.), or ``None``."""
    entity = get_entity(exterior_id, refresh=refresh)
    if entity is None:
        return None
    typeinfo = entity.get("typeinfo") or {}
    if typeinfo.get("type") != "Exterior":
        return None
    return entity


def get_projectile(ammo_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Return a Projectile GameParams entry (PAPA/PAPT/...), or ``None``."""
    entity = get_entity(ammo_id, refresh=refresh)
    if entity is None:
        return None
    typeinfo = entity.get("typeinfo") or {}
    if typeinfo.get("type") != "Projectile":
        return None
    return entity


__all__ = [
    "load_full",
    "unload_full",
    "iter_top_level_keys",
    "resolve_ship_id",
    "get_entity",
    "get_ship",
    "get_exterior",
    "get_projectile",
]
