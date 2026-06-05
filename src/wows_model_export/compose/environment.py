"""Build the shared environment library — PMREM cubes + BRDF LUT + per-weather
HDR/SH params, one global artefact the IBL/tonemap consumers join against.

This is the producer half of the engine render-parity items documented in
``reference/engine/wg_render_hdr_tonemap.md`` and ``wg_render_pmrem_ibl.md``.
Per space + weather, WG authors:

* a prefiltered PMREM reflection cube — a single-file 6-face ``.dds`` with a
  full mip chain (mip = roughness), at the ``cubemapsPath`` declared in
  ``space.ubersettings`` (conventionally ``main_probe.dds``; a few old docks
  use a numbered ``<n>/PMREM.dds``). Two on-disk formats coexist — DX10
  ``BC6H_UF16`` and legacy D3D9 ``A16B16G16R16F`` (fourcc ``0x71``) — so the
  manifest records the sniffed format for the consumer's decoder.
* the GT (Uchimura) tonemap curve + bloom + eye-adaptation (the HDR block).
* the diffuse-irradiance spherical harmonics (9 RGB L2 coeffs, on disk — no
  offline cube projection needed).

Plus one global ``system/maps/env_brdf_lut.dds`` (the split-sum BRDF LUT,
RGBA16F 256x256), shared across all spaces.

The module mirrors the ``effects_textures`` + ``library_particles`` pattern:
raw ``.dds`` bytes are extracted verbatim into a ``content/environment/`` cache
(``wowsunpack extract`` does no decoding — consumers convert), and a single
``library/environment/manifest.json`` keys everything by space + weather. The
webview fetches both straight from the workspace via ``/repo``; the
``environment`` publish domain mirrors them into the consumer tree for
Unity / Blender (see ``compose.publish``).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read import ubersettings as _ubersettings
from ..resolve.sidecar._helpers import _now_iso
from ..toolkit import vfs as _vfs

# Workspace-relative cache root for extracted env DDS (cubes + BRDF LUT),
# mirroring the VFS layout so the same file is content-addressable + extracted
# once. Mirrors ``effects_textures.TEXTURE_CACHE_ROOT``.
CACHE_ROOT = Path("content") / "environment"

# Global manifest artefact (per-space / per-weather IBL + tonemap params).
LIBRARY_ROOT = Path("library") / "environment"
MANIFEST_FILE = LIBRARY_ROOT / "manifest.json"
SCHEMA_VERSION = 1

# The one shared split-sum BRDF LUT (packed only — not in res_unpack).
ENV_BRDF_LUT_VFS = "system/maps/env_brdf_lut.dds"

# Conventional reflection-cube filename inside ``cubemapsPath``.
_CUBE_FILENAME = "main_probe.dds"
# Older docks store the probe under a numbered subdir as ``PMREM.dds``.
_CUBE_FALLBACK_GLOB = "**/*.dds"


# ---------------------------------------------------------------------------
# DDS header sniff (format / dims / cube flag) — so the manifest tells the
# consumer which decoder path to take without re-reading the file.
# ---------------------------------------------------------------------------

_DXGI_NAMES = {
    10: "rgba16f",      # R16G16B16A16_FLOAT
    26: "r11g11b10f",
    67: "r9g9b9e5",
    95: "bc6h_uf16",
    96: "bc6h_sf16",
    98: "bc7",
}


def sniff_dds(path: Path) -> dict[str, Any] | None:
    """Return ``{format, width, height, mips, is_cube}`` for a DDS file.

    Handles both DX10-extended headers (BC6H/BC7/R11G11B10F via the dxgiFormat
    code) and legacy D3D9 fourccs — notably ``0x71`` = ``D3DFMT_A16B16G16R16F``
    (the legacy RGBA16F path that a naive DX10-only reader misses). Returns
    ``None`` if the file isn't a readable DDS.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(148)
    except OSError:
        return None
    if len(head) < 128 or head[:4] != b"DDS ":
        return None
    height = struct.unpack_from("<I", head, 12)[0]
    width = struct.unpack_from("<I", head, 16)[0]
    mips = struct.unpack_from("<I", head, 28)[0]
    fourcc = head[84:88]
    fourcc_u32 = int.from_bytes(fourcc, "little")
    caps2 = struct.unpack_from("<I", head, 112)[0]
    is_cube = bool(caps2 & 0x200)  # DDSCAPS2_CUBEMAP

    fmt: str
    if fourcc == b"DX10" and len(head) >= 144:
        dxgi = struct.unpack_from("<I", head, 128)[0]
        misc = struct.unpack_from("<I", head, 136)[0]  # D3D11 miscFlag
        is_cube = is_cube or bool(misc & 0x4)  # D3D11_RESOURCE_MISC_TEXTURECUBE
        fmt = _DXGI_NAMES.get(dxgi, f"dxgi:{dxgi}")
    elif fourcc_u32 == 0x71:
        fmt = "rgba16f"  # legacy D3DFMT_A16B16G16R16F
    elif fourcc_u32 == 0:
        fmt = "uncompressed"
    else:
        fmt = fourcc.decode("latin1").strip("\x00") or "unknown"

    return {
        "format": fmt,
        "width": width,
        "height": height,
        "mips": mips,
        "is_cube": is_cube,
    }


