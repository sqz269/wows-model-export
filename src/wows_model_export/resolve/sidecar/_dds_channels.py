"""Single source of truth for the WG-vs-conformant DDS suffix convention.

Two consumers share this table:

* :mod:`wows_model_export.compose.skin_pack` (loose-mod + VFS-variant
  producer) — uses :data:`SKIN_PACK_CHANNEL_SLOTS` to recognise BOTH the
  glTF-conformant siblings the toolkit's swizzle pass emits AND the raw
  WG-original ``_n`` / ``_mg`` files loose mods ship before the swizzle
  has run.
* :mod:`wows_model_export.resolve.sidecar._materials` (extract-time
  binder) — uses :data:`CHANNEL_SLOTS` to classify the conformant DDS
  files in the toolkit's raw-DDS dump. Legacy raw ``_n`` / ``_mg`` files
  on the same dump get filtered out via :data:`LEGACY_RAW_SUFFIXES`
  before the classifier runs — the sidecar carries only conformant slot
  names (since 2026-05-17).

Two suffix vocabularies coexist on disk:

* **Conformant glTF-style siblings** the toolkit's swizzle pass emits
  when invoked via ``export-ship --raw-dds-dir``:

  ``_normal``    tangent-space normal (B = reconstructed Z)
  ``_mr``        metallic-roughness (G = roughness, B = metallic)
  ``_nbmask``    BC4 single-channel "no-camo region" mask extracted from
                 the WG normal map's B channel (Path B 4-threshold deny
                 list source).
  ``_camomask``  BC4 single-channel "Path A paint mask" extracted from
                 the WG MG map's B channel (the binary exclusion gate
                 ``ship_camo_material.fx`` reads — see
                 ``reference/topics/camo/wg_camo_shader_reference.md``
                 §"Path A").
  ``_emissive``  synthesised emissive map (ARP / Azur Lane / Sabaton
                 crossover skins — diffuse * mg.B * emissivePower).
  ``_ao``        ambient occlusion (R channel).
  ``_a``         baseColor diffuse (WG's standard albedo suffix).
  ``_detail``    shared tangent-space detail-normal atlas
                 (``ship_atlas_detail.dds``) — 2048² tiled DDS bound by
                 every PBS material that has ``g_detailNormalInfluence``
                 > 0 in its MFM, sampled at ``vMapUv × (g_detailScaleU,
                 g_detailScaleV)``. Per-material blend weights live on
                 the sidecar ``materials[].detail_params`` field.

* **WG-original channels** that loose mods ship before the swizzle pass
  has run (skin_pack invokes ``wowsunpack swizzle-dir`` to produce the
  conformant siblings):

  ``_n``      raw WG normal (B = categorical mask, not Z)
  ``_mg``     raw WG MG (R=cavity / G=metallic / B=binary paint mask)
"""
from __future__ import annotations

#: Single source of truth for the WG-vs-conformant DDS suffix convention.
#: Each entry is ``(filename suffix, canonical sidecar slot name)``.
#:
#: Order matches the longest-suffix-first preference needed by both
#: consumers' classifier loops: ``_normal`` matches before ``_n`` (raw
#: fallback), ``_camomask`` before any shorter suffix, etc. ``_a`` lives
#: last because it's the shortest conformant suffix and shouldn't shadow
#: longer ones.
CHANNEL_SLOTS: tuple[tuple[str, str], ...] = (
    ("_emissive", "emissive"),
    ("_nbmask",   "camoMask"),
    ("_camomask", "camoExclusionMask"),
    ("_normal",   "normal"),
    ("_detail",   "detail"),
    ("_mr",       "metallicRoughness"),
    ("_ao",       "occlusion"),
    ("_a",        "baseColor"),
)


#: Raw WG-original suffixes the swizzle pass replaces with conformant
#: siblings. Listed here so :mod:`._materials`'s extract-time indexer can
#: filter them out before classification — modern VFS extracts always
#: carry the conformant sibling, so the raw form is a redundant artifact.
LEGACY_RAW_SUFFIXES: tuple[str, ...] = ("_mg", "_n")


#: Extended classifier view for :mod:`..compose.skin_pack` — accepts
#: BOTH conformant siblings AND raw WG fallbacks. Loose-mod folders may
#: ship only the raw forms (before skin_pack invokes ``swizzle-dir``);
#: when both forms land on disk, :data:`SUFFIX_PRIORITY` decides which
#: wins per ``(stem, slot)``.
#:
#: Order: conformant first (longest-suffix-first within), then raw
#: fallbacks last. ``_normal`` and ``_mr`` precede ``_n`` and ``_mg`` so
#: the endswith() loop strips the conformant suffix when both forms are
#: present in the same filename context.
SKIN_PACK_CHANNEL_SLOTS: tuple[tuple[str, str], ...] = CHANNEL_SLOTS + (
    ("_mg", "metallicRoughness"),
    ("_n",  "normal"),
)


#: Priority per channel suffix; 0 = preferred (glTF-conformant), 1 = raw
#: WG fallback. Used by :mod:`..compose.skin_pack` to dedupe
#: ``(stem, slot)`` pairs at apply time — the lower-priority number wins
#: per pair, so the renderer always sees the conformant sibling when one
#: exists on disk.
SUFFIX_PRIORITY: dict[str, int] = {
    # 0 = conformant (preferred), 1 = raw WG fallback.
    "_emissive": 0,
    "_nbmask":   0,
    "_camomask": 0,
    "_normal":   0,
    "_detail":   0,
    "_mr":       0,
    "_ao":       0,
    "_a":        0,
    "_n":        1,  # raw WG normal (B = mask, not Z)
    "_mg":       1,  # raw WG metallic-gloss
}


__all__ = [
    "CHANNEL_SLOTS",
    "LEGACY_RAW_SUFFIXES",
    "SKIN_PACK_CHANNEL_SLOTS",
    "SUFFIX_PRIORITY",
]
