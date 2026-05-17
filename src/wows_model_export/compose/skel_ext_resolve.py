"""Compose `resolve_decorative_placements` -- merge HP_ placements +
skel_ext decorative candidates into the unified accessories JSON.

Lifted from ``tools/ship/skel_ext_resolve.py`` on the I:-side warships
repo. This is the Layer 4 orchestrator that reads two toolkit-produced
inputs:

  * ``<Ship>_placements.json``  -- HP_-only mounts from
    ``wowsunpack export-ship --placements-json``.
  * ``<Ship>_skel_ext.json``    -- decorative placement candidates from
    ``wowsunpack export-ship --skel-ext-candidates-json``.

and emits ``<Ship>_accessories.json`` -- the canonical sidecar input
consumed by :mod:`wows_model_export.compose.scaffold_ship`.

Resolves each candidate's ``p0_hash`` via
:func:`wows_model_export.resolve.skel_ext_hashes.resolve_candidates`
against a Murmur3_32 lookup table built from ``assets.bin``'s
``MP_*`` / ``SP_*`` string corpus, catching the full set of WG
decoratives.

Canonical :class:`StepEvent` names emitted at step boundaries:

    "load_inputs"     -- read both JSON files + auto-discover side files
    "resolve_hashes"  -- p0_hash table lookup via resolve.skel_ext_hashes
    "merge_placements" -- HP_ dedup + on-hull filter + classification
    "write_output"    -- emit final accessories JSON

Per-step failures are wrapped in :class:`StepError` with ``step=`` set
to one of the names above; ``raise ... from e`` preserves the chain.
"""
from __future__ import annotations

import json
import math
import struct
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .. import toolkit
from ..config import PipelineConfig
from ..errors import StepError
from ..resolve import skel_ext_hashes
from ..types import OnEvent
from ._step_runner import StepRunner

# VFS manifest path resolution lives in :mod:`wows_model_export.toolkit.vfs`.
# The composer entry resolves a path via :func:`toolkit.default_manifest_path`
# (override: ``WOWS_VFS_MANIFEST`` env var). Hash-mode is read-only over the
# manifest, so the file is not built on demand here -- a missing manifest
# degrades into a warning rather than triggering an extract pass.

# Position-dedup tolerance for "same physical placement" between a
# candidate and an HP_-bound mount -- catches peculiarityModels swap
# variants at the same slot. 10cm tolerance handles float drift between
# the two emit paths.
_SWAP_POSITION_TOL_M = 0.1

# Base hull-section names. WG's skel_ext segment field carries these
# verbatim for normal placements, plus themed variants like
# `Bow_base_battle_A`, `Bow_base_dock_A`, `Bow_GermanArc2020`,
# `MidBack_Hull_A` for crossover / Halloween / soviet-arc / dock-state
# overlays. Downstream consumers (webview `placement.ts`, Unity
# `ShipPrefabBuilder.cs`) key materials and damage-state visibility off
# the four canonical sections; the resolver normalizes
# `parent_section` to the base prefix so the lookup hits.
_HULL_SECTIONS = ("MidFront", "MidBack", "Bow", "Stern")


def _normalize_section(segment: str) -> str | None:
    """Map a raw skel_ext segment string to one of the four canonical
    hull sections, or ``None`` if it doesn't carry a base-section
    prefix. Order in ``_HULL_SECTIONS`` matters: ``MidFront`` /
    ``MidBack`` come before ``Bow`` / ``Stern`` so the longer prefixes
    win over their shorter substrings."""
    if not segment:
        return None
    for s in _HULL_SECTIONS:
        if segment == s or segment.startswith(s + "_"):
            return s
    return None



# ---------------------------------------------------------------------------
# Hull-GLB patch-mesh parsing
# ---------------------------------------------------------------------------
#
# Patches are bridge meshes between adjacent hull sections (e.g.
# `MidFront_patch_MidBackShape`). Webview's damage cascade
# (`damage_cascade.ts`) hides them when the bridged seam is Broken; any
# accessory whose placement is inside a patch's AABB needs to hide with
# it. This is the only hull-GLB-derived signal the resolver actually
# needs — section / sub-mesh assignment is WG-authoritative via the
# `segment` field. See `reference/topics/audits/near_origin_filter_audit.md`
# for the audit that led to this slim parse.


