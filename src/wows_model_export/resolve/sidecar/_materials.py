"""Material + texture + skin discovery built on top of the hull GLB.

This module carries the heavy lifting that converts the toolkit's hull
GLB + raw DDS dump + ``material_mappings.json`` into the sidecar's
``materials[]`` / ``skins[]`` arrays. Functions split into three
groups:

- texture-set classifiers (per-DDS filename → ``(slot, scheme, role)``)
- :func:`materials_from_glb` — the main composer
- :func:`discover_skins_from_materials` — palette-aware skin builder

The functions read disk (GLB JSON chunk + DDS file scans) but do not
spawn subprocesses; the heavy `wowsunpack` work is upstream of this.
"""

from __future__ import annotations

import json
import re
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ._constants import (
    DDS_MIP_SUFFIXES,
    _CHANNEL_SUFFIX_MAP,
    _MFM_STRIP_SUFFIXES,
)
from ._makers import make_default_skin, make_skin

def _glb_json_chunk(glb_path: str | Path) -> dict[str, Any]:
    """Extract and parse the JSON chunk from a GLB file.

    Minimal standalone reader (no dependency on the glTF SDK) so this module
    stays stdlib-only.
    """
    import struct
    with open(glb_path, "rb") as f:
        magic = f.read(4)
        if magic != b"glTF":
            raise ValueError(f"{glb_path}: not a GLB file")
        _version, _total = struct.unpack("<II", f.read(8))
        json_len, json_type = struct.unpack("<II", f.read(8))
        if json_type != 0x4E4F534A:
            raise ValueError(f"{glb_path}: first chunk is not JSON")
        return json.loads(f.read(json_len).decode("utf-8"))


# Ordered glTF mip-suffix preferences for WG's split-mip DDS convention.
# `.dd0` = top mip (highest resolution); `.dd1`, `.dd2` are intermediate;
# `.dds` is the bundled low-res mip tail. Streaming-aware consumers pick
# the best available set for their quality tier. Ordering is high-to-low.

def _png_stem_to_dds_candidates(png_stem: str) -> list[str]:
    """Given a glTF PNG stem (no extension), produce DDS-side stem candidates.

    The toolkit's PNG output uses MFM-stem naming; the DDS dump uses the
    VFS texture filename verbatim. They differ in two ways:

    - **MFM suffix stripping** — PNG has `_skinned`/`_dead`/etc; DDS often
      strips them (`AGM034_..._skinned` MFM → `AGM034_..._a.dd0` DDS).
    - **Albedo-slot suffix** — for standard PBS materials the DDS has
      `_a` (`_..._a.dd0`); for CAMO variants the DDS has no `_a`
      (`_..._camo_01.dd0`). BaseColor slot must try both.

    Returns a list of stem candidates to try in order; first one whose
    mip files exist on disk wins.
    """
    # Identify the channel suffix (if any).
    channel_suffix = ""
    base = png_stem
    for png_sfx, dds_sfx in _CHANNEL_SUFFIX_MAP:
        if base.endswith(png_sfx):
            base = base[: -len(png_sfx)]
            channel_suffix = dds_sfx
            break

    # Enumerate bases. The MFM marker (`_skinned`/`_wire`/etc) lives at
    # the end for simple PBS slots (`foo_skinned_n`) but in the middle
    # for camo variants (`foo_skinned_camo_01_B`). We try both.
    bases = [base]
    for strip in _MFM_STRIP_SUFFIXES:
        # End-of-stem strip.
        if base.endswith(strip):
            bases.append(base[: -len(strip)])
        # Mid-stem strip: remove `{strip}_` anywhere it occurs (e.g.
        # `AGM034_..._skinned_camo_01_B` → `AGM034_..._camo_01_B`).
        token = strip + "_"
        if token in base:
            bases.append(base.replace(token, "_", 1).lstrip("_"))
            # Also try removing the FIRST occurrence + also the leading `_` it left.
            idx = base.find(token)
            if idx >= 0:
                variant = base[:idx] + base[idx + len(strip):]
                if variant != base and variant not in bases:
                    bases.append(variant)

    cands: list[str] = []
    for b in bases:
        if channel_suffix:
            # Non-baseColor: stem is just base + channel.
            cands.append(f"{b}{channel_suffix}")
        else:
            # BaseColor: try `_a` suffix first (standard PBS), then bare
            # (camo variants don't use `_a`).
            cands.append(f"{b}_a")
            cands.append(f"{b}")
    return cands


def _texture_manifest_for_slot(
    png_name: str | None,
    textures_dir: Path | None,
    textures_dds_dir: Path | None,
    *,
    png_uri_prefix: str = "textures/",
    dds_uri_prefix: str = "textures_dds/",
) -> dict[str, Any] | None:
    """Given a glTF image URI (e.g. ``textures/Foo_skinned_n.png``), build the
    per-slot texture manifest with relative paths to both the PNG and every
    available raw DDS mip level.

    Returns ``None`` when nothing resolves.
    """
    if not png_name:
        return None
    filename = Path(png_name).name
    png_stem = filename.rsplit(".", 1)[0]

    out: dict[str, Any] = {}
    if textures_dir is not None:
        png_path = textures_dir / filename
        if png_path.is_file():
            out["png"] = f"{png_uri_prefix}{filename}"

    # WG's raw DDS can live under a different stem than the PNG because
    # the toolkit's PNG export keeps the full MFM stem while the DDS
    # dump keeps the VFS filename. Try each candidate stem in order
    # until one produces mip files.
    if textures_dds_dir is not None:
        for dds_stem in _png_stem_to_dds_candidates(png_stem):
            dds_mips: list[str] = []
            for suffix in DDS_MIP_SUFFIXES:
                candidate = textures_dds_dir / f"{dds_stem}{suffix}"
                if candidate.is_file():
                    dds_mips.append(f"{dds_uri_prefix}{dds_stem}{suffix}")
            if dds_mips:
                out["dds_mips"] = dds_mips
                break  # first-hit wins (longest match if needed)

    return out or None


