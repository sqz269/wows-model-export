"""Compose ``ingest_skin_pack`` — fold a texture-replacement skin pack
into a ship's sidecar as a new ``mat_albedo`` skin.

Lifted from ``tools/ship/ingest_skin_pack.py`` on the I:-side warships
repo (1532 lines). Layer 4 (composer): chains
:mod:`wows_model_export.toolkit` invocations,
:mod:`wows_model_export.resolve` transforms, and per-ship sidecar
mutations into the end-to-end skin-ingest flow.

Two source modes:

  ``"loose_mod"``    Loose content-SDK mod folder. Walks the folder
                     for ``*.dds`` files, classifies each by stem
                     (ship-side material vs library accessory), runs
                     the mesh-comparison verdict gate, and copies the
                     survivors into the ship's
                     ``models/skins/<skin_id>/`` tree.

  ``"vfs_variant"``  WG-authored ship variant in the VFS (e.g.
                     ``ASC080_Baltimore_1944_Azur`` or
                     ``JSC507_Takao_1944_Arpeggio``). Reads the
                     specified Exterior to enumerate per-HP swap
                     models, extracts hull + swapped-accessory textures
                     from VFS, and rewrites mod stems to vanilla stems
                     before running the same comparison-gated copy.

  ``"auto"``         Detects from ``skin_source``: a directory →
                     ``loose_mod``; a string ID (or a non-existent
                     path) → ``vfs_variant``. Looks up the source
                     either as a Vehicle's ``nativePermoflage``
                     (Exterior auto-discovered) or as a bare Exterior
                     entity ID; the Exterior's ``peculiarityModels``
                     ``/ship/`` entry then provides the variant
                     asset_id.

Output: a new ``Skin`` entry in the ship's sidecar
(``kind="mat_albedo"``, ``scheme_key=<skin_id>``) plus
``texture_sets[<skin_id>]`` blocks on every ship-side material the pack
provides hull / deckhouse paint for. Library accessories' overrides
land in ``Skin.asset_overrides[<asset_id>]``.

Re-running the ingester is idempotent — ``apply_plan`` replaces the
prior Skin entry with the same ``skin_id`` while preserving other
schemes in the sidecar.

This composer closes the
:exc:`NotImplementedError` dangling dependency at the native-
permoflage call site in :mod:`wows_model_export.compose.scaffold_ship`
— callers needing native-permoflage auto-ingest (ARP / Azur Lane /
Sabaton crossover ships) can now route through here.

Inline dependencies lifted alongside this composer:

* :mod:`wows_model_export.read.bw_geometry`     — Layer 1 ``.geometry``
                                                  parser (was
                                                  ``tools/shared/bw_geometry.py``)
* :mod:`wows_model_export.resolve.mesh_compare` — Layer 3 mesh-diff
                                                  transforms (was
                                                  ``tools/ship/compare_skin_meshes.py``)
* :mod:`wows_model_export.resolve.exterior_compare` — Layer 3 Exterior
                                                  swap-map walker (was
                                                  ``tools/ship/compare_exterior_swaps.py``)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .. import toolkit
from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp
from ..read import localization as _localization
from ..read import sidecar as read_sidecar
from ..resolve import camo as wg_camo
from ..resolve import exterior_compare as ces
from ..resolve import mesh_compare as csm
from ..resolve import sidecar as resolve_sidecar
from ..resolve import synth_emission
from ..types import OnEvent, SkinPackResult, StepEvent

# ---------------------------------------------------------------------------
# Channel → slot mapping for skin-pack DDS files
# ---------------------------------------------------------------------------

# Two suffix vocabularies coexist:
#
#  * Conformant glTF-style siblings the toolkit's swizzle pass emits when
#    invoked via ``export-ship --raw-dds-dir``:
#       ``_normal``  → tangent-space normal (B = reconstructed Z)
#       ``_mr``      → metallic-roughness (G = roughness, B = metallic)
#       ``_nbmask``  → BC4 single-channel "no-camo region" mask extracted
#                       from the WG normal map's B channel
#
#  * WG-original channels the loose mod folders ship (no swizzle pass):
#       ``_n``       → raw WG normal (B = camo no-camo mask)
#       ``_mg``      → raw WG MG (R=cavity, G=metalmask, B=gloss)
#
# Both routes land in the same canonical glTF slot names so the renderer
# prefers conformant when present and falls back to raw via the
# WgShipStandard shader chunk's WG-pack handling. Order is longest-
# suffix-first so ``_normal`` matches before ``_n``, ``_mr`` before
# ``_mg``.
CHANNEL_SLOTS: tuple[tuple[str, str], ...] = (
    ("_emissive", "emissive"),
    ("_nbmask",   "camoMask"),
    ("_normal",   "normal"),
    ("_mr",       "metallicRoughness"),
    ("_ao",       "occlusion"),
    ("_mg",       "metallicRoughness"),
    ("_n",        "normal"),
    ("_a",        "baseColor"),
)


#: Priority per channel suffix; 0 = preferred (glTF-conformant), 1 = raw
#: WG fallback. When both forms land on disk for the same slot (toolkit's
#: ``--raw-dds-dir`` emits both), :func:`apply_plan` keeps only the
#: lower-priority number per ``(stem, slot)``.
_SUFFIX_PRIORITY: dict[str, int] = {
    "_emissive": 0,
    "_nbmask": 0, "_normal": 0, "_mr": 0, "_ao": 0, "_a": 0,
    "_n": 1, "_mg": 1,
}


# Verdicts that mean "mod texture safely applies to vanilla mesh".
#
# `mirrored_uv_stable` and `mirrored_uv_partial` are included because mod
# textures painted for a Z-mirrored variant of the vanilla mesh end up
# sampling the SAME texture content for the UV regions that are preserved
# under the flip (typically ~65% of the surface; the un-preserved 35% is
# where the artist added themed details). For uniformly-coloured skins
# (e.g. Azur Lane Baltimore's white turrets), even the 35% wrong-region
# sampling lands within the same broad colour and reads acceptably.
APPLICABLE_VERDICTS = {
    "identical", "uv_stable", "uv_drift", "texture_only",
    "mirrored_uv_stable", "mirrored_uv_partial",
}

# Verdicts that mean "mod texture WILL NOT fit vanilla mesh".
SKIP_VERDICTS = {"mismatched", "mirrored_uv_diverged", "error"}


# ---------------------------------------------------------------------------
# Plan dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _ShipSideTexture:
    """A DDS that overrides one slot of one ship-side material's main
    texture set (hull / deckhouse / crack / etc.). The material stem is
    the existing ``texture_sets["main"]`` baseColor's filename without the
    channel suffix — that's the key that identifies which material to
    patch."""
    src_path: Path
    material_stem: str   # e.g. "ASB017_Montana"
    slot: str            # e.g. "baseColor"
    priority: int = 1    # 0 = conformant glTF, 1 = raw WG fallback


@dataclass
class _AccessoryTexture:
    """A DDS that overrides one slot of one library-accessory material."""
    src_path: Path
    asset_id: str        # e.g. "AGM034_16in50_Mk7"
    slot: str
    priority: int = 1


@dataclass
class _IngestPlan:
    skin_id: str
    display_name: str
    source_label: str
    ship_side: list[_ShipSideTexture] = field(default_factory=list)
    accessories: dict[str, list[_AccessoryTexture]] = field(default_factory=dict)
    accessory_geometries: dict[str, Path] = field(default_factory=dict)
    skipped_dds: list[tuple[Path, str]] = field(default_factory=list)
    # ``mat_*`` per-category atlas paint, sourced from the Exterior's
    # ``camouflage`` field (a ``mat_<…>`` entry in camouflages.xml).
    mat_textures: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_display_name(skin_id: str | None) -> str:
    """Resolve a default display_name for a skin_id when the caller
    didn't pass an explicit override.

    Skin IDs that look like an Exterior entity (``PJES477_ARP_TAKAO``,
    ``PAES118_AZUR_ENTERPRISE``, …) get the WoWS gettext catalogue's
    English label (``"ARPEGGIO"``, ``"Azur Lane"``); other skin_ids
    fall through to the raw ``skin_id`` so loose mods authored with
    custom names keep their explicit identifiers visible.

    Failures (catalogue file missing, lookup error) silently fall
    through to ``skin_id``; never raises.
    """
    if not skin_id:
        return skin_id or ""
    try:
        db = _localization.load()
        loc = db.exterior_display_name(skin_id)
        if loc:
            return loc
    except Exception as exc:
        print(f"[skin_pack] localization lookup failed: {exc}", file=sys.stderr)
    return skin_id


def _classify_channel(stem_with_channel: str) -> tuple[str, str, int] | None:
    """Strip the longest matching channel suffix; return
    ``(stem_without_channel, slot, priority)``. Priority 0 = preferred
    (conformant glTF), 1 = raw WG fallback.

    Returns ``None`` if no recognised channel suffix matches.
    """
    low = stem_with_channel.lower()
    for suffix, slot in CHANNEL_SLOTS:
        if low.endswith(suffix):
            return (
                stem_with_channel[: -len(suffix)],
                slot,
                _SUFFIX_PRIORITY.get(suffix, 1),
            )
    return None


def _build_ship_side_stems(ship_sidecar: dict) -> set[str]:
    """Stems of ship-side materials' baseColor textures, used to recognise
    a hull / deckhouse / crack DDS in the mod folder.
    """
    out: set[str] = set()
    for m in ship_sidecar.get("materials") or []:
        if not isinstance(m, dict):
            continue
        ts = m.get("texture_sets") or {}
        bc = (ts.get("main") or {}).get("baseColor") or {}
        mips = bc.get("dds_mips") or []
        if not mips:
            continue
        first = Path(mips[0]).name
        # Strip mip suffix (.dd0/.dd1/.dd2/.dds).
        for ext in (".dd0", ".dd1", ".dd2", ".dds"):
            if first.lower().endswith(ext):
                first = first[: -len(ext)]
                break
        # Strip channel suffix.
        cls = _classify_channel(first)
        if cls is not None:
            out.add(cls[0])
    return out


def _build_library_asset_index(workspace: Path) -> dict[str, dict]:
    """Read ``<workspace>/libraries/accessories/index.json``; return
    ``{asset_id: entry}``."""
    idx_path = workspace / "libraries" / "accessories" / "index.json"
    if not idx_path.is_file():
        return {}
    return (json.loads(idx_path.read_text(encoding="utf-8")) or {}).get("assets") or {}


def _vanilla_stems_by_variant_infix(ship_sidecar: dict) -> dict[str, str]:
    """Build a ``{variant_infix_lower: vanilla_stem}`` map for the
    variant→vanilla stem rewrite.

    A variant DDS like ``JSC507_Takao_1944_Arpeggio_Deck_house_a.dd0`` is
    keyed by its ``_Deck_house`` infix (the portion AFTER the variant's
    asset_id, BEFORE the channel suffix). Each vanilla ship-side material
    has a corresponding infix relative to the sidecar's ``wg_asset_id``;
    matching infixes line up the swap.

    Two hull-naming conventions coexist in WG ships:

      * **Bare** (Myoko, Iowa older form): vanilla hull stem == ``wg_asset_id``
      * **Suffixed** (Atago, Montana): vanilla hull stem == ``<wg_asset_id>_Hull``

    The empty-infix entry is filled in two passes: pass 1 populates it
    iff a bare-stem vanilla material exists; pass 2 falls back to the
    canonical ``_Hull`` stem when only the suffixed form is present.

    Returns ``{}`` when the sidecar lacks ``ship.wg_asset_id``.
    """
    wg_id = (ship_sidecar.get("ship") or {}).get("wg_asset_id")
    if not isinstance(wg_id, str) or not wg_id:
        return {}
    stems = _build_ship_side_stems(ship_sidecar)
    wg_lower = wg_id.lower()

    out: dict[str, str] = {}
    # Pass 1
    for stem in stems:
        sl = stem.lower()
        if not sl.startswith(wg_lower):
            continue
        infix = stem[len(wg_id):]                # original case of stem
        out[infix.lower()] = stem

    # Pass 2: ensure empty-infix maps to canonical hull material.
    if "" not in out:
        if "_hull" in out:
            out[""] = out["_hull"]
        else:
            for stem in stems:
                sl = stem.lower()
                if not sl.startswith(wg_lower):
                    continue
                infix = sl[len(wg_id):]
                if any(tok in infix for tok in ("deck", "crack", "glass")):
                    continue
                out[""] = stem
                break
    return out


def _material_role_tag(material_id: str | None) -> tuple[str, str] | None:
    """Extract ``(family, role)`` from a SHIP material_id, dropping
    variant modifier tokens like ``_EMISSIVE_``.

    Examples::

        SHIPMAT_PBS_Hull           → ('SHIPMAT', 'Hull')
        SHIPMAT_EMISSIVE_PBS_Hull  → ('SHIPMAT', 'Hull')
        SHIPGLASS_PBS_Hull         → ('SHIPGLASS', 'Hull')

    Returns ``None`` for non-ship materials (armor, hitbox, accessories).
    """
    if not isinstance(material_id, str):
        return None
    for fam in ("SHIPMAT", "SHIPWIRE", "SHIPGLASS"):
        prefix = fam + "_"
        if not material_id.startswith(prefix):
            continue
        rest = material_id[len(prefix):]
        if "_PBS_" in rest:
            _modifier, role = rest.rsplit("_PBS_", 1)
            return (fam, role)
        if rest.startswith("PBS_"):
            return (fam, rest[len("PBS_"):])
    return None


def _stem_from_material_main_basecolor(m: dict) -> str | None:
    """Read a material entry's ``main`` scheme baseColor first-mip
    filename and strip the channel + extension suffixes to recover the
    bare texture stem."""
    ts = (m.get("texture_sets") or {}).get("main") or {}
    bc = ts.get("baseColor") or {}
    mips = bc.get("dds_mips") or []
    if not mips:
        for slot in ("normal", "metallicRoughness", "emissive"):
            mips = (ts.get(slot) or {}).get("dds_mips") or []
            if mips:
                break
        if not mips:
            return None
    name = Path(mips[0]).name
    for ext in (".dd0", ".dd1", ".dd2", ".dds"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    for ch, _slot in CHANNEL_SLOTS:
        if name.lower().endswith(ch):
            return name[: -len(ch)]
    return name


def _build_variant_stem_mapping(
    *,
    variant_glb: Path,
    cache_dds_dir: Path,
    variant_asset_id: str,
    ship_sidecar: dict,
    variant_mappings_json: Path | None = None,
) -> dict[str, str]:
    """Deterministic ``variant_stem → vanilla_stem`` map by joining the
    role tag (family + suffix from material_id) between the variant's
    GLB and the base ship's sidecar.

    Returns ``{}`` when the variant GLB can't be parsed or the sidecar
    has no SHIP* materials. Caller falls back to the infix heuristic.
    """
    if not variant_glb.is_file():
        return {}

    # Stage variant-prefix-only files in a sibling temp dir.
    variant_only = cache_dds_dir.parent / "variant_only_dds"
    if variant_only.exists():
        shutil.rmtree(variant_only, ignore_errors=True)
    variant_only.mkdir(parents=True, exist_ok=True)
    prefix = variant_asset_id
    for f in cache_dds_dir.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith(prefix):
            shutil.copy2(f, variant_only / f.name)
    if not any(variant_only.iterdir()):
        return {}

    try:
        variant_mats = resolve_sidecar.materials_from_glb(
            variant_glb, textures_dds_dir=variant_only,
            material_mappings_json=variant_mappings_json,
        )
    except Exception as e:
        print(
            f"[skin_pack] deterministic stem map: materials_from_glb on variant "
            f"GLB failed ({e}) — falling back to infix heuristic",
            file=sys.stderr,
        )
        return {}

    # Build {role_tag: variant_stem} from variant GLB.
    variant_role_to_stem: dict[tuple[str, str], str] = {}
    for m in variant_mats:
        role = _material_role_tag(m.get("material_id"))
        if not role:
            continue
        stem = _stem_from_material_main_basecolor(m)
        if stem and stem.startswith(variant_asset_id):
            variant_role_to_stem[role] = stem

    # Build {role_tag: vanilla_stem} from base ship sidecar.
    vanilla_role_to_stem: dict[tuple[str, str], str] = {}
    for m in ship_sidecar.get("materials") or []:
        role = _material_role_tag(m.get("material_id"))
        if not role:
            continue
        stem = _stem_from_material_main_basecolor(m)
        if stem:
            vanilla_role_to_stem[role] = stem

    # Join.
    out: dict[str, str] = {}
    for role, vstem in variant_role_to_stem.items():
        van = vanilla_role_to_stem.get(role)
        if van:
            out[vstem] = van
    return out


def _vanilla_asset_at_hp(ship_dir: Path, ship_label: str) -> dict[str, str]:
    """Read the ship's ``<Ship>_placements.json`` to map ``HP_<id>`` →
    ``vanilla_asset_id``. Used to resolve Exterior swaps to their vanilla
    counterparts."""
    pl_path = ship_dir / read_sidecar.MODELS_SUBDIR / f"{ship_label}_placements.json"
    if not pl_path.is_file():
        return {}
    data = json.loads(pl_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for section in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for entry in data.get(section, []) or []:
            if not isinstance(entry, dict):
                continue
            hp = entry.get("hp_name")
            aid = entry.get("asset_id")
            if isinstance(hp, str) and isinstance(aid, str):
                out.setdefault(hp, aid)
    return out


def _resolve_wg_source(wg_id: str) -> tuple[str, str]:
    """Resolve a WG GameParams ID to ``(variant_asset_id, exterior_id)``.

    Accepts either a Vehicle ID (``PJSC705`` / ``PJSC705_Myoko``) — in
    which case the Vehicle's ``nativePermoflage`` provides the Exterior
    — or an Exterior ID (``PJES457`` / ``PJES457_MYOKO``) directly. The
    Exterior is expected to carry exactly one ``/ship/`` entry in
    ``peculiarityModels``; the swap target's basename becomes the
    variant_asset_id.

    Raises :class:`ValueError` for any case the resolver can't handle
    uniquely (no ``nativePermoflage`` on a Vehicle, no ``/ship/``
    peculiarityModel on an Exterior, multiple ``/ship/`` swaps, missing
    entity, etc.).
    """
    full_id = _gp.resolve_ship_id(wg_id) or wg_id
    entity = _gp.get_entity(full_id)
    if entity is None:
        raise ValueError(
            f"WG ID {wg_id!r} (resolved to {full_id!r}) not found in GameParams"
        )
    et = (entity.get("typeinfo") or {}).get("type")

    if et == "Ship":
        ext_id = entity.get("nativePermoflage")
        if not ext_id:
            raise ValueError(
                f"Vehicle {full_id} has no nativePermoflage — no Exterior to "
                f"drive the ingest. Use source_kind='vfs_variant' with the "
                f"variant asset_id directly."
            )
        ext = _gp.get_entity(ext_id)
        if ext is None:
            raise ValueError(
                f"nativePermoflage {ext_id!r} of {full_id} not found in GameParams"
            )
    elif et == "Exterior":
        ext_id = full_id
        ext = entity
    else:
        raise ValueError(
            f"{full_id} is type={et!r}; expected Ship or Exterior"
        )

    pm = ext.get("peculiarityModels") or {}
    ship_swaps = [
        (k, v) for k, v in pm.items()
        if isinstance(k, str) and isinstance(v, str) and "/ship/" in k
    ]
    if not ship_swaps:
        raise ValueError(
            f"Exterior {ext_id} has no /ship/ peculiarityModels entry — "
            f"this skin doesn't swap the hull asset_id. Use "
            f"source_kind='vfs_variant' with the variant asset_id directly."
        )
    if len(ship_swaps) > 1:
        targets = ", ".join(sorted(Path(v).stem for _, v in ship_swaps))
        raise ValueError(
            f"Exterior {ext_id} has {len(ship_swaps)} /ship/ peculiarityModels "
            f"({targets}); can't pick one automatically."
        )
    _, swap_path = ship_swaps[0]
    variant_asset_id = Path(swap_path).stem
    return variant_asset_id, ext_id


# ---------------------------------------------------------------------------
# Source-mode loaders (private — invoked by the step pipeline)
# ---------------------------------------------------------------------------


def _load_loose_mod(
    mod_dir: Path,
    *,
    skin_id: str,
    display_name: str,
    ship_sidecar: dict,
    library_index: dict[str, dict],
) -> _IngestPlan:
    """Walk a content-SDK mod folder and build an :class:`_IngestPlan`.

    Expects the swizzle pass has already fired (the
    ``swizzle_textures`` composer step runs ``toolkit.swizzle_dir`` for
    loose mods before this loader is called). Idempotent re-runs of the
    composer pick up existing conformant siblings via the lower
    priority in ``_SUFFIX_PRIORITY``.
    """
    ship_stems = _build_ship_side_stems(ship_sidecar)
    plan = _IngestPlan(
        skin_id=skin_id, display_name=display_name,
        source_label=f"loose:{mod_dir}",
    )

    # First pass: collect every .geometry (for comparison verdicts).
    for geom in mod_dir.rglob("*.geometry"):
        # Strip _dead suffix when keying — main mesh is the one we compare.
        stem = geom.stem
        if stem.endswith("_dead"):
            continue
        plan.accessory_geometries[stem] = geom

    # Second pass: classify DDS files.
    for dds in sorted(mod_dir.rglob("*.dds")):
        stem_with_channel = dds.stem
        cls = _classify_channel(stem_with_channel)
        if cls is None:
            plan.skipped_dds.append((dds, "no recognised channel suffix"))
            continue
        stem, slot, prio = cls

        # Ship-side?
        if stem in ship_stems:
            plan.ship_side.append(_ShipSideTexture(
                src_path=dds, material_stem=stem, slot=slot, priority=prio,
            ))
            continue

        # Library accessory?
        if stem in library_index:
            plan.accessories.setdefault(stem, []).append(_AccessoryTexture(
                src_path=dds, asset_id=stem, slot=slot, priority=prio,
            ))
            continue

        plan.skipped_dds.append((
            dds,
            f"stem {stem!r} is neither a ship-side material "
            f"nor a library asset_id",
        ))

    return plan


def _load_vfs_variant(
    *,
    variant_asset_id: str,
    exterior_id: str,
    skin_id: str,
    display_name: str,
    ship_sidecar: dict,
    ship_label: str,
    ship_dir: Path,
    library_index: dict[str, dict],
    cache_dir: Path,
    config: PipelineConfig | None,
) -> _IngestPlan:
    """Build an :class:`_IngestPlan` from a WG-authored ship variant in
    the VFS.

    Reads the Exterior to enumerate the per-HP swap models, extracts
    every relevant ``.dds`` / ``.geometry`` from VFS, and rewrites each
    mod-stem to its vanilla counterpart so the same apply step can
    consume the result unchanged.
    """
    print(f"[skin_pack/vfs] loading Exterior {exterior_id}")
    ext = ces.load_exterior(exterior_id)
    swaps = ces.extract_swaps(ext)
    camo_name = ext.get("camouflage")

    vanilla_stems_by_infix = _vanilla_stems_by_variant_infix(ship_sidecar)
    if not vanilla_stems_by_infix:
        raise RuntimeError(
            "couldn't infer vanilla hull stems from ship sidecar's "
            "materials.texture_sets[main]; can't rewrite variant stems"
        )

    vanilla_at_hp = _vanilla_asset_at_hp(ship_dir, ship_label)
    swap_to_vanilla: dict[str, str] = {}
    for s in swaps:
        van_aid = s.get("vanilla_asset_id")
        if not van_aid and s.get("hp_name"):
            van_aid = vanilla_at_hp.get(s["hp_name"])
        if van_aid:
            swap_to_vanilla[s["swap_asset_id"]] = van_aid

    print(
        f"[skin_pack/vfs] Exterior defines {len(swaps)} swap entries "
        f"({len(swap_to_vanilla)} resolved)"
    )
    for s in swaps:
        van = swap_to_vanilla.get(s["swap_asset_id"], "?")
        hp_label = s.get("hp_name") or f"<{s['kind']}>"
        print(f"  {hp_label:24s}  {s['swap_asset_id']}  →  vanilla {van}")

    # Pull the variant ship + swap accessories. Two extraction paths;
    # see the lifted module for the rationale on why we can't go via
    # a single flat extract glob.
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Run export-ship for the variant hull → swizzle-conformant DDS chain.
    print(f"[skin_pack/vfs] export-ship {variant_asset_id} → {cache_dir}/dds/")
    dds_root = cache_dir / "dds"
    dds_root.mkdir(parents=True, exist_ok=True)
    variant_glb_path = cache_dir / f"{variant_asset_id}_variant.glb"
    variant_mappings_json = cache_dir / f"{variant_asset_id}_material_mappings.json"
    toolkit.export_ship(
        variant_asset_id, variant_glb_path,
        accessories="exclude",
        no_textures=True,
        raw_dds_dir=dds_root,
        material_mappings_json=variant_mappings_json,
        config=config,
    )

    # Run export-model per swap accessory → conformant-sibling DDS chain.
    swap_to_stems: dict[str, set[str]] = {}
    per_swap_root = cache_dir / "per_swap"
    for swap_aid in sorted(swap_to_vanilla):
        swap_model = next(
            (s["swap_model"] for s in swaps if s["swap_asset_id"] == swap_aid),
            None,
        )
        if not swap_model:
            continue
        # export-model expects the .geometry path, not .model.
        vfs_geom = swap_model.replace(".model", ".geometry")
        swap_dds_dir = per_swap_root / swap_aid
        swap_dds_dir.mkdir(parents=True, exist_ok=True)
        print(f"[skin_pack/vfs] export-model {swap_aid} → {swap_dds_dir}")
        with tempfile.NamedTemporaryFile(suffix=".glb", delete=False) as tg:
            tmp_glb = Path(tg.name)
        try:
            toolkit.export_model(
                vfs_geom, tmp_glb,
                no_textures=True,
                raw_dds_dir=swap_dds_dir,
                config=config,
            )
        except Exception as e:
            print(
                f"[skin_pack/vfs] export-model {swap_aid} failed ({e}) — "
                f"falling back to glob"
            )
        finally:
            try:
                tmp_glb.unlink(missing_ok=True)
            except Exception:
                pass

        # Snapshot the stems this swap's .mfm chain pulled, then mirror
        # files into the shared dds_root for the existing routing pass.
        stems_for_swap: set[str] = set()
        for f in swap_dds_dir.iterdir():
            if not f.is_file():
                continue
            sfx = next(
                (s for s in (".dd0", ".dd1", ".dd2", ".dds")
                 if f.name.lower().endswith(s)), None,
            )
            if sfx is None:
                continue
            cls = _classify_channel(f.name[: -len(sfx)])
            if cls is None:
                continue
            stems_for_swap.add(cls[0])
            dst = dds_root / f.name
            if not dst.exists() or dst.stat().st_size != f.stat().st_size:
                shutil.copy2(f, dst)
        swap_to_stems[swap_aid] = stems_for_swap

    # Reverse map: material stem → set of vanilla aids whose swap mesh
    # binds this stem in its .mfm chain.
    stem_to_vanilla_aids: dict[str, set[str]] = {}
    for swap_aid, stems in swap_to_stems.items():
        van = swap_to_vanilla.get(swap_aid)
        if not van:
            continue
        for stem in stems:
            stem_to_vanilla_aids.setdefault(stem, set()).add(van)

    # Pull `.geometry` files alongside (no swizzling needed).
    geom_patterns = [f"**/{variant_asset_id}*.geometry"]
    for swap_aid in swap_to_vanilla:
        geom_patterns.append(f"**/{swap_aid}*.geometry")
    print(
        f"[skin_pack/vfs] extracting {len(geom_patterns)} geometry "
        f"pattern(s) → {cache_dir}/geom/"
    )
    toolkit.extract(geom_patterns, out_dir=cache_dir / "geom", config=config)

    # Emissive synthesis. ARP / Azur Lane / Sabaton crossover skins ship
    # `<stem>_emissive.mfm` siblings in the VFS that flag certain stems
    # as emissive — the .mfm's emissivePower scales `diffuse * mg.B`
    # into a glow texture. Run before the classifier walks the dir so
    # synthesized `<stem>_emissive.dd0/.dds` files land in the plan via
    # the regular CHANNEL_SLOTS path.
    try:
        synth_paths = synth_emission.synthesize_emissive_textures(
            dds_root, label=f"{skin_id}-skin", config=config,
        )
        if synth_paths:
            stems_lit = {p.stem.rsplit("_emissive", 1)[0] for p in synth_paths}
            print(
                f"[skin_pack/vfs] emissive synth: wrote {len(synth_paths)} "
                f"file(s) across {len(stems_lit)} stem(s) → {sorted(stems_lit)}"
            )
        else:
            print(
                "[skin_pack/vfs] emissive synth: no `*_emissive.mfm` matches "
                "in VFS for this skin's stems (non-emissive variant)"
            )
    except Exception as e:
        print(
            f"[skin_pack/vfs] emissive synth failed ({e}) — sidecar will "
            f"lack the emissive slot for this skin",
            file=sys.stderr,
        )

    # Deterministic variant→vanilla stem map.
    variant_to_vanilla_stem = _build_variant_stem_mapping(
        variant_glb=variant_glb_path,
        cache_dds_dir=dds_root,
        variant_asset_id=variant_asset_id,
        ship_sidecar=ship_sidecar,
        variant_mappings_json=(
            variant_mappings_json if variant_mappings_json.is_file() else None
        ),
    )
    if variant_to_vanilla_stem:
        print(
            f"[skin_pack/vfs] deterministic stem map: "
            f"{len(variant_to_vanilla_stem)} variant→vanilla pair(s):"
        )
        for v, n in sorted(variant_to_vanilla_stem.items()):
            print(f"        {v}  →  {n}")
    else:
        print(
            "[skin_pack/vfs] deterministic stem map: 0 pairs resolved "
            "(likely a non-mesh-swap skin); falling back to infix heuristic"
        )

    plan = _IngestPlan(
        skin_id=skin_id, display_name=display_name,
        source_label=f"vfs:{variant_asset_id} via {exterior_id}",
    )

    # Resolve the Exterior's mat_* per-category atlas.
    if camo_name:
        try:
            db = wg_camo.CamouflageDb.load(config=config)
            entry = db.find_entry_by_name(camo_name)
            if entry is None:
                print(
                    f"[skin_pack/vfs] camouflage {camo_name!r} not in DB — "
                    f"skipping mat_textures"
                )
            else:
                wg_camo.ensure_mat_camo_textures([entry], config=config)
                extracted = wg_camo.list_extracted_mips(
                    wg_camo._mat_dir(config),
                )
                full = wg_camo.mat_textures_for_entry(entry, extracted)
                # Drop hull-side categories — we extract those directly via
                # ASC080_*_<channel>.dds into per-material texture_sets[<skin_id>]
                # blocks; that's higher fidelity than the camo atlas.
                HULL_SIDE_CATEGORIES = {"tile", "deckhouse", "bulge"}
                plan.mat_textures = {
                    cat: data for cat, data in full.items()
                    if cat not in HULL_SIDE_CATEGORIES
                }
                dropped = sorted(set(full) & HULL_SIDE_CATEGORIES)
                if plan.mat_textures:
                    print(
                        f"[skin_pack/vfs] resolved {len(plan.mat_textures)} "
                        f"mat_albedo categor"
                        f"{'y' if len(plan.mat_textures)==1 else 'ies'} "
                        f"from {camo_name}: {sorted(plan.mat_textures)}"
                    )
                if dropped:
                    print(
                        f"[skin_pack/vfs] dropped {dropped} (hull-side categories "
                        f"handled by texture_sets)"
                    )
        except Exception as e:
            print(
                f"[skin_pack/vfs] mat_textures resolution failed ({e}) — "
                f"sidecar will carry hull paint only",
                file=sys.stderr,
            )

    ship_stems = _build_ship_side_stems(ship_sidecar)
    sorted_swap_keys = sorted(swap_to_vanilla, key=len, reverse=True)

    def rewrite_swap_stem(stem: str) -> str:
        """Map a stem from the variant's VFS extract to its vanilla
        counterpart. Four-stage priority (see lifted module docstring).
        """
        # 1. Deterministic stem map (highest priority).
        if stem in variant_to_vanilla_stem:
            return variant_to_vanilla_stem[stem]
        # 2. Infix heuristic for hull-side stems.
        if stem.startswith(variant_asset_id):
            variant_infix = stem[len(variant_asset_id):]
            mapped = vanilla_stems_by_infix.get(variant_infix.lower())
            if mapped is not None:
                return mapped
            hull_stem = vanilla_stems_by_infix.get("", "")
            return hull_stem + variant_infix
        # 3. Exact swap-asset_id.
        if stem in swap_to_vanilla:
            return swap_to_vanilla[stem]
        # 4. Peculiarity-infix prefix match.
        for swap_aid in sorted_swap_keys:
            if stem.startswith(swap_aid + "_"):
                return swap_to_vanilla[swap_aid]
        return stem

    # Index .geometry by stem (for verdict comparison).
    for geom in cache_dir.rglob("*.geometry"):
        stem = geom.stem
        if stem.endswith("_dead"):
            continue
        rewritten = rewrite_swap_stem(stem)
        if rewritten != stem:
            plan.accessory_geometries[rewritten] = geom

    # Walk EVERY toolkit-emitted DDS. apply_plan dedupes per (stem, slot)
    # by suffix priority so the renderer sees only conformant paths when
    # both exist. Only walk dds_root to avoid duplicates from per-swap dirs.
    for dds in sorted(dds_root.rglob("*.dd*")):
        if dds.suffix.lower() not in {".dd0", ".dd1", ".dd2", ".dds"}:
            continue
        stem_with_channel = dds.stem
        cls = _classify_channel(stem_with_channel)
        if cls is None:
            plan.skipped_dds.append((dds, "no recognised channel suffix"))
            continue
        stem, slot, prio = cls

        rewritten = rewrite_swap_stem(stem)

        if rewritten in ship_stems:
            plan.ship_side.append(_ShipSideTexture(
                src_path=dds, material_stem=rewritten, slot=slot, priority=prio,
            ))
            continue

        # Multi-target aid fanout for shared-mfm stems (ARP gun-diffuse case).
        target_aids: set[str] = set()
        if rewritten in library_index:
            target_aids.add(rewritten)
        if stem not in library_index:
            for aid in stem_to_vanilla_aids.get(stem, ()):
                if aid in library_index:
                    target_aids.add(aid)

        if target_aids:
            for aid in target_aids:
                plan.accessories.setdefault(aid, []).append(_AccessoryTexture(
                    src_path=dds, asset_id=aid, slot=slot, priority=prio,
                ))
            continue

        plan.skipped_dds.append((
            dds,
            f"rewritten stem {rewritten!r} (from {stem!r}) is neither a "
            f"ship-side material nor a library asset_id",
        ))

    return plan


# ---------------------------------------------------------------------------
# Verdict gating
# ---------------------------------------------------------------------------


def _run_comparison(
    plan: _IngestPlan,
    *,
    cache_dir: Path,
    config: PipelineConfig | None,
) -> dict[str, dict]:
    """For each accessory in the plan that has a .geometry file, run the
    mesh-comparison verdict against vanilla.

    Returns ``{asset_id: result_dict}``. Vanilla geometries are extracted
    via ``wowsunpack extract`` once (batched) and parsed with
    :mod:`wows_model_export.read.bw_geometry`. Accessories without a
    matching geometry file in the mod are recorded with verdict
    ``"texture_only"``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    asset_ids_with_geom = sorted(set(plan.accessories) & set(plan.accessory_geometries))
    asset_ids_no_geom   = sorted(set(plan.accessories) - set(plan.accessory_geometries))

    if asset_ids_with_geom:
        patterns = [f"**/{aid}.geometry" for aid in asset_ids_with_geom]
        print(
            f"  [skin_pack/verdict] extracting {len(patterns)} vanilla "
            f".geometry from VFS…"
        )
        toolkit.extract(patterns, out_dir=cache_dir, config=config)

    vanilla_by_id: dict[str, Path] = {}
    for f in cache_dir.rglob("*.geometry"):
        if f.stem in asset_ids_with_geom:
            vanilla_by_id.setdefault(f.stem, f)

    results: dict[str, dict] = {}
    for aid in asset_ids_with_geom:
        van_path = vanilla_by_id.get(aid)
        if van_path is None:
            results[aid] = {
                "verdict": "texture_only",
                "skip_reason": "no vanilla .geometry found in VFS",
            }
            continue
        try:
            mod_snaps = csm.load_all_prims(plan.accessory_geometries[aid], "mod", aid)
            van_snaps = csm.load_all_prims(van_path,                       "vanilla", aid)
            mod_snap, van_snap = csm.pair_lod0_via_best_match(mod_snaps, van_snaps)
            cr = csm.compare(mod_snap, van_snap)
            results[aid] = {
                "verdict":      cr.verdict,
                "v2m_p95":      (cr.uv_stats or {}).get("vanilla_to_mod", {}).get("p95"),
                "v2m_tight":    (cr.uv_stats or {}).get("vanilla_to_mod", {}).get("pct_within_tight"),
                "v2m_covered":  (cr.uv_stats or {}).get("vanilla_to_mod", {}).get("covered_pct"),
                "mod_verts":    (cr.mod or {}).get("vertex_count"),
                "vanilla_verts": (cr.vanilla or {}).get("vertex_count"),
            }
        except Exception as e:
            results[aid] = {
                "verdict": "error",
                "skip_reason": f"{type(e).__name__}: {e}",
            }

    for aid in asset_ids_no_geom:
        results[aid] = {
            "verdict": "texture_only",
            "skip_reason": (
                "mod has no .geometry for this asset — texture-only swap, "
                "applies to vanilla mesh"
            ),
        }
    return results