def parse_hull_patches(
    glb_path: Path,
) -> dict[str, tuple[tuple[float, ...], tuple[float, ...]]]:
    """Parse a toolkit-emitted hull GLB; return ``{patch_name: (mn, mx)}``
    for every mesh whose short name contains ``_patch_``. Reads only the
    GLB JSON chunk and the POSITION accessor min/max fields. Returns
    ``{}`` on any IO/parse failure or when the GLB carries no patches.
    """
    try:
        data = Path(glb_path).read_bytes()
    except (OSError, FileNotFoundError):
        return {}
    if len(data) < 28 or data[:4] != b"glTF":
        return {}
    json_chunk_len = struct.unpack_from("<I", data, 12)[0]
    try:
        gltf = json.loads(data[20:20 + json_chunk_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}

    accessors = gltf.get("accessors", [])
    meshes = gltf.get("meshes", [])
    nodes = gltf.get("nodes", [])

    INF = float("inf")
    out: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
    for n in nodes:
        nm = n.get("name", "") or ""
        short = nm.split(" / ")[-1]
        if "_patch_" not in short.lower():
            continue
        if "mesh" not in n:
            continue
        mesh = meshes[n["mesh"]] if n["mesh"] < len(meshes) else None
        if mesh is None:
            continue
        mn = [INF, INF, INF]
        mx = [-INF, -INF, -INF]
        for prim in mesh.get("primitives", []):
            pos_idx = prim.get("attributes", {}).get("POSITION")
            if pos_idx is None or pos_idx >= len(accessors):
                continue
            acc = accessors[pos_idx]
            a_mn, a_mx = acc.get("min"), acc.get("max")
            if not (a_mn and a_mx and len(a_mn) == 3 and len(a_mx) == 3):
                continue
            for i in range(3):
                if a_mn[i] < mn[i]:
                    mn[i] = a_mn[i]
                if a_mx[i] > mx[i]:
                    mx[i] = a_mx[i]
        if INF in mn:
            continue
        out[short] = (tuple(mn), tuple(mx))
    return out


def _patch_for_position(
    pos: tuple[float, float, float] | list[float],
    patches: dict[str, tuple[tuple[float, ...], tuple[float, ...]]],
) -> str | None:
    """Return the first patch mesh whose AABB contains ``pos``, else
    ``None``. Order is hull-GLB declaration order; in the live corpus
    patch AABBs don't overlap so the first match is unambiguous."""
    for name, (mn, mx) in patches.items():
        if all(mn[i] <= pos[i] <= mx[i] for i in range(3)):
            return name
    return None


# DELETED 2026-05-15: full hull-mesh classification (section_aabbs,
# mesh_aabbs, library_aabbs, is_placement_on_hull, classify_accessory,
# transform_aabb, aabb_overlap_volume). Replaced by WG-authoritative
# `segment` field for parent_section + patch-only AABB check for
# parent_mesh. See `reference/topics/audits/near_origin_filter_audit.md`.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp_dead_asset_id_in_place(placements: dict, dead_stems: set) -> int:
    """Stamp ``dead_asset_id: str | null`` on every placement entry.
    Returns the count of entries with a non-null dead variant.
    """
    n_with_dead = 0
    for section in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for entry in placements.get(section, []):
            aid = entry.get("asset_id", "") or ""
            if aid and aid.lower() in dead_stems:
                entry["dead_asset_id"] = f"{aid}_dead"
                n_with_dead += 1
            else:
                entry["dead_asset_id"] = None
    return n_with_dead


def build_vfs_name_index(
    manifest_path: Path,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Scan the VFS manifest for ``.geometry`` files; return
    ``(asset_name_index, dead_variant_stems)``.

    Raises :class:`FileNotFoundError` if the manifest is missing -- the
    caller should treat this as a degraded-mode warning, not a hard
    error.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"VFS manifest not found at {manifest_path}. "
            f"Generate it via `wowsunpack metadata --format json --output {manifest_path}`."
        )
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    index: dict[str, tuple[str, str]] = {}
    dead_stems: set[str] = set()
    for entry in manifest:
        p = entry.get("path", "")
        if not p.endswith(".geometry") or "/content/gameplay/" not in p:
            continue
        parts = p.strip("/").split("/")
        if len(parts) < 6:
            continue
        full_stem = parts[-1].removesuffix(".geometry")
        dir_path = "/".join(parts[:-1])

        if full_stem.endswith("_dead"):
            base_stem = full_stem[:-len("_dead")]
            dead_stems.add(base_stem.lower())
            continue

        index[full_stem.lower()] = (full_stem, dir_path)
    return index, dead_stems


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _parse_counter_suffix(instance_id: str) -> int:
    """Extract trailing integer counter from an instance_id like
    ``Montana_AGM034_16in50_Mk7_00``. Returns -1 if none.
    """
    if not instance_id:
        return -1
    tail = instance_id.rsplit("_", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return -1


def _build_resolve_context(
    placements: dict,
    manifest_path: Path,
    warnings: list[str],
) -> dict:
    """Build the VFS name index + HP dedup keys + asset counter state."""
    try:
        vfs_name_index, dead_stems = build_vfs_name_index(manifest_path)
    except FileNotFoundError as e:
        warnings.append(
            f"VFS manifest unavailable ({e}); asset metadata derivation degraded"
        )
        vfs_name_index = {}
        dead_stems = set()

    ship_info = placements.get("ship", {})
    ship_stem = ship_info.get("display_name") or ship_info.get("model_dir", "ship")

    asset_counts: dict[str, int] = {}
    for entry in placements.get("accessories", []):
        aid = entry.get("asset_id", "")
        if aid:
            asset_counts[aid] = max(
                asset_counts.get(aid, 0),
                _parse_counter_suffix(entry.get("instance_id", "")) + 1,
            )

    existing_hp_keys: set[tuple[str, str, str]] = set()
    existing_hp_positions: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for section in ("turrets", "secondaries", "antiair", "torpedoes", "accessories"):
        for entry in placements.get(section, []):
            aid = entry.get("asset_id", "")
            if not aid:
                continue
            scope_lc = (entry.get("scope") or "").lower()
            category_lc = (entry.get("category") or "").lower()
            if scope_lc or category_lc:
                existing_hp_keys.add((scope_lc, category_lc, aid.lower()))
            pos = (entry.get("transform") or {}).get("position")
            if isinstance(pos, list) and len(pos) == 3:
                existing_hp_positions.setdefault((scope_lc, category_lc), []).append(
                    (float(pos[0]), float(pos[1]), float(pos[2]))
                )

    return {
        "vfs_name_index":          vfs_name_index,
        "dead_stems":              dead_stems,
        "ship_stem":               ship_stem,
        "asset_counts":            asset_counts,
        "existing_hp_keys":        existing_hp_keys,
        "existing_hp_positions":   existing_hp_positions,
    }


def _norm_offset(s: Any) -> str:
    if isinstance(s, int):
        return f"0x{s:X}"
    s = str(s).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return f"0x{int(s, 16):X}"


# ---------------------------------------------------------------------------
# Core hash-mode resolver (lifted; print()s removed in favor of warnings)
# ---------------------------------------------------------------------------


def _resolve_hash_mode(
    placements_json: Path,
    candidates_json: Path,
    output_json: Path,
    *,
    config: PipelineConfig,
    runner: StepRunner,
    manifest_path: Path | None = None,
    hull_glb: Path | None = None,
    include_dock: bool = False,
    drop_skinned: bool = True,
    ship_nation: str | None = None,
    extra_scopes: tuple[str, ...] = ("common",),
    drop_degenerate: bool = True,
    keep_record_offsets: tuple[str, ...] | None = ("0x0",),
    warnings: list[str] | None = None,
) -> dict:
    """End-to-end hash-mode resolve. See the public
    :func:`resolve_decorative_placements` for the documented filter list.
    """
    if warnings is None:
        warnings = []
    if manifest_path is None:
        manifest_path = toolkit.default_manifest_path(config)

    # ── Step: load_inputs ─────────────────────────────────────────────
    with runner.step("load_inputs", detail=str(placements_json.name)) as st:
        placements = _load_json(placements_json)
        candidates_doc = _load_json(candidates_json)
        candidates = candidates_doc.get("candidates", [])

        if ship_nation is None:
            nation_raw = (placements.get("ship") or {}).get("nation") or ""
            resolved_nation = nation_raw.lower().strip()
        else:
            resolved_nation = ship_nation.lower().strip()
        allowed_scopes: set[str] | None
        if resolved_nation:
            allowed_scopes = {resolved_nation, *(s.lower() for s in extra_scopes)}
        else:
            allowed_scopes = None

        if keep_record_offsets is None or len(keep_record_offsets) == 0:
            allowed_offsets: set[str] | None = None
        else:
            allowed_offsets = {_norm_offset(s) for s in keep_record_offsets}

        if hull_glb is None:
            stem = placements_json.stem
            if stem.endswith("_placements"):
                stem = stem[:-len("_placements")]
            hull_glb = placements_json.with_name(f"{stem}_hull.glb")
        patches_aabbs = (
            parse_hull_patches(hull_glb) if hull_glb.exists() else {}
        )

        ctx = _build_resolve_context(placements, manifest_path, warnings)
        st.annotate(
            f"candidates={len(candidates):,} patches={len(patches_aabbs)}",
            data={
                "candidates_total":   len(candidates),
                "patches_indexed":    len(patches_aabbs),
                "allowed_scopes":     sorted(allowed_scopes) if allowed_scopes else None,
                "allowed_offsets":    sorted(allowed_offsets) if allowed_offsets else None,
            },
        )

    # ── Step: resolve_hashes ──────────────────────────────────────────
    with runner.step("resolve_hashes", detail=f"{len(candidates):,} candidates") as st:
        cache_path = config.require_cache_dir() / "skel_ext_hashes.json"
        table = skel_ext_hashes.load_or_build(cache=cache_path)
        resolved = skel_ext_hashes.resolve_candidates(candidates, table)
        st.annotate(
            f"resolved {resolved['summary']['resolved']:,}/{len(candidates):,} "
            f"({resolved['summary']['unique_asset_ids']:,} unique assets)",
            data=resolved["summary"],
        )

    # ── Step: merge_placements ────────────────────────────────────────
    with runner.step("merge_placements") as st:
        n_dead_stamped = _stamp_dead_asset_id_in_place(placements, ctx["dead_stems"])

        # Build asset_id → (scope, category) map from vfs_name_index.
        asset_meta: dict[str, tuple[str | None, str | None]] = {}
        for camel_id, vfs_dir in ctx["vfs_name_index"].values():
            if not vfs_dir:
                asset_meta[camel_id] = (None, None)
                continue
            parts = vfs_dir.replace("\\", "/").strip("/").split("/")
            if parts[:2] == ["content", "gameplay"]:
                parts = parts[2:]
            scope = parts[0] if parts else None
            category = parts[1] if len(parts) >= 2 else None
            asset_meta[camel_id] = (scope, category)

        merged: list[dict] = list(placements.get("accessories", []))
        n_hp_mesh_resolved = 0
        for entry in merged:
            if entry.get("parent_mesh") is not None:
                continue
            mat = (entry.get("transform") or {}).get("matrix")
            if not mat or len(mat) != 16:
                continue
            pm = _patch_for_position(
                (mat[12], mat[13], mat[14]), patches_aabbs,
            )
            if pm is not None:
                entry["parent_mesh"] = pm
                n_hp_mesh_resolved += 1

        def _swap_variant_of_existing_hp(
            scope_low: str, category_low: str, position: list[float],
        ) -> bool:
            hp_positions = ctx["existing_hp_positions"].get(
                (scope_low, category_low), [],
            )
            for hx, hy, hz in hp_positions:
                dx = position[0] - hx
                dy = position[1] - hy
                dz = position[2] - hz
                if (dx * dx + dy * dy + dz * dz) ** 0.5 <= _SWAP_POSITION_TOL_M:
                    return True
            return False

        skipped_already_in_hp = 0
        skipped_swap_variant_at_hp = 0
        skipped_dock = 0
        skipped_skinned = 0
        skipped_cross_nation = 0
        skipped_degenerate = 0
        skipped_variant_block = 0
        skipped_unresolved = (
            len(candidates) - resolved["summary"]["resolved"]
        )
        emitted_by_category: dict[tuple, int] = defaultdict(int)
        cross_nation_by_scope: dict[str, int] = defaultdict(int)
        n_patch_anchored = 0

        sorted_resolved = sorted(
            resolved["resolved"],
            key=lambda c: (
                c.get("segment", ""), c.get("p0_hash", ""),
                c.get("instance_index") or 0,
            ),
        )

        for cand in sorted_resolved:
            asset_id = cand["asset_id"]
            scope, category = asset_meta.get(asset_id, (None, None))

            if allowed_offsets is not None:
                try:
                    cand_off = _norm_offset(cand.get("record_offset"))
                except (TypeError, ValueError):
                    cand_off = ""
                if cand_off not in allowed_offsets:
                    skipped_variant_block += 1
                    continue

            segment = cand.get("segment") or ""
            if segment.endswith("_dock") and not include_dock:
                skipped_dock += 1
                continue

            prefix = cand.get("prefix") or ""
            if prefix == "SP_" and drop_skinned:
                skipped_skinned += 1
                continue

            scope_lc = (scope or "").lower()
            if allowed_scopes is not None and scope_lc not in allowed_scopes:
                skipped_cross_nation += 1
                cross_nation_by_scope[scope_lc or "(unknown)"] += 1
                continue

            hp_key = (scope_lc, (category or "").lower(), asset_id.lower())
            if hp_key in ctx["existing_hp_keys"]:
                skipped_already_in_hp += 1
                continue

            matrix = cand.get("transform", {}).get("matrix")
            if matrix is None or len(matrix) != 16:
                continue
            position = [matrix[12], matrix[13], matrix[14]]

            if drop_degenerate:
                cn0 = math.sqrt(matrix[0] ** 2 + matrix[4] ** 2 + matrix[8] ** 2)
                cn1 = math.sqrt(matrix[1] ** 2 + matrix[5] ** 2 + matrix[9] ** 2)
                cn2 = math.sqrt(matrix[2] ** 2 + matrix[6] ** 2 + matrix[10] ** 2)
                if (cn0 < 0.01 or cn1 < 0.01 or cn2 < 0.01
                        or cn0 > 100.0 or cn1 > 100.0 or cn2 > 100.0
                        or not (math.isfinite(cn0) and math.isfinite(cn1)
                                and math.isfinite(cn2))):
                    skipped_degenerate += 1
                    continue

            if _swap_variant_of_existing_hp(scope_lc, (category or "").lower(), position):
                skipped_swap_variant_at_hp += 1
                continue

            # The skel_ext segment is WG-authoritative for damage-state
            # grouping — each segment block ties placements to one hull
            # section the engine evaluates together. Normalize themed
            # variants like Bow_base_battle_A / Bow_GermanArc2020 to
            # their base section so downstream consumers' typed lookups
            # against the four canonical sections still hit. parent_mesh
            # is filled only for patch-anchored placements (the one
            # webview damage-cascade case that hides at sub-section
            # granularity); everything else has parent_mesh=None and
            # rides with its segment.
            parent_section = _normalize_section(segment)
            parent_mesh = _patch_for_position(position, patches_aabbs)
            if parent_mesh is not None:
                n_patch_anchored += 1

            counter = ctx["asset_counts"].get(asset_id, 0)
            ctx["asset_counts"][asset_id] = counter + 1
            has_dead = asset_id.lower() in ctx["dead_stems"]
            dead_asset_id = f"{asset_id}_dead" if has_dead else None

            merged.append({
                "instance_id":      f"{ctx['ship_stem']}_{asset_id}_{counter:02d}",
                "asset_id":         asset_id,
                "dead_asset_id":    dead_asset_id,
                "hp_name":          None,
                "parent_section":   parent_section,
                "parent_mesh":      parent_mesh,
                "scope":            scope,
                "category":         category,
                "subcategory":      None,
                "species":          None,
                "source":           "skel_ext_hash",
                "source_segment":   cand.get("segment"),
                "source_p0_hash":   cand.get("p0_hash"),
                "source_p1_hash":   cand.get("p1_hash"),
                "instance_index":   cand.get("instance_index"),
                "transform": {
                    "matrix":   [round(x, 6) for x in matrix],
                    "position": [round(x, 4) for x in position],
                },
            })
            emitted_by_category[(scope or "", category or "")] += 1
            if has_dead:
                n_dead_stamped += 1

        n_emitted = sum(emitted_by_category.values())
        st.annotate(
            f"emitted {n_emitted} new + kept {len(merged) - n_emitted} HP_-bound; "
            f"patch-anchored {n_patch_anchored} new + {n_hp_mesh_resolved} HP_",
            data={
                "emitted":                       n_emitted,
                "kept_hp_bound":                 len(merged) - n_emitted,
                "skipped_unresolved":            skipped_unresolved,
                "skipped_variant_block":         skipped_variant_block,
                "skipped_dock":                  skipped_dock,
                "skipped_skinned":               skipped_skinned,
                "skipped_cross_nation":          skipped_cross_nation,
                "skipped_degenerate":            skipped_degenerate,
                "skipped_already_in_hp":         skipped_already_in_hp,
                "skipped_swap_variant_at_hp":    skipped_swap_variant_at_hp,
                "patch_anchored_new":            n_patch_anchored,
                "patch_anchored_hp":             n_hp_mesh_resolved,
                "dead_asset_id_set":             n_dead_stamped,
            },
        )

        # Low-match-rate diagnostic.
        n_resolved_total = resolved["summary"]["resolved"]
        if n_emitted < 0.5 * n_resolved_total and n_resolved_total > 0:
            warnings.append(
                f"emitted {n_emitted} < 50% of resolved {n_resolved_total}; "
                f"review filter chain",
            )

    # ── Step: write_output ────────────────────────────────────────────
    with runner.step("write_output", detail=str(output_json.name)) as st:
        out = dict(placements)
        out["accessories"] = merged
        out["skel_ext_resolve"] = {
            "mode":                       "hash",
            "include_dock":               include_dock,
            "drop_skinned":               drop_skinned,
            "ship_nation":                resolved_nation or None,
            "allowed_scopes":             sorted(allowed_scopes) if allowed_scopes else None,
            "keep_record_offsets":        sorted(allowed_offsets) if allowed_offsets else None,
            "drop_degenerate":            drop_degenerate,
            "candidates_total":           len(candidates),
            "candidates_resolved":        resolved["summary"]["resolved"],
            "candidates_unresolved":      skipped_unresolved,
            "skipped_variant_block":      skipped_variant_block,
            "skipped_dock":               skipped_dock,
            "skipped_skinned":            skipped_skinned,
            "skipped_cross_nation":       skipped_cross_nation,
            "skipped_degenerate":         skipped_degenerate,
            "skipped_already_in_hp":      skipped_already_in_hp,
            "skipped_swap_variant_at_hp": skipped_swap_variant_at_hp,
            "dead_asset_id_set":          n_dead_stamped,
            "hull_glb_for_patches":       str(hull_glb) if patches_aabbs else None,
            "patches_indexed":            len(patches_aabbs),
            "patch_anchored_new":         n_patch_anchored,
            "patch_anchored_hp":          n_hp_mesh_resolved,
            "cross_nation_by_scope":      dict(sorted(
                cross_nation_by_scope.items(), key=lambda x: -x[1],
            )),
            "emit_by_category":           {
                f"{s}/{c}": n for (s, c), n in
                sorted(emitted_by_category.items(), key=lambda x: -x[1])
            },
        }

        output_json.parent.mkdir(parents=True, exist_ok=True)
        _save_json(output_json, out)
        try:
            sz = output_json.stat().st_size
        except OSError:
            sz = 0
        st.annotate(f"wrote {output_json.name} ({sz:,} bytes)", data={"bytes": sz})

    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def resolve_decorative_placements(
    placements_json: Path,
    *,
    candidates_json: Path,
    output_json: Path,
    keep_record_offsets: tuple[str, ...] | None = ("0x0",),
    manifest_path: Path | None = None,
    hull_glb: Path | None = None,
    include_dock: bool = False,
    drop_skinned: bool = True,
    ship_nation: str | None = None,
    extra_scopes: tuple[str, ...] = ("common",),
    drop_degenerate: bool = True,
    config: PipelineConfig | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Merge HP_ placements + decorative candidates into a unified
    accessories JSON.  Returns the output path.

    Inputs:
        placements_json
            Toolkit's main placements JSON
            (``wowsunpack export-ship --placements-json``).  Carries HP_-only
            mounts + ship metadata.
        candidates_json
            Toolkit's skel_ext candidates JSON
            (``wowsunpack export-ship --skel-ext-candidates-json``).  Carries
            decorative placement candidates resolved via their ``p0_hash``.
        output_json
            Final merged accessories JSON.  Sidecar (and webview /
            other downstream consumers) all read this file rather than
            the raw placements.
        keep_record_offsets
            Tuple of ``.skel_ext`` record-offset hex strings to keep.  Default
            ``("0x0",)`` retains only the base ship's record block.  Pass
            ``None`` (or an empty tuple) to keep all permoflage variant
            blocks.  Cherry-pick specific variants via e.g.
            ``("0x0", "0x14080")``.

    See the I:-side module docstring for the full semantics of each
    filter (cross-nation, dock, skinned-mesh bone, degenerate-matrix).
    parent_section comes from the WG-authoritative ``segment`` field
    on each skel_ext record; parent_mesh is set only for placements
    contained in a hull patch mesh (the one webview damage-cascade
    case that hides at sub-section granularity).
    """
    cfg = config or PipelineConfig.load()
    placements_json = Path(placements_json)
    candidates_json = Path(candidates_json)
    output_json = Path(output_json)

    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    manifest = manifest_path or toolkit.default_manifest_path(cfg)

    try:
        _resolve_hash_mode(
            placements_json,
            candidates_json,
            output_json,
            config=cfg,
            runner=runner,
            manifest_path=manifest,
            hull_glb=hull_glb,
            include_dock=include_dock,
            drop_skinned=drop_skinned,
            ship_nation=ship_nation,
            extra_scopes=extra_scopes,
            drop_degenerate=drop_degenerate,
            keep_record_offsets=keep_record_offsets,
            warnings=warnings,
        )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="merge_placements",
            underlying=e,
            detail=str(e),
        ) from e

    # Surface accumulated warnings through stderr so consumers without an
    # on_event handler still see them; consumers that want structured access
    # listen to the StepEvent stream instead.
    #
    # Wrap each write in a guard: on Windows, writing to stderr from a
    # `ThreadPoolExecutor` worker inside the in-process FastAPI server has
    # been observed to raise `OSError: [Errno 22] Invalid argument` after
    # a long-running session — most likely the parent console's pipe state
    # going bad mid-extract. Warnings are best-effort diagnostic output;
    # losing one shouldn't fail the whole extract right after `write_output`
    # already succeeded.
    for w in warnings:
        try:
            print(f"[skel_ext_resolve] warn: {w}", file=sys.stderr, flush=True)
        except OSError:
            # stderr is unwritable; further attempts will fail the same way.
            # Bail out of the warning loop and let the function return
            # normally — the JSON is already on disk.
            break

    return output_json


__all__ = [
    # Public composer entry
    "resolve_decorative_placements",
    # Hull-GLB patch parsing
    "parse_hull_patches",
    # VFS-manifest helper
    "build_vfs_name_index",
]
