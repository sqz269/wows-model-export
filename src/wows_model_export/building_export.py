"""Building (shore-structure) export — GameParams ``Building`` entries.

WoWS buildings are stripped ships: a ``hull`` dict (model/deadModel, health,
``armor {materialId: mm}``, canBeSuppressed, visibility) plus optional gun
components (``artillery`` / ``airDefense``) whose ``HP_*`` mounts are full
GameParams ``Gun`` entries — the same schema as ship guns. Their hit geometry
ships inside the model ``.geometry`` as the ``hit_locations`` collision model
(no armor BVH, no ``.splash``); the toolkit's ``--collision-hitbox-groups``
synthesizes ship-convention ``Armor`` / ``Hitboxes`` GLB groups from it, and
``--emit-hardpoints`` keeps the static ``HP_*`` mount nodes in the node tree.

Output mirrors the ship layout so Unity-side consumers reuse the ship bakers:

    <workspace>/buildings/<index>/
      <index>.meta.json            <- building sidecar (ship-shaped sections)
      export.json                  <- run report
      models/<index>_hull.glb      <- hull (+ Armor/Hitboxes groups, HP_* nodes)
      models/<index>_hull_dead.glb <- deadModel (only when distinct from live)
      models/<index>_material_mappings.json
      models/textures_dds/         <- raw DDS for the hull
    <workspace>/buildings/_shared/guns/<stem>/
      <stem>.glb  (+ <stem>_dead.glb, material mappings, textures_dds/)
                                   <- gun mounts, shared across buildings

The sidecar's ``turrets`` / ``hitbox`` / ``armor`` / ``ballistics`` sections
use the same shapes as ship ``.meta.json`` files (see
``resolve/sidecar/_makers.py``), with building-specific identity under
``building``. Armor thickness comes from GameParams ``hull.armor`` — keys are
``(layer << 16) | material_id``; the GLB's per-vertex ``_MATERIAL_ID`` is the
low 16 bits, so the ``materials_table`` here is keyed by the same fold.
"""

from __future__ import annotations

import json
import math
import struct
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ._glb import flip_winding, parse_glb, write_glb
from .config import PipelineConfig
from .read import gameparams
from .resolve.gameparams_autofill import _hit_location_meta
from .toolkit.ship import batch_export_model

MODELS_SUBDIR = "models"
SHARED_GUNS_SUBDIR = "_shared/guns"

# Sidecar schema version for building meta.json files. Independent of the
# ship sidecar's schema_version — consumers key off ``pipeline.kind``.
BUILDING_SCHEMA_VERSION = 1

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

    # turrets — ship-turret-shaped entries for every gun mount.
    artillery = entry.get("artillery") if isinstance(entry.get("artillery"), dict) else {}
    max_dist = artillery.get("maxDist") if isinstance(artillery, dict) else None
    turrets: list[dict[str, Any]] = []
    ammo_ids_all: list[str] = []
    for i, (comp_name, species, hp_name, gun) in enumerate(_gun_mounts(entry)):
        stem = _stem(gun["model"])
        dead_mesh = gun.get("deadMesh") or ""
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

        transform = None
        if hp_name in world:
            m = world[hp_name]
            transform = {"matrix": m, "position": [m[12], m[13], m[14]]}
        else:
            warn(f"{index}: hardpoint {hp_name} not found in hull GLB node tree")

        rot = gun.get("rotationSpeed") or [None, None]
        turret: dict[str, Any] = {
            "instance_id": f"{index}_{stem}_{i:02d}",
            "asset_id": stem,
            "dead_asset_id": _stem(dead_mesh) if dead_mesh.endswith(".model") else None,
            "hp_name": hp_name,
            "parent_section": "Hull",
            "scope": _scope_of(gun["model"]),
            "category": "gun",
            "subcategory": species,
            "species": species,
            "component": comp_name,
            "transform": transform,
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
    if has_distinct_dead:
        dead_glb = building_dir(cfg, index) / MODELS_SUBDIR / f"{index}_hull_dead.glb"
        if not dead_glb.is_file():
            warn(f"{index}: dead model {_stem(dead)} GLB missing — dead swap disabled")
            has_distinct_dead = False

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
        "provenance": {
            "generator": "wows-export-building",
            "exported_at_unix": int(time.time()),
        },
    }


# ── public entry point ────────────────────────────────────────────────


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
        out = sidecar_path_for(cfg, index)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=1, sort_keys=True)
        report["buildings"][index] = {
            "sidecar": str(out),
            "turrets": len(sidecar["turrets"]),
            "boxes": len(sidecar["hitbox"]["boxes"]),
            "shells": len(sidecar["ballistics"]["shells"]),
            "warnings": warnings,
        }
        report["warnings"] += [f"{index}: {w}" if not w.startswith(index) else w for w in warnings]
        on_event(
            f"  {index}: sidecar ok — {len(sidecar['turrets'])} mounts, "
            f"{len(sidecar['hitbox']['boxes'])} hit boxes, {len(sidecar['ballistics']['shells'])} shells"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )

    report_path = buildings_root(cfg) / "export.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    on_event(f"report: {report_path}")
    return report