# ---------------------------------------------------------------------------
# Apply: copy DDS + patch sidecar
# ---------------------------------------------------------------------------


def _apply_plan(
    plan: _IngestPlan,
    *,
    ship_dir: Path,
    sidecar_path: Path,
    verdicts: dict[str, dict],
) -> dict:
    """Materialise the plan: copy DDS files into the skin directory and
    patch the sidecar via the canonical writer. Returns a summary dict.
    """
    skin_dir = ship_dir / read_sidecar.MODELS_SUBDIR / "skins" / plan.skin_id
    skin_dir.mkdir(parents=True, exist_ok=True)
    accessories_subdir = skin_dir / "accessories"

    # Sidecar update: build the new texture_sets entries + Skin entry.
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))

    # 1. Ship-side: per-material texture_sets[<skin_id>] additions.
    grouped_ship: dict[tuple[str, str], dict[int, list[_ShipSideTexture]]] = {}
    for tx in plan.ship_side:
        bucket = grouped_ship.setdefault((tx.material_stem, tx.slot), {})
        bucket.setdefault(tx.priority, []).append(tx)
    by_stem_textures: dict[str, dict[str, list[str]]] = {}
    for (stem, slot), prio_buckets in grouped_ship.items():
        best_prio = min(prio_buckets)
        for tx in prio_buckets[best_prio]:
            dst = skin_dir / tx.src_path.name
            if not dst.exists() or dst.stat().st_size != tx.src_path.stat().st_size:
                shutil.copy2(tx.src_path, dst)
            rel = f"skins/{plan.skin_id}/{tx.src_path.name}"
            by_stem_textures.setdefault(stem, {}).setdefault(slot, []).append(rel)
        # Lower-priority files still copied to disk (offline workflows).
        for prio, txs in prio_buckets.items():
            if prio == best_prio:
                continue
            for tx in txs:
                dst = skin_dir / tx.src_path.name
                if not dst.exists() or dst.stat().st_size != tx.src_path.stat().st_size:
                    shutil.copy2(tx.src_path, dst)

    n_materials_patched = 0
    for m in sc.get("materials") or []:
        if not isinstance(m, dict):
            continue
        ts = m.get("texture_sets")
        if not isinstance(ts, dict):
            continue
        main_bc = (ts.get("main") or {}).get("baseColor") or {}
        mips = main_bc.get("dds_mips") or []
        if not mips:
            continue
        first = Path(mips[0]).name
        for ext in (".dd0", ".dd1", ".dd2", ".dds"):
            if first.lower().endswith(ext):
                first = first[: -len(ext)]
                break
        stem_cls = _classify_channel(first)
        if stem_cls is None:
            continue
        stem = stem_cls[0]
        new_slots = by_stem_textures.get(stem)
        if not new_slots:
            continue
        # Replace per-skin block wholesale so a re-ingest can drop slots
        # that no longer exist. Other schemes left untouched.
        ts[plan.skin_id] = {
            slot: {"dds_mips": paths}
            for slot, paths in new_slots.items()
        }
        n_materials_patched += 1

    # 2. Accessory overrides.
    asset_overrides: dict[str, dict] = {}
    n_accessories_kept = 0
    n_accessories_skipped = 0
    for aid, slots in plan.accessories.items():
        v = verdicts.get(aid, {"verdict": "texture_only"})
        verdict = v.get("verdict", "texture_only")

        entry: dict[str, object] = {"verdict": verdict}
        if verdict in APPLICABLE_VERDICTS:
            asset_dir = accessories_subdir / aid
            asset_dir.mkdir(parents=True, exist_ok=True)
            grouped_acc: dict[str, dict[int, list[_AccessoryTexture]]] = {}
            for tx in slots:
                grouped_acc.setdefault(tx.slot, {}).setdefault(tx.priority, []).append(tx)
            tex_set: dict[str, dict] = {}
            for slot, prio_buckets in grouped_acc.items():
                best_prio = min(prio_buckets)
                kept_paths: list[str] = []
                for tx in prio_buckets[best_prio]:
                    dst = asset_dir / tx.src_path.name
                    if not dst.exists() or dst.stat().st_size != tx.src_path.stat().st_size:
                        shutil.copy2(tx.src_path, dst)
                    kept_paths.append(
                        f"skins/{plan.skin_id}/accessories/{aid}/{tx.src_path.name}"
                    )
                for prio, txs in prio_buckets.items():
                    if prio == best_prio:
                        continue
                    for tx in txs:
                        dst = asset_dir / tx.src_path.name
                        if not dst.exists() or dst.stat().st_size != tx.src_path.stat().st_size:
                            shutil.copy2(tx.src_path, dst)
                tex_set[slot] = {"dds_mips": kept_paths}
            entry["texture_sets"] = {"main": tex_set}
            n_accessories_kept += 1
        else:
            entry["skip_reason"] = v.get("skip_reason") or (
                f"verdict={verdict}; mod texture wouldn't sample correctly on vanilla mesh"
            )
            entry["fallback"] = "vanilla"
            n_accessories_skipped += 1
        asset_overrides[aid] = entry

    # 3. New Skin entry.
    new_skin = resolve_sidecar.make_skin(
        skin_id=plan.skin_id,
        display_name=plan.display_name,
        scheme_key=plan.skin_id,
        kind="mat_albedo",
        source=plan.source_label,
        mat_textures=plan.mat_textures or None,
        asset_overrides=asset_overrides,
    )
    skins = list(sc.get("skins") or [])
    # Replace or append by skin_id.
    out_skins = [
        s for s in skins
        if not (isinstance(s, dict) and s.get("skin_id") == plan.skin_id)
    ]
    out_skins.append(new_skin)
    sc["skins"] = out_skins

    # Write back via the compose layer's atomic writer.
    from . import sidecar as compose_sidecar
    compose_sidecar.write(sc, sidecar_path)

    return {
        "skin_dir":             str(skin_dir),
        "materials_patched":    n_materials_patched,
        "accessories_kept":     n_accessories_kept,
        "accessories_skipped":  n_accessories_skipped,
        "skipped_dds":          len(plan.skipped_dds),
    }