def texture_sets_from_dir(
    textures_dds_dir: str | Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> dict[str, dict[str, list[str]]]:
    """Enumerate DDS files in a directory and group them into a structured
    per-variant, per-slot texture manifest.

    The pipeline emits WG DDS files with canonical naming:

        <asset-stem>[<_variant>][<_camo_scheme>][<_channel>].<dd0|dd1|dd2|dds>

        where:
          variant   ∈ {"", "_dead"}                     (intact or destroyed)
          scheme    ∈ {"", "_camo_01", "_camo_01_B", …} (camo variant, if any)
          channel   ∈ {"_a", "_n", "_mg", "_ao"}        (PBR slot)
                      or omitted for camo base (which has no _a)

    We group on two dimensions:
      * **variant key** — what the ship looks like right now. ``"main"`` is
        the default intact + no-camo appearance. ``"dead"`` is the
        destroyed mesh's default appearance. ``"camo_01"`` /
        ``"camo_01_B"`` / etc. are per-camouflage overrides.
      * **slot name** — ``baseColor`` / ``normal`` / ``metallicRoughness``
        / ``occlusion``, matching the sidecar `materials[].textures` keys.

    Returns a dict shaped:

    .. code-block:: json

        {
          "main":      { "baseColor": ["<uri>", "<uri>", ...],
                         "normal":    [...], "metallicRoughness": [...],
                         "occlusion": [...] },
          "dead":      { "baseColor": [...], ... },
          "camo_01":   { "baseColor": [...] },
          "camo_01_B": { "baseColor": [...] }
        }

    Each slot's value is the mip-chain path list in priority order (top
    mip first). Consumers pick the top mip or let their importer stream
    through the chain.

    Returns an empty dict if the directory doesn't exist.
    """
    d = Path(textures_dds_dir)
    if not d.is_dir():
        return {}

    # Gather all DDS files, grouped by "stem" (filename minus the mip suffix).
    per_stem: dict[str, list[str]] = {}
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        stem: str | None = None
        for sfx in DDS_MIP_SUFFIXES:
            if name.endswith(sfx):
                stem = name[: -len(sfx)]
                break
        if stem is None:
            continue
        per_stem.setdefault(stem, []).append(f"{dds_uri_prefix}{name}")

    # Sort each stem's mips by DDS_MIP_SUFFIXES order (so the `.dd0` top mip
    # is always first). The paths already end in a suffix from the known set.
    for _stem, paths in per_stem.items():
        def mip_rank(p: str) -> int:
            for i, sfx in enumerate(DDS_MIP_SUFFIXES):
                if p.endswith(sfx):
                    return i
            return len(DDS_MIP_SUFFIXES)
        paths.sort(key=mip_rank)

    # Group stems into (variant, slot) using the shared classifier from
    # `_classify_dds_filename`. That function understands the full suffix
    # vocabulary including the toolkit-emitted conformant siblings
    # (`_normal`, `_mr`, `_nbmask`) and routes WG originals (`_n`, `_mg`)
    # to internal "raw" slot names that the finalisation step
    # (`_promote_legacy_raw_slots`) collapses.
    sets: dict[str, dict[str, list[str]]] = {}
    for stem, paths in per_stem.items():
        # `_classify_dds_filename` strips the channel suffix, normalises
        # the camo / dead scheme key, and returns (asset_base, scheme,
        # slot). We don't need asset_base here (the directory boundary
        # already isolates one asset's stems).
        _asset_base, scheme, slot = _classify_dds_filename(stem)
        sets.setdefault(scheme, {}).setdefault(slot, []).extend(paths)

    # Apply the same conformant-sibling-wins finalisation as the per-ship
    # binder. After this, raw slots (`_normalRawOrMgRaw_*`) are gone:
    # promoted to the canonical name when no conformant sibling is on
    # disk, dropped when one exists.
    for scheme_slots in sets.values():
        _promote_legacy_raw_slots_for_dir(scheme_slots)

    return sets


def _promote_legacy_raw_slots_for_dir(slots: dict[str, list[str]]) -> None:
    """Variant of [`_promote_legacy_raw_slots`] for the
    [`texture_sets_from_dir`] manifest shape (slot → list[str] of mip
    URIs, not slot → {dds_mips: [...]}). Same behaviour matrix.
    """
    for raw_slot in [s for s in slots if s.startswith(_LEGACY_RAW_SLOT_PREFIX)]:
        canonical = raw_slot[len(_LEGACY_RAW_SLOT_PREFIX):]
        raw_value = slots.pop(raw_slot)
        if canonical and canonical not in slots:
            slots[canonical] = raw_value


def materials_from_glb(
    glb_path: str | Path,
    *,
    textures_dir: str | Path | None = None,
    textures_dds_dir: str | Path | None = None,
    material_mappings_json: str | Path | None = None,
    png_uri_prefix: str = "textures/",
    dds_uri_prefix: str = "textures_dds/",
) -> list[dict[str, Any]]:
    """Extract an authoritative per-material texture manifest from a GLB +
    its sibling texture directories.

    The glTF file is read only for material names + PBR slot → image URI
    mappings + scalar factors + alpha/cull state. Each slot's manifest
    then carries BOTH the PNG reference (what the glTF spec allows) AND
    the raw WG DDS mip chain (for streaming-aware consumers).

    Consumers read this manifest and build materials programmatically
    instead of parsing the glTF's internal material section. That makes
    the consumer side robust against glTF-importer quirks and gives
    every mesh access to the full BC-compressed mip pyramid.

    Returns a list of material entries shaped per :func:`make_material`,
    with ``textures`` populated as a dict of slot → manifest:

    .. code-block:: json

        {
          "material_id":   "TL2_SHIPMAT_PBS_Gun_skinned",
          "display_name":  "TL2_SHIPMAT_PBS_Gun_skinned",
          "shader_intent": "opaque_pbr",
          "render_queue":  "opaque",
          "double_sided":  false,
          "mesh_slots":    [],
          "textures": {
            "baseColor": {
              "png": "textures/Foo_a.png",
              "dds_mips": ["textures_dds/Foo_a.dd0", "textures_dds/Foo_a.dd1",
                           "textures_dds/Foo_a.dd2", "textures_dds/Foo_a.dds"]
            },
            "normal":            { ... },
            "metallicRoughness": { ... },
            "occlusion":         { ... }
          },
          "factors": {
            "baseColor":         [1.0, 1.0, 1.0, 1.0],
            "metallic":          1.0,
            "roughness":         1.0,
            "emissive":          [0.0, 0.0, 0.0]
          },
          "uv_channels": {}
        }
    """
    doc = _glb_json_chunk(glb_path)
    tex_dir = Path(textures_dir) if textures_dir is not None else None
    dds_dir = Path(textures_dds_dir) if textures_dds_dir is not None else None

    gltf_mats = doc.get("materials", []) or []
    gltf_texs = doc.get("textures", []) or []
    gltf_imgs = doc.get("images", []) or []

    def tex_uri(slot_ref: dict[str, Any] | None) -> str | None:
        """Resolve a glTF material slot (`{"index": N, "texCoord": 0, ...}`)
        to the image's URI field. Returns None if unresolvable."""
        if not slot_ref or "index" not in slot_ref:
            return None
        tex_idx = slot_ref["index"]
        if tex_idx >= len(gltf_texs):
            return None
        img_idx = gltf_texs[tex_idx].get("source")
        if img_idx is None or img_idx >= len(gltf_imgs):
            return None
        return gltf_imgs[img_idx].get("uri")

    out: list[dict[str, Any]] = []
    for mat in gltf_mats:
        name = mat.get("name") or f"material_{len(out)}"
        pbr = mat.get("pbrMetallicRoughness", {})

        # Resolve each slot → image URI → sidecar texture manifest.
        slots: dict[str, dict[str, Any]] = {}
        for slot_name, slot_ref in (
            ("baseColor", pbr.get("baseColorTexture")),
            ("metallicRoughness", pbr.get("metallicRoughnessTexture")),
            ("normal", mat.get("normalTexture")),
            ("occlusion", mat.get("occlusionTexture")),
            ("emissive", mat.get("emissiveTexture")),
        ):
            uri = tex_uri(slot_ref)
            if not uri:
                continue
            manifest = _texture_manifest_for_slot(
                uri, tex_dir, dds_dir,
                png_uri_prefix=png_uri_prefix,
                dds_uri_prefix=dds_uri_prefix,
            )
            if manifest:
                slots[slot_name] = manifest

        factors: dict[str, Any] = {}
        if "baseColorFactor" in pbr:
            factors["baseColor"] = [float(v) for v in pbr["baseColorFactor"]]
        if "metallicFactor" in pbr:
            factors["metallic"] = float(pbr["metallicFactor"])
        if "roughnessFactor" in pbr:
            factors["roughness"] = float(pbr["roughnessFactor"])
        if "emissiveFactor" in mat:
            factors["emissive"] = [float(v) for v in mat["emissiveFactor"]]

        # Derive a schema-valid shader_intent from the glTF alpha mode.
        # VALID_SHADER_INTENTS constrains the output vocabulary.
        alpha_mode = mat.get("alphaMode", "OPAQUE")
        if alpha_mode == "BLEND":
            shader_intent = "transparent"
            render_queue = "transparent"
        elif alpha_mode == "MASK":
            shader_intent = "cutout"
            render_queue = "cutout"
        else:
            shader_intent = "opaque_pbr"
            render_queue = "opaque"

        # v3: wrap the resolved slots under `texture_sets["main"]`. Per-camo
        # variants are added by `_bind_dds_textures_by_name()` below.
        entry = {
            "material_id": name,
            "display_name": name,
            "shader_intent": shader_intent,
            "render_queue": render_queue,
            "double_sided": bool(mat.get("doubleSided", False)),
            "mesh_slots": [],
            "texture_sets": {"main": slots} if slots else {},
            "factors": factors,
            "uv_channels": {},
        }
        out.append(entry)

    # DDS-only mode: when the glTF has no texture references, populate
    # `texture_sets[*]` from the on-disk DDS dir. Two passes:
    #
    #   1. Toolkit-emitted material_mappings.json (deterministic, when
    #      available). Pre-fills `texture_sets["main"]` from authoritative
    #      .mfm material descriptors — every material identifier maps to
    #      a definite mfm_stem regardless of WG's filename quirks.
    #
    #   2. Filename-heuristic resolver (`_bind_dds_textures_by_name`).
    #      Catches materials the deterministic pass didn't cover (older
    #      ships exported pre-toolkit-feature, library shaders not in
    #      .visual chain, ...) AND fills in per-camo + dead variants on
    #      every material (the JSON only carries the main appearance —
    #      camos are a disk-side discovery).
    #
    # Either pass alone is functional; running both gives best coverage.
    if dds_dir is not None and any(not m.get("texture_sets", {}).get("main") for m in out):
        if material_mappings_json is not None:
            _apply_material_mappings_json(
                out, material_mappings_json, dds_dir,
                dds_uri_prefix=dds_uri_prefix,
            )
        unresolved = _bind_dds_textures_by_name(out, dds_dir, dds_uri_prefix=dds_uri_prefix)
        # Surface opaque-PBR / cutout materials neither resolver could find
        # a stem for — they're almost always new WG-library material variants
        # we haven't taught `_LIBRARY_MATERIAL_STEMS` about yet. Hitboxes and
        # armor materials are `shader_intent: transparent` so they don't reach
        # the resolver in the first place and correctly don't show up here.
        if unresolved:
            import sys
            uniq = sorted(set(unresolved))
            print(
                f"[sidecar] WARNING: {len(uniq)} material(s) unresolved — "
                f"no matching DDS stem. These will render untextured downstream: "
                f"{', '.join(uniq)}",
                file=sys.stderr,
            )

    return out


# -- DDS stem ↔ material name resolver ---------------------------------------
#
# glTF material names follow WG's convention `<SHADER>_PBS_<Part>`:
#   SHIPMAT_PBS_Hull        → base ship texture (no _<Part> suffix in DDS)
#   SHIPMAT_PBS_DeckHouse   → base stem + "_Deckhouse" (case-insensitive)
#   SHIPWIRE_PBS_Hull       → dead/wireframe variant of the hull
#   SHIPMAT_PBS_Crack       → shared-across-ships damage-interior texture.
#                             WG's asset library stores this as `CxxxRazlom*`
#                             (Russian "разлом" = cleavage / fracture) —
#                             Montana ships `C002_Razlom`, Baltimore ships
#                             `C003_Razlom_old`. The `_HINT_ALIASES` table
#                             below maps the material-name hint to the
#                             DDS-stem substring we should look for.
#   armor_*, hitbox_*       → vertex-colored, no PBR textures
#
# Some ships (Iowa → `TL2_SHIPMAT_PBS_Hull`, Fletcher → `TL2_SHIPWIRE_PBS_Hull`)
# prefix the shader with `TL<N>_`. Observed values so far are TL2 only; the
# prefix appears to tag a texture-level / quality variant but the material
# semantics and the DDS stem layout are identical to the un-prefixed form,
# so we strip it before matching. Without stripping, the hull bindings
# silently disappear for those ships.
#
# We index the DDS directory by stem (filename minus mip suffix minus channel
# suffix), then for each opaque_pbr material extract a "part hint" from its
# name and look up the matching stem.

_SHADER_PREFIXES = (
    # Order matters: longest prefix first so SHIPMAT_EMISSIVE_PBS_ matches
    # before the bare SHIPMAT_PBS_ would (it wouldn't, but defensively).
    "SHIPMAT_EMISSIVE_PBS_", "shipmat_emissive_pbs_",
    "SHIPMAT_PBS_", "SHIPWIRE_PBS_", "shipmat_pbs_", "shipwire_pbs_",
)

# When the direct `<base_stem>_<hint>` / `_<hint>` match fails, try each alias
# substring in order. Match is lowercased + substring-in-stem; first match
# wins. Keys are lowercased hint strings (e.g. "crack" for SHIPMAT_PBS_Crack).
_HINT_ALIASES: dict[str, tuple[str, ...]] = {
    "crack": ("razlom",),
    # Atago: WG names the deckhouse DDS `Deck_house` (snake_case) instead of
    # the `Deckhouse` (run-together) used by every other ship. Without this
    # alias the SHIPMAT_PBS_DeckHouse material on Atago resolves to no stem
    # and ships with empty texture_sets.
    "deckhouse": ("deck_house",),
    # Myoko: SHIPMAT_PBS_Bulge is bound to ~50 hull mesh primitives (the
    # Bow_BuglesShape / MidFront_BuglesShape "torpedo-blister" sections
    # all along the lower hull), but its textures are named ``_bools_*``
    # instead of ``_bulge_*`` — Russian-derived "бугель" via "bools".
    # Additionally, the textures use the JSC032_Myoko_1941 hull prefix
    # (the 1941 ancestor) on every Myoko variant: WG ships these once and
    # reuses them across every Myoko hull tier (1945, ARP). Without the
    # ``bools`` alias the Bulge material binds to no stem and the
    # underwater hull renders as solid grey.
    "bulge": ("bools",),
}

# Library-material aliases. WG ships several "shared" materials that are not
# per-ship (don't use the SHIPMAT_/SHIPWIRE_ shader prefix) — their MFMs live
# under `content/gameplay/common/textures/` and the DDS stems don't include
# any ship-specific tokens. Key is a lowercase prefix that must appear at the
# start of the material name; value is (stem_tokens, shader_intent). First
# prefix-match wins, then first stem-token that resolves. When a library
# material resolves here, we bind whatever channels the stem exposes
# (typically only baseColor — these are usually single-map alpha-tested
# shaders like nets and glass panes) AND override the material's
# shader_intent + render_queue based on the entry's classification, because
# the source GLB flags them all as OPAQUE even though the underlying WG
# shader (`assets.bin shader_id=0x00010000`) needs alpha handling.
#
# Observed so far:
#   GRID_Misc (Fletcher)  → MFM C008_Grid_5_alpha      → stem contains "grid"
#   GRID_Misc (Baltimore) → MFM C001_Net_alpha         → stem contains "net"
#   SHIPGLASS_PBS_Hull    → MFM transparent_glass_alpha → stem contains "glass"
# The same material name can point at different library MFMs across ships;
# the tuple of fallback tokens captures that spread.
_LIBRARY_MATERIAL_STEMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # name_prefix,  stem_tokens,       shader_intent
    ("grid_",       ("grid", "net"),   "cutout"),       # nets: alpha-tested
    ("shipglass_",  ("glass",),        "transparent"),  # glass: alpha-blended
)

