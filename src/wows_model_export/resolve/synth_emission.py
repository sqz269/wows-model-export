"""Synthesize a WG emissive map (mg.B-gated, linear-space scaling).

Lifted from ``tools/shared/synth_emission.py`` (private I:-side repo).
Lives in :mod:`wows_model_export.resolve` because it's a deterministic
transform — diffuse + mg → emission — even though it touches disk to
read DDS inputs and write DDS outputs. Two of the higher-level helpers
(:func:`synthesize_emissive_textures` and
:func:`synthesize_emissive_textures_batch`) call
:func:`wows_model_export.toolkit.extract` to pull missing ``_emissive.mfm``
/ ``_a`` / ``_mg`` files from the VFS — they're the necessary
side-effecting bridge for the otherwise-pure synthesis pipeline.

Background: WG's ``ship_emissive_material.fx`` shader (used by ARP,
Azur Lane, Sabaton crossover skins — anything with a sibling
``*_emissive.mfm``) repurposes the B channel of the ``_mg`` map as the
emissive-region mask. The mask is binary in practice (0 = no glow,
255 = full glow), with the masked pixels emitting in their diffuse
colour at intensity ``emissivePower`` (default 1.8 across all tested
ARP / AL ships).

This module bakes the ARP-style emission as a standalone DDS that any
glTF / three.js / standard PBR material can bind directly via
``emissiveMap`` with ``emissiveIntensity = 1.0``. The runtime shader's mask-color
animation (cyan↔teal cycling on ARP ships) is OUT OF SCOPE — for
static rendering we treat the emission as a frozen frame.

Synthesis math:

    emission_RGB = diffuse_RGB * (mg.B / 255) * emissivePower

with the multiplication done in linear-light space and the result
re-encoded back to sRGB.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import PipelineConfig


#: Default ``emissivePower`` when an ``*_emissive.mfm`` doesn't carry one.
#: Empirically 1.8 across every tested ARP / AL / Sabaton ship.
DEFAULT_EMISSIVE_POWER: float = 1.8

#: Shader-ID family prefix for ``ship_emissive_material.fx`` variants
#: (the engine's emissive PBS shader). Toolkit writes ``shader_id`` as
#: ``"0x00060xxx"`` for emissive materials; observed variants in the
#: live corpus are ``0x00060600`` (BA_Montana_Hoshino, AzurLane / ARP
#: crossovers — 607 materials), ``0x00060700`` (Hellcarrier / Hakuryu
#: Space — 275 materials), and ``0x00060900`` (Azur_Hindenburg, BA_I56
#: — 177 materials). The low byte distinguishes feature flags
#: (skinned, glow animation, etc.); the upper bytes flag the shader
#: family. Detection uses the family prefix so all variants — current
#: and future — route through the emissive synthesis. Used to detect
#: ships whose BASE ``.mfm`` is itself emissive (no sibling
#: ``*_emissive.mfm`` overlay), e.g. Hellcarrier / BA_Montana_Hoshino.
EMISSIVE_SHADER_ID_PREFIX: str = "0x00060"


# 8-bit sRGB ↔ linear LUTs — standard glTF / three.js / PBR convention.
def _srgb_to_linear(c: float) -> float:
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


_S2L = [int(round(_srgb_to_linear(i / 255.0) * 65535)) for i in range(256)]
_L2S = [int(round(_linear_to_srgb(min(1.0, i / 65535.0)) * 255))
        for i in range(65536 + 1)]

# NumPy mirrors of the LUTs for the vectorised pixel pass below. Built once
# at import time so per-image synthesis is a handful of array ops rather
# than a Python double-for over millions of pixels.
_S2L_NP = np.asarray(_S2L, dtype=np.int32)            # 256 entries, 0..65535
_L2S_NP = np.asarray(_L2S, dtype=np.uint8)            # 65537 entries, 0..255


def _open_dd(path: Path) -> Image.Image:
    """PIL's DDS plugin only recognises files with a ``.dds`` extension.

    For ``.dd0`` / ``.dd1`` / ``.dd2`` inputs, copy to a per-process temp
    dir so PIL accepts them — and crucially, so the temp ``.dds`` file
    does NOT live alongside the source in ``textures_dds/``. Putting it
    next to the source pollutes the sidecar's stem index (e.g.
    ``<stem>_a.dd0.dds`` becomes a fake ``<stem>_a.dd0`` stem on
    subsequent scans). Returns a PIL image fully loaded into memory; the
    temp file is unlinked before return.
    """
    if path.suffix.lower() in {".dd0", ".dd1", ".dd2"}:
        with tempfile.NamedTemporaryFile(suffix=".dds", delete=False) as tf:
            tmp_path = Path(tf.name)
            shutil.copyfileobj(open(path, "rb"), tf)
        try:
            img = Image.open(tmp_path)
            img.load()  # force decode while file is alive
            return img
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return Image.open(path)


def synth_emissive(diffuse: Image.Image, mg: Image.Image,
                   emissive_power: float) -> Image.Image:
    """Return an sRGB RGB image of ``diffuse * (mg.B / 255) *
    emissive_power`` computed in linear-light space."""
    if diffuse.mode != "RGB":
        diffuse = diffuse.convert("RGB")
    if mg.size != diffuse.size:
        mg = mg.resize(diffuse.size, Image.LANCZOS)
    if mg.mode != "RGB":
        mg = mg.convert("RGB")

    diff_arr = np.asarray(diffuse, dtype=np.uint8)            # (h, w, 3)
    mg_arr = np.asarray(mg, dtype=np.uint8)                   # (h, w, 3)
    mask_b = mg_arr[..., 2]                                   # (h, w) uint8

    # sRGB → linear via LUT, scale by mask*emissive_power, clip to [0, 65535],
    # back to sRGB via the second LUT. The mask==0 fast-path in the original
    # loop is preserved implicitly: lin == 0 → _L2S_NP[0] == 0.
    diff_lin = _S2L_NP[diff_arr].astype(np.float32, copy=False)  # (h, w, 3)
    mfac = (mask_b.astype(np.float32) / 255.0) * float(emissive_power)
    lin = diff_lin * mfac[..., np.newaxis]
    np.clip(lin, 0.0, 65535.0, out=lin)
    out_arr = _L2S_NP[lin.astype(np.int32)]                   # (h, w, 3) uint8
    return Image.fromarray(out_arr, mode="RGB")


def synth_emissive_dds(
    diffuse_path: str | Path,
    mg_path: str | Path,
    output_path: str | Path,
    *,
    emissive_power: float = DEFAULT_EMISSIVE_POWER,
    pixel_format: str = "DXT1",
) -> Path:
    """Synthesize ``output_path`` (a DDS) from the ARP-style mask + diffuse.

    PIL's DDS writer requires a ``.dds`` extension on the destination,
    but we want WG's ``.dd0`` / ``.dd1`` / ``.dd2`` mip-suffix convention
    so the sidecar's existing ``_classify_dds_filename`` /
    ``DDS_MIP_SUFFIXES`` discovery logic picks the file up automatically.
    So we encode to a temp ``.dds`` then rename to whatever the caller
    asked for.

    Returns the resolved output path. Output is RGB-only DXT1 — emission
    has no alpha channel.
    """
    diff = _open_dd(Path(diffuse_path)).convert("RGB")
    mg = _open_dd(Path(mg_path)).convert("RGB")
    out_img = synth_emissive(diff, mg, emissive_power)

    out_path = Path(output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.suffix.lower() == ".dds":
        out_img.save(out_path, "DDS", pixel_format=pixel_format)
    else:
        # PIL writer keys on extension. Encode → rename.
        tmp = out_path.with_suffix(".dds")
        out_img.save(tmp, "DDS", pixel_format=pixel_format)
        if out_path.exists():
            out_path.unlink()
        tmp.rename(out_path)
    return out_path


def _load_material_mappings(
    material_mappings_json: str | Path | None,
) -> list[dict] | None:
    """Return ``materials`` list from ``material_mappings.json`` or
    ``None`` if missing / unreadable."""
    if material_mappings_json is None:
        return None
    p = Path(material_mappings_json)
    if not p.is_file():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    mats = doc.get("materials")
    return mats if isinstance(mats, list) else None


def _collect_path_b_targets(
    materials: list[dict] | None,
) -> list[tuple[str, str, str]]:
    """Return ``(diffuse_stem, mg_stem, mfm_basename)`` for every material
    whose base ``.mfm`` uses any variant of the emissive shader
    (``shader_id`` matches :data:`EMISSIVE_SHADER_ID_PREFIX`).

    Covers ships like Hellcarrier / Hakuryu_Space and BA_Montana's
    Hoshino variant where the canonical ``.mfm`` is emissive (no
    sibling ``*_emissive.mfm`` overlay).

    Skips entries whose ``mfm_stem`` already ends in ``_emissive`` —
    those are handled by Path A's filename-based discovery.

    Deduplicated by ``(diffuse_stem, mg_stem)`` since one material is
    typically reused across many sub-models / render-sets in the JSON.
    """
    if not materials:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for entry in materials:
        sid = (entry.get("shader_id") or "").strip().lower()
        if not sid.startswith(EMISSIVE_SHADER_ID_PREFIX):
            continue
        mfm_stem = (entry.get("mfm_stem") or "").strip()
        # Path-A entries (the legacy overlay convention) carry an
        # `_emissive`-suffixed stem; let Path A own them so we don't
        # double-extract or double-synthesize.
        if mfm_stem.endswith("_emissive"):
            continue
        textures = entry.get("textures") or {}
        diffuse_stem = ((textures.get("diffuseMap") or {}).get("stem") or "").strip()
        mg_stem = ((textures.get("metallicGlossMap") or {}).get("stem") or "").strip()
        if not diffuse_stem or not mg_stem:
            continue
        key = (diffuse_stem, mg_stem)
        if key in seen:
            continue
        seen.add(key)
        mfm_path = (entry.get("mfm_path") or "").strip()
        mfm_basename = Path(mfm_path).name if mfm_path else f"{mfm_stem}.mfm"
        out.append((diffuse_stem, mg_stem, mfm_basename))
    return out


def _build_mg_fallback_map(
    material_mappings_json: str | Path | None,
) -> dict[str, str]:
    """Read the toolkit's ``material_mappings.json`` and return a map
    ``{<emissive_stem> → <resolved_mg_stem>}`` for entries where the
    ``metallicGlossMap`` is on a *different* stem than the ``diffuseMap``.

    Mesh-swap variants encode this divergence: ARP Takao Red's
    ``SHIPMAT_EMISSIVE_PBS_Hull`` material has
    ``diffuseMap.stem = JSC508_Takao_1944_Red_Arpeggio`` (Red variant)
    but ``metallicGlossMap.stem = JSC507_Takao_1944_Arpeggio`` (Blue
    Arpeggio inheritance). Without this map, synth would skip the
    top-mip output for the variant entirely. With it, synth uses the
    resolved stem's ``_mg.dd0`` while keeping the variant's ``_a.dd0``
    for diffuse.

    Returns ``{}`` for missing / unreadable / library-only mappings —
    callers fall back to the same-stem behaviour.
    """
    if material_mappings_json is None:
        return {}
    p = Path(material_mappings_json)
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    fallback: dict[str, str] = {}
    for entry in doc.get("materials", []):
        mfm_stem = (entry.get("mfm_stem") or "").strip()
        if not mfm_stem.endswith("_emissive"):
            continue
        textures = entry.get("textures") or {}
        diffuse_stem = ((textures.get("diffuseMap") or {}).get("stem") or "").strip()
        mg_stem = ((textures.get("metallicGlossMap") or {}).get("stem") or "").strip()
        if not diffuse_stem or not mg_stem:
            continue
        if mg_stem == diffuse_stem:
            continue  # same-stem case — the existing path already works
        fallback[diffuse_stem] = mg_stem
    return fallback


def synthesize_emissive_textures(
    textures_dds_dir: Path,
    *,
    config: PipelineConfig | None = None,
    label: str = "",
    material_mappings_json: str | Path | None = None,
) -> list[Path]:
    """Discover ``*_emissive.mfm`` files in the VFS for each texture stem
    in ``textures_dds_dir``, then synthesize per-stem emissive DDS files
    next to the diffuse.

    Detection is definitive (not heuristic): a stem owns an emissive
    material iff a sibling ``<stem>_emissive.mfm`` exists in the VFS.

    For each emissive stem found, this writes:

      - ``<stem>_emissive.dd0``  (top mip — synthesized from ``_a.dd0`` + ``_mg.dd0``)
      - ``<stem>_emissive.dds``  (low mip — synthesized from ``_a.dds`` + ``_mg.dds``)

    Sidecar's ``_DDS_CHANNEL_TO_SLOT`` then routes these into
    ``texture_sets[<scheme>]["emissive"]`` automatically.

    For mesh-swap variants whose MFM resolves ``_mg`` to a different
    stem, pass ``material_mappings_json`` so synth can swap in the
    resolved stem's ``_mg.dd0``. Without it, the variant's own (low-mip-
    only) ``_mg`` is used — synth still produces a ``.dds`` output but
    skips the ``.dd0`` top mip.

    No-op for non-emissive ships / accessories (no ``*_emissive.mfm``
    matches → 0 files synthesized). Returns the list of synth output
    paths.

    For multi-asset workflows (per-ship accessory builds), prefer
    :func:`synthesize_emissive_textures_batch` — it amortises the
    ~8s wowsunpack-extract cost across many ``textures_dds`` dirs.
    """
    # Lazy import to avoid forcing every consumer of synth_emission to
    # have the toolkit on its sys.path.
    from .. import toolkit
    from ..read import mfm as wg_mfm

    if not textures_dds_dir.is_dir():
        return []

    label = label or textures_dds_dir.parent.name

    mg_fallback = _build_mg_fallback_map(material_mappings_json)
    path_b_targets = _collect_path_b_targets(
        _load_material_mappings(material_mappings_json)
    )

    # Discover stems from extracted `_a.{dd0,dds}` AND `_mg.{dd0,dds}` files.
    stems: set[str] = set()
    for f in textures_dds_dir.glob("*_a.dd0"):
        stems.add(f.name[: -len("_a.dd0")])
    for f in textures_dds_dir.glob("*_mg.dd0"):
        stems.add(f.name[: -len("_mg.dd0")])
    for f in textures_dds_dir.glob("*_a.dds"):
        stems.add(f.name[: -len("_a.dds")])
    for f in textures_dds_dir.glob("*_mg.dds"):
        stems.add(f.name[: -len("_mg.dds")])
    if not stems and not path_b_targets:
        return []

    # Bulk-extract everything we need to read emissivePower from in ONE
    # wowsunpack call:
    #   - Path A: `**/<stem>_emissive.mfm` for each on-disk stem (legacy
    #             ARP / Sabaton overlay convention).
    #   - Path B: `**/<base_mfm>.mfm` for each material whose shader-id
    #             in material_mappings.json is the emissive shader.
    # Non-emissive assets get a no-op (no patterns match).
    mfm_patterns = [f"**/{stem}_emissive.mfm" for stem in stems]
    for _, _, mfm_basename in path_b_targets:
        mfm_patterns.append(f"**/{mfm_basename}")
    if mfm_patterns:
        try:
            toolkit.extract(
                mfm_patterns,
                textures_dds_dir,
                flatten=True,
                config=config,
            )
        except Exception as e:
            print(
                f"  warn: emissive .mfm extract failed for {label}: {e}",
                file=sys.stderr,
            )
            return []

    # Pull in any missing `<stem>_a.*` / `<stem>_mg.*` files that the
    # toolkit's `--raw-dds-dir` skipped but exist in the VFS — for both
    # Path A (`*_emissive.mfm` siblings) and Path B (material_mappings
    # shader-id detected).
    fill_patterns: list[str] = []
    for mfm in textures_dds_dir.glob("*_emissive.mfm"):
        stem = mfm.name[: -len("_emissive.mfm")]
        mg_stem = mg_fallback.get(stem, stem)
        if not (textures_dds_dir / f"{stem}_a.dd0").is_file() \
                and not (textures_dds_dir / f"{stem}_a.dds").is_file():
            fill_patterns.append(f"**/{stem}_a.dd?")
            fill_patterns.append(f"**/{stem}_a.dds")
        if not (textures_dds_dir / f"{mg_stem}_mg.dd0").is_file() \
                and not (textures_dds_dir / f"{mg_stem}_mg.dds").is_file():
            fill_patterns.append(f"**/{mg_stem}_mg.dd?")
            fill_patterns.append(f"**/{mg_stem}_mg.dds")
    for diffuse_stem, mg_stem, _ in path_b_targets:
        if not (textures_dds_dir / f"{diffuse_stem}_a.dd0").is_file() \
                and not (textures_dds_dir / f"{diffuse_stem}_a.dds").is_file():
            fill_patterns.append(f"**/{diffuse_stem}_a.dd?")
            fill_patterns.append(f"**/{diffuse_stem}_a.dds")
        if not (textures_dds_dir / f"{mg_stem}_mg.dd0").is_file() \
                and not (textures_dds_dir / f"{mg_stem}_mg.dds").is_file():
            fill_patterns.append(f"**/{mg_stem}_mg.dd?")
            fill_patterns.append(f"**/{mg_stem}_mg.dds")
    if fill_patterns:
        try:
            toolkit.extract(
                fill_patterns,
                textures_dds_dir,
                flatten=True,
                config=config,
            )
        except Exception as e:
            print(
                f"  warn: emissive supplemental texture extract failed for "
                f"{label}: {e}",
                file=sys.stderr,
            )

    # Synthesize one emissive set per (diffuse_stem, mg_stem) pair.
    # Path A keys off the on-disk `*_emissive.mfm` filename; Path B
    # iterates the shader-id targets and reads emissivePower from the
    # base ``.mfm`` (extracted in the bulk call above).
    written: list[Path] = []
    synthesized: set[str] = set()  # diffuse_stems already produced

    def _synth_one(stem: str, mg_stem: str, mfm_for_power: Path) -> None:
        if stem in synthesized:
            return
        diff_dd0 = textures_dds_dir / f"{stem}_a.dd0"
        diff_low = textures_dds_dir / f"{stem}_a.dds"
        mg_dd0 = textures_dds_dir / f"{mg_stem}_mg.dd0"
        mg_low = textures_dds_dir / f"{mg_stem}_mg.dds"
        if not ((diff_dd0.is_file() or diff_low.is_file())
                and (mg_dd0.is_file() or mg_low.is_file())):
            print(
                f"  skip emissive synth for {stem}: missing _a.* "
                f"or {mg_stem}_mg.* (VFS pull-in didn't surface them)",
                file=sys.stderr,
            )
            return

        power = (
            wg_mfm.get_emissive_power(mfm_for_power, default=DEFAULT_EMISSIVE_POWER)
            if mfm_for_power.is_file() else DEFAULT_EMISSIVE_POWER
        )

        mip_count = 0
        if diff_dd0.is_file() and mg_dd0.is_file():
            out_dd0 = textures_dds_dir / f"{stem}_emissive.dd0"
            synth_emissive_dds(diff_dd0, mg_dd0, out_dd0, emissive_power=power)
            written.append(out_dd0)
            mip_count += 1

        if diff_low.is_file() and mg_low.is_file():
            out_low = textures_dds_dir / f"{stem}_emissive.dds"
            synth_emissive_dds(diff_low, mg_low, out_low, emissive_power=power)
            written.append(out_low)
            mip_count += 1

        synthesized.add(stem)
        mg_note = f" (mg from {mg_stem})" if mg_stem != stem else ""
        print(
            f"  emissive synth: {stem}_emissive.{{dd0,dds}}  "
            f"(power={power:.2f}, {mip_count} mip(s)){mg_note}"
        )

    for mfm in sorted(textures_dds_dir.glob("*_emissive.mfm")):
        stem = mfm.name[: -len("_emissive.mfm")]
        mg_stem = mg_fallback.get(stem, stem)
        _synth_one(stem, mg_stem, mfm)

    for diffuse_stem, mg_stem, mfm_basename in path_b_targets:
        _synth_one(
            diffuse_stem, mg_stem, textures_dds_dir / mfm_basename,
        )

    return written


def synthesize_emissive_textures_batch(
    textures_dds_dirs: list[Path],
    *,
    config: PipelineConfig | None = None,
    label: str = "batch",
) -> dict[Path, list[Path]]:
    """Batch-synthesize emissive textures across many ``textures_dds``
    dirs in ONE consolidated wowsunpack-extract call.

    Per-asset cost when called as :func:`synthesize_emissive_textures` is
    ~16 s (two ``toolkit.extract`` invocations × ~8 s VFS parse). For
    130 accessories per ship that's 35 minutes of pure VFS parse
    overhead. This batched version aggregates ALL stems across all input
    dirs, runs ONE extract for ``*_emissive.mfm``, then ONE follow-up
    extract for the supplemental diffuse / mg pull-ins. Total cost:
    ~16 s regardless of asset count.

    Implementation: extract everything into a per-call temp dir, then
    distribute the extracted files to their owning asset's
    ``textures_dds`` dir. Synth runs locally per-asset on the
    distributed files.

    Path B (base-mfm shader-id detection — see
    :data:`EMISSIVE_SHADER_ID_PREFIX`) is auto-discovered by looking for
    a ``<parent>_material_mappings.json`` sibling next to each
    ``textures_dds`` dir. Asset layout under the accessory library is
    ``<library_root>/<scope>/<category>/<asset_id>/{textures_dds,
    <asset_id>_material_mappings.json}``; the per-asset
    material_mappings JSON is authoritative for which materials use
    the emissive shader family.

    Returns ``{textures_dds_dir: [synth output paths]}``. Dirs with no
    emissive content are absent from the returned dict.
    """
    from .. import toolkit
    from ..read import mfm as wg_mfm

    # Index: stem → owning textures_dds_dir. Each stem must be unique
    # across the batch — accessory IDs are globally unique so this
    # holds in practice. Fall back to first-wins if collisions occur.
    stem_to_dir: dict[str, Path] = {}
    # Path B targets per owning dir: list of (diffuse_stem, mg_stem,
    # mfm_basename) tuples sourced from each dir's sibling
    # material_mappings.json.
    path_b_by_dir: dict[Path, list[tuple[str, str, str]]] = {}
    for d in textures_dds_dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*_a.dd0"):
            stem = f.name[: -len("_a.dd0")]
            stem_to_dir.setdefault(stem, d)
        for f in d.glob("*_mg.dd0"):
            stem = f.name[: -len("_mg.dd0")]
            stem_to_dir.setdefault(stem, d)
        for f in d.glob("*_a.dds"):
            stem = f.name[: -len("_a.dds")]
            stem_to_dir.setdefault(stem, d)
        for f in d.glob("*_mg.dds"):
            stem = f.name[: -len("_mg.dds")]
            stem_to_dir.setdefault(stem, d)
        # Path B discovery: each accessory carries
        # `<asset_id>_material_mappings.json` one level above the
        # textures_dds dir. The asset_id is the parent dir's name.
        parent = d.parent
        candidate = parent / f"{parent.name}_material_mappings.json"
        if candidate.is_file():
            targets = _collect_path_b_targets(_load_material_mappings(candidate))
            if targets:
                path_b_by_dir[d] = targets

    if not stem_to_dir and not path_b_by_dir:
        return {}

    results: dict[Path, list[Path]] = {}

    with tempfile.TemporaryDirectory(prefix="emissive_synth_") as scratch_str:
        scratch = Path(scratch_str)

        # Single bulk extract for all `*_emissive.mfm` files (Path A) +
        # base ``<mfm_basename>.mfm`` files (Path B). Both share one
        # toolkit.extract call.
        mfm_patterns = [f"**/{stem}_emissive.mfm" for stem in stem_to_dir]
        path_b_basenames: set[str] = set()
        for targets in path_b_by_dir.values():
            for _, _, mfm_basename in targets:
                if mfm_basename:
                    path_b_basenames.add(mfm_basename)
        for bn in path_b_basenames:
            mfm_patterns.append(f"**/{bn}")
        try:
            toolkit.extract(
                mfm_patterns,
                scratch,
                flatten=True,
                config=config,
            )
        except Exception as e:
            print(
                f"  warn: batch emissive .mfm extract failed for "
                f"{label}: {e}",
                file=sys.stderr,
            )
            return {}

        emissive_mfms = sorted(scratch.glob("*_emissive.mfm"))
        # Bail early only if we have NEITHER an `_emissive.mfm` overlay
        # nor any Path B target — both paths produce work.
        if not emissive_mfms and not path_b_by_dir:
            return {}

        # For each emissive stem (Path A), copy the .mfm into its asset
        # dir. Then check whether the asset's diffuse / mg are already
        # on disk; if not, queue them for the supplemental extract pass.
        # Path B stems queue their own fill patterns afterward.
        fill_patterns: list[str] = []
        for mfm in emissive_mfms:
            stem = mfm.name[: -len("_emissive.mfm")]
            owner = stem_to_dir.get(stem)
            if owner is None:
                continue
            target_mfm = owner / mfm.name
            if not target_mfm.is_file():
                shutil.copy2(mfm, target_mfm)
            if not (owner / f"{stem}_a.dd0").is_file() \
                    and not (owner / f"{stem}_a.dds").is_file():
                fill_patterns.append(f"**/{stem}_a.dd?")
                fill_patterns.append(f"**/{stem}_a.dds")
            if not (owner / f"{stem}_mg.dd0").is_file() \
                    and not (owner / f"{stem}_mg.dds").is_file():
                fill_patterns.append(f"**/{stem}_mg.dd?")
                fill_patterns.append(f"**/{stem}_mg.dds")

        # Path B: copy each base .mfm into its owning dir, and queue
        # supplementary diffuse/mg pulls if the canonical files aren't
        # on disk yet.
        for owner, targets in path_b_by_dir.items():
            for diffuse_stem, mg_stem, mfm_basename in targets:
                src_mfm = scratch / mfm_basename
                target_mfm = owner / mfm_basename
                if src_mfm.is_file() and not target_mfm.is_file():
                    shutil.copy2(src_mfm, target_mfm)
                if not (owner / f"{diffuse_stem}_a.dd0").is_file() \
                        and not (owner / f"{diffuse_stem}_a.dds").is_file():
                    fill_patterns.append(f"**/{diffuse_stem}_a.dd?")
                    fill_patterns.append(f"**/{diffuse_stem}_a.dds")
                if not (owner / f"{mg_stem}_mg.dd0").is_file() \
                        and not (owner / f"{mg_stem}_mg.dds").is_file():
                    fill_patterns.append(f"**/{mg_stem}_mg.dd?")
                    fill_patterns.append(f"**/{mg_stem}_mg.dds")

        # Single supplemental extract for missing diffuse + mg.
        if fill_patterns:
            try:
                toolkit.extract(
                    fill_patterns,
                    scratch,
                    flatten=True,
                    config=config,
                )
            except Exception as e:
                print(
                    f"  warn: batch emissive supplemental extract failed "
                    f"for {label}: {e}",
                    file=sys.stderr,
                )

        # Distribute supplemental textures from scratch to owning dirs.
        # Each scratch file matches a `<stem>_<a|mg>.<ext>` pattern; map
        # the stem back to its owner via either stem_to_dir (Path A) or
        # Path B's per-dir diffuse/mg stems.
        path_b_stem_to_dir: dict[str, Path] = {}
        for owner, targets in path_b_by_dir.items():
            for diffuse_stem, mg_stem, _ in targets:
                path_b_stem_to_dir.setdefault(diffuse_stem, owner)
                path_b_stem_to_dir.setdefault(mg_stem, owner)
        for src in scratch.iterdir():
            if not src.is_file():
                continue
            name = src.name
            if name.endswith("_emissive.mfm") or name.endswith(".mfm"):
                # Both Path A overlays and Path B base mfms are
                # already copied above (per-asset).
                continue
            owner = None
            # First try Path A stems, then Path B stems.
            for stem, d in stem_to_dir.items():
                if name.startswith(f"{stem}_a.") or name.startswith(f"{stem}_mg."):
                    owner = d
                    break
            if owner is None:
                for stem, d in path_b_stem_to_dir.items():
                    if name.startswith(f"{stem}_a.") or name.startswith(f"{stem}_mg."):
                        owner = d
                        break
            if owner is None:
                continue
            target = owner / name
            if not target.is_file():
                shutil.copy2(src, target)

    # Phase 2: synth per-asset using the now-local files. Reuse the
    # single-dir helper without its extract pass — distribution above
    # already pulled in everything from the VFS. Each owner dir has its
    # own dedup set so Path A and Path B can both write to the same
    # canonical `<diffuse_stem>_emissive.{dd0,dds}` output without one
    # clobbering the other.
    synthesized_by_dir: dict[Path, set[str]] = {}

    def _batch_synth_one(
        owner: Path, stem: str, mg_stem: str, mfm_for_power: Path,
    ) -> None:
        already = synthesized_by_dir.setdefault(owner, set())
        if stem in already:
            return
        diff_dd0 = owner / f"{stem}_a.dd0"
        diff_low = owner / f"{stem}_a.dds"
        mg_dd0 = owner / f"{mg_stem}_mg.dd0"
        mg_low = owner / f"{mg_stem}_mg.dds"
        if not ((diff_dd0.is_file() or diff_low.is_file())
                and (mg_dd0.is_file() or mg_low.is_file())):
            print(
                f"  skip emissive synth for {stem}: missing _a.* or "
                f"{mg_stem}_mg.* (not in VFS for this stem)",
                file=sys.stderr,
            )
            return

        power = (
            wg_mfm.get_emissive_power(mfm_for_power, default=DEFAULT_EMISSIVE_POWER)
            if mfm_for_power.is_file() else DEFAULT_EMISSIVE_POWER
        )

        written: list[Path] = []
        if diff_dd0.is_file() and mg_dd0.is_file():
            out_dd0 = owner / f"{stem}_emissive.dd0"
            synth_emissive_dds(diff_dd0, mg_dd0, out_dd0, emissive_power=power)
            written.append(out_dd0)

        if diff_low.is_file() and mg_low.is_file():
            out_low = owner / f"{stem}_emissive.dds"
            synth_emissive_dds(diff_low, mg_low, out_low, emissive_power=power)
            written.append(out_low)

        already.add(stem)
        mg_note = f" (mg from {mg_stem})" if mg_stem != stem else ""
        print(
            f"  emissive synth: {stem}_emissive.{{dd0,dds}}  "
            f"(power={power:.2f}, {len(written)} mip(s)){mg_note}"
        )
        if written:
            results.setdefault(owner, []).extend(written)

    # Path A: ARP/Sabaton-style sibling `_emissive.mfm` overlays.
    for stem, owner in stem_to_dir.items():
        mfm = owner / f"{stem}_emissive.mfm"
        if mfm.is_file():
            _batch_synth_one(owner, stem, stem, mfm)

    # Path B: base-mfm shader-id detection. Skips entries already
    # synthesized via Path A (same diffuse_stem keys both).
    for owner, targets in path_b_by_dir.items():
        for diffuse_stem, mg_stem, mfm_basename in targets:
            _batch_synth_one(
                owner, diffuse_stem, mg_stem, owner / mfm_basename,
            )

    return results


def synth_emissive_dds_pyramid(
    diffuse_dd_paths: list[Path],
    mg_dd_paths: list[Path],
    out_dir: Path,
    out_stem: str,
    *,
    emissive_power: float = DEFAULT_EMISSIVE_POWER,
    pixel_format: str = "DXT1",
    mip_suffixes: tuple[str, ...] = (".dd0", ".dd1", ".dd2", ".dds"),
) -> list[Path]:
    """Synthesize a multi-mip emissive set, mirroring WG's ``.dd0`` /
    ``.dd1`` / ``.dd2`` / ``.dds`` mip pyramid convention.

    Inputs are mip-aligned lists of diffuse and ``_mg`` paths (same
    length, same indexing). Output goes to ``out_dir/<out_stem>_emissive<sfx>``
    for each ``sfx`` in ``mip_suffixes`` that has matching inputs.

    Returns the list of paths actually written.
    """
    written: list[Path] = []
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    n = min(len(diffuse_dd_paths), len(mg_dd_paths), len(mip_suffixes))
    for i in range(n):
        sfx = mip_suffixes[i]
        out_path = out_dir / f"{out_stem}_emissive{sfx}"
        synth_emissive_dds(
            diffuse_dd_paths[i], mg_dd_paths[i], out_path,
            emissive_power=emissive_power,
            pixel_format=pixel_format,
        )
        written.append(out_path)
    return written


__all__ = [
    "DEFAULT_EMISSIVE_POWER",
    "EMISSIVE_SHADER_ID_PREFIX",
    "synth_emissive",
    "synth_emissive_dds",
    "synth_emissive_dds_pyramid",
    "synthesize_emissive_textures",
    "synthesize_emissive_textures_batch",
]
