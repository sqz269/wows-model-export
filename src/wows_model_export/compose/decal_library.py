"""Fleet-wide decal library mirror.

Lifted from ``tools/build_decal_library.py`` (private I:-side repo).
Layer 4 (composer): mirrors WG's ``dyndecals/`` directory into the
curated subset under ``<workspace>/libraries/decals/`` with a
``manifest.json`` describing the prototype layout (SHOT / GROUND /
FIRE / HEAT classification, U-flip, technique, influence). The
prototype tables are lifted verbatim from the decompiled
``ClientDecals/DecalProperties.pyc``.

Output layout::

    <library_root>/
      damage_dec_1_{d,p,e}.dds          # 7 damage prototype texture sets
      damage_dec_2_{d,p,e}.dds          # damage_dec_2 + heat_dec_0 also
      damage_dec_2_d.{dd0,dd1,dd2}      # carry WG's high-res mip-strip quartet
      …
      heat_dec_0_{d,p,e}.dds
      heat_dec_0_d.{dd0,dd1,dd2}
      heat_dec_0_e.{dd0,dd1}
      heat_dec_0_p.{dd0,dd1}
      manifest.json                     # decal-proto schema (see source dict)

When the source ``dyndecals/`` directory isn't on disk, the composer
falls back to ``toolkit.extract`` to pull the files out of the VFS.
The ``extract_dyndecals`` step is skipped when the directory already
exists.

Idempotent — re-running mirrors only changed files (mtime + size).

The composer emits the following canonical :class:`StepEvent` names:

    "extract_dyndecals"   "discover_decals"   "copy_dds"   "write_manifest"
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path

from .. import toolkit
from ..config import PipelineConfig
from ..errors import StepError
from ..types import DecalLibraryResult, OnEvent
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# Prototype tables, lifted verbatim from ClientDecals/DecalProperties.py
# ---------------------------------------------------------------------------
#
# Mirrors:
#   SHOT_DECALS_PROTO_LIST   — decals 2..6, applied to STATIC (ships)
#   GROUND_DECALS_PROTO_LIST — decal 1, decal 7, applied to TERRAIN
#   FIRE_DECALS_PROTO_LIST   — decal 2 reused, applied to STATIC
#   HEAT_DECALS_PROTO_LIST   — heat_dec_0, technique = EMISSIVE
#
# All prototypes set DECAL_FLIP.U (U-axis flip applied to texture
# sampling).

# The first four lists (shot/ground/fire/heat) are lifted verbatim from
# ClientDecals/DecalProperties.py. The last two (he/scuff) are NOT WG-native
# prototype lists — they are curated routing groups so a consumer can pick a
# look by hit kind: ``he`` = wide soot/scorch/smoke for HE detonations,
# ``scuff`` = light hole-less patches for ricochets/shatters. They reuse the
# same damage_dec_* assets, sub-selected from the WG damage set.
PROTOTYPE_LISTS = {
    "shot":   ["damage_dec_2", "damage_dec_3", "damage_dec_4",
               "damage_dec_5", "damage_dec_6"],
    "ground": ["damage_dec_1", "damage_dec_7"],
    "fire":   ["damage_dec_2"],
    "heat":   ["heat_dec_0"],
    "he":     ["damage_dec_1", "damage_dec_5", "damage_dec_6"],
    "scuff":  ["damage_dec_3", "damage_dec_5"],
}

TECHNIQUES = {
    "shot": "DAMAGE", "ground": "DAMAGE",
    "fire": "DAMAGE", "heat": "EMISSIVE",
    "he": "DAMAGE", "scuff": "DAMAGE",
}

INFLUENCE = {
    "shot": "APPLY_TO_STATIC", "ground": "APPLY_TO_TERRAIN",
    "fire": "APPLY_TO_STATIC", "heat": "APPLY_TO_STATIC",
    "he": "APPLY_TO_STATIC", "scuff": "APPLY_TO_STATIC",
}

# Per-decal-name parallax channel encoding. Empirically determined by
# inspecting the .dds content (see ``wg_dyndecals.md``):
#   - heat_dec_0_p is a true tangent-space normal map (rainbow pattern)
#   - all damage_dec_*_p are dark grayscale-ish maps, likely parallax-
#     height (encoded as grayscale RGB; values cluster near 0).
PARALLAX_KIND = {
    name: "tangent_normal" if name.startswith("heat_") else "grayscale_height"
    for cat in PROTOTYPE_LISTS.values()
    for name in cat
}

# Projector box depth in metres — the 5th arg to WG's
# ``setTransform(pos, dir, tangent, size, depth)`` (Ghidra FUN_1401b1c40;
# equals Unity ``DecalProjector.size.z``). Constant across all prototypes.
PROJECTION_DEPTH = 0.7

# HDR multiplier applied to the sRGB-decoded ``_e`` channel in the forward-lit
# decal pixel shader (DXBC ``decal.win.dx11`` chunk001/009: ``mad r, e_sample,
# l(104.0,104.0,104.0,0), r``). Emitted so consumers source it instead of
# hardcoding; treat as WG's pre-tonemap HDR scale and re-tune visually under
# the consumer's tonemapper rather than copying the literal.
EMISSIVE_HDR_SCALE = 104.0

# Default WG install path (matches the I:-side convention). Overridable
# via the ``source_dir`` parameter on the public entry.
_DEFAULT_DYNDECALS_DIR = Path(
    r"I:/SteamLibrary/steamapps/common/World of Warships/res_unpack/dyndecals"
)

# Default patch identifier stamped in the manifest.
_DEFAULT_PATCH_ID = "12116141"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_changed(src: Path, dest: Path) -> bool:
    if not dest.exists():
        return True
    s, d = src.stat(), dest.stat()
    return s.st_mtime > d.st_mtime or s.st_size != d.st_size


def _copy_if_changed(src: Path, dest: Path) -> bool:
    """Copy ``src`` → ``dest`` if changed; return True if copied."""
    if not _file_changed(src, dest):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def discover_decals(src_dir: Path) -> dict[str, dict]:
    """Walk ``src_dir`` and group files into ``{decal_name: {channel: [files]}}``.

    A decal name like ``damage_dec_2`` aggregates ``damage_dec_2_d.dds``,
    ``damage_dec_2_d.dd0/.dd1/.dd2``, ``damage_dec_2_p.dds``, etc.
    """
    if not src_dir.is_dir():
        raise FileNotFoundError(f"source dir not found: {src_dir}")

    out: dict[str, dict] = {}
    pat = re.compile(
        r"^([a-z]+_dec_\d+)_([dpe])\.(dds|dd[012])$",
        re.IGNORECASE,
    )
    for f in sorted(src_dir.iterdir()):
        if not f.is_file() or f.suffix == ".bak":
            continue
        m = pat.match(f.name)
        if not m:
            continue
        decal = m.group(1).lower()
        channel = m.group(2).lower()
        d = out.setdefault(decal, {
            "diffuse": [], "parallax": [], "emissive": [],
        })
        slot = {"d": "diffuse", "p": "parallax", "e": "emissive"}[channel]
        d[slot].append(f.name)
    return out


def build_manifest(decals: dict[str, dict], patch_id: str) -> dict:
    out: dict = {
        "patch_id": patch_id,
        "u_flip": True,
        "projection_depth": PROJECTION_DEPTH,
        "emissive_hdr_scale": EMISSIVE_HDR_SCALE,
        "decals": {},
        "prototype_lists": PROTOTYPE_LISTS,
        "techniques": TECHNIQUES,
        "influence": INFLUENCE,
    }
    for name, channels in sorted(decals.items()):
        # Pick the canonical .dds (lowest mip, base file) and the WG
        # mip-strip:
        def _pick_canonical(files: list[str]) -> str | None:
            dds = [f for f in files if f.endswith(".dds")]
            return dds[0] if dds else (files[0] if files else None)

        def _pick_mips(files: list[str]) -> list[str]:
            return sorted(f for f in files if re.search(r"\.dd[012]$", f))

        entry: dict = {
            "diffuse":       _pick_canonical(channels["diffuse"]),
            "parallax":      _pick_canonical(channels["parallax"]),
            "emissive":      _pick_canonical(channels["emissive"]),
            "diffuse_mips":  _pick_mips(channels["diffuse"]),
            "parallax_mips": _pick_mips(channels["parallax"]),
            "emissive_mips": _pick_mips(channels["emissive"]),
            "parallax_kind": PARALLAX_KIND.get(name, "unknown"),
        }
        # Drop empty mip lists for cleanliness.
        for k in ("diffuse_mips", "parallax_mips", "emissive_mips"):
            if not entry[k]:
                del entry[k]
        out["decals"][name] = entry
    return out


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def build_decal_library(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    force: bool = False,
    on_event: OnEvent | None = None,
    source_dir: Path | None = None,
    patch_id: str | None = None,
    cancel: threading.Event | None = None,
) -> DecalLibraryResult:
    """Mirror WG's ``dyndecals/`` into ``<library_root>/`` with a manifest.

    The composer copies every ``*_{d,p,e}.{dds,dd0,dd1,dd2}`` file from
    ``source_dir`` (default: the WG install's pre-extracted
    ``res_unpack/dyndecals/`` directory) into ``library_root``, skipping
    files whose mtime + size match the destination. A ``manifest.json``
    describing the prototype layout (SHOT / GROUND / FIRE / HEAT
    classification + per-decal channel paths) is written alongside.

    When ``source_dir`` doesn't exist on disk, the composer falls back
    to ``toolkit.extract`` to pull the dyndecals directory out of the
    VFS first. The fallback is skipped silently when the directory is
    already populated.

    Parameters:
        workspace        ``PipelineConfig.workspace`` when None.
        config           ``PipelineConfig.load()`` when None.
        library_root     ``workspace / "libraries/decals"`` when None.
        force            When True, re-copy every source file even when
                          the destination is up to date.
        on_event         Optional progress callback receiving
                          :class:`StepEvent` notifications.
        source_dir       Path to WG's pre-extracted ``dyndecals/``
                          directory. Defaults to the conventional
                          ``res_unpack/dyndecals`` under the WoWS
                          install.
        patch_id         Patch identifier stamped into the manifest.
                          Defaults to the I:-side convention; override
                          per game patch.
        cancel           Optional :class:`threading.Event` for
                          cooperative cancel; when set, the next step
                          boundary raises
                          :class:`wows_model_export.errors.CancelledError`.

    Returns a :class:`DecalLibraryResult` with the library root, copy
    counts, manifest path, warnings, and per-step timings.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    step names) when any step fails. The original exception is
    accessible via ``.underlying``.
    """
    cfg = config or PipelineConfig.load()
    ws = (workspace or cfg.workspace).resolve()
    lib_root = (library_root or (ws / "libraries" / "decals")).resolve()
    src = (source_dir or _DEFAULT_DYNDECALS_DIR).resolve()
    pid = patch_id or _DEFAULT_PATCH_ID

    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []

    # ── Step: extract_dyndecals ───────────────────────────────────────
    # If the source dir is missing on disk, pull it out of the VFS so
    # later steps have something to walk. Skipped silently when the
    # directory is already populated.
    try:
        with runner.step("extract_dyndecals") as st:
            if src.is_dir() and any(src.iterdir()):
                st.annotate(
                    f"source dir already populated at {src}",
                    data={"extracted": False, "source": str(src)},
                )
            else:
                # VFS fallback: pull dyndecals into the source dir.
                src.mkdir(parents=True, exist_ok=True)
                try:
                    toolkit.extract(
                        ["**/dyndecals/**"],
                        out_dir=src,
                        config=cfg,
                    )
                    st.annotate(
                        f"extracted dyndecals/ into {src}",
                        data={"extracted": True, "source": str(src)},
                    )
                except Exception as e:
                    warnings.append(
                        f"extract_dyndecals fallback failed: {e}"
                    )
                    st.annotate(
                        f"VFS extract failed; proceeding with empty {src}",
                        data={"extracted": False, "source": str(src)},
                    )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="extract_dyndecals", underlying=e, detail=str(e),
        ) from e

    # ── Step: discover_decals ─────────────────────────────────────────
    try:
        with runner.step("discover_decals") as st:
            try:
                decals = discover_decals(src)
            except FileNotFoundError as e:
                # Convert the bare not-found into a proper step error.
                raise StepError(
                    step="discover_decals", underlying=e,
                    detail=str(e),
                ) from e
            st.annotate(
                f"found {len(decals)} decal(s) at {src}",
                data={"decals": len(decals), "source": str(src)},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="discover_decals", underlying=e, detail=str(e),
        ) from e

    # ── Step: copy_dds ────────────────────────────────────────────────
    copied = 0
    skipped = 0
    try:
        with runner.step("copy_dds") as st:
            lib_root.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if not f.is_file() or f.suffix == ".bak":
                    continue
                dest = lib_root / f.name
                if force:
                    # Force-copy unconditionally (overwrites identical
                    # files too — useful when the destination got
                    # corrupted somehow).
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    copied += 1
                    continue
                if _copy_if_changed(f, dest):
                    copied += 1
                else:
                    skipped += 1
            st.annotate(
                f"copied={copied} skipped={skipped}",
                data={"copied": copied, "skipped": skipped},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="copy_dds", underlying=e, detail=str(e),
        ) from e

    # ── Step: write_manifest ──────────────────────────────────────────
    try:
        with runner.step("write_manifest") as st:
            manifest = build_manifest(decals, pid)
            manifest_path = lib_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            st.annotate(
                f"wrote {manifest_path.name}",
                data={"manifest": str(manifest_path)},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="write_manifest", underlying=e, detail=str(e),
        ) from e

    return DecalLibraryResult(
        library_root=lib_root,
        decals_copied=copied,
        decals_skipped=skipped,
        manifest_path=manifest_path,
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "INFLUENCE",
    "PARALLAX_KIND",
    "PROTOTYPE_LISTS",
    "TECHNIQUES",
    "build_decal_library",
    "build_manifest",
    "discover_decals",
]
