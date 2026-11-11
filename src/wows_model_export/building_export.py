"""Building (shore-structure) export — GameParams ``Building`` entries.

WoWS buildings are stripped ships: a ``hull`` dict (model/deadModel, health,
``armor {materialId: mm}``, canBeSuppressed, visibility) plus optional gun
components (``artillery`` / ``airDefense``) whose ``HP_*`` mounts are full
GameParams ``Gun`` entries — the same schema as ship guns. Their hit geometry
ships inside the model ``.geometry`` as the ``hit_locations`` collision model
(no armor BVH, no ``.splash``); the toolkit's ``--collision-hitbox-groups``
synthesizes ship-convention ``Armor`` / ``Hitboxes`` GLB groups from it, and
``--emit-hardpoints`` keeps the static ``HP_*`` mount nodes in the node tree.

Output mirrors the ship layout so consumers reuse their ship bakers:

    <workspace>/buildings/<index>/
      <index>.meta.json            <- building sidecar (ship-shaped sections)
      export.json                  <- run report
      models/<index>_hull.glb      <- hull (+ Armor/Hitboxes groups, HP_* nodes)
      models/<index>_hull_dead.glb <- deadModel (only when distinct from live)
      models/<index>_material_mappings.json
      models/textures_dds/         <- raw DDS for the hull
    <workspace>/buildings/_shared/guns/<stem>/
      <stem>.glb  (+ material mappings, textures_dds/)
                                   <- gun mounts (live and dead models are
                                      separate stems), shared across buildings
    <workspace>/buildings/_shared/guns/index.json
                                   <- gun library index: per stem its GLB and
                                      its materials (texture paths)

The sidecar's ``turrets`` / ``hitbox`` / ``armor`` / ``ballistics`` /
``materials`` sections use the same shapes as ship ``.meta.json`` files (see
``resolve/sidecar/_makers.py``), with building-specific identity under
``building``. Armor thickness comes from GameParams ``hull.armor`` — keys are
``(layer << 16) | material_id``; the GLB's per-vertex ``_MATERIAL_ID`` is the
low 16 bits, so the ``materials_table`` here is keyed by the same fold.

Textures. ``materials`` (hull GLB) and ``dead_materials`` (dead hull GLB)
carry each material's texture slots as ``dds_mips`` path lists, bound from
the toolkit's material mappings; the gun index carries the same per gun
GLB. Paths follow the workspace rule: one that starts with ``libraries/``
is relative to the workspace root (the shared texture store), any other is
relative to the directory of the GLB that owns it (``models/`` for a hull,
``_shared/guns/<stem>/`` for a gun). Never find a texture by listing a
``textures_dds/`` folder: a set moves to the store whenever another export
holds the same bytes.

Mount transforms. ``turrets[].transform`` is the ship-placement rule the
toolkit applies to ship mounts: the hull's ``HP_*`` node world matrix
times the inverse rotation of the gun model's ``Rotate_Y_BlendBone``,
made a proper rotation (a Z-mirror bone gives ``HP x Ry(180)``). The gun
GLB rendered at that matrix faces the way WG's engine renders it.
``dead_transform`` applies the same rule with the dead model's own bone
(dead gun models carry none, so it is the raw node), and ``hp_transform``
is the raw node. A consumer that parents a gun under the ``HP_*`` node
uses ``inverse(hp_transform) x transform`` as the local matrix.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import shared_textures
from ._glb import flip_winding, parse_glb, write_glb
from .config import PipelineConfig
from .read import gameparams
from .resolve import sidecar as resolve_sidecar
from .resolve.gameparams_autofill import _hit_location_meta
from .toolkit.ship import batch_export_model

MODELS_SUBDIR = "models"
SHARED_GUNS_SUBDIR = "_shared/guns"
GUN_INDEX_NAME = "index.json"
GUN_INDEX_SCHEMA_VERSION = 1

# Sidecar schema version for building meta.json files. Independent of the
# ship sidecar's schema_version — consumers key off ``pipeline.kind``.
# Additions since the first version (materials, dead_materials,
# turrets[].hp_transform / dead_transform) are additive; ``transform`` now
# follows the ship-placement rule (see the module docstring).
BUILDING_SCHEMA_VERSION = 1

# The gun bone whose rest rotation the ship-placement rule inverts.
ROTATE_Y_BLEND_BONE = "Rotate_Y_BlendBone"

_HULL_FLIP_PREFIXES = ("Armor_", "CM_SB_")

# Gun-bearing component dicts on a Building entry. ``airDefense`` mounts are
# exported for visual completeness (flak guns on forts) but carry
# ``species: "aa"`` so consumers skip gunnery wiring until aircraft exist.
_GUN_COMPONENTS = (("artillery", "main"), ("airDefense", "aa"))

# Ship ballistics-schema field <- GameParams Projectile field. Mirrors the
# toolkit's `ammo --json` output (that command is Vehicle-only, so buildings
# format shells here).
_SHELL_FIELDS = (
    ("ammo_type", "ammoType", None),
    ("mass_kg", "bulletMass", None),
    ("muzzle_velocity_mps", "bulletSpeed", None),
    ("air_drag_coefficient", "bulletAirDrag", None),
    ("krupp", "bulletKrupp", None),
    ("cap", "bulletCap", None),
    ("cap_normalize_max_deg", "bulletCapNormalizeMaxAngle", None),
    ("fuze_arming_threshold_mm", "bulletDetonatorThreshold", None),
    ("fuze_delay_s", "bulletDetonator", None),
    ("ricochet_min_deg", "bulletRicochetAt", None),
    ("ricochet_always_deg", "bulletAlwaysRicochetAt", None),
    ("alpha_damage", "alphaDamage", None),
    ("alpha_piercing_he_mm", "alphaPiercingHE", None),
    ("alpha_piercing_cs_mm", "alphaPiercingCS", None),
    ("burn_probability", "burnProb", None),
)

# Mount-level dispersion keys copied verbatim into the turret entry's
# ``dispersion`` dict (same key names as ship sidecars — the ship pipeline
# passes WG's names through unchanged).
_DISPERSION_MOUNT_KEYS = (
    "aiMGmaxEllipseRanging",
    "aiMGmedEllipseRanging",
    "aiMGminEllipseRanging",
    "delim",
    "ellipseRangeMax",
    "ellipseRangeMin",
    "idealDistance",
    "idealRadius",
    "maxEllipseRanging",
    "medEllipseRanging",
    "minEllipseRanging",
    "minRadius",
    "radiusOnDelim",
    "radiusOnMax",
    "radiusOnZero",
)
# Component-level dispersion keys (ship: artillery component).
_DISPERSION_COMPONENT_KEYS = ("maxDist", "normalDistribution", "sigmaCount", "taperDist")


# ── layout ────────────────────────────────────────────────────────────


def buildings_root(config: PipelineConfig) -> Path:
    return (config.workspace / "buildings").resolve()


def building_dir(config: PipelineConfig, index: str) -> Path:
    return buildings_root(config) / index


def sidecar_path_for(config: PipelineConfig, index: str) -> Path:
    return building_dir(config, index) / f"{index}.meta.json"


def shared_gun_dir(config: PipelineConfig, stem: str) -> Path:
    return buildings_root(config) / SHARED_GUNS_SUBDIR / stem


def gun_index_path(config: PipelineConfig) -> Path:
    return buildings_root(config) / SHARED_GUNS_SUBDIR / GUN_INDEX_NAME


# ── GameParams access ─────────────────────────────────────────────────


def iter_building_entries(gp: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(index, entry)`` for every ``typeinfo.type == "Building"``."""
    for key, entry in gp.items():
        if not isinstance(entry, dict):
            continue
        ti = entry.get("typeinfo")
        if isinstance(ti, dict) and ti.get("type") == "Building":
            yield key, entry