# ---------------------------------------------------------------------------
# Step timer
# ---------------------------------------------------------------------------


class _StepTimer:
    """Records wall time per step + emits :class:`StepEvent`s.

    A no-op when ``on_event`` is ``None`` so consumers that don't care
    about progress pay zero per-step overhead.
    """

    def __init__(self, on_event: OnEvent | None) -> None:
        self.on_event = on_event
        self.spans: dict[str, float] = {}
        self._t_run = time.perf_counter()
        self._t_step: float | None = None
        self._step: str | None = None

    def _emit(
        self,
        step: str,
        state: str,
        *,
        detail: str = "",
        step_ms: float | None = None,
        data: dict | None = None,
    ) -> None:
        if self.on_event is None:
            return
        elapsed_ms = (time.perf_counter() - self._t_run) * 1000.0
        self.on_event(
            StepEvent(
                step=step,
                state=state,
                detail=detail,
                elapsed_ms=elapsed_ms,
                step_ms=step_ms,
                data=data,
            )
        )

    def start(self, step: str, *, detail: str = "") -> None:
        self._step = step
        self._t_step = time.perf_counter()
        self._emit(step, "started", detail=detail)

    def complete(self, *, detail: str = "", data: dict | None = None) -> None:
        if self._step is None or self._t_step is None:
            return
        step_ms = (time.perf_counter() - self._t_step) * 1000.0
        self.spans[self._step] = step_ms
        self._emit(self._step, "completed", detail=detail, step_ms=step_ms, data=data)
        self._step = None
        self._t_step = None

    def skip(self, step: str, *, detail: str = "") -> None:
        self._emit(step, "skipped", detail=detail)


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def ingest_skin_pack(
    skin_source: Path | str,
    *,
    ship_id: str,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    skin_id: str | None = None,
    display_name: str | None = None,
    source_kind: Literal["loose_mod", "vfs_variant", "auto"] = "auto",
    on_event: OnEvent | None = None,
) -> SkinPackResult:
    """Ingest a skin pack and append it to the ship's sidecar.

    Parameters
    ----------
    skin_source
        Either a loose-mod folder path (``Path`` / str) or a VFS-variant
        identifier — a variant asset_id (``"ASC080_Baltimore_1944_Azur"``),
        a Vehicle/Exterior GameParams ID (``"PJSC705"`` / ``"PJES477_ARP_TAKAO"``),
        or any of the above as a string.
    ship_id
        Filesystem label of the ship whose sidecar gets the new
        ``skins[]`` entry (matches ``<workspace>/<ship_id>/``).
    workspace
        Workspace root. Defaults to ``config.workspace``.
    config
        Pipeline configuration; loaded from env vars when omitted.
    skin_id
        Caller-chosen skin identifier (also the ``scheme_key`` and the
        on-disk folder name). When ``None``, derived from the source:
        loose-mod → mod-folder basename; vfs-variant → Exterior entity ID.
    display_name
        Override the auto-derived display name. For Exterior-shaped
        skin_ids the localization catalogue's English label is used by
        default; otherwise the raw ``skin_id`` is used.
    source_kind
        ``"loose_mod"`` for content-SDK mod folders, ``"vfs_variant"``
        for WG-authored permoflage variants, ``"auto"`` to detect from
        ``skin_source``.
    on_event
        Optional callback invoked at each step boundary with a
        :class:`StepEvent`. Canonical step names:
        ``detect_source`` / ``extract_textures`` / ``swizzle_textures`` /
        ``compare_meshes`` / ``compare_exteriors`` / ``build_skin_entry`` /
        ``merge_sidecar``.

    Returns
    -------
    :class:`SkinPackResult`
        Sidecar path that received the new skin, the resolved skin_id,
        which source mode was used, whether the swizzle pass fired
        (loose-mod only), and any warnings.

    Raises
    ------
    :class:`wows_model_export.errors.StepError`
        Wraps the underlying exception from whichever step failed. The
        ``.step`` attribute identifies the failure point.
    """
    cfg = config or PipelineConfig.load()
    if workspace is None:
        workspace = cfg.workspace
    workspace = Path(workspace)

    ship_dir = (workspace / ship_id).resolve()
    sidecar_path = read_sidecar.sidecar_path_for(ship_dir, ship_id)
    if not sidecar_path.is_file():
        raise StepError(
            step="detect_source",
            underlying=FileNotFoundError(
                f"sidecar missing for {ship_id!r}: {sidecar_path}"
            ),
            detail=f"sidecar not found at {sidecar_path}",
        )

    timer = _StepTimer(on_event)
    warnings: list[str] = []
    swizzled_flag = [False]

    # ── Step: detect_source ──────────────────────────────────────────
    timer.start("detect_source", detail=str(skin_source))
    try:
        skin_source_path = Path(skin_source)
        # Resolution rules:
        #   - explicit kind wins
        #   - auto: existing directory → loose_mod; otherwise vfs_variant
        if source_kind == "auto":
            if skin_source_path.is_dir():
                resolved_kind: Literal["loose_mod", "vfs_variant"] = "loose_mod"
            else:
                resolved_kind = "vfs_variant"
        else:
            resolved_kind = source_kind  # type: ignore[assignment]

        variant_asset_id: str | None = None
        exterior_id: str | None = None
        mod_dir: Path | None = None
        if resolved_kind == "loose_mod":
            mod_dir = skin_source_path.resolve()
            if not mod_dir.is_dir():
                raise FileNotFoundError(
                    f"loose-mod source dir not found: {mod_dir}"
                )
            if skin_id is None:
                skin_id = mod_dir.name
        else:
            # VFS variant: either we got a variant asset_id directly or a
            # Vehicle/Exterior ID. Try the WG resolver first; if it raises
            # because the user passed a bare asset_id (no `nativePermoflage`
            # / no `/ship/` peculiarityModel), treat the source as the
            # variant asset_id and require the caller to provide an
            # Exterior via a future skin_pack option (Phase 2). For now,
            # always go through the resolver and surface a clear error
            # otherwise.
            wg_id = str(skin_source)
            variant_asset_id, exterior_id = _resolve_wg_source(wg_id)
            if skin_id is None:
                skin_id = exterior_id
    except Exception as e:
        timer._emit("detect_source", "failed")
        raise StepError(
            step="detect_source",
            underlying=e,
            detail=f"couldn't resolve source {skin_source!r}",
        ) from e
    timer.complete(detail=f"{resolved_kind}: skin_id={skin_id}")

    if display_name is None:
        display_name = _default_display_name(skin_id)

    # Load the ship's sidecar + library index up front (read-only).
    sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    library_index = _build_library_asset_index(workspace)
    if not library_index:
        warnings.append(
            "libraries/accessories/index.json not found or empty — "
            "accessory classification will fall through; only ship-side "
            "textures will be ingested"
        )

    cache_dir_base = cfg.cache_dir or (workspace / ".cache")
    cache_dir = (cache_dir_base / "skin_pack_cache" / skin_id).resolve()

    # ── Step: extract_textures ───────────────────────────────────────
    timer.start("extract_textures", detail=resolved_kind)
    try:
        if resolved_kind == "loose_mod":
            assert mod_dir is not None
            # Loose-mod path: extraction is implicit (files are already
            # on disk); the swizzle step below is what makes this a
            # multi-stage flow. The "extract" step is therefore a thin
            # source-acknowledgement.
            timer.complete(detail=f"loose mod at {mod_dir}")
        else:
            assert variant_asset_id is not None and exterior_id is not None
            # We don't pre-extract for vfs_variant here — the plan loader
            # below runs export-ship + per-swap export-model with its own
            # cache. This event marks the point we know the source is
            # accepted for VFS extraction (the actual subprocess fires in
            # build_skin_entry).
            timer.complete(detail=f"vfs variant {variant_asset_id}")
    except Exception as e:
        timer._emit("extract_textures", "failed")
        raise StepError(
            step="extract_textures",
            underlying=e,
            detail="source-mode extraction setup failed",
        ) from e

    # ── Step: swizzle_textures (loose_mod only) ──────────────────────
    if resolved_kind == "loose_mod":
        assert mod_dir is not None
        timer.start("swizzle_textures", detail=str(mod_dir))
        try:
            # Loose mods ship raw WG-pack ``_n.dds`` / ``_mg.dds`` channel
            # layouts that don't go through the VFS-extract pipeline (and
            # therefore miss the implicit Phase B swizzle). Running the
            # swizzle once at ingest emits ``_normal.dds`` /
            # ``_nbmask.dds`` / ``_mr.dds`` siblings on disk so every
            # consumer sees stock glTF-conformant inputs. Idempotent —
            # re-runs skip existing siblings.
            result = toolkit.swizzle_dir(mod_dir, recursive=True, config=cfg)
            info = result.data or {}
            processed = int(info.get("processed", 0))
            siblings_written = int(info.get("siblings_written", 0))
            swizzled_flag[0] = True
            swizzle_data: dict = {
                "processed": processed,
                "siblings_written": siblings_written,
            }
        except Exception as e:
            # Non-fatal: webview-side WG-pack uniforms still render
            # correctly with raw `_n`/`_mg`; Unity-side rendering will
            # be off until the swizzle lands but that's not a blocker.
            warnings.append(
                f"swizzle-dir failed ({e}) — continuing with raw _n/_mg only"
            )
            swizzle_data = {"failed": str(e)}
        timer.complete(data=swizzle_data)
    else:
        timer.skip("swizzle_textures", detail="vfs_variant: --raw-dds-dir handles swizzle")

    # ── Step: compare_exteriors (vfs_variant only) ───────────────────
    if resolved_kind == "vfs_variant":
        timer.start("compare_exteriors")
        try:
            # Exterior validation happens inside _load_vfs_variant; the
            # exterior_compare module's extract_swaps walker provides the
            # peculiarityModels diff. This event is for consumers tracking
            # the (potentially slow) GameParams-walk + swap enumeration.
            pass
        except Exception as e:
            timer._emit("compare_exteriors", "failed")
            raise StepError(
                step="compare_exteriors",
                underlying=e,
                detail="Exterior peculiarityModels walk failed",
            ) from e
        timer.complete()
    else:
        timer.skip("compare_exteriors", detail="loose_mod: no Exterior to validate")

    # ── Step: build_skin_entry ───────────────────────────────────────
    timer.start("build_skin_entry")
    plan: _IngestPlan
    try:
        if resolved_kind == "loose_mod":
            assert mod_dir is not None
            plan = _load_loose_mod(
                mod_dir,
                skin_id=skin_id,
                display_name=display_name,
                ship_sidecar=sc,
                library_index=library_index,
            )
        else:
            assert variant_asset_id is not None and exterior_id is not None
            plan = _load_vfs_variant(
                variant_asset_id=variant_asset_id,
                exterior_id=exterior_id,
                skin_id=skin_id,
                display_name=display_name,
                ship_sidecar=sc,
                ship_label=ship_id,
                ship_dir=ship_dir,
                library_index=library_index,
                cache_dir=cache_dir / "vfs_extract",
                config=cfg,
            )
    except Exception as e:
        timer._emit("build_skin_entry", "failed")
        raise StepError(
            step="build_skin_entry",
            underlying=e,
            detail=f"plan build for {resolved_kind} failed",
        ) from e
    timer.complete(
        detail=(
            f"{len(plan.ship_side)} ship-side, "
            f"{sum(len(v) for v in plan.accessories.values())} accessory DDS, "
            f"{len(plan.skipped_dds)} skipped"
        ),
        data={
            "ship_side_count": len(plan.ship_side),
            "accessory_dds_count": sum(len(v) for v in plan.accessories.values()),
            "skipped_dds_count": len(plan.skipped_dds),
        },
    )

    # ── Step: compare_meshes ─────────────────────────────────────────
    timer.start("compare_meshes")
    try:
        verdicts = _run_comparison(plan, cache_dir=cache_dir, config=cfg)
        by_verdict: dict[str, int] = {}
        for v in verdicts.values():
            by_verdict[v["verdict"]] = by_verdict.get(v["verdict"], 0) + 1
    except Exception as e:
        timer._emit("compare_meshes", "failed")
        raise StepError(
            step="compare_meshes",
            underlying=e,
            detail="mesh-comparison verdict pass failed",
        ) from e
    timer.complete(
        detail=", ".join(f"{k}={n}" for k, n in sorted(by_verdict.items(), key=lambda kv: -kv[1])),
        data={"by_verdict": by_verdict},
    )

    # ── Step: merge_sidecar ──────────────────────────────────────────
    timer.start("merge_sidecar", detail=sidecar_path.name)
    try:
        summary = _apply_plan(
            plan, ship_dir=ship_dir, sidecar_path=sidecar_path, verdicts=verdicts,
        )
    except Exception as e:
        timer._emit("merge_sidecar", "failed")
        raise StepError(
            step="merge_sidecar",
            underlying=e,
            detail=f"sidecar merge/write to {sidecar_path} failed",
        ) from e
    timer.complete(data=summary)

    return SkinPackResult(
        ship_id=ship_id,
        sidecar_path=sidecar_path,
        skin_id=skin_id,
        source=resolved_kind,
        swizzled=bool(swizzled_flag[0]),
        warnings=tuple(warnings),
    )


__all__ = ["ingest_skin_pack"]
