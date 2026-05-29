"""Resolve per-asset skel_ext candidates into ``attached_accessories.json``.

Lifted from ``tools/ship/asset_attachments_resolve.py`` (private I:-side
repo). Layer 4 (composer) — disk I/O, writes JSON sidecars next to each
asset's GLB inside the accessory library.

The toolkit emits ``<asset>.skel_ext_candidates.json`` next to each
accessory GLB (when ``--skel-ext-candidates-json`` is set on
``export-model``). It carries every per-placement record from the
asset's sibling ``<asset>.skel_ext`` plus its ``_dead`` variant — about
17k candidates per heavy mount, raw, with no asset_id resolution and no
filtering applied.

This resolver:

1. Filters to ``record_offset == 0x0`` (the base/native record block,
   same convention as the hull pipeline per
   ``project_skel_ext_cross_nation_phantoms.md``). Non-zero blocks
   carry per-permoflage variant data and would pollute the asset
   library with cross-style placements.
2. Resolves each candidate's ``p0_hash`` against the Murmur3-32 lookup
   table from :mod:`wows_model_export.resolve.skel_ext_hashes`.
3. Splits attachments by skel_ext segment: the live segment (named
   like the asset stem) populates ``attachments_live``; any ``_dead``
   segment populates ``attachments_dead``.
4. Emits ``<asset>.attached_accessories.json`` next to the GLB.

The per-HP miscFilter (whitelist) gating is the runtime authority and
runs at composition time on the ship side — the resolver doesn't
second-guess it.

Run as part of the post-build sweep in
:mod:`wows_model_export.compose.accessory_library`, after the per-asset
``skel_ext_candidates.json`` lands. Library-wide re-runs are idempotent
— same input, same output.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import PipelineConfig
from ..resolve import skel_ext_hashes
from ..types import AttachmentResolveStats
from ._atomic import atomic_write_text as _atomic_write_text

SCHEMA_VERSION = "6"
# Matrices are bone-rest-composed with Ry(180°) baked in and translations
# in metres — consumers decompose the matrix verbatim.
#
# Candidate schema v3 identifies hosts emitted with
# ``host_bone_z_mirror == false`` (Convention-B). Those candidates already
# carry the toolkit's X/Z position mirror. Host-space fragments consume
# that rotation as-is; reusable external props need a rotation-only
# Ry(180) conjugation with translation preserved — equivalent to a
# parent-frame vertical 180 plus a child-local vertical 180.

CANDIDATES_SUFFIX = ".skel_ext_candidates.json"
ATTACHMENTS_SUFFIX = ".attached_accessories.json"

# Filter to the base/native record block. Hull-side resolver uses
# the same constant — see project_skel_ext_cross_nation_phantoms.md.
DEFAULT_KEEP_RECORD_OFFSETS: frozenset[str] = frozenset({"0x0"})

_COMMON_MATERIAL_STEMS = frozenset({
    "default",
    "ship_atlas",
    "ship_atlas_detail",
    "ship_atlas_decals",
})

_MATERIAL_PATHS_BY_ROOT: dict[str, dict[str, Path]] = {}
_MATERIAL_STEMS_BY_ASSET: dict[tuple[str, str], set[str]] = {}
_LIBRARY_INDEX_BY_ROOT: dict[str, dict] = {}


def _invalidate_root_caches(library_root: Path) -> None:
    """Drop material/index caches for one library root.

    The accessory-library composer runs the resolver, discovers missing
    attached children, builds them, then runs the resolver again in the
    same Python process. Material mappings created between those passes
    must be visible.
    """
    key = str(library_root.resolve())
    _MATERIAL_PATHS_BY_ROOT.pop(key, None)
    _LIBRARY_INDEX_BY_ROOT.pop(key, None)
    for cache_key in list(_MATERIAL_STEMS_BY_ASSET):
        if cache_key[0] == key:
            _MATERIAL_STEMS_BY_ASSET.pop(cache_key, None)


@dataclass
class Attachment:
    """One resolved Mesh-Placement record on the host accessory."""

    asset_id: str           # e.g. "AM756_Rangefinder"
    p0_hash: str            # raw hex string, e.g. "0xBC199982"
    p1_hash: str            # parent-bone hash on the host's skeleton
    placement_id: str       # full string, e.g. "MP_AM756_Rangefinder"
    instance_index: int | None  # 1-based, None for single-instance
    matrix: list[float]     # 16 floats, column-major, metric
    position: list[float]   # [x, y, z] convenience
    record_offset: str
    matrix_index: int
    rotation_policy: str = "as_emitted"
    host_space_child: bool | None = None

    def to_json(self) -> dict:
        return {
            "asset_id":       self.asset_id,
            "placement_id":   self.placement_id,
            "instance_index": self.instance_index,
            "p0_hash":        self.p0_hash,
            "p1_hash":        self.p1_hash,
            "transform": {
                "matrix":   self.matrix,
                "position": self.position,
            },
            "rotation_policy":   self.rotation_policy,
            "host_space_child":  self.host_space_child,
            "source": {
                "record_offset": self.record_offset,
                "matrix_index":  self.matrix_index,
            },
        }


def _is_dead_segment(segment: str, asset_id: str) -> bool:
    """A segment is the dead variant if its name carries the ``_dead``
    suffix or equals the bare string ``"dead"`` (the current toolkit
    encoding — see ``find_skel_ext_paths`` segment-stripping rule)."""
    if not segment:
        return False
    return segment == "dead" or segment.endswith("_dead")


def _find_library_root(asset_dir: Path) -> Path:
    """Infer the accessory-library root for material-provenance lookups.

    Walks up from ``asset_dir`` looking for a directory named
    ``accessories``. Falls back to ``asset_dir.parents[2]`` (asset_dir's
    ``<library>/<scope>/<cat>/<asset>`` granny) when no such name is on
    the path — supports custom library layouts where the leaf is named
    something other than ``accessories``.
    """
    resolved = asset_dir.resolve()
    for path in (resolved, *resolved.parents):
        if path.name == "accessories":
            return path
    # Heuristic fallback: <library_root>/<scope>/<cat>/<asset_dir>.
    if len(resolved.parents) >= 3:
        return resolved.parents[2]
    return resolved


def _material_paths_for_root(library_root: Path) -> dict[str, Path]:
    key = str(library_root.resolve())
    cached = _MATERIAL_PATHS_BY_ROOT.get(key)
    if cached is not None:
        return cached

    paths: dict[str, Path] = {}
    if library_root.is_dir():
        for path in library_root.rglob("*_material_mappings.json"):
            paths.setdefault(path.parent.name, path)
    _MATERIAL_PATHS_BY_ROOT[key] = paths
    return paths


def _add_material_stem(stems: set[str], value: object) -> None:
    if not isinstance(value, str):
        return
    stem = value.strip()
    if not stem or stem in _COMMON_MATERIAL_STEMS:
        return
    stems.add(stem)


def _texture_stem_from_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stem = Path(value).stem
    for suffix in (
        "_normal",
        "_nbmask",
        "_camomask",
        "_ao",
        "_mr",
        "_mg",
        "_em",
        "_a",
        "_n",
    ):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem or stem in _COMMON_MATERIAL_STEMS:
        return None
    return stem


def _add_texture_set_stems(stems: set[str], node: object) -> None:
    if isinstance(node, list):
        for value in node:
            stem = _texture_stem_from_path(value)
            if stem:
                stems.add(stem)
        return
    if isinstance(node, dict):
        dds_mips = node.get("dds_mips")
        if isinstance(dds_mips, list):
            _add_texture_set_stems(stems, dds_mips)
        for value in node.values():
            if isinstance(value, (dict, list)):
                _add_texture_set_stems(stems, value)


def _library_index_for_root(library_root: Path) -> dict:
    key = str(library_root.resolve())
    cached = _LIBRARY_INDEX_BY_ROOT.get(key)
    if cached is not None:
        return cached
    path = library_root / "index.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    _LIBRARY_INDEX_BY_ROOT[key] = raw
    return raw


def _index_material_stems_for_asset(asset_id: str, library_root: Path) -> set[str]:
    stems: set[str] = set()
    raw = _library_index_for_root(library_root)
    assets = raw.get("assets") or {}
    asset = assets.get(asset_id) or {}
    _add_texture_set_stems(stems, asset.get("texture_sets") or {})
    for material in asset.get("materials") or []:
        if isinstance(material, dict):
            _add_texture_set_stems(stems, material.get("texture_sets") or {})
    return stems


def _material_stems_for_asset(asset_id: str, library_root: Path) -> set[str]:
    root_key = str(library_root.resolve())
    cache_key = (root_key, asset_id)
    cached = _MATERIAL_STEMS_BY_ASSET.get(cache_key)
    if cached is not None:
        return cached

    stems: set[str] = set()
    path = _material_paths_for_root(library_root).get(asset_id)
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        for material in raw.get("materials") or []:
            if not isinstance(material, dict):
                continue
            _add_material_stem(stems, material.get("mfm_stem"))
            mfm_path = material.get("mfm_path")
            if isinstance(mfm_path, str):
                _add_material_stem(stems, Path(mfm_path).stem)
            textures = material.get("textures") or {}
            if isinstance(textures, dict):
                for texture in textures.values():
                    if isinstance(texture, dict):
                        _add_material_stem(stems, texture.get("stem"))

    stems.update(_index_material_stems_for_asset(asset_id, library_root))

    _MATERIAL_STEMS_BY_ASSET[cache_key] = stems
    return stems


def _host_token_variants(host_asset_id: str, host_stems: set[str]) -> set[str]:
    tokens = {host_asset_id, *host_stems}
    for stem in list(tokens):
        if stem.endswith("_skinned"):
            tokens.add(stem.removesuffix("_skinned"))
    return {token for token in tokens if token}


def _is_host_space_child(
    *,
    host_asset_id: str,
    child_asset_id: str,
    library_root: Path,
) -> bool:
    """Return True when a resolved child appears authored from host meshes.

    WG's Convention-B candidates contain two visually different child
    classes. Host-authored fragments, like rangefinders split out from a
    turret model, share the host's material stems and carry vertex offsets
    in the host frame. Reusable props, like boats and float baskets, use
    their own material stems and are centered around their own origins.
    """
    if child_asset_id == host_asset_id:
        return True

    host_stems = _material_stems_for_asset(host_asset_id, library_root)
    child_stems = _material_stems_for_asset(child_asset_id, library_root)
    if not child_stems:
        return False
    if host_stems & child_stems:
        return True

    host_tokens = _host_token_variants(host_asset_id, host_stems)
    for child_stem in child_stems:
        for token in host_tokens:
            if child_stem == token or child_stem.startswith(f"{token}_"):
                return True
    return False


def _conjugate_rotate_y_180_basis(matrix: list[float]) -> list[float]:
    """Return ``Ry(180) * matrix * Ry(180)`` with translation preserved.

    The operation applies only to the 3x3 basis. In column-major storage,
    right-multiplying by Ry(180) negates local X/Z columns; left-multiplying
    by Ry(180) negates parent-frame X/Z rows. The placement origin remains
    the toolkit's Convention-B X/Z-position-mirrored value.
    """
    if len(matrix) < 16:
        return matrix
    out = list(matrix)
    for col in range(3):
        col_sign = -1.0 if col in (0, 2) else 1.0
        for row in range(3):
            row_sign = -1.0 if row in (0, 2) else 1.0
            i = col * 4 + row
            out[i] = matrix[i] * row_sign * col_sign
    return out


def _ensure_hash_table(
    table: dict[int, dict] | None,
    *,
    config: PipelineConfig | None,
) -> dict[int, dict]:
    """Return ``table`` if supplied, else build the lookup from cache."""
    if table is not None:
        return table
    cfg = config or PipelineConfig.load()
    cache_dir = cfg.require_cache_dir()
    return skel_ext_hashes.load_or_build(
        cache=cache_dir / "skel_ext_hashes.json",
    )


def resolve_asset_attachments(
    candidates_path: Path,
    *,
    asset_id: str,
    keep_record_offsets: Iterable[str] = DEFAULT_KEEP_RECORD_OFFSETS,
    hash_table: dict[int, dict] | None = None,
    include_skinned: bool = False,
    config: PipelineConfig | None = None,
) -> tuple[dict, AttachmentResolveStats]:
    """Resolve one asset's skel_ext candidates into an attachments doc.

    Returns ``(doc, stats)`` where ``doc`` is the JSON-ready dict and
    ``stats`` is per-asset diagnostics. ``hash_table`` is an optional
    injection point for batch callers (avoids repeated assets.bin
    lookups across N assets). When omitted, the resolver builds (or
    loads-from-cache) the table via ``PipelineConfig.require_cache_dir``.

    ``include_skinned`` (default False) controls whether ``SP_*`` skinned
    placements survive the filter pass. They live under
    ``/content/styles/<StyleName>/`` in the VFS and only render when a
    permoflage style is active — the base ship runtime drops them, so
    the default also drops them and downstream consumers don't see
    "unresolved attach" noise. Set to True when feeding a per-permoflage
    composer that knows how to look style assets up.
    """
    raw = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates = raw.get("candidates") or []
    pipeline = raw.get("pipeline") or {}
    convention_b_position_only_emit = pipeline.get("host_bone_z_mirror") is False
    library_root = _find_library_root(candidates_path.parent)

    table = _ensure_hash_table(hash_table, config=config)

    keep_offsets = {x.lower() for x in keep_record_offsets}

    stats = AttachmentResolveStats(candidates_total=len(candidates))
    # AttachmentResolveStats is frozen — accumulate plain ints, swap into
    # a fresh instance before returning.
    candidates_in_kept_records = 0
    unresolved_p0_hashes = 0
    filtered_skinned = 0
    convention_b_external_y_conjugate = 0
    convention_b_host_space_children = 0

    live: list[Attachment] = []
    dead: list[Attachment] = []
    distinct: set[str] = set()

    for c in candidates:
        rec_off = str(c.get("record_offset", "")).lower()
        if rec_off not in keep_offsets:
            continue
        candidates_in_kept_records += 1

        p0_hex = c.get("p0_hash", "")
        try:
            p0_int = int(p0_hex, 16)
        except (TypeError, ValueError):
            unresolved_p0_hashes += 1
            continue
        info = table.get(p0_int)
        if info is None:
            unresolved_p0_hashes += 1
            continue

        placement_id = info["string"]

        # Skinned placements (`SP_*`) are permoflage-style-conditional —
        # WG renders them only when a style with the matching mesh is
        # active, and the source assets live under
        # `/content/styles/<StyleName>/`, NOT `/content/gameplay/`. The
        # base ship runtime skips them. Default `include_skinned=False`
        # drops them so the standard library build doesn't emit
        # unresolved-attach warnings for assets that legitimately don't
        # belong on a base/no-permoflage render.
        if not include_skinned and placement_id.startswith("SP_"):
            filtered_skinned += 1
            continue

        transform = c.get("transform") or {}
        matrix = [float(x) for x in (transform.get("matrix") or [])]
        position = [float(x) for x in (transform.get("position") or [])]
        rotation_policy = "as_emitted"
        host_space_child: bool | None = None

        if convention_b_position_only_emit:
            host_space_child = _is_host_space_child(
                host_asset_id=asset_id,
                child_asset_id=info["asset_id"],
                library_root=library_root,
            )
            if host_space_child:
                rotation_policy = "convention_b_host_space_position_only"
                convention_b_host_space_children += 1
            else:
                matrix = _conjugate_rotate_y_180_basis(matrix)
                rotation_policy = "convention_b_external_prop_y_conjugate"
                convention_b_external_y_conjugate += 1

        attachment = Attachment(
            asset_id=info["asset_id"],
            p0_hash=p0_hex,
            p1_hash=str(c.get("p1_hash", "")),
            placement_id=placement_id,
            instance_index=info.get("index"),
            matrix=list(matrix),
            position=list(position),
            record_offset=rec_off,
            matrix_index=int(c.get("matrix_index", 0)),
            rotation_policy=rotation_policy,
            host_space_child=host_space_child,
        )

        segment = str(c.get("segment", ""))
        if _is_dead_segment(segment, asset_id):
            dead.append(attachment)
        else:
            live.append(attachment)
        distinct.add(info["asset_id"])

    # No dedup pass: WG's skel_ext lists every potential placement
    # label and the per-HP miscFilter whitelist (sidecar v3.x → consumer)
    # is the runtime gate that picks which alternates render. An earlier
    # collinear-dedup heuristic was added 2026-05-07 (af3eda1) as a
    # defence against z-fighting back when the consumer treated empty
    # `miscFilter: []` as "render all". Once the 2026-05-09 fix made
    # consumers honour the empty-whitelist semantics, dedup became
    # actively wrong — it ate alternates that the per-HP whitelist
    # explicitly named.

    final_stats = AttachmentResolveStats(
        candidates_total=stats.candidates_total,
        candidates_in_kept_records=candidates_in_kept_records,
        unresolved_p0_hashes=unresolved_p0_hashes,
        filtered_skinned=filtered_skinned,
        attachments_live=len(live),
        attachments_dead=len(dead),
        distinct_assets=len(distinct),
        convention_b_external_y_conjugate=convention_b_external_y_conjugate,
        convention_b_host_space_children=convention_b_host_space_children,
    )

    doc: dict = {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset_id,
        "source": {
            "skel_ext_candidates": candidates_path.name,
            "candidates_total":         final_stats.candidates_total,
            "kept_record_offsets":      sorted(keep_offsets),
        },
        "stats": {
            "candidates_total":            final_stats.candidates_total,
            "candidates_in_kept_records":  final_stats.candidates_in_kept_records,
            "unresolved_p0_hashes":        final_stats.unresolved_p0_hashes,
            "filtered_skinned":            final_stats.filtered_skinned,
            "attachments_live":            final_stats.attachments_live,
            "attachments_dead":            final_stats.attachments_dead,
            "distinct_assets":             final_stats.distinct_assets,
            "convention_b_external_y_conjugate":
                final_stats.convention_b_external_y_conjugate,
            "convention_b_host_space_children":
                final_stats.convention_b_host_space_children,
        },
        "attachments_live": [a.to_json() for a in live],
        "attachments_dead": [a.to_json() for a in dead],
    }
    return doc, final_stats


def _stats_from_prior_doc(doc: dict) -> AttachmentResolveStats | None:
    """Reconstruct an AttachmentResolveStats from a prior
    ``attached_accessories.json``'s ``stats`` block. Returns ``None``
    when the block is missing or malformed (caller should recompute)."""
    s = doc.get("stats")
    if not isinstance(s, dict):
        return None
    try:
        return AttachmentResolveStats(
            candidates_total=int(s.get("candidates_total", 0)),
            candidates_in_kept_records=int(s.get("candidates_in_kept_records", 0)),
            unresolved_p0_hashes=int(s.get("unresolved_p0_hashes", 0)),
            filtered_skinned=int(s.get("filtered_skinned", 0)),
            attachments_live=int(s.get("attachments_live", 0)),
            attachments_dead=int(s.get("attachments_dead", 0)),
            distinct_assets=int(s.get("distinct_assets", 0)),
            convention_b_external_y_conjugate=int(
                s.get("convention_b_external_y_conjugate", 0)
            ),
            convention_b_host_space_children=int(
                s.get("convention_b_host_space_children", 0)
            ),
        )
    except (TypeError, ValueError):
        return None


def resolve_for_asset_dir(
    asset_dir: Path,
    *,
    hash_table: dict[int, dict] | None = None,
    keep_record_offsets: Iterable[str] = DEFAULT_KEEP_RECORD_OFFSETS,
    include_skinned: bool = False,
    config: PipelineConfig | None = None,
    rebuild: bool = False,
) -> tuple[Path, AttachmentResolveStats] | None:
    """Resolve one library asset directory's candidates JSON, write the
    attachments JSON next to it, and return ``(out_path, stats)``.

    Returns ``None`` when the candidates JSON is absent — most
    decorative miscs have no sibling skel_ext, so this is normal.
    Also returns ``None`` when the candidates JSON resolves to zero
    live + zero dead attachments (typical for empty manifests the
    toolkit emits when the source had no real skel_ext): we leave any
    stale ``attached_accessories.json`` from a prior build alone if
    present, but skip emitting an empty one.

    When ``rebuild=False`` (default), an existing
    ``<asset_id>.attached_accessories.json`` whose mtime is newer than
    the candidates JSON is reused as-is — stats are reconstructed from
    its embedded ``stats`` block. Pass ``rebuild=True`` to force a
    fresh hash-resolve pass.
    """
    asset_id = asset_dir.name
    candidates_path = asset_dir / f"{asset_id}{CANDIDATES_SUFFIX}"
    if not candidates_path.is_file():
        return None
    out_path = asset_dir / f"{asset_id}{ATTACHMENTS_SUFFIX}"
    if (not rebuild
            and out_path.is_file()
            and out_path.stat().st_mtime >= candidates_path.stat().st_mtime):
        try:
            prior = json.loads(out_path.read_text(encoding="utf-8"))
            cached = _stats_from_prior_doc(prior)
            if cached is not None:
                return (out_path, cached)
        except (OSError, ValueError, json.JSONDecodeError):
            pass  # fall through and recompute
    doc, stats = resolve_asset_attachments(
        candidates_path,
        asset_id=asset_id,
        hash_table=hash_table,
        keep_record_offsets=keep_record_offsets,
        include_skinned=include_skinned,
        config=config,
    )
    # Empty result — clean up any stale prior file but don't write a
    # new one. Most accessories emit an empty manifest because their
    # source had no skel_ext (the toolkit emits the manifest
    # unconditionally for callers that want to confirm "yes I checked").
    if stats.attachments_live == 0 and stats.attachments_dead == 0:
        if out_path.is_file():
            try:
                out_path.unlink()
            except OSError:
                pass
        return None
    # Atomic + unique-temp write: accessory_library reads these per-asset
    # manifests back during the same (possibly concurrent) build.
    _atomic_write_text(out_path, json.dumps(doc, indent=2, ensure_ascii=False))
    return (out_path, stats)


def resolve_library(
    library_root: Path,
    *,
    keep_record_offsets: Iterable[str] = DEFAULT_KEEP_RECORD_OFFSETS,
    include_skinned: bool = False,
    quiet: bool = False,
    config: PipelineConfig | None = None,
    rebuild: bool = False,
) -> dict[str, AttachmentResolveStats]:
    """Walk the accessory library and resolve every asset that has a
    candidates JSON. Returns ``{asset_id: stats}`` for the pass.

    Hash table is loaded once and shared across all asset resolves.

    With ``rebuild=False`` (default), per-asset reuses the existing
    ``attached_accessories.json`` when its mtime is newer than the
    sibling candidates JSON. Pass ``rebuild=True`` to force a fresh
    hash-resolve pass for every asset.
    """
    _invalidate_root_caches(library_root)
    hash_table = _ensure_hash_table(None, config=config)
    out: dict[str, AttachmentResolveStats] = {}

    # Layout: <library_root>/<scope>/<category>/<subcategory?>/<asset_id>/
    for cand_path in sorted(library_root.rglob(f"*{CANDIDATES_SUFFIX}")):
        asset_dir = cand_path.parent
        asset_id = asset_dir.name
        result = resolve_for_asset_dir(
            asset_dir,
            hash_table=hash_table,
            keep_record_offsets=keep_record_offsets,
            include_skinned=include_skinned,
            config=config,
            rebuild=rebuild,
        )
        if result is None:
            continue
        _, stats = result
        out[asset_id] = stats
        if not quiet:
            print(
                f"  {asset_id}: live={stats.attachments_live} "
                f"dead={stats.attachments_dead} "
                f"distinct={stats.distinct_assets} "
                f"unresolved={stats.unresolved_p0_hashes}"
            )
    return out


__all__ = [
    "ATTACHMENTS_SUFFIX",
    "Attachment",
    "CANDIDATES_SUFFIX",
    "DEFAULT_KEEP_RECORD_OFFSETS",
    "SCHEMA_VERSION",
    "resolve_asset_attachments",
    "resolve_for_asset_dir",
    "resolve_library",
]