def _gun_mounts(entry: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Return ``(component, species, hp_name, gun_dict)`` for every gun mount.

    Mounts are the sub-dicts of a gun component whose ``typeinfo.type`` is
    ``Gun`` — the dict key is the hull hardpoint name (``HP_GGM_1`` etc.).
    Aura blocks (``AuraNear`` …) carry no typeinfo and are skipped.
    """
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    for comp_name, species in _GUN_COMPONENTS:
        comp = entry.get(comp_name)
        if not isinstance(comp, dict):
            continue
        for hp_name, gun in sorted(comp.items()):
            if not isinstance(gun, dict):
                continue
            ti = gun.get("typeinfo")
            if isinstance(ti, dict) and ti.get("type") == "Gun" and isinstance(gun.get("model"), str):
                out.append((comp_name, species, hp_name, gun))
    return out


def _geometry_vfs(model_path: str) -> str:
    """``…/LYB030.model`` → ``…/LYB030.geometry`` (VFS path)."""
    if not model_path.endswith(".model"):
        raise ValueError(f"not a .model path: {model_path!r}")
    return model_path[: -len(".model")] + ".geometry"


def _stem(model_path: str) -> str:
    return model_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _scope_of(model_path: str) -> str:
    """Nation/scope segment: ``content/gameplay/usa/gun/…`` → ``usa``;
    ``content/location/gameplay/…`` → ``location``."""
    parts = model_path.split("/")
    try:
        i = parts.index("content")
    except ValueError:
        return ""
    rest = parts[i + 1 :]
    if rest and rest[0] == "gameplay" and len(rest) > 1:
        return rest[1]
    return rest[0] if rest else ""


# ── sidecar composition helpers ───────────────────────────────────────


def _fold_armor_table(hull_armor: dict[str, Any], warn: Callable[[str], None]) -> dict[str, Any]:
    """GameParams ``hull.armor`` → ship-shaped ``materials_table``.

    WG keys are ``(layer << 16) | material_id``; the GLB ``_MATERIAL_ID``
    vertex attribute is the low 16 bits, so thickness lookups must be keyed
    by the fold. Cross-layer thickness conflicts for one material id take
    the MAX (conservative for penetration checks) with a warning — not
    observed in the current corpus (all layers of a material share one mm).
    """
    table: dict[str, dict[str, Any]] = {}
    for raw_key, mm in sorted(hull_armor.items()):
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            warn(f"armor key {raw_key!r} is not numeric — skipped")
            continue
        mat_id = key & 0xFFFF
        layer = (key >> 16) & 0xFFFF
        slot = table.setdefault(str(mat_id), {"thickness_mm": float(mm), "layers": [], "zones": []})
        slot["layers"].append(layer)
        if float(mm) != slot["thickness_mm"]:
            warn(
                f"armor material {mat_id}: layer {layer} thickness {mm} != {slot['thickness_mm']} — keeping max"
            )
            slot["thickness_mm"] = max(slot["thickness_mm"], float(mm))
    return table


def _shell_from_projectile(proj: dict[str, Any], max_range_m: float | None) -> dict[str, Any]:
    shell: dict[str, Any] = {}
    for out_key, in_key, default in _SHELL_FIELDS:
        val = proj.get(in_key, default)
        if val is not None:
            shell[out_key] = val
    diam = proj.get("bulletDiametr")
    if diam is not None:
        shell["caliber_mm"] = float(diam) * 1000.0
    if max_range_m is not None:
        shell["max_range_m"] = float(max_range_m)
    return shell


def _float_pair(val: Any) -> list[float] | None:
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            return [float(val[0]), float(val[1])]
        except (TypeError, ValueError):
            return None
    return None


# ── GLB introspection (hitbox names + HP transforms) ──────────────────


def _quat_to_matrix(q: list[float]) -> list[list[float]]:
    x, y, z, w = q
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _node_local_matrix(node: dict[str, Any]) -> list[float]:
    """glTF node → column-major 4×4 local matrix."""
    if "matrix" in node:
        return [float(v) for v in node["matrix"]]
    t = node.get("translation", [0.0, 0.0, 0.0])
    r = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    s = node.get("scale", [1.0, 1.0, 1.0])
    rm = _quat_to_matrix([float(v) for v in r])
    m = [0.0] * 16
    for col in range(3):
        for row in range(3):
            m[col * 4 + row] = rm[row][col] * float(s[col])
    m[12], m[13], m[14], m[15] = float(t[0]), float(t[1]), float(t[2]), 1.0
    return m


def _mat_mul(a: list[float], b: list[float]) -> list[float]:
    """Column-major 4×4 multiply: result = a · b."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return out


_IDENTITY4 = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _world_transforms(gltf: dict[str, Any]) -> dict[str, list[float]]:
    """Node name → world (scene-root-relative) column-major 4×4."""
    nodes = gltf.get("nodes") or []
    scenes = gltf.get("scenes") or []
    roots = scenes[gltf.get("scene", 0)]["nodes"] if scenes else []
    out: dict[str, list[float]] = {}

    def walk(idx: int, parent: list[float]) -> None:
        node = nodes[idx]
        world = _mat_mul(parent, _node_local_matrix(node))
        name = node.get("name")
        if name:
            out[name] = world
        for child in node.get("children", []) or []:
            walk(child, world)

    for r in roots:
        walk(r, _IDENTITY4)
    return out


def _group_children(gltf: dict[str, Any], group_name: str) -> list[str]:
    nodes = gltf.get("nodes") or []
    for node in nodes:
        if node.get("name") == group_name:
            return [nodes[c].get("name") or f"node_{c}" for c in node.get("children", []) or []]
    return []


def _read_glb_json(path: Path) -> dict[str, Any]:
    gltf, _bin = parse_glb(path.read_bytes())
    return gltf


# ── mount placement (the toolkit's ship-mount rule) ───────────────────


def _rotate_y_bone(gltf: dict[str, Any]) -> list[float] | None:
    """Local matrix of the GLB's ``Rotate_Y_BlendBone`` node, or None."""
    for node in gltf.get("nodes") or []:
        if node.get("name") == ROTATE_Y_BLEND_BONE:
            return _node_local_matrix(node)
    return None


def _rotation_inverse(m: list[float]) -> list[float]:
    """Inverse of the 3×3 rotation (its transpose), no translation — the
    toolkit's ``mat4_rotation_inverse``."""
    return [
        m[0], m[4], m[8], 0.0,
        m[1], m[5], m[9], 0.0,
        m[2], m[6], m[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _det3(m: list[float]) -> float:
    return (
        m[0] * (m[5] * m[10] - m[6] * m[9])
        - m[4] * (m[1] * m[10] - m[2] * m[9])
        + m[8] * (m[1] * m[6] - m[2] * m[5])
    )


def _mount_matrix(hp_world: list[float], bone_local: list[float] | None) -> list[float]:
    """World matrix to render a mount GLB at: ``hp_world x inverse(bone)``,
    made proper by negating column 0 when the result mirrors (a Z-mirror
    bone yields ``hp_world x Ry(180)``). The toolkit's ship-mount rule
    (``visual_transform = hp_transform * inverse(Rotate_Y_BlendBone)``
    then ``ensure_proper_rotation``), written in glTF space: the BW->glTF
    Z flip conjugates both factors, so the product converts the same way.
    A GLB without the bone renders at the raw node."""
    if bone_local is None:
        return [float(v) for v in hp_world]
    m = _mat_mul(hp_world, _rotation_inverse(bone_local))
    if _det3(m) < 0.0:
        m[0], m[1], m[2] = -m[0], -m[1], -m[2]
    return m


def _transform_entry(m: list[float]) -> dict[str, Any]:
    return {"matrix": m, "position": [m[12], m[13], m[14]]}


# ── textures ──────────────────────────────────────────────────────────


def _model_materials(
    glb_path: Path,
    mappings_json: Path,
    warn: Callable[[str], None],
    label: str,
) -> list[dict[str, Any]]:
    """Material manifest of one exported model GLB (ship ``materials[]``
    shape): texture slots bound from the toolkit's material mappings,
    paths relative to the GLB's directory or to the workspace root
    (``libraries/...`` store paths). ``dead_stems`` lets the materials of
    a dead model bind its ``<stem>_dead_*`` textures; live mappings never
    name a ``_dead`` stem, so it changes nothing for them."""
    dds_dir = glb_path.parent / "textures_dds"
    mats = resolve_sidecar.materials_from_glb(
        glb_path,
        textures_dds_dir=dds_dir if dds_dir.is_dir() else None,
        material_mappings_json=mappings_json if mappings_json.is_file() else None,
        legacy_name_fallback=False,
        dead_stems=True,
    )
    for m in mats:
        if m.get("shader_intent") in ("opaque_pbr", "cutout"):
            main = (m.get("texture_sets") or {}).get("main") or {}
            if "baseColor" not in main:
                warn(f"{label}: material {m.get('material_id')} has no baseColor texture")
    return mats


def _write_json_atomic(path: Path, doc: Any, *, indent: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(doc, f, indent=indent, sort_keys=True)
    tmp.replace(path)


def _write_gun_index(cfg: PipelineConfig, warnings: list[str]) -> Path | None:
    """(Re)build ``_shared/guns/index.json`` from every gun GLB on disk.

    One entry per stem: ``glb`` and ``material_mappings`` relative to the
    index, ``materials`` with texture paths relative to the GLB's own
    directory (``libraries/...`` paths: workspace root). The shared
    texture store rewrites the entries when it moves a set out of a gun
    folder, as it does for the accessory library index.
    """
    root = buildings_root(cfg) / SHARED_GUNS_SUBDIR
    if not root.is_dir():
        return None
    assets: dict[str, Any] = {}
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        stem = d.name
        glb = d / f"{stem}.glb"
        if not glb.is_file():
            continue
        mm = d / f"{stem}_material_mappings.json"
        entry = {
            "glb": f"{stem}/{glb.name}",
            "material_mappings": f"{stem}/{mm.name}" if mm.is_file() else None,
            "materials": _model_materials(glb, mm, warnings.append, f"gun {stem}"),
        }
        shared_textures.repair_refs(entry, d)
        assets[stem] = entry
    out = gun_index_path(cfg)
    _write_json_atomic(
        out,
        {
            "schema_version": GUN_INDEX_SCHEMA_VERSION,
            "generator": "wows-export-building",
            "asset_count": len(assets),
            "assets": assets,
        },
    )
    return out


def _flip_hull_winding(glb_path: Path) -> None:
    """Same fixup the ship pipeline applies to hull GLBs: the toolkit emits
    ``Armor_*`` / ``CM_SB_*`` meshes with inverted winding. Fresh exports
    only — not idempotent."""
    data = glb_path.read_bytes()
    gltf, bin_data = parse_glb(data)
    new_bin, _report = flip_winding(
        gltf,
        bin_data,
        mesh_filter=lambda m: (m.get("name") or "").startswith(_HULL_FLIP_PREFIXES),
    )
    write_glb(gltf, new_bin, glb_path)


# ── export core ───────────────────────────────────────────────────────


def _hull_glb_jobs(index: str, entry: dict[str, Any], cfg: PipelineConfig) -> list[dict[str, Any]]:
    """Batch items for the building's hull (+ distinct dead hull)."""
    hull = entry["hull"]
    bdir = building_dir(cfg, index)
    models = bdir / MODELS_SUBDIR
    jobs = [
        {
            "geometry": _geometry_vfs(hull["model"]),
            "output": models / f"{index}_hull.glb",
            "raw_dds_dir": models / "textures_dds",
            "material_mappings_json": models / f"{index}_material_mappings.json",
        }
    ]
    dead = hull.get("deadModel") or ""
    if dead and dead != hull["model"]:
        jobs.append(
            {
                "geometry": _geometry_vfs(dead),
                "output": models / f"{index}_hull_dead.glb",
                "raw_dds_dir": models / "textures_dds",
                "material_mappings_json": models / f"{index}_dead_material_mappings.json",
            }
        )
    return jobs


def _gun_glb_jobs(entry: dict[str, Any], cfg: PipelineConfig, seen: set[str]) -> list[dict[str, Any]]:
    """Batch items for every unique gun model (+ dead mesh) not yet queued."""
    jobs: list[dict[str, Any]] = []
    for _comp, _species, _hp, gun in _gun_mounts(entry):
        for model_key in ("model", "deadMesh"):
            model_path = gun.get(model_key) or ""
            if not (isinstance(model_path, str) and model_path.endswith(".model")):
                continue
            stem = _stem(model_path)
            if stem in seen:
                continue
            seen.add(stem)
            gdir = shared_gun_dir(cfg, stem)
            jobs.append(
                {
                    "geometry": _geometry_vfs(model_path),
                    "output": gdir / f"{stem}.glb",
                    "raw_dds_dir": gdir / "textures_dds",
                    "material_mappings_json": gdir / f"{stem}_material_mappings.json",
                }
            )
    return jobs


def _compose_sidecar(
    index: str,
    entry: dict[str, Any],
    cfg: PipelineConfig,
    gp: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    hull = entry["hull"]
    warn = warnings.append

    hull_glb = building_dir(cfg, index) / MODELS_SUBDIR / f"{index}_hull.glb"
    gltf = _read_glb_json(hull_glb)
    world = _world_transforms(gltf)
    box_names = _group_children(gltf, "Hitboxes")

    # hitbox — every box routes to the single "Hull" section; buildings have
    # no per-zone splashBoxes partition.
    boxes = {
        name: {"hl_type": "simple_hitlocation", "section": "Hull"}
        for name in box_names
    }
    if not boxes:
        warn(
            f"{index}: hull GLB has NO Hitboxes group (hit_locations collision "
            f"model missing/unparseable?) — building would be an unhittable target"
        )
    hull_hit_location: dict[str, Any] = {
        "hl_type": "simple_hitlocation",
        "max_hp": float(hull.get("health", 0.0)),
        "regen_part": 0.0,
    }
    hitbox = {
        "source_glb": hull_glb.name,
        "region_count": 1,
        "regions": {"Hull": {"box_count": len(boxes)}},
        "boxes": boxes,
        "hit_locations": {"Hull": hull_hit_location},
    }

    # Gun model bones for the mount rule, read once per stem from the
    # exported gun GLBs. A stem whose GLB is missing maps to False.
    bones: dict[str, list[float] | None | bool] = {}

    def gun_bone(stem: str) -> list[float] | None | bool:
        if stem not in bones:
            glb = shared_gun_dir(cfg, stem) / f"{stem}.glb"
            bones[stem] = _rotate_y_bone(_read_glb_json(glb)) if glb.is_file() else False
        return bones[stem]

    # turrets — ship-turret-shaped entries for every gun mount.
    artillery = entry.get("artillery") if isinstance(entry.get("artillery"), dict) else {}
    max_dist = artillery.get("maxDist") if isinstance(artillery, dict) else None
    turrets: list[dict[str, Any]] = []
    ammo_ids_all: list[str] = []
    for i, (comp_name, species, hp_name, gun) in enumerate(_gun_mounts(entry)):
        stem = _stem(gun["model"])
        dead_mesh = gun.get("deadMesh") or ""
        dead_stem = _stem(dead_mesh) if dead_mesh.endswith(".model") else None
        dispersion: dict[str, Any] = {}
        comp = entry.get(comp_name) or {}
        for k in _DISPERSION_COMPONENT_KEYS:
            if isinstance(comp, dict) and comp.get(k) is not None:
                dispersion[k] = comp[k]
        for k in _DISPERSION_MOUNT_KEYS:
            if gun.get(k) is not None:
                dispersion[k] = gun[k]

        ammo_ids = [a for a in (gun.get("ammoList") or []) if isinstance(a, str)]
        ammo_ids_all.extend(ammo_ids)
        ammo_types = []
        for a in ammo_ids:
            proj = gp.get(a)
            if isinstance(proj, dict) and proj.get("ammoType"):
                ammo_types.append(proj["ammoType"])

        transform = dead_transform = hp_transform = None
        if hp_name in world:
            hp_m = world[hp_name]
            hp_transform = _transform_entry(hp_m)
            bone = gun_bone(stem)
            if bone is False:
                warn(f"{index}: gun GLB {stem} missing — {hp_name} transform left at the raw node")
                bone = None
            transform = _transform_entry(_mount_matrix(hp_m, bone))
            if dead_stem:
                dead_bone = gun_bone(dead_stem)
                if dead_bone is False:
                    warn(f"{index}: dead gun GLB {dead_stem} missing — {hp_name} dead transform left at the raw node")
                    dead_bone = None
                dead_transform = _transform_entry(_mount_matrix(hp_m, dead_bone))
        else:
            warn(f"{index}: hardpoint {hp_name} not found in hull GLB node tree")

        rot = gun.get("rotationSpeed") or [None, None]
        turret: dict[str, Any] = {
            "instance_id": f"{index}_{stem}_{i:02d}",
            "asset_id": stem,
            "dead_asset_id": dead_stem,
            "hp_name": hp_name,
            "parent_section": "Hull",
            "scope": _scope_of(gun["model"]),
            "category": "gun",
            "subcategory": species,
            "species": species,
            "component": comp_name,
            # Live GLB placement (ship-mount rule), the dead GLB's own
            # placement, and the raw HP node (module docstring).
            "transform": transform,
            "dead_transform": dead_transform,
            "hp_transform": hp_transform,
            "display_name": stem.replace("_", " "),
            "caliber_mm": (float(gun["barrelDiameter"]) * 1000.0) if gun.get("barrelDiameter") else None,
            # WG stores numBarrels as a float (4.0); Unity Placement.barrel_count is int.
            "barrel_count": int(gun["numBarrels"]) if gun.get("numBarrels") is not None else None,
            "ammo_ids": ammo_ids,
            "ammo_types": ammo_types,
            "dispersion": dispersion,
            "yaw_range_deg": _float_pair(gun.get("horizSector")),
            "elev_range_deg": _float_pair(gun.get("vertSector")),
            "traverse_rate": rot[0] if isinstance(rot, (list, tuple)) and rot else None,
            "elev_rate": rot[1] if isinstance(rot, (list, tuple)) and len(rot) > 1 else None,
            "reload_s": gun.get("shotDelay"),
            # Consumer field is Placement.pitch_dead_zones_deg (float pairs,
            # stored verbatim by TurretControllerBuilder.ToPitchZones).
            "pitch_dead_zones_deg": gun.get("pitchDeadZones") or None,
            "shot_effect": gun.get("shotEffect"),
        }
        hl = gun.get("HitLocationArtillery") or gun.get("HitLocationAirDefense")
        if isinstance(hl, dict):
            turret["hit_location"] = _hit_location_meta(hl)
        turrets.append(turret)

    # ballistics — shells formatted straight from GameParams Projectile
    # entries (the toolkit `ammo` command is Vehicle-only).
    shells: dict[str, Any] = {}
    for ammo_id in sorted(set(ammo_ids_all)):
        proj = gp.get(ammo_id)
        if isinstance(proj, dict) and proj.get("typeinfo", {}).get("type") == "Projectile":
            shells[ammo_id] = _shell_from_projectile(proj, max_dist)
        else:
            warn(f"{index}: ammo {ammo_id} not found in GameParams — shell skipped")
    # NOTE: `source` must be a dict — the Unity SidecarSchema types
    # BallisticsInfo.source as Dictionary<string,string> (ship shape).
    # `main_battery_m` mirrors artillery.maxDist so the ship GunneryBuilder's
    # per-battery range cap applies to building artillery unchanged.
    ballistics = {
        "source": {"shells": "gameparams"},
        "ranges": (
            {"artillery_max_m": float(max_dist), "main_battery_m": float(max_dist)}
            if max_dist is not None
            else {}
        ),
        "shells": shells,
        "torpedoes": {},
    }

    dead = hull.get("deadModel") or ""
    has_distinct_dead = bool(dead) and dead != hull["model"]
    models = building_dir(cfg, index) / MODELS_SUBDIR
    dead_glb = models / f"{index}_hull_dead.glb"
    if has_distinct_dead and not dead_glb.is_file():
        warn(f"{index}: dead model {_stem(dead)} GLB missing — dead swap disabled")
        has_distinct_dead = False

    # materials — texture paths relative to models/ (or the workspace
    # root for store paths), as in ship sidecars.
    materials = _model_materials(
        hull_glb, models / f"{index}_material_mappings.json", warn, f"{index} hull"
    )
    dead_materials = (
        _model_materials(
            dead_glb, models / f"{index}_dead_material_mappings.json", warn, f"{index} dead hull"
        )
        if has_distinct_dead
        else None
    )

    return {
        "schema_version": BUILDING_SCHEMA_VERSION,
        "pipeline": {"kind": "building", "generator": "wows-export-building"},
        "building": {
            "index": index,
            "id": entry.get("id"),
            "name": entry.get("name"),
            "species": entry.get("typeinfo", {}).get("species"),
            "level": entry.get("level"),
            "health": float(hull.get("health", 0.0)),
            "can_be_suppressed": bool(hull.get("canBeSuppressed", False)),
            "permanently_visible": bool(hull.get("permanentlyVisibleByEnemies", False)),
            "vision_distance": hull.get("visionDistance"),
            "model": hull.get("model"),
            "dead_model": dead or None,
            "dead_hull_glb": f"{index}_hull_dead.glb" if has_distinct_dead else None,
            "death_model_replacement_delay_s": hull.get("deathModelReplacementDelay"),
        },
        "armor": {
            "source": "gameparams hull.armor (keys folded to low-16 material id)",
            "materials_table": _fold_armor_table(hull.get("armor") or {}, warn),
        },
        "hitbox": hitbox,
        "turrets": turrets,
        "ballistics": ballistics,
        "materials": materials,
        "dead_materials": dead_materials,
        "provenance": {
            "generator": "wows-export-building",
            "exported_at_unix": int(time.time()),
        },
    }


# ── public entry point ────────────────────────────────────────────────


def _buildings_session_roots(
    *_args: object,
    config: PipelineConfig | None = None,
    **_kwargs: object,
) -> tuple[Path, list[Path]]:
    """Export session of a building export: it claims ``buildings/``, so no
    other export's texture adoption moves a copy the sidecars being composed
    still name (see :func:`shared_textures.export_session`)."""
    cfg = config or PipelineConfig.load()
    return cfg.workspace, [buildings_root(cfg)]


@shared_textures.with_export_session(_buildings_session_roots)
def run_export(
    *,
    indices: Iterable[str] | None = None,
    species: str | None = None,
    all_buildings: bool = False,
    skip_glb: bool = False,
    config: PipelineConfig | None = None,
    on_event: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Export the selected Building entries. Returns the run report dict.

    Selection: explicit ``indices``, or every entry of a ``species``
    (e.g. ``CoastalArtillery``), or ``all_buildings``. ``skip_glb`` reuses
    GLBs already on disk and only recomposes sidecars (fast iteration).
    """
    cfg = config or PipelineConfig.load()
    on_event("loading GameParams (full dump — this takes a minute cold)…")
    gp = gameparams.load_full(config=cfg)

    selected: list[tuple[str, dict[str, Any]]] = []
    want = set(indices or [])
    for key, entry in iter_building_entries(gp):
        if all_buildings or key in want or (species and entry.get("typeinfo", {}).get("species") == species):
            selected.append((key, entry))
    missing = want - {k for k, _ in selected}
    if missing:
        raise ValueError(f"unknown building indices: {sorted(missing)}")
    if not selected:
        raise ValueError("no buildings selected (use indices=…, species=…, or all_buildings=True)")
    selected.sort()

    report: dict[str, Any] = {"buildings": {}, "gun_models": [], "skipped": {}, "warnings": []}

    # Guard hull-less entries UP FRONT: `--all` sweeps every typeinfo.type ==
    # "Building" and some (logic/aggregate entries) carry no usable hull dict.
    # A KeyError in Phase-1 job assembly would abort the whole run before any
    # export; skip-with-report instead, like the new-format .visual skip.
    viable: list[tuple[str, dict[str, Any]]] = []
    for index, entry in selected:
        hull = entry.get("hull")
        model = hull.get("model") if isinstance(hull, dict) else None
        if isinstance(model, str) and model.endswith(".model"):
            viable.append((index, entry))
        else:
            reason = "no usable hull.model in GameParams entry"
            report["skipped"][index] = reason
            on_event(f"  {index}: SKIPPED — {reason}")
    selected = viable
    on_event(f"selected {len(selected)} building(s)")

    # Phase 1 — GLB exports, two batches (flags differ):
    #   hulls: --emit-hardpoints + --collision-hitbox-groups
    #   guns:  --emit-hardpoints only (skinned guns already carry their rig;
    #          collision groups would mislabel gun hit models as hull armor)
    #
    # Per-item batch failures don't abort (keep_going): ~half the building
    # corpus ships in the NEW merged .visual container (no loose .geometry
    # in the VFS — count + 5 tail relptrs, neither models.bin nor .geometry
    # layout) which export-model can't read yet. Those buildings are skipped
    # with a report entry; toolkit support is a tracked follow-up.
    if not skip_glb:
        hull_jobs: list[dict[str, Any]] = []
        gun_jobs: list[dict[str, Any]] = []
        seen_guns: set[str] = set()
        for index, entry in selected:
            hull_jobs += _hull_glb_jobs(index, entry, cfg)
            gun_jobs += _gun_glb_jobs(entry, cfg, seen_guns)

        on_event(f"exporting {len(hull_jobs)} hull GLB(s) …")
        batch_export_model(
            hull_jobs,
            shared={"no_textures": True, "emit_hardpoints": True, "collision_hitbox_groups": True},
            config=cfg,
        )
        for job in hull_jobs:
            out = Path(job["output"])
            if out.is_file():
                _flip_hull_winding(out)

        if gun_jobs:
            on_event(f"exporting {len(gun_jobs)} gun GLB(s) …")
            # all_render_sets, matching the ship accessory library: the
            # collapsed single-LOD path skips skin emit on divergent bone
            # palettes, leaving JOINTS_0 without a skin — which the gltFast
            # editor importer rejects (SortAndNormalizeBoneWeightsJob safety
            # error). Per-render-set meshes each carry their own skin.
            batch_export_model(
                gun_jobs,
                shared={"no_textures": True, "emit_hardpoints": True, "all_render_sets": True},
                config=cfg,
            )
            for job in gun_jobs:
                out = Path(job["output"])
                if not out.is_file():
                    report["warnings"].append(
                        f"gun GLB missing after batch export (new-format model?): {out.name}"
                    )
        report["gun_models"] = sorted(seen_guns)

    # Phase 2 — sidecars (only for buildings whose hull GLB landed).
    for index, entry in selected:
        hull_glb = building_dir(cfg, index) / MODELS_SUBDIR / f"{index}_hull.glb"
        if not hull_glb.is_file():
            reason = "hull GLB missing — likely new-format merged .visual (no loose .geometry in VFS)"
            report["skipped"][index] = reason
            on_event(f"  {index}: SKIPPED — {reason}")
            continue
        warnings: list[str] = []
        sidecar = _compose_sidecar(index, entry, cfg, gp, warnings)
        # Safety net: repoint any texture path whose folder copy moved to
        # the shared store after the materials were resolved.
        shared_textures.repair_refs(sidecar, building_dir(cfg, index) / MODELS_SUBDIR)
        out = sidecar_path_for(cfg, index)
        _write_json_atomic(out, sidecar)
        n_mats = len(sidecar["materials"]) + len(sidecar["dead_materials"] or [])
        report["buildings"][index] = {
            "sidecar": str(out),
            "turrets": len(sidecar["turrets"]),
            "boxes": len(sidecar["hitbox"]["boxes"]),
            "shells": len(sidecar["ballistics"]["shells"]),
            "materials": n_mats,
            "warnings": warnings,
        }
        report["warnings"] += [f"{index}: {w}" if not w.startswith(index) else w for w in warnings]
        on_event(
            f"  {index}: sidecar ok — {len(sidecar['turrets'])} mounts, "
            f"{len(sidecar['hitbox']['boxes'])} hit boxes, {len(sidecar['ballistics']['shells'])} shells, "
            f"{n_mats} materials"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )

    # Phase 3 — the gun library index, rebuilt from every gun GLB on disk.
    gun_warnings: list[str] = []
    gun_index = _write_gun_index(cfg, gun_warnings)
    report["warnings"] += gun_warnings
    if gun_index is not None:
        report["gun_index"] = str(gun_index)
        on_event(f"gun index: {gun_index}" + (f" ({len(gun_warnings)} warning(s))" if gun_warnings else ""))

    report_path = buildings_root(cfg) / "export.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    on_event(f"report: {report_path}")
    return report