# ---------------------------------------------------------------------------
# Extraction (raw DDS -> workspace cache) — mirrors effects_textures
# ---------------------------------------------------------------------------


def _strip_content_prefix(vfs_path: str) -> str:
    """Drop a leading ``content/`` so the cache layout is uniform across the
    ``content/...`` cubes and the ``system/...`` BRDF LUT."""
    norm = vfs_path.replace("\\", "/").lstrip("/")
    if norm.startswith("content/"):
        return norm[len("content/"):]
    return norm


def _cache_url(vfs_path: str) -> str:
    """Workspace-relative URL a consumer fetches (``content/environment/...``)."""
    return (CACHE_ROOT / _strip_content_prefix(vfs_path)).as_posix()


def _extract_to_cache(
    vfs_paths: list[str],
    *,
    config: PipelineConfig,
    workspace: Path,
    extract: bool = True,
) -> dict[str, str]:
    """Extract each VFS path into the env cache; return ``{vfs_path: url}``.

    Idempotent (already-cached paths skipped) and atomic per file. The
    leading-``/`` prepend works around the toolkit glob matcher (VFS keys
    carry a leading slash; a slash-less literal never matches) — same fix as
    ``effects_textures``.

    ``extract=False`` resolves only the paths already in the cache (no toolkit
    call) — a params-only manifest refresh still links previously-extracted
    cubes instead of dropping them.
    """
    cache_root = (workspace / CACHE_ROOT).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, str] = {}
    to_extract: list[tuple[str, Path, str]] = []
    for vfs_path in vfs_paths:
        rel = _strip_content_prefix(vfs_path)
        on_disk = (cache_root / rel).resolve()
        url = (CACHE_ROOT / rel).as_posix()
        if on_disk.is_file():
            resolved[vfs_path] = url
        else:
            to_extract.append((vfs_path, on_disk, url))

    if not extract or not to_extract:
        return resolved

    with tempfile.TemporaryDirectory(prefix="wms-env-") as td:
        out_dir = Path(td)
        patterns = [
            ("/" + v) if not v.startswith("/") else v for v, _, _ in to_extract
        ]
        try:
            _vfs.extract(patterns, out_dir=out_dir, config=config)
        except Exception as e:  # toolkit unconfigured / exec missing
            print(
                f"  warn: environment: extract failed for "
                f"{len(to_extract)} path(s): {e}",
                file=sys.stderr,
            )
            return resolved

        for vfs_path, on_disk, url in to_extract:
            candidates = [
                out_dir / vfs_path.replace("\\", "/").lstrip("/"),
                out_dir / vfs_path.replace("\\", "/"),
                out_dir / _strip_content_prefix(vfs_path),
            ]
            src: Path | None = next((c for c in candidates if c.is_file()), None)
            if src is None:
                fname = Path(vfs_path).name
                matches = list(out_dir.rglob(fname))
                if matches:
                    src = max(matches, key=lambda p: p.stat().st_size)
            if src is None:
                continue
            on_disk.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, on_disk)
            except OSError:
                import shutil

                shutil.copyfile(src, on_disk)
            resolved[vfs_path] = url

    return resolved


def _resolve_cube_vfs(cubemaps_path: str) -> str:
    """The conventional cube VFS path for a ``cubemapsPath`` directory."""
    base = cubemaps_path.replace("\\", "/")
    if not base.endswith("/"):
        base += "/"
    return base + _CUBE_FILENAME


