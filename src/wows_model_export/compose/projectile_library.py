"""Fleet-wide projectile library builder.

Lifted from ``tools/build_projectile_library.py`` (private I:-side
repo). Layer 4 (composer): walks every ``Projectile`` entity in the
VFS manifest and chains :mod:`wows_model_export.toolkit.batch_export_model`
+ :mod:`wows_model_export.resolve.synth_emission` +
:mod:`wows_model_export.resolve.sidecar` (for material manifest
extraction) into the end-to-end library build.

Output layout::

    <library_root>/{nation}/{category}/{asset_id}/
      ├── {asset_id}.glb             — geometry + visual material setup
      └── textures_dds/              — raw WG DDS + glTF-conformant siblings
            {tex_stem}_{a,n,mg,ao,normal,mr,nbmask}.{dd0,dd1,dd2,dds}

Texture stems do not always match the asset_id — multiple projectile
models share PBR maps via .visual material refs (JPT019 air torpedo
references JPR001 rocket textures, etc.). This is intentional WG asset
reuse, not a bug.

A small ``effect render set`` post-process pass renames + alpha-blends
the tracer / bow-shock geometry baked into ``CPA001_Shell_Main.visual``
so downstream consumers don't render them as solid 25-m PBR cylinders.
The heuristic is conservative (substring match on render-set names) and
leaves all other projectiles untouched.

The composer emits the following canonical :class:`StepEvent` names so
consumers can branch reliably:

    "discover_projectiles"   "plan_batch"           "batch_export"
    "recover_missing_diffuses"  "populate_materials"  "write_index"

Each step emits ``started`` / ``completed`` (or ``skipped`` /
``failed``). Per-step failures are wrapped in :class:`StepError` with
``step=`` set to one of the names above.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import toolkit
from ..config import PipelineConfig
from ..errors import StepError, ToolkitError
from ..resolve import sidecar as resolve_sidecar
from ..resolve import synth_emission
from ..types import OnEvent, ProjectileLibraryResult
from ._step_runner import StepRunner

# Default VFS manifest path (mirrors the I:-side convention). Override
# via the ``WOWS_VFS_MANIFEST`` env var or the ``manifest_path``
# parameter on the public entry.
_DEFAULT_MANIFEST_PATH = Path(
    os.environ.get(
        "WOWS_VFS_MANIFEST",
        r"C:/Users/sqz269/AppData/Local/Temp/wows_manifest.json",
    )
)

# /content/gameplay/{nation}/projectile/{category}/{ID}/{ID}.geometry
_PROJECTILE_PATH_RE = re.compile(
    r"^/?content/gameplay/(?P<nation>[^/]+)/projectile/(?P<category>[^/]+)"
    r"/(?P<asset_id>[^/]+)/(?P=asset_id)\.geometry$"
)

# Match `<dir>/textures/<stem>_a.dd0/.dd1/.dd2/.dds` anywhere under
# /content/gameplay/ — used to recover diffuse maps the toolkit's
# projectile-shader code path skips (compound MFM stems like
# `<base>_projectile.mfm` break its `<stem>_a.*` lookup).
_DIFFUSE_PATH_RE = re.compile(
    r"^/content/gameplay/(?:[^/]+/)+textures/"
    r"(?P<stem>.+?)_a\.(?:dd0|dd1|dd2|dds)$"
)

# Match channel suffixes on already-extracted DDS files. Used to recover
# the unique texture stems present in an asset's textures_dds/ dir.
_CHANNEL_SUFFIX_RE = re.compile(
    r"^(?P<stem>.+?)_(?:n|mg|mr|nbmask|normal|ao|emissive)\."
    r"(?:dd0|dd1|dd2|dds)$"
)

# Effect-overlay detection patterns (see ``mark_effect_render_sets``).
_EFFECT_NODE_PATTERNS: tuple[str, ...] = (
    "friction", "tracer", "trail", "shock",
)
_EFFECT_EMISSIVE_PATTERNS: tuple[str, ...] = (
    "head", "shock", "halo",
)


# ---------------------------------------------------------------------------
# Asset records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectileKey:
    """Identifies a unique projectile asset across the VFS."""

    nation:   str
    category: str
    asset_id: str


@dataclass
class ProjectileRecord:
    """Per-asset collected metadata + provenance."""

    key:              ProjectileKey
    geometry_vfs:     str
    glb_rel_path:     str | None = None
    glb_bytes:        int | None = None
    built_at:         int | None = None
    textures_dds_rel_dir: str | None = None
    # Material metadata extracted from the GLB.
    materials:        list[dict] = field(default_factory=list)
    # Variant → slot → DDS mip chain, scanned from textures_dds/.
    # Projectiles only ever have ``"main"`` (no dead variant, no camo).
    texture_sets:     dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _ensure_manifest(
    manifest_path: Path,
    *,
    config: PipelineConfig | None,
    refresh: bool,
) -> Path:
    """Return a usable VFS manifest, generating one if missing or stale."""
    if manifest_path.is_file() and not refresh:
        return manifest_path
    toolkit.metadata_json(manifest_path, config=config)
    return manifest_path


def discover_projectiles(
    manifest_path: Path,
) -> dict[ProjectileKey, ProjectileRecord]:
    """Walk the VFS manifest and return one record per projectile asset_id."""
    with open(manifest_path, encoding="utf-8") as f:
        entries = json.load(f)
    records: dict[ProjectileKey, ProjectileRecord] = {}
    for e in entries:
        path = e.get("path") or ""
        m = _PROJECTILE_PATH_RE.match(path)
        if not m:
            continue
        # Skip deleted entries — WG's index keeps stale paths as
        # zero-size directory-flagged stubs when assets get removed
        # (e.g. russia/projectile/missile/RPI2000_3M24_Uran). Their
        # .visual / .model siblings survive in pkg but the .geometry
        # is gone, so export-model would fail.
        if e.get("is_directory") or e.get("unpacked_size", 0) == 0:
            continue
        key = ProjectileKey(
            nation=m.group("nation"),
            category=m.group("category"),
            asset_id=m.group("asset_id"),
        )
        # Strip leading "/" from VFS path — toolkit accepts both but the
        # accessory builder uses unprefixed paths.
        records[key] = ProjectileRecord(
            key=key,
            geometry_vfs=path.lstrip("/"),
        )
    return records


def build_diffuse_index(manifest_path: Path) -> dict[str, list[str]]:
    """Scan the VFS manifest for every ``<stem>_a.dd?`` file under
    ``/content/gameplay/``. Returns ``{stem: [vfs_path, ...]}``.

    Used by the post-build diffuse recovery pass to fetch albedo maps
    the toolkit doesn't extract for projectile-shader materials. The
    index covers projectile texture dirs (where most diffuses live),
    misc accessory texture dirs (referenced by some torpedoes /
    depth-charges via shared accessory props), and the common/textures
    dir (the shared ``default_a.dds`` placeholder).
    """
    with open(manifest_path, encoding="utf-8") as f:
        entries = json.load(f)
    index: dict[str, list[str]] = {}
    for e in entries:
        if e.get("is_directory") or e.get("unpacked_size", 0) == 0:
            continue
        m = _DIFFUSE_PATH_RE.match(e.get("path") or "")
        if not m:
            continue
        index.setdefault(m.group("stem"), []).append(e["path"])
    return index


# ---------------------------------------------------------------------------
# Path computation
# ---------------------------------------------------------------------------


def output_dir_for(library_root: Path, key: ProjectileKey) -> Path:
    return library_root / key.nation / key.category / key.asset_id


# ---------------------------------------------------------------------------
# Effect-render-set marking (CPA001_Shell_Main fix)
# ---------------------------------------------------------------------------


def mark_effect_render_sets(
    glb_path: Path,
    *,
    emissive_strength: float = 2.5,
) -> int:
    """Detect 'effect overlay' render sets in a projectile GLB and rewrite
    its material/node entries so they don't render as solid PBR meshes.

    Some projectile .visual files bake effect geometry (tracer trails,
    bow shock cones) alongside the real shell body. The toolkit emits
    these as plain opaque PBR primitives — viewed in any glTF viewer
    they appear as a 25 m solid cylinder + 4 m cone hat overlapping the
    shell body. This pass detects them by node name (``friction``,
    ``tracer``, …) and:

      * Renames node + mesh + material with an ``effect_`` prefix so
        downstream consumers (Unity ShipMaterialBuilder, the web
        viewer) can filter them or route them to a particles/trails
        shader.
      * Sets ``alphaMode: BLEND`` + low ``baseColorFactor`` alpha so
        naive glTF viewers show them as translucent overlays instead
        of solid opaque meshes.
      * For emissive-style effects (the bow-shock cone), adds
        ``KHR_materials_emissive_strength`` so the cone glows under
        URP Bloom rather than rendering flat.
      * Stamps ``material.extras.wg_intent`` for direct identification.

    Returns the number of effect render sets marked. The body render
    set is left untouched. Idempotent: re-running on an already-marked
    GLB is a no-op.

    Currently only triggers on ``CPA001_Shell_Main`` (the only
    projectile of 222 with friction-style geometry baked into the
    visual); the heuristic is conservative and leaves every other
    projectile untouched.
    """
    if not glb_path.is_file():
        return 0

    with open(glb_path, "rb") as f:
        magic = f.read(4)
        if magic != b"glTF":
            return 0
        _version, _total = struct.unpack("<II", f.read(8))
        json_len = struct.unpack("<I", f.read(4))[0]
        json_type = f.read(4)
        if json_type != b"JSON":
            return 0
        json_bytes = f.read(json_len)
        gltf = json.loads(json_bytes.decode("utf-8"))
        rest = f.read()

    bin_data = b""
    if len(rest) >= 8:
        bin_len = struct.unpack("<I", rest[:4])[0]
        bin_type = rest[4:8]
        if bin_type == b"BIN\x00":
            bin_data = rest[8:8 + bin_len]

    nodes = gltf.get("nodes", []) or []
    meshes = gltf.get("meshes", []) or []
    materials = gltf.get("materials", []) or []

    marked = 0
    for node in nodes:
        node_name = node.get("name") or ""
        if node_name.startswith("effect_"):
            continue  # already marked
        lower = node_name.lower()
        if not any(p in lower for p in _EFFECT_NODE_PATTERNS):
            continue
        is_emissive = any(p in lower for p in _EFFECT_EMISSIVE_PATTERNS)
        intent_label = (
            "bow_shock_emissive" if is_emissive else "tracer_trail_additive"
        )

        node["name"] = f"effect_{node_name}"

        mesh_idx = node.get("mesh")
        if mesh_idx is None or not (0 <= mesh_idx < len(meshes)):
            marked += 1
            continue
        mesh = meshes[mesh_idx]
        if mesh.get("name") and not mesh["name"].startswith("effect_"):
            mesh["name"] = f"effect_{mesh['name']}"

        for prim in mesh.get("primitives", []) or []:
            mat_idx = prim.get("material")
            if mat_idx is None or not (0 <= mat_idx < len(materials)):
                continue
            mat = materials[mat_idx]
            if (mat.get("name") or "").startswith("effect_"):
                continue  # already marked (multiple nodes share material)

            if mat.get("name"):
                mat["name"] = f"effect_{mat['name']}"

            mat["alphaMode"] = "BLEND"
            pbr = mat.setdefault("pbrMetallicRoughness", {})
            bc = list(pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0]))
            while len(bc) < 4:
                bc.append(1.0)
            bc[3] = 0.5 if is_emissive else 0.4
            pbr["baseColorFactor"] = bc

            if is_emissive:
                mat["emissiveFactor"] = [1.0, 1.0, 1.0]
                ext = mat.setdefault("extensions", {})
                ext["KHR_materials_emissive_strength"] = {
                    "emissiveStrength": float(emissive_strength)
                }
                exts_used = gltf.setdefault("extensionsUsed", [])
                if "KHR_materials_emissive_strength" not in exts_used:
                    exts_used.append("KHR_materials_emissive_strength")

            extras = mat.setdefault("extras", {})
            extras["wg_intent"] = intent_label

        marked += 1

    if marked == 0:
        return 0

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    while len(new_json) % 4 != 0:
        new_json += b" "
    if bin_data:
        while len(bin_data) % 4 != 0:
            bin_data += b"\x00"
        new_total = 12 + 8 + len(new_json) + 8 + len(bin_data)
    else:
        new_total = 12 + 8 + len(new_json)

    with open(glb_path, "wb") as f:
        f.write(b"glTF")
        f.write(struct.pack("<II", 2, new_total))
        f.write(struct.pack("<I", len(new_json)))
        f.write(b"JSON")
        f.write(new_json)
        if bin_data:
            f.write(struct.pack("<I", len(bin_data)))
            f.write(b"BIN\x00")
            f.write(bin_data)

    return marked


# ---------------------------------------------------------------------------
# Build (batch toolkit call + post-processing)
# ---------------------------------------------------------------------------


def _post_build_rec(
    rec: ProjectileRecord,
    library_root: Path,
    out_dir: Path,
    glb_path: Path,
) -> bool:
    if not glb_path.is_file():
        return False
    rec.glb_rel_path = str(glb_path.relative_to(library_root)).replace("\\", "/")
    stat = glb_path.stat()
    rec.glb_bytes = stat.st_size
    rec.built_at = int(stat.st_mtime)
    dds_dir = out_dir / "textures_dds"
    rec.textures_dds_rel_dir = (
        str(dds_dir.relative_to(library_root)).replace("\\", "/")
        if dds_dir.is_dir() else None
    )
    return True


def _stems_in_dir(dds_dir: Path) -> set[str]:
    """Unique texture stems present in an asset's ``textures_dds/`` dir,
    derived from existing channel files (``_n``, ``_mg``, ``_ao``, …).

    The toolkit dumps PBR auxiliary channels reliably, so any diffuse
    we need to recover will share a stem with one of those siblings.
    """
    if not dds_dir.is_dir():
        return set()
    stems: set[str] = set()
    for f in dds_dir.iterdir():
        if not f.is_file():
            continue
        m = _CHANNEL_SUFFIX_RE.match(f.name)
        if m:
            stems.add(m.group("stem"))
    return stems


def build_projectiles_batch(
    records: dict[ProjectileKey, ProjectileRecord],
    library_root: Path,
    *,
    mode: str,
    force: bool,
    config: PipelineConfig | None,
    only: set[str] | None = None,
) -> tuple[int, int, int, list[str]]:
    """Batch-export every projectile in one wowsunpack invocation.

    Amortises the ``assets.bin`` parse cost. ``only`` (when set)
    restricts the *force-rebuild* scope to the listed asset_ids — every
    other record is treated as "skip if GLB exists". The full
    ``records`` dict still flows through so :func:`_post_build_rec`
    populates ``glb_rel_path`` etc. for every existing on-disk GLB.

    Returns ``(built, skipped, failed, failure_strings)``.
    """
    items: list[dict] = []
    record_for_geom: dict[str, ProjectileRecord] = {}
    skipped = 0

    for rec in sorted(records.values(), key=lambda r: r.key.asset_id):
        out_dir = output_dir_for(library_root, rec.key)
        glb_path = out_dir / f"{rec.key.asset_id}.glb"

        # Rebuild iff the GLB is missing, OR the caller asked for force AND
        # this record is in scope (no `only`, or `only` listed it).
        rebuild = not glb_path.is_file() or (
            force and (only is None or rec.key.asset_id in only)
        )
        if not rebuild:
            if glb_path.is_file():
                _post_build_rec(rec, library_root, out_dir, glb_path)
            skipped += 1
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        entry: dict = {"geometry": rec.geometry_vfs, "output": str(glb_path)}
        if mode in ("png", "both"):
            entry["textures_dir"] = str(out_dir / "textures")
        if mode in ("dds", "both"):
            entry["raw_dds_dir"] = str(out_dir / "textures_dds")
        items.append(entry)
        record_for_geom[rec.geometry_vfs] = rec

    if not items:
        return (0, skipped, 0, [])

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
        return (
            0, skipped, len(items),
            [f"batch aborted: {str(e).splitlines()[0][:120]}"],
        )

    built = 0
    failed = 0
    failure_strings: list[str] = []
    for entry in items:
        rec = record_for_geom[entry["geometry"]]
        out_dir = output_dir_for(library_root, rec.key)
        expected = Path(entry["output"])
        if not expected.is_file():
            failed += 1
            if len(failure_strings) < 5:
                failure_strings.append(
                    f"{rec.key.asset_id}: no GLB at {expected.name} "
                    "(see toolkit output above)"
                )
            continue
        if _post_build_rec(rec, library_root, out_dir, expected):
            built += 1

    return (built, skipped, failed, failure_strings)


def recover_missing_diffuses(
    records: dict[ProjectileKey, ProjectileRecord],
    library_root: Path,
    diffuse_index: dict[str, list[str]],
    *,
    config: PipelineConfig | None,
) -> int:
    """Bulk-extract diffuse ``_a.dd?`` files the toolkit's projectile
    code path skips, then redistribute them into per-asset
    ``textures_dds/`` dirs.

    Why this pass is needed: the toolkit's ``load_base_albedo_bytes``
    derives the albedo stem from the MFM filename (``<stem>.mfm`` →
    ``<stem>_a.dd?``). Most projectiles use a ``<base>_projectile.mfm``
    side material whose stem doesn't match the actual texture base —
    so the lookup fails silently and ``_a.dd?`` is never dumped.

    For every asset missing diffuse files, we read the actual texture
    stems from its already-emitted PBR siblings (via
    :func:`_stems_in_dir`), look them up in the VFS-wide diffuse index,
    and bulk-extract the missing files in a single
    ``wowsunpack extract`` invocation. Then we copy each extracted file
    into the right per-asset directory (multiple assets may share the
    same source stem — JPT torpedoes reusing JPR rocket textures, for
    instance).

    Returns the count of files copied into per-asset directories.
    """
    # Plan: collect (asset_dds_dir, stem, vfs_path) triples.
    work: list[tuple[Path, str, str]] = []
    needed_paths: set[str] = set()
    for rec in records.values():
        if rec.glb_rel_path is None:
            continue
        out_dir = output_dir_for(library_root, rec.key)
        dds_dir = out_dir / "textures_dds"
        existing = (
            {f.name for f in dds_dir.iterdir() if f.is_file()}
            if dds_dir.is_dir() else set()
        )
        # If the asset already has any `_a.dd?`, treat it as covered.
        if any(re.search(r"_a\.(dd0|dd1|dd2|dds)$", f) for f in existing):
            continue
        for stem in _stems_in_dir(dds_dir):
            for vfs_path in diffuse_index.get(stem, []):
                fname = vfs_path.rsplit("/", 1)[-1]
                if fname in existing:
                    continue
                work.append((dds_dir, stem, vfs_path))
                needed_paths.add(vfs_path)

    if not work:
        return 0

    # ONE extract call for all missing files — pays the VFS-parse cost once.
    # Prepend `**/` so each fully-qualified VFS path gets matched as a glob
    # (the toolkit's extract matcher requires anchoring patterns; a literal
    # `content/...` path matches zero files, but `**/content/...` matches
    # the one we want).
    staging = Path(tempfile.mkdtemp(prefix="wows_proj_diffuse_"))
    try:
        patterns = [
            f"**{p}" if p.startswith("/") else f"**/{p}"
            for p in needed_paths
        ]
        toolkit.extract(patterns, out_dir=staging, config=config)

        # Redistribute. extract() preserves VFS path under out_dir; the
        # path matches the manifest path verbatim (with leading "/").
        copied = 0
        for asset_dir, _stem, vfs_path in work:
            src = staging / vfs_path.lstrip("/")
            if not src.is_file():
                continue
            dst = asset_dir / src.name
            asset_dir.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
                copied += 1
        return copied
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def populate_material_manifest(
    records: dict[ProjectileKey, ProjectileRecord],
    library_root: Path,
    *,
    emission_intensity: float = 2.5,
    warnings: list[str],
) -> None:
    """For each successfully-built record, extract per-material metadata
    from its GLB and a per-variant texture-set manifest from its
    ``textures_dds/`` directory. Joined at runtime by Unity to bind
    materials to DDS files.

    Mirrors the accessory library's ``_extract_material_manifest`` with
    one important difference: projectile shaders
    (``SHIPMAT_NODAMAGE_PBS_Projectile``, ``SHIPMAT_EMISSIVE_PBS_*``,
    ``PBS_field2d*``, …) are NOT in ``_LIBRARY_MATERIAL_STEMS``, so
    ``_bind_dds_textures_by_name`` can't fill in per-material
    ``texture_sets``. Without intervention, every material would carry
    an empty ``texture_sets["main"]`` block — the GLB's mesh primitives
    would resolve to material 0 with ``baseColorFactor=[1,1,1,1]`` and
    no textures, rendering as flat white in Unity.

    The fix: for any material whose ``texture_sets["main"]`` came back
    empty, copy the asset-level ``texture_sets["main"]`` (built by
    :func:`texture_sets_from_dir` from the on-disk DDS scan) into the
    material entry.
    """
    for rec in records.values():
        if rec.glb_rel_path is None:
            continue
        glb_path = library_root / rec.glb_rel_path
        out_dir = output_dir_for(library_root, rec.key)
        dds_dir = out_dir / "textures_dds"
        try:
            rec.materials = resolve_sidecar.materials_from_glb(
                glb_path,
                textures_dds_dir=dds_dir if dds_dir.is_dir() else None,
            )
            rec.texture_sets = (
                resolve_sidecar.texture_sets_from_dir(dds_dir)
                if dds_dir.is_dir() else {}
            )
        except Exception as e:
            warnings.append(
                f"material manifest extraction failed for "
                f"{rec.key.asset_id}: {e}"
            )
            rec.materials = []
            rec.texture_sets = {}
            continue

        asset_main_slots = (rec.texture_sets or {}).get("main") or {}
        if not asset_main_slots:
            continue
        for mat in rec.materials:
            mat_id_raw = mat.get("material_id") or ""
            is_effect = mat_id_raw.startswith("effect_")
            mat_ts = mat.setdefault("texture_sets", {})

            if is_effect:
                # Effect overlays (tracer trail, bow-shock cone) don't share
                # body textures — their MFMs reference particle/trail DDS
                # files (Trail_GK.dds, Trail_Shell_Hat.dds) that aren't in
                # this asset's textures_dds/ dir. Skip the asset-fallback
                # so Unity binds nothing and falls back to baseColorFactor
                # + alphaMode=BLEND from the GLB.
                if "HEAD" in mat_id_raw.upper():
                    # Bow shock: route through the emissive shader path.
                    mat["shader_intent"] = "emissive"
                    factors = mat.setdefault("factors", {})
                    factors["emissive_strength"] = float(emission_intensity)
                continue

            if not mat_ts.get("main"):
                # Deep-copy the slot dict so future per-material edits
                # don't propagate across siblings.
                mat_ts["main"] = {
                    slot: list(mips)
                    for slot, mips in asset_main_slots.items()
                }

            # Emissive fallback. Star Trek / Halloween / Space-themed
            # projectiles use `[TL2_]SHIPMAT_EMISSIVE_PBS*` materials but
            # most don't ship a paired `*_emissive.mfm` (only 2 of 24
            # do — GPT011 Vulcan torpedo + JGB3007 Star Trek depth charge).
            # The remaining 22 use their `_a.dds` directly as the emission
            # colour: WG's `ship_emissive_material.fx` reads the diffuse
            # texel as RGB emission when no separate mask is bound. We
            # mirror that here by aliasing `baseColor` → `emissive` so
            # ShipMaterialBuilder binds `_EmissionMap` and sets
            # `_EmissionColor` to white.
            mat_id = mat_id_raw.upper()
            if "EMISSIVE" in mat_id:
                main = mat_ts.setdefault("main", {})
                if not main.get("emissive") and main.get("baseColor"):
                    main["emissive"] = list(main["baseColor"])

            # Emission intensity. Set `factors.emissive_strength` on
            # any material that has an emissive slot (whether
            # synthesised, aliased, or from a future material-name
            # convention) so ShipMaterialBuilder pushes
            # `_EmissionStrength` above 1.0 — required for URP Bloom
            # to pick up the emission. Non-emissive materials leave
            # the field absent (shader default 1.0 applies).
            if mat_ts.get("main", {}).get("emissive"):
                factors = mat.setdefault("factors", {})
                factors["emissive_strength"] = float(emission_intensity)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def write_index(
    records: dict[ProjectileKey, ProjectileRecord],
    library_root: Path,
) -> Path:
    """Emit ``libraries/projectiles/index.json``.

    Carries ``built_at`` forward across rebuilds (matches accessory-
    library convention) so downstream consumers can sort by "made
    available" without being skewed by re-export touches.
    """
    out = library_root / "index.json"
    prior_built_at: dict[str, int] = {}
    if out.is_file():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
            for aid, entry in (prior.get("assets") or {}).items():
                ts = entry.get("built_at")
                if isinstance(ts, (int, float)):
                    prior_built_at[aid] = int(ts)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    assets: dict[str, dict] = {}
    for rec in sorted(records.values(), key=lambda r: r.key.asset_id):
        if rec.glb_rel_path is None:
            continue
        kept = prior_built_at.get(rec.key.asset_id)
        if kept is not None:
            rec.built_at = kept
        entry = {
            "nation": rec.key.nation,
            "category": rec.key.category,
            "geometry_vfs": rec.geometry_vfs,
            "glb": rec.glb_rel_path,
            "glb_bytes": rec.glb_bytes,
            "built_at": rec.built_at,
            "textures_dds": rec.textures_dds_rel_dir,
        }
        if rec.materials:
            entry["materials"] = rec.materials
        if rec.texture_sets:
            entry["texture_sets"] = rec.texture_sets
        assets[rec.key.asset_id] = entry

    doc = {
        "version": time.strftime("%Y-%m-%d", time.gmtime()),
        "asset_count": len(assets),
        "assets": assets,
    }
    # Atomic write: an interrupt mid-write would otherwise produce
    # invalid JSON that breaks both the next library run and Unity's
    # import.
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def build_projectile_library(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    rebuild: bool = False,
    on_event: OnEvent | None = None,
    manifest_path: Path | None = None,
    refresh_manifest: bool = False,
    mode: str = "dds",
    only: tuple[str, ...] | None = None,
    emission_intensity: float = 2.5,
) -> ProjectileLibraryResult:
    """Build / refresh the fleet-wide projectile library.

    The composer enumerates every
    ``content/gameplay/{nation}/projectile/{category}/{ID}/{ID}.geometry``
    in the VFS manifest, deduplicates per-asset_id, and calls
    ``toolkit.batch_export_model`` to write one GLB + DDS mip chain per
    asset under ``library_root``.

    Parameters:
        workspace            ``PipelineConfig.workspace`` when None.
        config               ``PipelineConfig.load()`` when None.
        library_root         ``workspace / "libraries/projectiles"``
                              when None.
        rebuild              When True, re-export every asset's GLB
                              even if it already exists. Default skips
                              already-built assets.
        on_event             Optional progress callback receiving
                              :class:`StepEvent` notifications.
        manifest_path        VFS manifest JSON path (default: the
                              ``WOWS_VFS_MANIFEST`` env var, falling back
                              to the I:-side temp-dir convention).
        refresh_manifest     When True, regenerate the VFS manifest
                              before discovery.
        mode                 ``"dds"`` (default), ``"both"`` (DDS+PNG),
                              ``"png"``, or ``"none"`` (GLB only).
        only                 Restrict the force-rebuild scope to the
                              listed asset_ids. Records outside ``only``
                              still flow through so the index reflects
                              every on-disk GLB.
        emission_intensity   Multiplier on emission output for materials
                              with an emissive slot. Above 1.0 pushes
                              emission into HDR territory so URP Bloom
                              picks it up; default 2.5 produces a
                              moderate glow on Star Trek / Halloween /
                              Vulcan torpedoes.

    Returns a :class:`ProjectileLibraryResult` with the library root,
    counts, warnings, and per-step timings.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    step names) when any step fails. The original exception is
    accessible via ``.underlying``.
    """
    cfg = config or PipelineConfig.load()
    ws = (workspace or cfg.workspace).resolve()
    lib_root = (library_root or (ws / "libraries" / "projectiles")).resolve()
    manifest = (manifest_path or _DEFAULT_MANIFEST_PATH).resolve()
    only_ids = set(only) if only else None

    runner = StepRunner(on_event)
    warnings: list[str] = []

    # ── Step: discover_projectiles ────────────────────────────────────
    try:
        with runner.step("discover_projectiles") as st:
            _ensure_manifest(manifest, config=cfg, refresh=refresh_manifest)
            records = discover_projectiles(manifest)
            if only_ids:
                unknown = only_ids - {r.key.asset_id for r in records.values()}
                if unknown:
                    warnings.append(
                        f"only listed unknown asset_id(s): "
                        f"{', '.join(sorted(unknown))}"
                    )
            by_cat: dict[tuple[str, str], int] = {}
            for k in records:
                by_cat[(k.nation, k.category)] = (
                    by_cat.get((k.nation, k.category), 0) + 1
                )
            st.annotate(
                f"{len(records)} unique projectile(s) in "
                f"{len(by_cat)} (nation, category) bucket(s)",
                data={
                    "projectiles": len(records),
                    "buckets": {
                        f"{n}/{c}": v for (n, c), v in by_cat.items()
                    },
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="discover_projectiles", underlying=e, detail=str(e),
        ) from e

    if not records:
        return ProjectileLibraryResult(
            library_root=lib_root,
            projectiles_built=0,
            projectiles_audited=0,
            warnings=tuple(warnings),
            step_timings_ms=dict(runner.step_timings_ms),
        )

    lib_root.mkdir(parents=True, exist_ok=True)

    # ── Step: plan_batch ──────────────────────────────────────────────
    try:
        with runner.step("plan_batch") as st:
            planned = 0
            for rec in records.values():
                out_dir = output_dir_for(lib_root, rec.key)
                glb_path = out_dir / f"{rec.key.asset_id}.glb"
                if not glb_path.is_file() or (
                    rebuild and (only_ids is None
                                 or rec.key.asset_id in only_ids)
                ):
                    planned += 1
            st.annotate(
                f"planned build of {planned} projectile(s) "
                f"(force={rebuild}, only={'<set>' if only_ids else None})",
                data={"planned": planned},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="plan_batch", underlying=e, detail=str(e)) from e

    # ── Step: batch_export ────────────────────────────────────────────
    try:
        with runner.step("batch_export") as st:
            built, skipped, failed, fail_examples = build_projectiles_batch(
                records, lib_root,
                mode=mode, force=rebuild, only=only_ids,
                config=cfg,
            )
            for fx in fail_examples:
                warnings.append(fx)
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
        raise StepError(
            step="batch_export", underlying=e, detail=str(e),
        ) from e

    # ── Step: recover_missing_diffuses ────────────────────────────────
    # Only runs in dds / both modes when at least one GLB built.
    if mode in ("dds", "both") and any(
        rec.glb_rel_path for rec in records.values()
    ):
        try:
            with runner.step("recover_missing_diffuses") as st:
                diffuse_index = build_diffuse_index(manifest)
                recovered = recover_missing_diffuses(
                    records, lib_root, diffuse_index, config=cfg,
                )
                st.annotate(
                    f"recovered {recovered} diffuse file(s)",
                    data={"recovered": recovered},
                )
        except StepError:
            raise
        except Exception as e:
            warnings.append(f"recover_missing_diffuses failed: {e}")
    else:
        runner.emit(
            "recover_missing_diffuses", "skipped",
            detail="mode not in (dds, both) or no built GLBs",
        )
        runner.step_timings_ms["recover_missing_diffuses"] = 0.0

    # ── Emissive synthesis ────────────────────────────────────────────
    # Not its own canonical step — see accessory_library for the same
    # pattern. Runs after diffuse recovery so synth has both `_a` and
    # `_mg` siblings available.
    if mode in ("dds", "both"):
        synth_dirs: list[Path] = []
        for rec in records.values():
            if rec.glb_rel_path is None:
                continue
            dds_dir = output_dir_for(lib_root, rec.key) / "textures_dds"
            if dds_dir.is_dir() and dds_dir not in synth_dirs:
                synth_dirs.append(dds_dir)
        if synth_dirs:
            try:
                synth_emission.synthesize_emissive_textures_batch(
                    synth_dirs,
                    config=cfg,
                    label="projectile-library",
                )
            except Exception as e:
                warnings.append(f"batched emissive synth failed: {e}")

    # ── Effect-render-set marking pass ────────────────────────────────
    # Must run BEFORE populate_material_manifest so the rewritten
    # alpha/emissive flow into the index entries. Not its own canonical
    # step — bundled into the post-export side-effect chain.
    for rec in records.values():
        if rec.glb_rel_path is None:
            continue
        glb_path = lib_root / rec.glb_rel_path
        try:
            n = mark_effect_render_sets(
                glb_path, emissive_strength=emission_intensity,
            )
        except Exception as e:
            warnings.append(
                f"effect-marking failed for {rec.key.asset_id}: {e}"
            )
            continue
        if n > 0:
            # GLB was rewritten — refresh size + mtime in the record.
            stat = glb_path.stat()
            rec.glb_bytes = stat.st_size
            rec.built_at = int(stat.st_mtime)

    # ── Step: populate_materials ──────────────────────────────────────
    try:
        with runner.step("populate_materials") as st:
            populate_material_manifest(
                records, lib_root,
                emission_intensity=emission_intensity,
                warnings=warnings,
            )
            n_with_materials = sum(
                1 for r in records.values() if r.materials
            )
            st.annotate(
                f"{n_with_materials} asset(s) with material manifests",
                data={"with_materials": n_with_materials},
            )
    except StepError:
        raise
    except Exception as e:
        warnings.append(f"populate_materials failed: {e}")

    # ── Step: write_index ─────────────────────────────────────────────
    try:
        with runner.step("write_index") as st:
            index_path = write_index(records, lib_root)
            st.annotate(f"wrote {index_path.name}")
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="write_index", underlying=e, detail=str(e),
        ) from e

    projectiles_built = sum(
        1 for r in records.values() if r.glb_rel_path is not None
    )
    return ProjectileLibraryResult(
        library_root=lib_root,
        projectiles_built=projectiles_built,
        projectiles_audited=len(records),
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "ProjectileKey",
    "ProjectileRecord",
    "build_diffuse_index",
    "build_projectile_library",
    "build_projectiles_batch",
    "discover_projectiles",
    "mark_effect_render_sets",
    "output_dir_for",
    "populate_material_manifest",
    "recover_missing_diffuses",
    "write_index",
]
