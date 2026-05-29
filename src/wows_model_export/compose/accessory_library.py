"""Fleet-wide accessory library builder.

Lifted from ``tools/ship/build_accessory_library.py`` (private I:-side
repo). Layer 4 (composer): chains :mod:`wows_model_export.toolkit`
invocations, :mod:`wows_model_export.resolve` transforms, and per-asset
sidecar passes into the end-to-end library build.

The library de-duplicates every unique ``(scope, category, subcategory,
asset_id)`` tuple across the fleet's sidecars and calls the toolkit's
``batch-export-model`` once per asset to produce:

    <library_root>/<scope>/<cat>[/<sub>]/<asset_id>/
      ├── <asset_id>.glb                (external-URI, references siblings)
      ├── textures/                     (optional PNGs)
      └── textures_dds/                 (raw WG DDS, all mip levels)

Plus one ``index.json`` mapping each asset_id to its on-disk paths,
category metadata, and which ships reference it.

The composer emits the following canonical :class:`StepEvent` names so
consumers can branch reliably (see ``migration/PIPELINE_API.md``):

    "discover_assets"   "plan_batch"   "batch_export"
    "swizzle_textures"  "build_rigs"   "resolve_attachments"
    "dead_variant_audit" "write_index"

Each step emits ``started`` / ``completed`` (or ``skipped`` /
``failed``). Per-step failures are wrapped in :class:`StepError` with
``step=`` set to one of the names above.
"""

from __future__ import annotations

import glob
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .. import _glb, toolkit
from ..config import PipelineConfig
from ..errors import StepError, ToolkitError
from ..read import sidecar as read_sidecar
from ..resolve import sidecar as resolve_sidecar
from ..resolve import synth_emission
from ..types import (
    AccessoryLibraryResult,
    AttachmentResolveStats,
    OnEvent,
)
from . import attached_accessories_library, dead_variant_audit
from ._atomic import atomic_write_text as _atomic_write_text
from ._step_runner import StepRunner

PLACEMENT_SECTIONS = ("turrets", "secondaries", "antiair", "torpedoes", "accessories")

# Placement attachments file suffix (mirrors the resolver's constant
# without an import-time dependency on it).
ATTACHMENTS_SUFFIX = ".attached_accessories.json"

# VFS manifest path resolution lives in :mod:`wows_model_export.toolkit.vfs`
# (``default_manifest_path`` / ``ensure_manifest``). Helpers below accept
# ``manifest_path=None`` and resolve to the toolkit default on use; the
# composer entry calls ``ensure_manifest`` so the file is materialised
# before the workers consume it.


# ---------------------------------------------------------------------------
# Asset records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetKey:
    """Identifies a unique accessory asset across the fleet.

    For the standard gameplay-tree assets (the overwhelming majority),
    the VFS path is computed from ``scope`` / ``category`` /
    ``subcategory`` via the ``content/gameplay/`` convention.

    Style-resident skinned-mesh assets (``YMS*`` etc.) live under
    ``/content/styles/<StyleName>/<asset_id>/<asset_id>.geometry`` and
    don't fit the gameplay tree. They surface only when the
    ``include_skinned`` flag is set on the resolver. Such keys carry
    ``scope="style"`` + ``category=<StyleName>``.
    """

    scope: str
    category: str
    subcategory: str | None
    asset_id: str


@dataclass
class AssetRecord:
    """Per-asset collected metadata + provenance."""

    key: AssetKey
    species: str | None = None
    used_by_ships: set[str] = field(default_factory=set)
    # Filled in during build:
    glb_rel_path: str | None = None
    glb_bytes: int | None = None
    built_at: int | None = None
    textures_rel_dir: str | None = None
    textures_dds_rel_dir: str | None = None
    glb_dead_rel_path: str | None = None
    glb_dead_bytes: int | None = None
    rig_descriptor_rel_path: str | None = None
    rig_variant_rel_paths: list[str] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    texture_sets: dict = field(default_factory=dict)
    skel_ext_candidates_rel_path: str | None = None
    skel_ext_candidate_count: int = 0
    attached_accessories_rel_path: str | None = None
    attachments_live_count: int = 0
    attachments_dead_count: int = 0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_sidecar_files(ship_root: Path) -> list[Path]:
    """Return every ``<ship>/<Ship>.meta.json`` under ``<ship_root>/``."""
    pattern = str(ship_root / "*" / f"*{read_sidecar.SIDECAR_SUFFIX}")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def find_placements_files(ship_root: Path) -> list[Path]:
    """Legacy fallback: discover ``*_accessories.json`` files for ships
    whose sidecar isn't available yet."""
    pattern = str(ship_root / "*" / read_sidecar.MODELS_SUBDIR / "*_accessories.json")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def _harvest_section_entries(
    section_items: list,
    *,
    ship_name: str,
    records: dict[AssetKey, AssetRecord],
) -> None:
    """Add every well-formed ``(scope, category, asset_id)`` tuple in
    ``section_items`` to ``records``, attributing them to ``ship_name``."""
    for entry in section_items or []:
        if not isinstance(entry, dict):
            continue
        asset_id = entry.get("asset_id")
        scope    = entry.get("scope")
        category = entry.get("category")
        if not asset_id or not scope or not category:
            continue
        key = AssetKey(
            scope=scope,
            category=category,
            subcategory=entry.get("subcategory"),
            asset_id=asset_id,
        )
        rec = records.setdefault(key, AssetRecord(key=key))
        rec.used_by_ships.add(ship_name)
        if rec.species is None:
            rec.species = entry.get("species")