def _extract_cube_fallback(
    cubemaps_path: str,
    *,
    config: PipelineConfig,
    workspace: Path,
) -> tuple[str, str] | None:
    """Glob a ``cubemapsPath`` dir for a probe cube when ``main_probe.dds`` is
    absent (the old-dock ``<n>/PMREM.dds`` case). Returns ``(vfs_path, url)``
    or ``None``."""
    base = cubemaps_path.replace("\\", "/").rstrip("/")
    cache_root = (workspace / CACHE_ROOT).resolve()
    with tempfile.TemporaryDirectory(prefix="wms-env-cube-") as td:
        out_dir = Path(td)
        try:
            _vfs.extract(
                [f"/{base}/{_CUBE_FALLBACK_GLOB}"], out_dir=out_dir, config=config
            )
        except Exception:
            return None
        found = list(out_dir.rglob("*.dds"))
        if not found:
            return None
        # Prefer PMREM.dds / main_probe.dds, else the largest (top-res cube).
        def _rank(p: Path) -> tuple[int, int]:
            name = p.name.lower()
            pref = 2 if name == "pmrem.dds" else 1 if name == _CUBE_FILENAME else 0
            return (pref, p.stat().st_size)

        src = max(found, key=_rank)
        rel = src.relative_to(out_dir).as_posix()
        vfs_path = f"content/{rel}" if not rel.startswith("content/") else rel
        on_disk = (cache_root / _strip_content_prefix(vfs_path)).resolve()
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, on_disk)
        except OSError:
            import shutil

            shutil.copyfile(src, on_disk)
        return vfs_path, _cache_url(vfs_path)


# ---------------------------------------------------------------------------
# Space discovery / parse
# ---------------------------------------------------------------------------


def _space_name(space: str) -> str:
    """Normalise ``"spaces/14_Atlantic"`` / ``"14_Atlantic"`` -> ``"14_Atlantic"``."""
    return space.replace("\\", "/").rstrip("/").split("/")[-1]


def _ubersettings_path(
    space_name: str, *, config: PipelineConfig
) -> Path | None:
    """Locate a space's ``space.ubersettings`` — res_unpack first, else extract.

    Returns ``None`` if it can't be produced (space has no ubersettings).
    """
    game_dir = config.game_dir
    if game_dir is not None:
        unpacked = (
            Path(game_dir)
            / "res_unpack"
            / "spaces"
            / space_name
            / "space.ubersettings"
        )
        if unpacked.is_file():
            return unpacked

    # Fallback: pull it from the VFS into the cache.
    vfs_path = f"spaces/{space_name}/space.ubersettings"
    cache_root = (config.workspace / CACHE_ROOT).resolve()
    on_disk = cache_root / "spaces" / space_name / "space.ubersettings"
    if on_disk.is_file():
        return on_disk
    with tempfile.TemporaryDirectory(prefix="wms-env-ubs-") as td:
        out_dir = Path(td)
        try:
            _vfs.extract([f"/{vfs_path}"], out_dir=out_dir, config=config)
        except Exception:
            return None
        src = out_dir / vfs_path
        if not src.is_file():
            matches = list(out_dir.rglob("space.ubersettings"))
            src = matches[0] if matches else None
        if src is None or not src.is_file():
            return None
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, on_disk)
        except OSError:
            import shutil

            shutil.copyfile(src, on_disk)
        return on_disk


def _list_spaces(config: PipelineConfig) -> list[str]:
    """All space names (delegates to ``toolkit.map.list_spaces``)."""
    from ..toolkit import map as _map

    return [_space_name(s) for s in _map.list_spaces(config)]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def manifest_paths(workspace: Path) -> dict[str, Path]:
    """Absolute on-disk paths for the manifest artefact."""
    ws = workspace.resolve()
    return {"root": ws / LIBRARY_ROOT, "manifest": ws / MANIFEST_FILE}