# Shared-library token list used by base-stem ranking + permoflage
# fallback resolver to skip stems that aren't ship-owned (damage
# interior, glass, nets, defaults, transparent surfaces). Mirror of
# `_HINT_ALIASES` keys + `_LIBRARY_MATERIAL_STEMS` tokens.
_SHARED_LIBRARY_TOKENS: tuple[str, ...] = (
    "razlom", "glass", "grid", "net", "default", "transparent",
)


def _is_library_stem(stem: str) -> bool:
    """True if ``stem`` matches a known shared-library token (damage
    interior / glass / nets / defaults). Used to keep permoflage
    fallback search from latching onto cross-ship texture sources."""
    return any(t in stem for t in _SHARED_LIBRARY_TOKENS)

# Channel suffix → glTF slot name. Order matters in two ways:
#
# 1. Longer/more-specific suffixes must come first so we don't strip the
#    wrong one. `_normal` must be checked before `_n`, `_mr` before `_mg`,
#    `_nbmask` is unique.
# 2. WG-original `_n` / `_mg` files map to "raw" slot names that the
#    finalisation step (`_promote_legacy_raw_slots`) demotes/promotes
#    based on whether the conformant sibling is present. Keeps backwards
#    compat: a ship extracted with the pre-2026-04-30 toolkit still binds
#    correctly via the WG originals; a ship extracted with the new
#    toolkit binds the conformant siblings and ignores the originals.
#
# Conformant siblings (toolkit-emitted, glTF-spec):
#   `_normal` → tangent-space normal map (B = reconstructed Z)
#   `_nbmask` → camo no-camo-region mask (BC4 single-channel)
#   `_mr`     → metallic-roughness (G = roughness, B = metallic)
#
# WG originals (kept for archaeology / RE — see
# `tools/reference/shared/texture_conventions.md`):
#   `_n`      → carries categorical mask in B (NOT Z) — wrong for shading
#   `_mg`     → R cavity / G metallic / B gloss — non-glTF channel order
_DDS_CHANNEL_TO_SLOT: tuple[tuple[str, str], ...] = (
    ("_emissive", "emissive"),    # synthesized by tools/shared/synth_emission.py
    ("_normal",   "normal"),
    ("_nbmask",   "camoMask"),
    ("_mr",       "metallicRoughness"),
    ("_mg",       "_normalRawOrMgRaw_metallicRoughness"),  # demoted in finalisation
    ("_ao",       "occlusion"),
    ("_n",        "_normalRawOrMgRaw_normal"),              # demoted in finalisation
    ("_a",        "baseColor"),
)