def union_assets(
    sidecar_files: list[Path],
    *,
    fallback_placements_files: list[Path] | None = None,
    warnings: list[str] | None = None,
) -> dict[AssetKey, AssetRecord]:
    """Union unique placement tuples across the fleet."""
    records: dict[AssetKey, AssetRecord] = {}
    sidecar_ships: set[Path] = set()
    warn_log = warnings if warnings is not None else []
    for path in sidecar_files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            warn_log.append(f"couldn't read sidecar {path}: {e}")
            continue
        sidecar_ships.add(path.parent.resolve())

        ship_obj = doc.get("ship") or {}
        ship_name = ship_obj.get("display_name") or ship_obj.get("model_dir") \
            or path.stem.replace(read_sidecar.SIDECAR_SUFFIX, "")

        for section in PLACEMENT_SECTIONS:
            _harvest_section_entries(
                doc.get(section), ship_name=ship_name, records=records,
            )

        hulls = doc.get("hulls")
        if isinstance(hulls, dict):
            for hull_entry in hulls.values():
                if not isinstance(hull_entry, dict):
                    continue
                for section in PLACEMENT_SECTIONS:
                    _harvest_section_entries(
                        hull_entry.get(section),
                        ship_name=ship_name, records=records,
                    )

    # Legacy fallback for ship folders without a sidecar yet.
    for path in (fallback_placements_files or []):
        ship_dir = path.parent.parent.resolve()  # …/<Ship>/models/<f> → …/<Ship>
        if ship_dir in sidecar_ships:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            warn_log.append(f"couldn't read placements {path}: {e}")
            continue
        ship_obj = doc.get("ship") or {}
        ship_name = ship_obj.get("display_name") or ship_obj.get("model_dir") \
            or path.stem.replace("_accessories", "")
        for section in PLACEMENT_SECTIONS:
            _harvest_section_entries(
                doc.get(section), ship_name=ship_name, records=records,
            )

    return records


# ---------------------------------------------------------------------------
# Path computation
# ---------------------------------------------------------------------------


def vfs_geometry_path(key: AssetKey, variant: str = "") -> str:
    """Construct the VFS path to an asset's ``.geometry`` file.

    Convention:
        ``content/gameplay/{scope}/{category}/{subcategory}/{asset_id}/{asset_id}[_variant].geometry``

    Style-resident assets (``scope == "style"``) live under
        ``content/styles/{category}/{asset_id}/{asset_id}[_variant].geometry``
    """
    if key.scope == "style":
        return (
            f"content/styles/{key.category}/{key.asset_id}/"
            f"{key.asset_id}{variant}.geometry"
        )
    parts = ["content/gameplay", key.scope, key.category]
    if key.subcategory:
        parts.append(key.subcategory)
    parts.append(key.asset_id)
    dir_path = "/".join(parts)
    return f"{dir_path}/{key.asset_id}{variant}.geometry"


def output_dir_for(library_root: Path, key: AssetKey) -> Path:
    parts = [key.scope, key.category]
    if key.subcategory:
        parts.append(key.subcategory)
    parts.append(key.asset_id)
    return library_root.joinpath(*parts)


# ---------------------------------------------------------------------------
# VFS manifest (for `_dead` variant detection)
# ---------------------------------------------------------------------------


_manifest_paths: set[str] | None = None


def _resolve_manifest_path(manifest_path: Path | None) -> Path:
    """Return ``manifest_path`` if set, else the toolkit default."""
    if manifest_path is not None:
        return manifest_path
    return toolkit.default_manifest_path()


def _load_manifest_paths(manifest_path: Path | None) -> set[str]:
    """Load all VFS file paths (without leading '/') into a set, cached."""
    global _manifest_paths
    if _manifest_paths is not None:
        return _manifest_paths
    resolved = _resolve_manifest_path(manifest_path)
    if not resolved.is_file():
        _manifest_paths = set()
        return _manifest_paths
    with open(resolved, encoding="utf-8") as f:
        entries = json.load(f)
    _manifest_paths = {e["path"].lstrip("/") for e in entries if "path" in e}
    return _manifest_paths


