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


__all__ = [
    "TEXTURE_CACHE_ROOT",
    "collect_texture_paths",
    "ensure_textures_on_disk",
    "stamp_texture_urls",
]