def _atomic_write_json(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def build(
    *,
    config: PipelineConfig | None = None,
    spaces: list[str] | None = None,
    extract_assets: bool = True,
) -> dict[str, Any]:
    """Build the environment library manifest (+ extract cubes & BRDF LUT).

    ``spaces`` restricts to a subset (bare names or ``spaces/<name>``); ``None``
    builds every space. ``extract_assets=False`` parses params only (fast — no
    DDS extraction), useful for refreshing just the HDR/SH numbers. Idempotent:
    already-cached DDS are skipped.
    """
    cfg = config or PipelineConfig.load()
    workspace = cfg.workspace.resolve()
    paths = manifest_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)

    space_names = (
        [_space_name(s) for s in spaces]
        if spaces is not None
        else _list_spaces(cfg)
    )

    # Parse every requested space first (cheap XML), collecting the cube
    # candidates so we can batch-extract them in one toolkit invocation.
    parsed: dict[str, dict[str, Any]] = {}
    cube_candidates: set[str] = set()
    warnings: list[str] = []
    for name in space_names:
        ubs = _ubersettings_path(name, config=cfg)
        if ubs is None:
            warnings.append(f"{name}: no space.ubersettings")
            continue
        try:
            env = _ubersettings.parse_ubersettings(ubs)
        except Exception as e:  # malformed XML
            warnings.append(f"{name}: parse failed: {e}")
            continue
        parsed[name] = env
        for w in env["weathers"].values():
            cp = w.get("cubemaps_path")
            if isinstance(cp, str) and cp:
                cube_candidates.add(_resolve_cube_vfs(cp))

    # Resolve cube + BRDF URLs from the cache. With extract_assets the missing
    # ones are pulled from the VFS; without it, only already-cached files are
    # linked (a params-only refresh keeps the cube links instead of nulling
    # them).
    batch = sorted(cube_candidates) + [ENV_BRDF_LUT_VFS]
    resolved = _extract_to_cache(
        batch, config=cfg, workspace=workspace, extract=extract_assets
    )
    env_brdf_url = resolved.get(ENV_BRDF_LUT_VFS)
    cube_urls = {k: v for k, v in resolved.items() if k != ENV_BRDF_LUT_VFS}

    # Assemble the per-space / per-weather manifest.
    spaces_out: dict[str, Any] = {}
    cubes_extracted = 0
    cubes_missing = 0
    for name, env in parsed.items():
        weathers_out: dict[str, Any] = {}
        for wname, w in env["weathers"].items():
            cp = w.get("cubemaps_path")
            cube_url: str | None = None
            cube_info: dict[str, Any] | None = None
            if isinstance(cp, str) and cp:
                cube_vfs = _resolve_cube_vfs(cp)
                cube_url = cube_urls.get(cube_vfs)
                if cube_url is None and extract_assets:
                    fb = _extract_cube_fallback(
                        cp, config=cfg, workspace=workspace
                    )
                    if fb is not None:
                        cube_url = fb[1]
                if cube_url is not None:
                    cubes_extracted += 1
                    on_disk = workspace / cube_url
                    cube_info = sniff_dds(on_disk)
                elif extract_assets:
                    cubes_missing += 1
                    warnings.append(f"{name}/{wname}: cube not found at {cp}")
            weathers_out[wname] = {
                "cube_url": cube_url,
                "cube": cube_info,
                "cubemaps_path": cp,
                "hdr": w.get("hdr") or {},
                "sh": w.get("sh"),
                "pbs_extras": w.get("pbs_extras") or {},
                "sun": w.get("sun"),
            }
        spaces_out[name] = {
            "weather_order": env.get("weather_order") or list(weathers_out),
            "weathers": weathers_out,
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "built_at": _now_iso(),
        "env_brdf_lut_url": env_brdf_url,
        "env_brdf_lut": (
            sniff_dds(workspace / env_brdf_url) if env_brdf_url else None
        ),
        "space_count": len(spaces_out),
        "spaces": spaces_out,
    }
    _atomic_write_json(paths["manifest"], manifest)

    return {
        "status": "built",
        "space_count": len(spaces_out),
        "cubes_extracted": cubes_extracted,
        "cubes_missing": cubes_missing,
        "env_brdf_lut_url": env_brdf_url,
        "manifest_path": str(paths["manifest"]),
        "warnings": warnings,
    }


__all__ = [
    "CACHE_ROOT",
    "LIBRARY_ROOT",
    "MANIFEST_FILE",
    "SCHEMA_VERSION",
    "ENV_BRDF_LUT_VFS",
    "sniff_dds",
    "manifest_paths",
    "build",
]