def has_dead_variant(
    key: AssetKey,
    manifest_path: Path | None = None,
) -> bool:
    """Return True iff the VFS has a ``<asset_id>_dead.geometry`` file
    next to the main geometry for this asset."""
    paths = _load_manifest_paths(manifest_path)
    if not paths:
        return False
    dead_path = vfs_geometry_path(key, variant="_dead")
    return dead_path in paths


def _vfs_lookup_asset_key(
    asset_id: str,
    manifest_paths: set[str],
    *,
    allow_style: bool = False,
) -> AssetKey | None:
    """Find an asset_id's VFS layout from the manifest."""
    if not asset_id:
        return None
    suffix = f"/{asset_id}/{asset_id}.geometry"
    style_match: str | None = None
    for p in manifest_paths:
        if not p.endswith(suffix):
            continue
        if p.startswith("content/gameplay/"):
            inner = p[len("content/gameplay/"):-len(suffix)]
            parts = inner.split("/")
            if len(parts) == 2:
                return AssetKey(
                    scope=parts[0], category=parts[1],
                    subcategory=None, asset_id=asset_id,
                )
            if len(parts) == 3:
                return AssetKey(
                    scope=parts[0], category=parts[1],
                    subcategory=parts[2], asset_id=asset_id,
                )
            return None
        if allow_style and p.startswith("content/styles/"):
            inner = p[len("content/styles/"):-len(suffix)]
            style_name = inner.split("/", 1)[0]
            if style_match is None or style_name < style_match:
                style_match = style_name
    if style_match is not None:
        return AssetKey(
            scope="style", category=style_match,
            subcategory=None, asset_id=asset_id,
        )
    return None