# Slots whose name starts with this prefix are treated as legacy-raw and
# get promoted to their canonical slot name in finalisation **only if**
# the canonical slot is empty (i.e. no conformant sibling shipped).
_LEGACY_RAW_SLOT_PREFIX = "_normalRawOrMgRaw_"


_YEAR_TOKEN_RE = re.compile(r"_\d{4}(?=_|$)")


def _strip_year_token(stem: str) -> str:
    """Drop a `_<4digit>` year token (boundaries: `_` or end-of-string)
    anywhere in the stem.

    WG names some camo files without the year suffix that the base
    albedo files carry. E.g. Baltimore ships ``ASC017_Baltimore_1944_a.dd0``
    but ``ASC017_Baltimore_camo_01.dd0`` — both refer to the same hull
    but the camo path drops `_1944`. Canonicalising stems with this
    helper lets the per-scheme grouping line both files up under one
    material. Yamato + Iowa carry `_1945`/`_1944` consistently across
    both files, so stripping is a uniform no-op for them. Montana has
    no year in its texture stems, so stripping is also a no-op.

    The regex matches a 4-digit token at a `_` boundary, so 2-digit
    calibres (`16in50`, `Mk32`) and 5-digit IDs (`AB12345`) don't fire.
    """
    return _YEAR_TOKEN_RE.sub("", stem, count=1)


def _classify_dds_filename(stem_and_channel: str) -> tuple[str, str, str]:
    """Decompose a DDS filename (without mip suffix) into
    ``(material_stem, scheme_key, slot)``.

    Examples:
        ``ASB017_Montana_a``                     → ("asb017_montana", "main",            "baseColor")
        ``ASB017_Montana_n``                     → ("asb017_montana", "main",            "normal")
        ``ASB017_Montana_camo_01``               → ("asb017_montana", "camo_01",         "baseColor")
        ``ASB017_Montana_camo_01_B``             → ("asb017_montana", "camo_01_B",       "baseColor")
        ``ASB017_Montana_Deckhouse_camo_01``     → ("asb017_montana_deckhouse", "camo_01", "baseColor")
        ``ASB017_Montana_dead_a``                → ("asb017_montana", "dead",            "baseColor")
        ``ASB017_Montana_dead_camo_01``          → ("asb017_montana", "dead_camo_01",    "baseColor")
        ``ASC017_Baltimore_1944_a``              → ("asc017_baltimore", "main",          "baseColor")  ← year stripped
        ``ASC017_Baltimore_camo_01``             → ("asc017_baltimore", "camo_01",       "baseColor")  ← already without year

    Camo variants don't carry an `_a` channel suffix in WG's naming
    (they're authored as the baseColor directly), so the channel
    detection loop comes BEFORE the camo/dead detection. The `_<year>`
    token is stripped from the material stem at the end so WG's
    inconsistent year-suffix authoring (e.g. Baltimore) doesn't
    fragment the stem index.
    """
    rest = stem_and_channel

    # 1. Trailing channel suffix (only present on `_a`/`_n`/`_mg`/`_ao`
    #    files; camo variants without a channel default to baseColor).
    slot = "baseColor"
    for chan, slot_name in _DDS_CHANNEL_TO_SLOT:
        if rest.endswith(chan):
            slot = slot_name
            rest = rest[: -len(chan)]
            break

    # 2. Camo scheme — `_camo_<rest>` token. Everything after `_camo_` is
    #    the scheme suffix (e.g. "01", "01_B", "Halloween20").
    scheme = "main"
    low = rest.lower()
    if "_camo_" in low:
        idx = low.index("_camo_")
        camo_suffix = rest[idx + len("_camo_"):]
        scheme = f"camo_{camo_suffix}"
        rest = rest[:idx]
        low = rest.lower()

    # 3. Dead variant. Can stack with camo (e.g. `_dead_camo_01_B`).
    if low.endswith("_dead"):
        rest = rest[: -len("_dead")]
        scheme = "dead" if scheme == "main" else f"dead_{scheme}"

    # 4. Strip year token so WG's inconsistent year-suffixing doesn't
    #    fragment the stem index. Mirrors the Rust toolkit's
    #    `texture_base_names` helper.
    rest = _strip_year_token(rest)

    return rest.lower(), scheme, slot


_MFM_PROP_TO_PBR_SLOTS: dict[str, tuple[str, ...]] = {
    # MFM property → canonical PBR slots that come from that texture's
    # channel family. Walking per-MFM-property lets mesh-swap variants
    # (where each slot can come from a different stem) bind correctly:
    # ARP Takao Red's Hull material has diffuse=JSC508_Red_Arpeggio,
    # mg=JSC507_Arpeggio (Blue inheritance), normal+ao=JSC038_Atago (base).
    #
    # `_n` files carry both shading normal AND camo no-camo-region mask
    # (the conformant `_normal` + `_nbmask` siblings split them); we pull
    # all three slots from the normalMap's stem so camoMask follows the
    # base ship's UV layout.
    #
    # `_normalRawOrMgRaw_*` slots are populated only when the toolkit
    # extracted WG-original `_n` / `_mg` without conformant siblings;
    # `_promote_legacy_raw_slots` (called in finalisation) collapses them
    # onto the canonical name.
    "diffuseMap":           ("baseColor",),
    "normalMap":            ("normal", "_normalRawOrMgRaw_normal", "camoMask"),
    "metallicGlossMap":     ("metallicRoughness", "_normalRawOrMgRaw_metallicRoughness"),
    "ambientOcclusionMap":  ("occlusion",),
}


