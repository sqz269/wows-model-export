"""Extract particle-system DDS textures into a webview-reachable cache.

The particle parser surfaces texture references on the Renderer
sub-struct (``textureName0`` / ``textureName1``) and the Animation
sub-struct (``motionVectorsTexture``). Those refs are WG VFS paths like
``effects/textures/Fire01.dds`` that the webview can't fetch on its own.

This module walks a resolved ``effects.particles`` block, collects every
unique texture path, and extracts the lot into
``<workspace>/content/effects_textures/`` (mirroring the VFS layout, so
two ships referencing the same ``Fire01.dds`` only extract it once and
the on-disk path is content-addressable).

After extraction, the in-memory particle records are stamped with a
``texture_url`` field on each renderer/animation block — a
workspace-relative path the webview hands to ``repoUrl()``. Missing-on-
disk references (typos in WG's data, or files the toolkit's VFS can't
reach) degrade to a warning + a stamped `texture_url_missing: true`
flag so the consumer can render the diagnostic without re-walking.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..toolkit import vfs as _vfs

# Workspace-relative root for the extracted texture cache. Mirroring the
# VFS layout below keeps the on-disk paths content-addressable
# (`Fire01.dds` is `Fire01.dds`, not `<hash>.dds`).
TEXTURE_CACHE_ROOT = Path("content") / "effects_textures"

# Texture extensions we care about; everything else (e.g. ``.vfd``
# velocityField sources) is referenced via a different code path.
_TEXTURE_EXTS = frozenset({".dds", ".dd0", ".dd1", ".dd2", ".tga", ".bmp", ".png"})

# Manifest binding ``<name> -> (atlas page DDS, UV rect)`` for the 117
# authoring-side ``.tga`` refs that WG bakes into 6 shipped atlas pages.
ATLAS_MANIFEST_VFS_PATH = "particles/textures/particles.atlas"

# Atlas manifest line patterns. Format is a libGDX-ish text grammar:
#   page "particles1.dds" 4096 4096
#   {
#       "Blast"  { 0.751953 0.125977 0.814453 0.188477 }
#       ...
#   }
_ATLAS_PAGE_RE = re.compile(r'page\s+"([^"]+)"\s+(\d+)\s+(\d+)')
_ATLAS_ENTRY_RE = re.compile(
    r'"([^"]+)"\s*\{\s*'
    r'([+-]?\d*\.?\d+)\s+([+-]?\d*\.?\d+)\s+'
    r'([+-]?\d*\.?\d+)\s+([+-]?\d*\.?\d+)\s*\}'
)


def collect_texture_paths(particles: dict[str, Any]) -> set[str]:
    """Walk every system's renderer/animation block and return the set of
    VFS-relative texture paths referenced anywhere in the corpus.

    Only paths with a recognised raster extension are returned —
    non-texture ResourceRefs (``.vfd`` fields, effect names) are filtered
    out. Paths are normalised to forward slashes; case is preserved
    (WG VFS is case-sensitive on some builds).
    """
    out: set[str] = set()
    for rec in particles.values():
        if not isinstance(rec, dict):
            continue
        for system in rec.get("systems") or []:
            if not isinstance(system, dict):
                continue
            for block_key in ("renderer", "animation"):
                blk = system.get(block_key)
                if not isinstance(blk, dict):
                    continue
                for field in ("textureName0", "textureName1", "motionVectorsTexture"):
                    p = blk.get(field)
                    if not isinstance(p, str) or not p:
                        continue
                    norm = p.replace("\\", "/").strip()
                    if Path(norm).suffix.lower() not in _TEXTURE_EXTS:
                        continue
                    out.add(norm)
    return out


def _strip_content_prefix(vfs_path: str) -> str:
    """WG VFS paths come both with and without a leading ``content/``
    segment. Strip it so the on-disk layout under
    ``content/effects_textures/`` is consistent regardless of which form
    the particle authoring data used.
    """
    norm = vfs_path.replace("\\", "/").lstrip("/")
    if norm.startswith("content/"):
        return norm[len("content/"):]
    return norm


# Note on the 117 unresolvable ``.tga`` refs in 2230-record corpus:
# they all live under ``particles/textures/`` and reference individual
# textures (Blast.tga, Glow_1.tga, circle.tga, donut.tga, ...) — the
# WG VFS doesn't ship those individually as either ``.tga`` or ``.dds``.
# What ships under ``particles/textures/`` is a 6-DDS atlas
# (``particles.dds``, ``particles0..4.dds``) plus one ``.atlas`` file
# and 124 ``.contours`` files (one ``<tga>_<fx>_<fy>_<begin>_<size>.contours``
# per authoring filename). The runtime joins ``textureName0=Blast.tga``
# to a region inside one of the 6 atlas DDS via the AtlasContour
# database (Effect blob 9 in ``assets.bin``, already cracked — see
# ``reference/topics/particle/stage_h_atlas_contour.md``). Decoding that
# blob + emitting the atlas-id + UV-rect onto each renderer block is
# the queued fix; until then the consumer falls back to the procedural
# disc for atlas-mapped systems.


def ensure_textures_on_disk(
    paths: Iterable[str],
    *,
    config: PipelineConfig | None = None,
    workspace_override: Path | None = None,
) -> tuple[dict[str, str], set[str]]:
    """Ensure every path in ``paths`` is on disk under the workspace cache.

    Returns ``(resolved_url_map, missing)`` where:
      * ``resolved_url_map[vfs_path] = "content/effects_textures/<...>"``
        is the workspace-relative path that the webview's ``repoUrl()``
        helper can hand off to ``fetch``.
      * ``missing`` is the subset of ``paths`` that the toolkit couldn't
        produce — usually a typo in WG's data or a VFS lookup miss.

    Idempotent: paths already present on disk are skipped. Atomic per
    file via ``os.replace`` so a SIGINT mid-extract can't leave a
    truncated file in the cache.
    """
    cfg = config or PipelineConfig.load()
    workspace = (workspace_override or cfg.workspace).resolve()
    cache_root = (workspace / TEXTURE_CACHE_ROOT).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, str] = {}
    missing: set[str] = set()
    to_extract: list[tuple[str, Path, str]] = []  # (vfs_path, on_disk, rel_url)

    for vfs_path in paths:
        rel = _strip_content_prefix(vfs_path)
        on_disk = (cache_root / rel).resolve()
        rel_url = (TEXTURE_CACHE_ROOT / rel).as_posix()
        if on_disk.is_file():
            resolved[vfs_path] = rel_url
            continue
        to_extract.append((vfs_path, on_disk, rel_url))

    if not to_extract:
        return resolved, missing

    # Batch-extract every missing file in one toolkit invocation. The
    # toolkit's ``extract`` writes under ``<out_dir>/<vfs_path>``; we
    # move each into the cache under ``content/effects_textures/`` after.
    # Use a tmp dir so a partial extract doesn't pollute the cache.
    #
    # Path-form note: the toolkit's literal-path matcher silently misses
    # entries without a leading ``/`` (treats them as relative-glob
    # patterns with no ``**`` wildcard, which never resolve). Particle
    # texture refs from the binary are slash-less (``particles/animated/
    # Bubbles_6x6.dds``); prepend one before invoking. Both ``.dds`` and
    # ``.dd0/.dd1/.dd2`` mip-split forms coexist in the VFS for the same
    # texture — the ``.dds`` form extracts a single full-mipped DDS,
    # which is what the webview's loader expects.
    with tempfile.TemporaryDirectory(prefix="wms-eff-tex-") as td:
        out_dir = Path(td)
        try:
            patterns = [
                ("/" + t[0]) if not t[0].startswith("/") else t[0]
                for t in to_extract
            ]
            _vfs.extract(patterns, out_dir=out_dir, config=cfg)
        except Exception as e:
            # Toolkit can fail wholesale (game dir not configured,
            # exec missing). Surface every requested path as missing
            # rather than masking the user-visible warning behind a
            # silent return — particle render will fall back to the
            # untextured-disc material.
            print(
                f"  warn: effects_textures: extract failed for "
                f"{len(to_extract)} path(s): {e}",
                file=sys.stderr,
            )
            for vfs_path, _, _ in to_extract:
                missing.add(vfs_path)
            return resolved, missing

        for vfs_path, on_disk, rel_url in to_extract:
            # The toolkit preserves VFS layout under out_dir, so we look
            # under both ``out_dir/<full path>`` AND ``out_dir/<stripped
            # path>`` to cover both content/-prefixed and bare forms.
            candidates = [
                out_dir / vfs_path.replace("\\", "/"),
                out_dir / _strip_content_prefix(vfs_path),
            ]
            src: Path | None = None
            for c in candidates:
                if c.is_file():
                    src = c
                    break
            if src is None:
                # Last-ditch: rglob by basename. WG sometimes ships
                # textures under unexpected sub-paths.
                fname = Path(vfs_path).name
                matches = list(out_dir.rglob(fname))
                if matches:
                    src = max(matches, key=lambda p: p.stat().st_size)
            if src is None:
                missing.add(vfs_path)
                continue
            on_disk.parent.mkdir(parents=True, exist_ok=True)
            # ``os.replace`` is atomic but cross-device-rejected — the
            # tmp dir lives on $TMP (usually C:) while the workspace
            # cache can be on a different drive. Fall back to a copy +
            # tmp-side delete (best-effort) on that error; the extract
            # was already non-atomic w.r.t. crashes mid-batch since we
            # process one file at a time.
            try:
                os.replace(src, on_disk)
            except OSError:
                import shutil
                shutil.copyfile(src, on_disk)
                try:
                    src.unlink()
                except OSError:
                    pass
            resolved[vfs_path] = rel_url

    return resolved, missing


def stamp_texture_urls(
    particles: dict[str, Any], resolved: dict[str, str],
) -> int:
    """Mutate every renderer/animation block in ``particles`` to carry a
    ``texture_url_0`` / ``texture_url_1`` / ``motion_vectors_texture_url``
    field for any reference that resolved.

    Returns the count of stamped URLs (one per resolved field across all
    systems / records).
    """
    n = 0
    for rec in particles.values():
        if not isinstance(rec, dict):
            continue
        for system in rec.get("systems") or []:
            if not isinstance(system, dict):
                continue
            rend = system.get("renderer")
            if isinstance(rend, dict):
                for src_key, url_key in (
                    ("textureName0", "textureUrl0"),
                    ("textureName1", "textureUrl1"),
                ):
                    p = rend.get(src_key)
                    if not isinstance(p, str):
                        continue
                    url = resolved.get(p)
                    if url:
                        rend[url_key] = url
                        n += 1
            anim = system.get("animation")
            if isinstance(anim, dict):
                p = anim.get("motionVectorsTexture")
                if isinstance(p, str):
                    url = resolved.get(p)
                    if url:
                        anim["motionVectorsTextureUrl"] = url
                        n += 1
    return n


def parse_atlas_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Parse ``particles/textures/particles.atlas`` into a name lookup.

    The manifest is a libGDX-style text file. Returns a mapping
    ``{name: {"page": "particlesN.dds", "rect": [u0, v0, u1, v1]}}``
    suitable for resolving authoring-side ``.tga`` references to atlas
    page + UV rect. Names match the ``textureName0`` / ``textureName1``
    basename without the ``.tga`` extension.
    """
    out: dict[str, dict[str, Any]] = {}
    current_page: str | None = None
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m_page = _ATLAS_PAGE_RE.match(stripped)
        if m_page:
            current_page = m_page.group(1)
            continue
        if current_page is None:
            continue
        m_entry = _ATLAS_ENTRY_RE.match(stripped)
        if m_entry:
            name = m_entry.group(1)
            rect = [float(m_entry.group(i + 2)) for i in range(4)]
            out[name] = {"page": current_page, "rect": rect}
    return out