def _discover_attached_children_keys(
    library_root: Path,
    existing: dict[AssetKey, AssetRecord],
    *,
    allow_style: bool = False,
    manifest_path: Path | None = None,
    warnings: list[str] | None = None,
) -> list[AssetKey]:
    """Walk every ``<asset>.attached_accessories.json`` in the library
    and return AssetKeys for child asset_ids not already in
    ``existing``."""
    have_ids = {k.asset_id for k in existing.keys()}
    needed: set[str] = set()
    for aa in library_root.rglob(f"*{ATTACHMENTS_SUFFIX}"):
        try:
            doc = json.loads(aa.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for grp_key in ("attachments_live", "attachments_dead"):
            for a in (doc.get(grp_key) or []):
                aid = a.get("asset_id")
                if aid and aid not in have_ids:
                    needed.add(aid)
    if not needed:
        return []
    manifest_paths = _load_manifest_paths(manifest_path)
    if not manifest_paths:
        if warnings is not None:
            warnings.append(
                f"{len(needed)} attached children unresolvable (no VFS "
                "manifest). Run "
                "`wowsunpack metadata --format json -o ...` and re-run."
            )
        return []
    keys: list[AssetKey] = []
    unfound: list[str] = []
    for aid in sorted(needed):
        key = _vfs_lookup_asset_key(aid, manifest_paths, allow_style=allow_style)
        if key is None:
            unfound.append(aid)
            continue
        keys.append(key)
    if unfound and warnings is not None:
        preview = ", ".join(unfound[:5])
        tail = f" (+{len(unfound) - 5} more)" if len(unfound) > 5 else ""
        warnings.append(
            f"{len(unfound)} attached children not in VFS: {preview}{tail}"
        )
    return keys


# ---------------------------------------------------------------------------
# Per-record post-build hooks
# ---------------------------------------------------------------------------


def _post_build_rec(
    rec: AssetRecord,
    library_root: Path,
    out_dir: Path,
    glb_path: Path,
    *,
    warnings: list[str] | None = None,
) -> bool:
    """Populate record fields from disk after a successful build."""
    if not glb_path.is_file():
        return False
    rec.glb_rel_path = str(glb_path.relative_to(library_root)).replace("\\", "/")
    stat = glb_path.stat()
    rec.glb_bytes = stat.st_size
    rec.built_at = int(stat.st_mtime)
    png_dir = out_dir / "textures"
    dds_dir = out_dir / "textures_dds"
    rec.textures_rel_dir = (
        str(png_dir.relative_to(library_root)).replace("\\", "/")
        if png_dir.is_dir() else None
    )
    rec.textures_dds_rel_dir = (
        str(dds_dir.relative_to(library_root)).replace("\\", "/")
        if dds_dir.is_dir() else None
    )
    dead_glb = out_dir / f"{rec.key.asset_id}_dead.glb"
    if dead_glb.is_file():
        rec.glb_dead_rel_path = (
            str(dead_glb.relative_to(library_root)).replace("\\", "/")
        )
        rec.glb_dead_bytes = dead_glb.stat().st_size
    _discover_rig_artifacts(rec, out_dir, library_root)
    _extract_material_manifest(rec, out_dir, glb_path, warnings=warnings)
    _scan_skel_ext_candidates(rec, out_dir, library_root)
    return True


def _scan_skel_ext_candidates(
    rec: AssetRecord, out_dir: Path, library_root: Path,
) -> None:
    sx_path = out_dir / f"{rec.key.asset_id}.skel_ext_candidates.json"
    if not sx_path.is_file():
        return
    try:
        data = json.loads(sx_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return
    rec.skel_ext_candidates_rel_path = (
        str(sx_path.relative_to(library_root)).replace("\\", "/")
    )
    rec.skel_ext_candidate_count = len(candidates)


def _scan_attached_accessories(
    rec: AssetRecord, out_dir: Path, library_root: Path,
) -> None:
    aa_path = out_dir / f"{rec.key.asset_id}.attached_accessories.json"
    if not aa_path.is_file():
        return
    try:
        doc = json.loads(aa_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    rec.attached_accessories_rel_path = (
        str(aa_path.relative_to(library_root)).replace("\\", "/")
    )
    stats = doc.get("stats") or {}
    rec.attachments_live_count = int(stats.get("attachments_live") or 0)
    rec.attachments_dead_count = int(stats.get("attachments_dead") or 0)


def _extract_material_manifest(
    rec: AssetRecord,
    out_dir: Path,
    glb_path: Path,
    *,
    warnings: list[str] | None = None,
) -> None:
    """Read material metadata from the GLB + build a texture-set manifest."""
    png_dir = out_dir / "textures"
    dds_dir = out_dir / "textures_dds"
    mm_path = out_dir / f"{rec.key.asset_id}_material_mappings.json"
    try:
        rec.materials = resolve_sidecar.materials_from_glb(
            glb_path,
            textures_dir=png_dir if png_dir.is_dir() else None,
            textures_dds_dir=dds_dir if dds_dir.is_dir() else None,
            material_mappings_json=mm_path,
        )
        rec.texture_sets = (
            resolve_sidecar.texture_sets_from_dir(dds_dir)
            if dds_dir.is_dir() else {}
        )
    except Exception as e:
        if warnings is not None:
            warnings.append(
                f"material manifest extraction failed for {rec.key.asset_id}: {e}"
            )
        rec.materials = []
        rec.texture_sets = {}


def _discover_rig_artifacts(
    rec: AssetRecord, out_dir: Path, library_root: Path,
) -> None:
    """Populate rec.rig_* fields from any hand-authored rig files."""
    descriptor = out_dir / f"{rec.key.asset_id}.rig.json"
    if not descriptor.is_file():
        return
    rec.rig_descriptor_rel_path = (
        str(descriptor.relative_to(library_root)).replace("\\", "/")
    )
    for ext in (".rig.fbx", ".rig.glb"):
        variant = out_dir / f"{rec.key.asset_id}{ext}"
        if variant.is_file():
            rec.rig_variant_rel_paths.append(
                str(variant.relative_to(library_root)).replace("\\", "/")
            )


# ---------------------------------------------------------------------------
# Build (batch toolkit call + post-processing)
# ---------------------------------------------------------------------------


def _build_assets_batch(
    records: dict[AssetKey, AssetRecord],
    library_root: Path,
    *,
    mode: str,
    force: bool,
    config: PipelineConfig | None,
    manifest_path: Path,
    warnings: list[str],
) -> tuple[int, int, int, list[str], list[Path]]:
    """Batch-export every asset in ``records`` in a single wowsunpack
    invocation.

    Returns ``(built, skipped, failed, failure_strings, newly_built_glbs)``.
    Raises ToolkitError only on total-batch failure (per-item failures
    propagate via the per-item GLB-exists check).
    """
    items: list[dict] = []
    record_for_geom: dict[str, tuple[AssetRecord, str]] = {}
    skipped = 0
    for rec in sorted(records.values(), key=lambda r: r.key.asset_id):
        out_dir = output_dir_for(library_root, rec.key)
        glb_path = out_dir / f"{rec.key.asset_id}.glb"
        dead_glb_path = out_dir / f"{rec.key.asset_id}_dead.glb"

        intact_exists = glb_path.is_file()
        if intact_exists and not force:
            _post_build_rec(rec, library_root, out_dir, glb_path, warnings=warnings)
            skipped += 1
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            geom = vfs_geometry_path(rec.key)
            entry: dict = {"geometry": geom, "output": str(glb_path)}
            if mode in ("png", "both"):
                entry["textures_dir"] = str(out_dir / "textures")
            if mode in ("dds", "both"):
                entry["raw_dds_dir"] = str(out_dir / "textures_dds")
            entry["material_mappings_json"] = str(
                out_dir / f"{rec.key.asset_id}_material_mappings.json"
            )
            entry["skel_ext_candidates_json"] = str(
                out_dir / f"{rec.key.asset_id}.skel_ext_candidates.json"
            )
            items.append(entry)
            record_for_geom[geom] = (rec, "intact")

        if has_dead_variant(rec.key, manifest_path=manifest_path):
            dead_exists = dead_glb_path.is_file()
            if dead_exists and not force:
                pass
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                dead_geom = vfs_geometry_path(rec.key, variant="_dead")
                dead_entry: dict = {
                    "geometry": dead_geom,
                    "output":   str(dead_glb_path),
                }
                if mode in ("png", "both"):
                    dead_entry["textures_dir"] = str(out_dir / "textures")
                if mode in ("dds", "both"):
                    dead_entry["raw_dds_dir"] = str(out_dir / "textures_dds")
                dead_entry["material_mappings_json"] = str(
                    out_dir / f"{rec.key.asset_id}_dead_material_mappings.json"
                )
                items.append(dead_entry)
                record_for_geom[dead_geom] = (rec, "dead")

    built = 0
    failed = 0
    failure_strings: list[str] = []
    newly_built_glbs: list[Path] = []
    if not items:
        return (built, skipped, failed, failure_strings, newly_built_glbs)

    shared: dict = {
        "no_textures": mode in ("none", "dds"),
        "all_render_sets": True,
    }
    try:
        toolkit.batch_export_model(
            items,
            shared=shared,
            keep_going=True,
            config=config,
        )
    except ToolkitError as e:
        # Re-raise so the composer wraps it in StepError.
        raise e

    # Emissive synthesis pass.
    synth_dirs: list[Path] = []
    for entry in items:
        out_dir = Path(entry["output"]).parent
        textures_dds = out_dir / "textures_dds"
        if textures_dds.is_dir() and textures_dds not in synth_dirs:
            synth_dirs.append(textures_dds)
    if synth_dirs:
        try:
            synth_emission.synthesize_emissive_textures_batch(
                synth_dirs,
                config=config,
                label="accessory-library",
            )
        except Exception as e:
            warnings.append(f"batched emissive synth failed: {e}")

    # Per-item finalisation.
    touched_recs: set[int] = set()
    for entry in items:
        rec, variant = record_for_geom[entry["geometry"]]
        out_dir = output_dir_for(library_root, rec.key)
        expected_path = Path(entry["output"])
        if not expected_path.is_file():
            failed += 1
            if len(failure_strings) < 5:
                failure_strings.append(
                    f"{rec.key.asset_id} ({variant}): no GLB produced "
                    f"at {expected_path.name}"
                )
            continue
        newly_built_glbs.append(expected_path)
        if id(rec) in touched_recs:
            continue
        intact_glb = out_dir / f"{rec.key.asset_id}.glb"
        if _post_build_rec(
            rec, library_root, out_dir, intact_glb, warnings=warnings,
        ):
            built += 1
            touched_recs.add(id(rec))

    return (built, skipped, failed, failure_strings, newly_built_glbs)



# ---------------------------------------------------------------------------
# Index writer
# ---------------------------------------------------------------------------


def _load_dead_orientation_map(library_root: Path) -> dict[str, str]:
    """Read ``dead_variant_audit.json`` and return ``{asset_id: verdict}``."""
    audit_path = library_root / "dead_variant_audit.json"
    if not audit_path.is_file():
        return {}
    try:
        doc = json.loads(audit_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        a.get("asset_id"): a.get("verdict")
        for a in (doc.get("assets") or [])
        if a.get("asset_id") and a.get("verdict")
    }


def _write_index(
    records: dict[AssetKey, AssetRecord],
    library_root: Path,
) -> Path:
    """Emit ``index.json`` for the accessory library."""
    prior_built_at: dict[str, int] = {}
    out = library_root / "index.json"
    if out.is_file():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
            for aid, entry in (prior.get("assets") or {}).items():
                ts = entry.get("built_at")
                if isinstance(ts, (int, float)):
                    prior_built_at[aid] = int(ts)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    dead_orient_map = _load_dead_orientation_map(library_root)

    assets: dict[str, dict] = {}
    for rec in sorted(records.values(), key=lambda r: r.key.asset_id):
        if rec.glb_rel_path is None:
            continue
        kept = prior_built_at.get(rec.key.asset_id)
        if kept is not None:
            rec.built_at = kept
        entry: dict = {
            "scope": rec.key.scope,
            "category": rec.key.category,
            "subcategory": rec.key.subcategory,
            "species": rec.species,
            "glb": rec.glb_rel_path,
            "textures": rec.textures_rel_dir,
            "textures_dds": rec.textures_dds_rel_dir,
            "glb_bytes": rec.glb_bytes,
            "built_at": rec.built_at,
            "used_by_ships": sorted(rec.used_by_ships),
        }
        if rec.glb_dead_rel_path:
            entry["glb_dead"] = rec.glb_dead_rel_path
            entry["glb_dead_bytes"] = rec.glb_dead_bytes
            verdict = dead_orient_map.get(rec.key.asset_id)
            if verdict:
                entry["dead_orientation"] = verdict
        if rec.materials:
            entry["materials"] = rec.materials
        if rec.texture_sets:
            entry["texture_sets"] = rec.texture_sets
        if rec.rig_descriptor_rel_path:
            entry["rig"] = rec.rig_descriptor_rel_path
            entry["rig_variants"] = list(rec.rig_variant_rel_paths)
        if rec.attached_accessories_rel_path:
            entry["attached_accessories"] = rec.attached_accessories_rel_path
            entry["attachments_live_count"] = rec.attachments_live_count
            entry["attachments_dead_count"] = rec.attachments_dead_count
        assets[rec.key.asset_id] = entry

    doc = {
        "version": time.strftime("%Y-%m-%d", time.gmtime()),
        "asset_count": len(assets),
        "assets": assets,
    }
    # Process-unique temp (not a shared "<name>.tmp") so two concurrent
    # library builds writing the same index don't collide on the temp
    # path (Windows raises PermissionError on a cross-writer clash).
    _atomic_write_text(out, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return out


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def build_accessory_library(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    only_ships: tuple[str, ...] | None = None,
    rebuild: bool = False,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> AccessoryLibraryResult:
    """Build / refresh the fleet-wide accessory library.

    The composer walks every in-tree sidecar under ``workspace``,
    deduplicates the unique ``(scope, category, subcategory, asset_id)``
    tuples, and calls ``toolkit.batch_export_model`` to write one GLB +
    DDS mip chain per asset under ``library_root``.

    Parameters:
        workspace            ``PipelineConfig.workspace`` when None.
                              Per-ship sidecars are walked under
                              ``workspace`` (matches the I:-side
                              ``ships/`` layout).
        config               ``PipelineConfig.load()`` when None.
        library_root         ``workspace / "libraries/accessories"``
                              when None.
        only_ships           Restrict the sidecar scan to ships whose
                              directory name appears in this tuple.
                              None (default) scans all ships.
        rebuild              When True, re-export every asset's GLB
                              even if it already exists. Default skips
                              already-built assets.
        on_event             Optional progress callback receiving
                              :class:`StepEvent` notifications. See the
                              "Canonical step names" docstring.
        cancel               Optional :class:`threading.Event` for
                              cooperative cancel. When set, the next
                              step boundary raises
                              :class:`wows_model_export.errors.CancelledError`.
                              Forwarded into per-asset
                              ``autorig_asset`` so cancel takes effect
                              between rigging passes too.

    Returns an :class:`AccessoryLibraryResult` with the library root,
    counts, the list of asset paths auto-flipped this run, the per-
    asset :class:`AttachmentResolveStats`, and per-step timings.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    step names) when any step fails. The original exception is
    accessible via ``.underlying``.
    """
    cfg = config or PipelineConfig.load()
    ws = (workspace or cfg.workspace).resolve()
    lib_root = (library_root or (ws / "libraries" / "accessories")).resolve()
    ship_root = ws / "ships"
    if not ship_root.is_dir():
        # I:-side convention: ship folders live directly under workspace
        # when there's no ships/ subdir. Fall back so this composer
        # works in either layout.
        ship_root = ws

    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []
    attachment_stats: dict[str, AttachmentResolveStats] = {}

    # ── Step: discover_assets ─────────────────────────────────────────
    try:
        with runner.step("discover_assets") as st:
            sidecars = find_sidecar_files(ship_root)
            placements = find_placements_files(ship_root)
            if only_ships:
                ship_filter = set(only_ships)
                sidecars = [
                    p for p in sidecars if p.parent.name in ship_filter
                ]
                placements = [
                    p for p in placements
                    if p.parent.parent.name in ship_filter
                ]
            records = union_assets(
                sidecars,
                fallback_placements_files=placements,
                warnings=warnings,
            )
            n_sidecars = len(sidecars)
            n_placements = len(placements)
            st.annotate(
                f"{len(records)} unique asset(s) across "
                f"{n_sidecars} sidecar(s) + {n_placements} legacy file(s)",
                data={
                    "sidecars": n_sidecars,
                    "placements": n_placements,
                    "assets": len(records),
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="discover_assets", underlying=e, detail=str(e),
        ) from e

    if not records:
        # Nothing to build — emit a useful result and return early.
        return AccessoryLibraryResult(
            library_root=lib_root,
            assets_built=0,
            assets_audited=0,
            warnings=tuple(warnings),
            attachment_stats=attachment_stats,
            step_timings_ms=dict(runner.step_timings_ms),
        )

    lib_root.mkdir(parents=True, exist_ok=True)
    # Materialise the VFS manifest at the toolkit default location
    # (env override: WOWS_VFS_MANIFEST). Builds via ``metadata_json`` on
    # first run; idempotent thereafter.
    try:
        manifest_path = toolkit.ensure_manifest(config=cfg)
    except Exception as e:
        manifest_path = toolkit.default_manifest_path(cfg)
        warnings.append(
            f"VFS manifest build failed ({e}); falling back to "
            f"{manifest_path} (downstream dead-variant + attached-child "
            "discovery may be incomplete)."
        )

    # ── Step: plan_batch ──────────────────────────────────────────────
    try:
        with runner.step("plan_batch") as st:
            by_cat: dict[tuple[str, str], int] = defaultdict(int)
            for k in records:
                by_cat[(k.scope, k.category)] += 1
            st.annotate(
                f"planned build of {len(records)} asset(s) in "
                f"{len(by_cat)} (scope, category) bucket(s)",
                data={"buckets": {f"{s}/{c}": n for (s, c), n in by_cat.items()}},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="plan_batch", underlying=e, detail=str(e)) from e

    # ── Step: batch_export ────────────────────────────────────────────
    try:
        with runner.step("batch_export") as st:
            built, skipped, failed, fail_examples, newly_built_glbs = (
                _build_assets_batch(
                    records, lib_root,
                    mode="dds",
                    force=rebuild,
                    config=cfg,
                    manifest_path=manifest_path,
                    warnings=warnings,
                )
            )
            st.annotate(
                f"built={built} skipped={skipped} failed={failed}",
                data={
                    "built": built, "skipped": skipped,
                    "failed": failed, "failures": fail_examples,
                },
            )
    except ToolkitError as e:
        raise StepError(
            step="batch_export", underlying=e,
            detail=f"batch_export_model failed: {e}",
        ) from e
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="batch_export", underlying=e, detail=str(e)) from e

    # ── Step: swizzle_textures ────────────────────────────────────────
    # Idempotent — only runs on freshly-built DDS dirs. Skipped silently
    # when nothing got rebuilt.
    if newly_built_glbs:
        try:
            with runner.step("swizzle_textures") as st:
                swizzle_dirs: list[Path] = []
                seen: set[Path] = set()
                for glb in newly_built_glbs:
                    dds_dir = glb.parent / "textures_dds"
                    if dds_dir.is_dir() and dds_dir not in seen:
                        swizzle_dirs.append(dds_dir)
                        seen.add(dds_dir)
                processed_total = 0
                siblings_total = 0
                for d in swizzle_dirs:
                    try:
                        result = toolkit.swizzle_dir(
                            d, recursive=False, config=cfg,
                        )
                        if result.data:
                            processed_total += int(result.data.get("processed", 0))
                            siblings_total += int(
                                result.data.get("siblings_written", 0)
                            )
                    except Exception as e:
                        warnings.append(f"swizzle_dir failed for {d}: {e}")
                st.annotate(
                    f"{len(swizzle_dirs)} dir(s); processed={processed_total} "
                    f"siblings_written={siblings_total}",
                    data={
                        "dirs": len(swizzle_dirs),
                        "processed": processed_total,
                        "siblings_written": siblings_total,
                    },
                )
        except StepError:
            raise
        except Exception as e:
            raise StepError(
                step="swizzle_textures", underlying=e, detail=str(e),
            ) from e
    else:
        runner.emit("swizzle_textures", "skipped",
                    detail="no newly-built GLBs")
        runner.step_timings_ms["swizzle_textures"] = 0.0

    # ── First-pass index write (dead_variant_audit reads from it) ─────
    try:
        with runner.step("write_index", "first pass") as st:
            index_path = _write_index(records, lib_root)
            st.annotate(f"wrote {index_path.name}")
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="write_index", underlying=e, detail=str(e)) from e

    # ── Step: dead_variant_audit ──────────────────────────────────────
    try:
        with runner.step("dead_variant_audit") as st:
            idx_doc = json.loads(index_path.read_text(encoding="utf-8"))
            results, audit_path = dead_variant_audit.audit_library(
                lib_root,
                index_doc=idx_doc,
                write_sidecar=True,
                rebuild=rebuild,
            )
            st.annotate(
                f"{len(results)} assets classified",
                data={"asset_count": len(results)},
            )
    except StepError:
        raise
    except Exception as e:
        warnings.append(f"dead-variant audit failed: {e}")

    # ── Step: resolve_attachments ─────────────────────────────────────
    try:
        with runner.step("resolve_attachments") as st:
            try:
                att_stats = attached_accessories_library.resolve_library(
                    lib_root, quiet=True, config=cfg, rebuild=rebuild,
                )
            except Exception as e:
                warnings.append(f"resolve_attachments first pass failed: {e}")
                att_stats = {}
            attachment_stats.update(att_stats)

            # Coverage extension: discover attached children missing
            # from the library, build them, then re-resolve.
            new_keys = _discover_attached_children_keys(
                lib_root, records, manifest_path=manifest_path,
                warnings=warnings,
            )
            if new_keys:
                extra_records: dict[AssetKey, AssetRecord] = {}
                for k in new_keys:
                    rec = AssetRecord(key=k)
                    rec.used_by_ships = set()
                    records[k] = rec
                    extra_records[k] = rec
                try:
                    _build_assets_batch(
                        extra_records, lib_root,
                        mode="dds",
                        force=rebuild,
                        config=cfg,
                        manifest_path=manifest_path,
                        warnings=warnings,
                    )
                except ToolkitError as e:
                    warnings.append(f"attached-children batch failed: {e}")

                try:
                    att_stats2 = attached_accessories_library.resolve_library(
                        lib_root, quiet=True, config=cfg, rebuild=rebuild,
                    )
                    attachment_stats.update(att_stats2)
                except Exception as e:
                    warnings.append(
                        f"resolve_attachments second pass failed: {e}"
                    )

            # Refresh per-record attached-accessories metadata.
            for rec in records.values():
                if rec.glb_rel_path is None:
                    out_dir = output_dir_for(lib_root, rec.key)
                    glb = out_dir / f"{rec.key.asset_id}.glb"
                    if glb.is_file():
                        _post_build_rec(
                            rec, lib_root, out_dir, glb,
                            warnings=warnings,
                        )
                    else:
                        continue
                out_dir = lib_root / Path(rec.glb_rel_path).parent
                _scan_attached_accessories(rec, out_dir, lib_root)
            st.annotate(
                f"{len(attachment_stats)} asset(s) with attachments",
                data={"asset_count": len(attachment_stats)},
            )
    except StepError:
        raise
    except Exception as e:
        warnings.append(f"resolve_attachments failed: {e}")

    # ── Step: write_index (second pass, with attachments + dead audit) ─
    try:
        with runner.step("write_index", "second pass") as st:
            index_path = _write_index(records, lib_root)
            st.annotate(f"wrote {index_path.name}")
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="write_index", underlying=e, detail=str(e)) from e

    # ── Step: build_rigs ──────────────────────────────────────────────
    # Iterate every ``category=="gun"`` asset and invoke the lifted
    # per-asset rig builder. Each call writes ``<asset_id>.rig_pivots.json``
    # next to the asset's GLB. Per-asset failures surface as warnings
    # so a single broken rig doesn't abort the whole library build.
    from . import turret_autorig as _turret_autorig

    with runner.step("build_rigs") as st:
        gun_records = [
            r for r in records.values()
            if r.key.category == "gun" and r.glb_rel_path is not None
        ]
        rigged = 0
        skipped_fresh = 0
        for rec in gun_records:
            out_dir = output_dir_for(lib_root, rec.key)
            rig_path = out_dir / f"{rec.key.asset_id}.rig_pivots.json"
            glb_path = lib_root / rec.glb_rel_path
            # Skip when a prior pass produced the rig JSON and the source
            # GLB hasn't been re-exported since. `rebuild=True` (or
            # CLI --rebuild-library) bypasses the skip and forces a full
            # re-rig pass.
            if (not rebuild and rig_path.is_file() and glb_path.is_file()
                    and rig_path.stat().st_mtime >= glb_path.stat().st_mtime):
                skipped_fresh += 1
                continue
            try:
                _turret_autorig.autorig_asset(
                    rec.key.asset_id,
                    config=cfg,
                    library_root=lib_root,
                    on_event=on_event,
                    cancel=cancel,
                )
                rigged += 1
            except StepError as e:
                warnings.append(
                    f"build_rigs[{rec.key.asset_id}] failed at step "
                    f"{e.step!r}: {e}"
                )
            except Exception as e:
                warnings.append(
                    f"build_rigs[{rec.key.asset_id}] unexpected error: {e}"
                )
        st.annotate(
            f"{rigged}/{len(gun_records)} gun assets rigged "
            f"({skipped_fresh} fresh, skipped)"
        )

    assets_built = sum(1 for r in records.values() if r.glb_rel_path is not None)
    return AccessoryLibraryResult(
        library_root=lib_root,
        assets_built=assets_built,
        assets_audited=len(records),
        warnings=tuple(warnings),
        attachment_stats=attachment_stats,
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "AssetKey",
    "AssetRecord",
    "build_accessory_library",
    "find_sidecar_files",
    "find_placements_files",
    "output_dir_for",
    "union_assets",
    "vfs_geometry_path",
]