def _apply_material_mappings_json(
    materials: list[dict[str, Any]],
    material_mappings_json: str | Path,
    textures_dds_dir: Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> int:
    """Pre-fill ``materials[*].texture_sets["main"]`` from the toolkit's
    deterministic material → texture-stem mapping JSON.

    The JSON's ``materials[*].textures`` dict carries one entry per MFM
    property (``diffuseMap`` / ``normalMap`` / ``metallicGlossMap`` /
    ``ambientOcclusionMap``) with the exact ``stem`` and ``vfs_path``
    WG resolved for that slot. For mesh-swap permoflage variants (Takao
    Arpeggio, ARP Takao Red, Iowa AzurLane, …), each slot can point at a
    different source stem — the variant ships only a partial texture
    set and inherits the rest from the base ship + a sibling variant.
    Walking per-slot (not per-material-mfm_stem) is the only way to
    honour that inheritance correctly.

    For each material:
      1. Look up the matching ``materials[]`` entry by
         ``material_identifier`` (case + TL-prefix tolerant).
      2. Skip if the entry's ``mfm_stem`` is a shared library stem
         (C002_Razlom etc.) — those are handled by the heuristic's
         ``_LIBRARY_MATERIAL_STEMS`` table.
      3. For each MFM property we recognise, take the per-slot ``stem``,
         normalise (lowercase + ``_strip_year_token``), and pull the
         canonical PBR slots from the dds_index built earlier.
      4. Synthesised emissive is keyed off the diffuseMap stem (synth
         writes ``<diffuse_stem>_emissive.dd?`` in
         ``textures_dds_dir``); we look up ``emissive`` under that stem.

    Mutates ``materials`` in place. Returns the count of materials that
    got a main appearance from this pass. Materials whose stems are
    library / unrecognised / missing DDS on disk are left untouched and
    fall through to the heuristic resolver, which still adds per-camo /
    dead schemes from disk.

    See ``write_material_mappings_json`` in the toolkit's
    ``crates/wowsunpack/src/export/ship.rs`` for the JSON shape.
    """
    import sys
    from collections import defaultdict

    p = Path(material_mappings_json)
    if not p.is_file():
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[sidecar] WARNING: material_mappings_json {p} unreadable ({e}); "
            f"falling back to heuristic resolver only",
            file=sys.stderr,
        )
        return 0

    # Build index: material_identifier (lowercase, post TL\d+_ strip) →
    # list of full entry dicts. Each glTF material name maps to ONE or
    # MORE entries (one per sub-model the same material is used in).
    # We pick the first non-library entry; the per-slot stems are read
    # from that entry's ``textures`` dict.
    by_ident: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in doc.get("materials", []):
        ident = (entry.get("material_identifier") or "").strip()
        if not ident:
            continue
        # Drop optional TL\d+_ prefix to align with glTF material names
        # that ship pre-stripped (Iowa: TL2_SHIPMAT_PBS_Hull → SHIPMAT_PBS_Hull
        # if the toolkit ever stripped it). Today the toolkit emits the
        # raw identifier so we index BOTH forms.
        norm = _strip_tl_prefix(ident).lower()
        by_ident[norm].append(entry)
        if norm != ident.lower():
            by_ident[ident.lower()].append(entry)

    if not by_ident:
        return 0

    # Build a stem-channel-mip index so we can resolve `<stem>_<channel>.dd?`
    # paths without rescanning the directory per material. Key is the
    # ``_classify_dds_filename`` stem (lowercase + year-stripped); slot
    # is the canonical PBR slot name (or `_normalRawOrMgRaw_*` for raw
    # WG `_n` / `_mg` files awaiting promotion in finalisation).
    dds_index: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for f in sorted(textures_dds_dir.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        mip_sfx = next((s for s in DDS_MIP_SUFFIXES if name.endswith(s)), None)
        if mip_sfx is None:
            continue
        stem_and_channel = name[: -len(mip_sfx)]
        if "_blaze" in stem_and_channel.lower():
            continue
        # Per-scheme variants (camo_NN, dead, dead_camo_NN) are handled
        # by the heuristic resolver's `_bind_dds_textures_by_name`, which
        # buckets by scheme. The deterministic pass only covers the
        # "main" appearance — let the classifier decide. Note: a bare
        # `endswith("_dead")` check would miss `<stem>_dead_<channel>`
        # files (e.g. `JGM024_..._RF_dead_mr`), which DO classify as
        # scheme=dead but end in `_mr` not `_dead`. Trust the classifier
        # output instead of the suffix shape.
        stem, scheme, slot = _classify_dds_filename(stem_and_channel)
        if scheme != "main":
            continue
        dds_index[stem][slot].append(f"{dds_uri_prefix}{name}")

    def _mip_rank(path: str) -> int:
        for i, sfx in enumerate(DDS_MIP_SUFFIXES):
            if path.endswith(sfx):
                return i
        return len(DDS_MIP_SUFFIXES)

    for slots_ in dds_index.values():
        for paths in slots_.values():
            paths.sort(key=_mip_rank)

    def _normalise_stem(raw: str) -> tuple[str, str]:
        """Return (lower, year_stripped_lower) candidate keys for the
        given raw stem from material_mappings.json."""
        low = raw.lower()
        return low, _strip_year_token(low)

    n_resolved = 0
    for mat in materials:
        if mat.get("shader_intent") not in ("opaque_pbr", "cutout"):
            continue
        existing_main = (mat.get("texture_sets") or {}).get("main") or {}
        if existing_main:
            continue  # already populated by the GLB-driven path

        raw_id = mat.get("material_id") or ""
        norm = _strip_tl_prefix(raw_id).lower()
        entries = by_ident.get(norm) or by_ident.get(raw_id.lower()) or []
        if not entries:
            continue

        # Pick the first non-library entry. Library mfm_stems
        # (C002_Razlom — shared damage interior, transparent_glass_alpha,
        # …) are handled by the heuristic's `_LIBRARY_MATERIAL_STEMS`
        # table which also fixes up shader_intent. Letting them fall
        # through preserves that path.
        chosen: dict[str, Any] | None = None
        for entry in entries:
            mfm_stem_low = (entry.get("mfm_stem") or "").lower()
            if mfm_stem_low and _is_library_stem(mfm_stem_low):
                continue
            chosen = entry
            break
        if chosen is None:
            continue

        textures = chosen.get("textures") or {}
        if not textures:
            continue

        # Walk per-MFM-property stems and pull canonical slots.
        slots: dict[str, dict[str, list[str]]] = {}
        diffuse_keys: tuple[str, str] | None = None
        for mfm_prop, pbr_slots in _MFM_PROP_TO_PBR_SLOTS.items():
            tex = textures.get(mfm_prop) or {}
            stem_raw = (tex.get("stem") or "").strip()
            if not stem_raw:
                continue
            # Skip library / shared-default per-slot stems (e.g. an AO
            # slot pointing at "default"). The heuristic / library
            # fallbacks pick the right defaults; binding `default_ao`
            # here would just attach a generic occlusion that overrides
            # the variant's actual AO from another stem.
            if _is_library_stem(stem_raw.lower()):
                continue
            for key in _normalise_stem(stem_raw):
                if key not in dds_index:
                    continue
                slot_table = dds_index[key]
                for pbr_slot in pbr_slots:
                    if pbr_slot in slots:
                        continue  # earlier slot from this prop already won
                    paths = slot_table.get(pbr_slot)
                    if paths:
                        slots[pbr_slot] = {"dds_mips": list(paths)}
                if mfm_prop == "diffuseMap":
                    diffuse_keys = (key,) if diffuse_keys is None else diffuse_keys
                break  # stop at first matching key (raw vs. year-stripped)

        # Synthesised emissive: `tools/shared/synth_emission.py` writes
        # `<diffuse_stem>_emissive.{dd0,dds}` for any stem with a sibling
        # `*_emissive.mfm` in the VFS (ARP / Azur Lane / Sabaton crossover
        # skins). The classifier indexes those under the diffuse stem
        # keyed `emissive`. For mesh-swap variants the diffuse stem is
        # the variant's (e.g. JSC508_Red_Arpeggio), so emissive follows
        # the variant's UV automatically.
        if diffuse_keys:
            for key in diffuse_keys:
                if key in dds_index and "emissive" in dds_index[key]:
                    slots["emissive"] = {"dds_mips": list(dds_index[key]["emissive"])}
                    break
        else:
            diffuse_tex = textures.get("diffuseMap") or {}
            stem_raw = (diffuse_tex.get("stem") or "").strip()
            if stem_raw and not _is_library_stem(stem_raw.lower()):
                for key in _normalise_stem(stem_raw):
                    if key in dds_index and "emissive" in dds_index[key]:
                        slots["emissive"] = {"dds_mips": list(dds_index[key]["emissive"])}
                        break

        # Require at least baseColor before claiming we resolved the
        # material. Otherwise the heuristic should still get a shot —
        # e.g. a material whose JSON entry only lists shared `default`
        # AO + glass-token diffuse falls through cleanly.
        if "baseColor" not in slots:
            continue

        ts = mat.setdefault("texture_sets", {})
        ts["main"] = slots
        n_resolved += 1

    return n_resolved


def _strip_tl_prefix(name: str) -> str:
    """Drop a leading ``TL<N>_`` texture-level prefix (Iowa / Fletcher use
    ``TL2_SHIPMAT_PBS_Hull`` instead of bare ``SHIPMAT_PBS_Hull``). No-op
    if the prefix isn't present."""
    if len(name) > 3 and name[:2].upper() == "TL" and name[2].isdigit():
        i = 3
        while i < len(name) and name[i].isdigit():
            i += 1
        if i < len(name) and name[i] == "_":
            return name[i + 1:]
    return name


def _bind_dds_textures_by_name(
    materials: list[dict[str, Any]],
    textures_dds_dir: Path,
    *,
    dds_uri_prefix: str = "textures_dds/",
) -> list[str]:
    """Populate ``materials[*].texture_sets`` by matching material names to
    the on-disk DDS stems. Mutates the list in place.

    Acts on ``opaque_pbr`` / ``cutout`` materials whose ``texture_sets``
    is empty or missing its ``main`` entry. The "main" appearance is
    populated from `<stem>_a/_n/_mg/_ao` files; per-camo + dead variants
    are bucketed under their scheme key (`camo_01`, `dead`,
    `dead_camo_01`, etc.). Unresolvable materials are left untouched and
    their ``material_id`` is returned as a list; callers should log /
    act on this to surface new shared-library variants the resolver
    doesn't yet know about.
    """
    from collections import defaultdict

    unresolved: list[str] = []

    # 1. Index: stem_lower → scheme_key → slot → [mip paths sorted top-first]
    stem_index: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for f in sorted(textures_dds_dir.iterdir()):
        if not f.is_file():
            continue
        name = f.name

        # Strip mip suffix (.dd0/.dd1/.dd2/.dds).
        mip_sfx = next((s for s in DDS_MIP_SUFFIXES if name.endswith(s)), None)
        if mip_sfx is None:
            continue
        stem_and_channel = name[: -len(mip_sfx)]

        # Skip blaze (camo-on-fire variants — present on some WG ships,
        # render-time-only, not part of player-selectable skin set).
        if "_blaze" in stem_and_channel.lower():
            continue

        stem, scheme, slot = _classify_dds_filename(stem_and_channel)
        stem_index[stem][scheme][slot].append(f"{dds_uri_prefix}{name}")

    def _mip_rank(p: str) -> int:
        for i, sfx in enumerate(DDS_MIP_SUFFIXES):
            if p.endswith(sfx):
                return i
        return len(DDS_MIP_SUFFIXES)

    for schemes in stem_index.values():
        for slots in schemes.values():
            for paths in slots.values():
                paths.sort(key=_mip_rank)

    if not stem_index:
        return unresolved

    # 2. "Base stem" = hull-wide texture stem. Heuristic: among stems that
    # carry a `main`/baseColor slot (so utility-only files like `default_ao`
    # don't win), prefer the one with the most main-scheme slots; break
    # ties on fewest underscore tokens, then shortest length. That picks
    # e.g. `ASB017_Montana` over both `default` (no baseColor) and
    # `ASB017_Montana_Deckhouse` (deeper part suffix).
    #
    # Exclude shared-library tokens from the candidate pool. Stems like
    # `c002_razlom` (damage interior) carry a full slot set and would
    # otherwise outrank a permoflage variant whose own diffuse/mg+mr live
    # alongside but whose normal/AO are inherited from the base ship's
    # stem (so the variant gets only ~4 slots in its own stem). Without
    # the exclusion, every `Hull` material on a permoflage binds to
    # `c002_razlom` instead of the variant.
    candidates = [
        s for s, schemes in stem_index.items()
        if "baseColor" in schemes.get("main", {})
        and not _is_library_stem(s)
    ]
    if not candidates:
        # Fallback: if filtering left nothing (e.g. a ship that genuinely
        # only uses razlom), allow library stems back in. Better to bind
        # to a wrong stem than to leave every material untextured.
        candidates = [
            s for s, schemes in stem_index.items()
            if "baseColor" in schemes.get("main", {})
        ]
    if not candidates:
        return unresolved
    base_stem = max(
        candidates,
        key=lambda s: (
            len(stem_index[s].get("main", {})),
            -s.count("_"),
            -len(s),
        ),
    )

    # 3. Resolve each opaque material to its stem. We DON'T early-skip
    #    materials that already have `texture_sets["main"]` from a
    #    GLB-driven resolution — they still need their per-camo +
    #    `_dead` scheme variants attached from the disk scan.
    for mat in materials:
        if mat.get("shader_intent") not in ("opaque_pbr", "cutout"):
            continue
        existing_main = (mat.get("texture_sets") or {}).get("main") or {}

        raw_name = mat.get("material_id") or ""
        # Strip optional leading `TL\d+_` texture-level prefix (see header
        # comment). "TL2_SHIPMAT_PBS_Hull" → "SHIPMAT_PBS_Hull".
        name = raw_name
        if len(name) > 3 and name[:2].upper() == "TL" and name[2].isdigit():
            i = 3
            while i < len(name) and name[i].isdigit():
                i += 1
            if i < len(name) and name[i] == "_":
                name = name[i + 1:]

        part_hint: str | None = None
        for prefix in _SHADER_PREFIXES:
            if name.startswith(prefix):
                part_hint = name[len(prefix):]
                break
        if part_hint is None:
            # Not a SHIPMAT_/SHIPWIRE_ material. Before giving up, try the
            # library-material table — some shared materials (GRID_Misc,
            # SHIPGLASS_PBS_Hull, …) live outside the SHIPMAT namespace but
            # still point at DDS stems we can find.
            name_low = name.lower()
            lib_target: str | None = None
            lib_intent: str | None = None
            for lib_prefix, stem_tokens, intent in _LIBRARY_MATERIAL_STEMS:
                if name_low.startswith(lib_prefix):
                    for token in stem_tokens:
                        lib_target = next(
                            (s for s in stem_index if token in s),
                            None,
                        )
                        if lib_target is not None:
                            lib_intent = intent
                            break
                    break
            if lib_target is None:
                if not existing_main:
                    unresolved.append(raw_name)
                continue
            # Library materials are typically schemeless (one stem, one
            # scheme = main). Project the stem's per-scheme slots into
            # `texture_sets`; if WG ever ships e.g. a camo'd net, the
            # scheme keys flow through naturally. Existing GLB-derived
            # main wins for slots it already resolved; disk fills in the
            # rest.
            _merge_multi_targets(mat, [stem_index[lib_target]], existing_main)
            # Library materials ship through the GLB as alphaMode=OPAQUE even
            # though the underlying WG shader (0x00010000) is alpha-tested /
            # blended. Override based on the entry's classification so the
            # downstream consumer picks the right shader mode.
            if lib_intent is not None:
                mat["shader_intent"] = lib_intent
                mat["render_queue"]  = lib_intent
            continue

        targets = _resolve_target_stems(
            part_hint, base_stem, stem_index,
        )
        if not targets:
            if not existing_main:
                unresolved.append(raw_name)
            continue

        # Project the stems' per-scheme slots into `texture_sets`,
        # earlier stems winning per-slot. Mesh-swap permoflage variants
        # (Takao Arpeggio, Iowa AzurLane, ...) get [variant_stem,
        # base_ship_stem]; the variant carries baseColor / mg / mr / and
        # synthesised emissive on its own stem, while normal / camoMask /
        # AO are inherited from the base ship's stem. Self-contained
        # ships get a single-element list and behave as before.
        target_schemes = [stem_index[t] for t in targets if t in stem_index]
        _merge_multi_targets(mat, target_schemes, existing_main)

    # Finalisation pass: collapse legacy-raw slots (`_n` / `_mg` files)
    # onto their canonical names only when the conformant sibling is
    # absent. New extracts (with `_normal` / `_mr` siblings on disk)
    # ignore the originals; old extracts (no siblings) fall back to the
    # WG packing.
    for mat in materials:
        ts = mat.get("texture_sets") or {}
        for scheme_slots in ts.values():
            _promote_legacy_raw_slots(scheme_slots)

    return unresolved


def _promote_legacy_raw_slots(slots: dict[str, Any]) -> None:
    """In-place: for each `_normalRawOrMgRaw_<canonical>` entry, copy it
    onto `<canonical>` if `<canonical>` isn't already populated, then
    drop the raw form. Always drops the raw form even if no promotion
    occurred — raw slots are internal-only, never serialised.

    Behaviour matrix:
        canonical present, raw present  → keep canonical, drop raw  (new extract)
        canonical absent,  raw present  → promote raw → canonical   (old extract)
        canonical present, raw absent   → no-op
        canonical absent,  raw absent   → no-op
    """
    for raw_slot in [s for s in slots if s.startswith(_LEGACY_RAW_SLOT_PREFIX)]:
        canonical = raw_slot[len(_LEGACY_RAW_SLOT_PREFIX):]
        raw_value = slots.pop(raw_slot)
        if canonical and canonical not in slots:
            slots[canonical] = raw_value


def _resolve_target_stems(
    part_hint: str,
    base_stem: str,
    stem_index: dict[str, dict[str, dict[str, list[str]]]],
) -> list[str]:
    """Return an ordered list of stems contributing to one material's
    texture set. Earlier entries win per-slot.

    For self-contained ships (Iowa, Montana, Maya ARP — variants whose
    own stem carries every channel), this returns a single-element list:
    the canonical ship stem matching the part hint.

    For mesh-swap permoflages where the variant inherits some channels
    from the base ship (Takao Arpeggio's variant stem
    ``jsc507_takao_arpeggio`` ships only ``baseColor`` / ``metallicRoughness``
    / synthesised ``emissive`` — its ``normal`` / ``camoMask`` / ``occlusion``
    live on the base Atago's stem ``jsc038_atago_hull``), this returns
    ``[variant_stem, base_stem]``: the variant wins per-slot, and the
    base ship fills the gaps.

    Detection: when multiple stems match the same part hint, the one
    with ``baseColor`` in its main scheme is the variant (it owns the
    diffuse paint); the others are inherited base-ship sources.
    """
    hint_low = part_hint.lower()

    # Collect candidate stems for this part hint. Three sources, in
    # priority order:
    #   1. The hull-wide `base_stem` (if hint is "hull" or empty).
    #   2. `<base_stem>_<hint>` exact match, then any stem ending in
    #      `_<hint>` (the part-specific texture stems WG ships).
    #   3. `_HINT_ALIASES` mapping for snake-case quirks (Atago's
    #      `Deck_house` → "deck_house") + library-token aliases.
    candidates: list[str] = []
    if hint_low in ("hull", ""):
        # Primary: the hull-wide `base_stem` itself.
        if base_stem in stem_index:
            candidates.append(base_stem)
        # Fallbacks #1: other stems ending in `_hull`. Catches the case
        # where WG's base ship texture is named `..._Hull_n.dd0` (Atago,
        # Takao, etc.) — the base stem itself ends in `_hull`.
        for s in stem_index:
            if s in candidates:
                continue
            if s.endswith("_hull"):
                candidates.append(s)
        # Fallbacks #2: non-library, non-part-specific ship stems with
        # hull-like slots. Catches mesh-swap permoflages where the base
        # ship's hull texture is named WITHOUT a `_Hull` suffix — e.g.
        # `ASB028_Iowa_1945_n.dd0` (stem `asb028_iowa` after year strip)
        # is the base hull for the `ASB077_Iowa_AzurLane` variant. Those
        # base stems don't satisfy any suffix scan but ARE the right
        # source for normal / camoMask / AO on the variant's hull.
        _HULL_LIKE_SLOTS = (
            "normal", "_normalRawOrMgRaw_normal", "camoMask", "occlusion",
        )
        for s in stem_index:
            if s in candidates:
                continue
            if _is_library_stem(s):
                continue
            # Skip part-specific stems (deck_house, deckhouse, etc.) —
            # those belong to other materials.
            if any(s.endswith(f"_{p}") for p in ("deck_house", "deckhouse", "casemate")):
                continue
            main = stem_index[s].get("main", {})
            if any(slot in main for slot in _HULL_LIKE_SLOTS):
                candidates.append(s)
    else:
        exact = f"{base_stem}_{hint_low}"
        if exact in stem_index:
            candidates.append(exact)
        suffix = f"_{hint_low}"
        for s in stem_index:
            if s != base_stem and s not in candidates and s.endswith(suffix):
                candidates.append(s)
        for alias in _HINT_ALIASES.get(hint_low, ()):
            for s in stem_index:
                if s in candidates:
                    continue
                if alias in s:
                    candidates.append(s)

    if not candidates:
        return []

    # Order: variants (have baseColor in main) before fallbacks (don't).
    # Within each group, preserve discovery order (which roughly matches
    # the canonical → suffix-scan → alias precedence).
    def has_basecolor(s: str) -> bool:
        return "baseColor" in stem_index[s].get("main", {})
    variants = [s for s in candidates if has_basecolor(s)]
    fallbacks = [s for s in candidates if not has_basecolor(s)]

    return variants + fallbacks


def _merge_multi_targets(
    mat: dict[str, Any],
    target_schemes: list[dict[str, dict[str, list[str]]]],
    existing_main: dict[str, Any],
) -> None:
    """Merge multiple stems' schemes into ``mat["texture_sets"]``.
    Earlier entries in ``target_schemes`` win per-slot; later entries
    fill missing slots only.

    ``existing_main`` (slots already resolved by the GLB-driven path)
    always wins. Used both for self-contained ships (single target) and
    permoflage variants (variant stem first, base-ship stem second).

    Side effect: replaces ``mat["texture_sets"]`` entirely. Caller is
    responsible for snapshotting prior content if it needs preserving.
    """
    accumulated: dict[str, dict[str, dict[str, list[str]]]] = {}

    for schemes in target_schemes:
        for scheme_key, slots in schemes.items():
            scheme_acc = accumulated.setdefault(scheme_key, {})
            for slot, paths in slots.items():
                if not paths:
                    continue
                if slot in scheme_acc:
                    # Earlier (higher-priority) target already filled
                    # this slot. Fallback can only fill gaps.
                    continue
                scheme_acc[slot] = {"dds_mips": list(paths)}

    # GLB-resolved main slots win unconditionally.
    if existing_main:
        main_acc = accumulated.setdefault("main", {})
        for slot, manifest in existing_main.items():
            main_acc[slot] = manifest

    # Drop empty schemes (none of the targets contributed anything for
    # that scheme key).
    accumulated = {k: v for k, v in accumulated.items() if v}
    mat["texture_sets"] = accumulated


def _merge_scheme_dict(
    mat: dict[str, Any],
    schemes: dict[str, dict[str, list[str]]],
    existing_main: dict[str, Any],
) -> None:
    """Merge disk-derived per-scheme slots into ``mat["texture_sets"]``.

    The GLB-driven path may have already populated ``texture_sets["main"]``
    from PNG references; that pre-existing data wins for slots it
    resolved. Disk-derived schemes (`camo_01`, `dead`, etc.) are added
    wholesale.
    """
    ts: dict[str, dict[str, dict[str, list[str]]]] = {}
    for scheme_key, slots in schemes.items():
        slot_manifests: dict[str, dict[str, list[str]]] = {}
        for slot, paths in slots.items():
            if not paths:
                continue
            # Preserve any GLB-resolved manifest for this slot in main;
            # disk-derived data fills in slots GLB didn't provide.
            if scheme_key == "main" and slot in existing_main:
                slot_manifests[slot] = existing_main[slot]
            else:
                slot_manifests[slot] = {"dds_mips": paths}
        if slot_manifests:
            ts[scheme_key] = slot_manifests
    # If GLB resolved any main slot the disk scan didn't have, keep it.
    if existing_main:
        ts.setdefault("main", {})
        for slot, manifest in existing_main.items():
            ts["main"].setdefault(slot, manifest)
    mat["texture_sets"] = ts



_CAMO_SCHEME_REGEX = re.compile(r"^camo_(?P<base>[A-Za-z0-9]+?)(?:_(?P<roll>[A-Z]))?$")


def discover_skins_from_materials(
    materials: Iterable[dict[str, Any]],
    *,
    palette_resolver: callable = None,  # type: ignore[name-defined]
    name_resolver: callable = None,  # type: ignore[name-defined]
) -> list[dict[str, Any]]:
    """Scan ``materials[i].texture_sets`` keys and emit one ``skins[]``
    entry per unique scheme found.

    Always emits the mandatory ``default`` skin first. Each non-``main``
    scheme becomes its own skin with ``scheme_key`` matching the texture
    set key. ``camo_<NN>[_<R>]`` keys are decomposed into a
    ``camo_pattern`` parent + ``color_roll`` subvariant tag so consumers
    can group colour rolls of the same base pattern.

    ``dead`` and ``dead_<scheme>`` schemes are NOT emitted as skins —
    they're damage states, surfaced through `HullDamageState` not skin
    selection. This keeps the player-facing skin list clean.

    When ``palette_resolver`` is supplied, each non-default scheme is
    expanded into ONE skin per color roll (default + alternate, etc.)
    with a populated ``color_scheme`` field. The resolver signature is::

        (scheme_key, mask_paths) -> (camo_name, [(roll_id, palette), …], categories)

    where ``palette`` is a list of 4 RGBA float tuples, ``camo_name``
    is the camouflages.xml entry name (e.g. ``camo_permanent_1``), and
    ``categories`` is the per-camo-category shared-mask dict (see
    :func:`make_skin`'s ``categories`` arg). Categories is shared
    across every roll of the same camo block — same masks, same UVs.

    Returning ``(None, [], {})`` means "no palette data; fall back to
    one skin with mask only" — preserves the legacy behavior. Legacy
    2-tuple resolvers still work (categories defaults to ``{}``).
    """
    # Index `(scheme_key) -> first-seen mask paths` so the resolver can
    # match by mask filename without re-walking materials. The resolver
    # only needs ONE example mask per scheme (the toolkit emits the
    # same scheme files across every material that uses them).
    seen: set[str] = set()
    discovered: list[str] = []
    mask_paths_by_scheme: dict[str, list[str]] = {}
    for mat in materials:
        ts = mat.get("texture_sets") or {}
        for scheme_key, scheme_data in ts.items():
            if scheme_key == "main":
                continue
            if scheme_key.startswith("dead"):
                continue
            if scheme_key not in seen:
                seen.add(scheme_key)
                discovered.append(scheme_key)
            # Stash the first non-empty baseColor.dds_mips for resolver
            # use. Subsequent materials with the same scheme overwrite —
            # they'd resolve to the same camo entry anyway.
            if scheme_key not in mask_paths_by_scheme:
                base = (scheme_data or {}).get("baseColor") or {}
                mips = base.get("dds_mips") or []
                if mips:
                    mask_paths_by_scheme[scheme_key] = mips

    skins: list[dict[str, Any]] = [make_default_skin()]
    for scheme_key in sorted(discovered):
        # Try the palette resolver first — it gives authoritative
        # display names (camouflages.xml entry name) + multi-roll info.
        resolved: list[tuple[str, list[list[float]]]] = []
        camo_name: str | None = None
        categories: dict[str, dict[str, Any]] = {}
        if palette_resolver is not None:
            mask_paths = mask_paths_by_scheme.get(scheme_key, [])
            try:
                result = palette_resolver(scheme_key, mask_paths)
            except Exception:
                # Resolver should never raise — but be defensive in
                # scaffold path. Fall through to legacy behaviour.
                result = (None, [], {})
            # Tolerate legacy 2-tuple resolvers (no categories).
            if isinstance(result, tuple):
                if len(result) == 3:
                    camo_name, resolved, categories = result
                elif len(result) == 2:
                    camo_name, resolved = result
                    categories = {}
                else:
                    camo_name, resolved, categories = None, [], {}
            else:
                camo_name, resolved, categories = None, [], {}

        if resolved:
            # Resolve a human-readable label for the camo entry once per
            # scheme. ``name_resolver`` (when supplied) walks GameParams
            # Exteriors → ``IDS_<name>`` in the WoWS gettext catalogue,
            # turning ``camo_permanent_1`` into ``"Iron Resilience"``
            # etc. See ``tools/shared/wg_localization.py`` and
            # ``tools/shared/wg_camo.display_name_for_camo_entry``.
            # Falls through to the raw entry name when missing or unset.
            human_pattern: str | None = None
            if name_resolver is not None and camo_name:
                try:
                    human_pattern = name_resolver(camo_name)
                except Exception:
                    human_pattern = None
            for roll_id, palette in resolved:
                pattern = camo_name or scheme_key
                skin_id = f"{pattern}__{roll_id}"
                # Display: human label preferred, falls back to the raw
                # entry name. The roll_id suffix disambiguates color
                # variants of the same base pattern (Iowa carries both
                # ``camo_permanent_1`` rolls IJNP03 and IJNP36) so even
                # when the human name is identical, skin_ids stay unique.
                display_base = human_pattern or pattern
                display = f"{display_base} ({roll_id})"
                skins.append(make_skin(
                    skin_id=skin_id,
                    display_name=display,
                    scheme_key=scheme_key,
                    camo_pattern=pattern,
                    color_roll=roll_id,
                    color_scheme={
                        "name":   roll_id,
                        "colors": [list(c) for c in palette],
                    },
                    # Same categories on every roll variant — they
                    # share the same <Textures> + <UV> block.
                    categories=categories or None,
                ))
            continue

        # ── Legacy fallback (no resolver / no palette match) ─────────
        # Same behaviour as before: one skin per scheme key, mask-only.
        camo_pattern: str | None = None
        color_roll: str | None = None
        m = _CAMO_SCHEME_REGEX.match(scheme_key)
        if m:
            base = m.group("base")
            roll = m.group("roll")
            camo_pattern = f"camo_{base}"
            color_roll = roll
        if camo_pattern is not None:
            base = m.group("base")
            display = f"Camo {base}"
            if color_roll:
                display = f"{display} ({color_roll})"
        else:
            display = scheme_key.replace("_", " ").title()

        skins.append(make_skin(
            skin_id=scheme_key,
            display_name=display,
            scheme_key=scheme_key,
            camo_pattern=camo_pattern,
            color_roll=color_roll,
        ))
    return skins