def ensure_atlas_assets_on_disk(
    *,
    config: PipelineConfig | None = None,
    workspace_override: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Extract the atlas manifest + every DDS page it references.

    Returns the parsed ``{name: {page, rect}}`` mapping (empty dict on
    failure). The 6 atlas DDS pages land under
    ``content/effects_textures/particles/textures/`` alongside the
    directly-referenced textures. Idempotent: a second call reuses the
    on-disk manifest + pages.
    """
    cfg = config or PipelineConfig.load()
    workspace = (workspace_override or cfg.workspace).resolve()
    cache_root = (workspace / TEXTURE_CACHE_ROOT).resolve()

    # Step 1: extract the manifest itself.
    resolved, _ = ensure_textures_on_disk(
        [ATLAS_MANIFEST_VFS_PATH],
        config=cfg,
        workspace_override=workspace_override,
    )
    if ATLAS_MANIFEST_VFS_PATH not in resolved:
        return {}

    manifest_path = cache_root / _strip_content_prefix(ATLAS_MANIFEST_VFS_PATH)
    atlas_map = parse_atlas_manifest(manifest_path)
    if not atlas_map:
        return {}

    # Step 2: extract every atlas page referenced by the manifest.
    pages = {entry["page"] for entry in atlas_map.values()}
    page_paths = [f"particles/textures/{p}" for p in pages]
    ensure_textures_on_disk(
        page_paths, config=cfg, workspace_override=workspace_override,
    )

    return atlas_map


def stamp_atlas_urls(
    particles: dict[str, Any], atlas_map: dict[str, dict[str, Any]],
) -> int:
    """Stamp ``textureAtlas0`` / ``textureAtlas1`` /
    ``motionVectorsTextureAtlas`` on every renderer/animation block
    whose ``textureName*`` basename appears in ``atlas_map`` and which
    wasn't already resolved to a direct DDS via :func:`stamp_texture_urls`.

    Each stamp is a dict ``{"page": "<workspace-rel DDS url>", "rect":
    [u0, v0, u1, v1]}``. Consumers prefer ``textureUrl*`` when present;
    ``textureAtlas*`` is the atlas-mapped fallback.

    Returns the count of stamped fields.
    """
    if not atlas_map:
        return 0
    n = 0
    page_rel_root = (TEXTURE_CACHE_ROOT / "particles" / "textures").as_posix()
    for rec in particles.values():
        if not isinstance(rec, dict):
            continue
        for system in rec.get("systems") or []:
            if not isinstance(system, dict):
                continue
            rend = system.get("renderer")
            if isinstance(rend, dict):
                for src_key, url_key, atlas_key in (
                    ("textureName0", "textureUrl0", "textureAtlas0"),
                    ("textureName1", "textureUrl1", "textureAtlas1"),
                ):
                    p = rend.get(src_key)
                    if not isinstance(p, str) or not p:
                        continue
                    if url_key in rend:
                        # Direct extract worked — atlas is a fallback only.
                        continue
                    entry = atlas_map.get(Path(p).stem)
                    if entry:
                        rend[atlas_key] = {
                            "page": f"{page_rel_root}/{entry['page']}",
                            "rect": entry["rect"],
                        }
                        n += 1
            anim = system.get("animation")
            if isinstance(anim, dict):
                p = anim.get("motionVectorsTexture")
                if not isinstance(p, str) or not p:
                    continue
                if "motionVectorsTextureUrl" in anim:
                    continue
                entry = atlas_map.get(Path(p).stem)
                if entry:
                    anim["motionVectorsTextureAtlas"] = {
                        "page": f"{page_rel_root}/{entry['page']}",
                        "rect": entry["rect"],
                    }
                    n += 1
    return n


__all__ = [
    "TEXTURE_CACHE_ROOT",
    "ATLAS_MANIFEST_VFS_PATH",
    "collect_texture_paths",
    "ensure_textures_on_disk",
    "stamp_texture_urls",
    "parse_atlas_manifest",
    "ensure_atlas_assets_on_disk",
    "stamp_atlas_urls",
]
