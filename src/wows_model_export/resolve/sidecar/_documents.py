"""Top-level document factories + hull-GLB walkers.

- :func:`new_document` / :func:`new_document_from_placements` —
  build a fresh ``<Ship>.meta.json`` document.
- :func:`sidecar_path_for` / :func:`build_ship_key` — filesystem +
  identity helpers.
- :func:`ship_from_placements` — extract a sidecar ``ship`` section
  from the toolkit's placements JSON.
- :func:`geometry_from_hull_glb` / :func:`hitbox_from_hull_glb` /
  :func:`geometry_and_hitbox_from_hull_glb` — walk the toolkit-emitted
  hull GLB to populate the ``geometry`` and ``hitbox`` sections.

These call into :mod:`._makers` for shape and :mod:`._helpers` for
normalisation; they don't synthesise schema themselves.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ._absorb import absorb_placements_json
from ._constants import (
    SCHEMA_VERSION,
    SIDECAR_SUFFIX,
    SidecarSchemaError,
)
from ._helpers import normalise_hitbox_token
from ._makers import (
    make_armor,
    make_ballistics,
    make_default_skin,
    make_geometry,
    make_hitbox,
    make_pipeline,
    make_ship,
)

# Root document
# ---------------------------------------------------------------------------

def new_document(
    *,
    pipeline: dict[str, Any],
    ship: dict[str, Any],
    geometry: dict[str, Any] | None = None,
    armor: dict[str, Any] | None = None,
    hitbox: dict[str, Any] | None = None,
    turrets: Iterable[dict[str, Any]] | None = None,
    secondaries: Iterable[dict[str, Any]] | None = None,
    antiair: Iterable[dict[str, Any]] | None = None,
    torpedoes: Iterable[dict[str, Any]] | None = None,
    accessories: Iterable[dict[str, Any]] | None = None,
    ballistics: dict[str, Any] | None = None,
    materials: Iterable[dict[str, Any]] | None = None,
    skins: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a complete v2 sidecar document.

    Sections not passed get minimal well-formed placeholders so the result
    always satisfies the root schema. ``skins`` defaults to ``[default]``;
    ``ballistics`` defaults to an empty section (no shells, no ranges).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline": pipeline,
        "ship": ship,
        "geometry": geometry if geometry is not None else make_geometry(),
        "armor": armor if armor is not None else make_armor(),
        "hitbox": hitbox if hitbox is not None else make_hitbox(),
        "turrets": list(turrets or []),
        "secondaries": list(secondaries or []),
        "antiair": list(antiair or []),
        "torpedoes": list(torpedoes or []),
        "accessories": list(accessories or []),
        "ballistics": ballistics if ballistics is not None else make_ballistics(),
        "materials": list(materials or []),
        "skins": list(skins) if skins is not None else [make_default_skin()],
    }



# ---------------------------------------------------------------------------

def sidecar_path_for(ship_dir: str | Path, ship_name: str) -> Path:
    """Canonical sidecar path: ``<ship_dir>/<ship_name>.meta.json``."""
    return Path(ship_dir) / (ship_name + SIDECAR_SUFFIX)


def build_ship_key(
    nation: str | None,
    cls: str | None,
    ship_name: str,
    suffix: str | None = None,
) -> str:
    """Construct the canonical ``ship_key``.

    Format: ``{NATION}_{ClassLong}_{ShipName}[_{suffix}]``.
    """
    class_long = {
        "CA": "Cruiser", "CL": "Cruiser", "BC": "Cruiser", "CB": "Cruiser",
        "BB": "Battleship",
        "DD": "Destroyer",
        "CV": "Carrier",
        "SS": "Submarine",
        "AUX": "Auxiliary",
    }.get((cls or "").strip().upper(), (cls or "Ship").capitalize())
    nation_u = (nation or "ship").upper()
    parts = [nation_u, class_long, ship_name]
    if suffix:
        parts.append(suffix)
    return "_".join(parts)


# Mapping from toolkit `Species` strings (from `placements.ship.species`) to
# our 2-letter class codes used in `ship.class` and `build_ship_key`.
# If the toolkit emits a species we don't recognize, fall through to the
# capitalized species string (so "Submarine" → "SS", anything novel shows up
# uppercased for manual triage).
_SPECIES_TO_CLASS = {
    "Destroyer":   "DD",
    "Cruiser":     "CA",
    "Battleship":  "BB",
    "AirCarrier":  "CV",
    "Submarine":   "SS",
}


def ship_from_placements(
    placements: str | Path | dict[str, Any],
    *,
    class_override: str | None = None,
    auto_derived_class: str | None = None,
    ship_key_suffix: str | None = None,
) -> dict[str, Any]:
    """Build a sidecar ``ship`` section from a toolkit placements JSON.

    Resolves the toolkit-emitted ``ship`` fields (``model_dir``,
    ``display_name``, ``param_index``, ``nation``, ``species``, ``tier``)
    into the sidecar's canonical shape — lowercased nation + asset_id,
    2-letter class code, ship_key built from the three. Returns a dict
    suitable for passing into :func:`new_document` as ``ship=…``.

    ``placements`` — path to the JSON file or the parsed dict.

    ``class_override`` — explicit 2-letter code that wins over everything
    else. Use this only when the user has hand-typed the right class
    (e.g. ``--class-override BC`` for a battlecruiser that GameParams
    can't disambiguate).

    ``auto_derived_class`` — 2-letter code derived from authoritative
    data (typically caliber-based via
    :func:`tools.shared.gameparams.class_from_caliber`). Wins over the
    species-only mapping, loses to ``class_override``. Pass-through here
    keeps the stdlib-only sidecar invariant — sidecar doesn't import
    gameparams; the caller (scaffold_ship) does.

    Class-resolution precedence: ``class_override`` > ``auto_derived_class``
    > :data:`_SPECIES_TO_CLASS` species lookup > ``species[:2].upper()``.

    ``ship_key_suffix`` — optional trailing segment in the canonical
    ship_key, for ship-variants like ``"Scharnhorst_B"`` that share the
    base name but differ in hull upgrade.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, encoding="utf-8") as f:
            placements = json.load(f)
    if not isinstance(placements, dict):
        raise TypeError(f"ship_from_placements: expected dict or path, got {type(placements).__name__}")
    ship_section = placements.get("ship")
    if not isinstance(ship_section, dict):
        raise SidecarSchemaError("placements JSON missing 'ship' section")

    model_dir    = ship_section.get("model_dir", "")
    display_name = ship_section.get("display_name") or model_dir
    param_index  = ship_section.get("param_index") or None
    nation_raw   = ship_section.get("nation", "") or ""
    species      = ship_section.get("species", "") or ""
    tier         = ship_section.get("tier", 0) or 0

    nation = nation_raw.lower()
    cls = (
        class_override
        or auto_derived_class
        or _SPECIES_TO_CLASS.get(species, (species[:2].upper() if species else None))
    )
    wg_asset_id = model_dir.lower() if model_dir else None
    # display_name is preserved as WG emitted it (may carry the toolkit's
    # disambiguation parenthetical, e.g. "Baltimore (old)" / "U-2501 (old)").
    # ship_key, however, ends up in filesystem paths, consumer asset
    # IDs, and URL fragments — strip the trailing parenthetical +
    # collapse spaces before deriving it. Any explicit `--ship-key-suffix` is appended in
    # `build_ship_key` after this sanitization.
    ship_key_name = _sanitize_for_ship_key(display_name)
    ship_key = build_ship_key(nation or None, cls, ship_key_name, suffix=ship_key_suffix)

    return make_ship(
        ship_key     = ship_key,
        display_name = display_name,
        wg_asset_id  = wg_asset_id,
        wg_ship_id   = param_index,
        nation       = nation or None,
        cls          = cls,
        tier         = int(tier) if tier else None,
    )


_SHIP_KEY_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _sanitize_for_ship_key(name: str) -> str:
    """Strip trailing parentheticals + collapse whitespace so the derived
    ``ship_key`` stays filesystem- and URL-safe.

    Toolkit emits display_names like ``"Baltimore (old)"`` or
    ``"U-2501 (old)"`` when its fuzzy ship-name resolver disambiguates
    multiple candidates. Embedding the parenthetical into ship_key bleeds
    spaces + parens into filesystem paths and consumer asset IDs.
    Display name itself stays as WG emitted it (UIs need the original);
    only the derived key is sanitized.

    Examples:
        "Montana"            -> "Montana"
        "Baltimore (old)"    -> "Baltimore"
        "U-2501 (old)"       -> "U-2501"
        "spaced  name"       -> "spaced_name"
    """
    cleaned = _SHIP_KEY_TRAILING_PAREN.sub("", name).strip()
    return "_".join(cleaned.split()) if cleaned else name

def _load_glb_json_chunk(glb_path: str | Path) -> dict[str, Any]:
    """Read just the JSON chunk of a GLB and return the parsed dict.

    Factored out so :func:`geometry_from_hull_glb` and
    :func:`hitbox_from_hull_glb` can share a single file read when
    callers go through :func:`geometry_and_hitbox_from_hull_glb`.
    """
    import struct
    p = Path(glb_path)
    with open(p, "rb") as f:
        header = f.read(12)
        if len(header) < 12:
            raise ValueError(f"{p}: file too short to be a GLB")
        magic, _ver, _total = struct.unpack("<4sII", header)
        if magic != b"glTF":
            raise ValueError(f"{p}: not a valid GLB (magic={magic!r})")
        chunk_len, chunk_type = struct.unpack("<I4s", f.read(8))
        if chunk_type != b"JSON":
            raise ValueError(f"{p}: expected JSON chunk, got {chunk_type!r}")
        return json.loads(f.read(chunk_len))


def geometry_from_hull_glb(
    glb_path: str | Path,
    *,
    group_name: str = "Hull",
    native_scale_m: float = 1.0,
    waterline_y: float = 0.0,
    keel_y: float | None = None,
    _gltf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute ``geometry.bounds`` (and optionally ``draft_m`` / ``keel_y``)
    by walking the hull GLB and aggregating the AABB of the named
    top-level group.

    The toolkit now emits hull vertices pre-scaled to metres, so
    ``native_scale_m`` defaults to ``1.0`` (pass-through). Set to ``15.0``
    only when reading a pre-bake GLB (pre-2026-04-23 exports) that still
    has native WoWS units (1 u ≈ 15 m). Axis mapping assumes glTF's Y-up
    convention: ``height_m`` is the Y-extent, ``length_m`` is the max of
    the other two axes, ``beam_m`` is the min.

    Only meshes under the ``group_name`` subtree count (defaults to
    ``"Hull"``), so hitbox cubes + armor meshes don't inflate the bounds.
    Node transforms (either ``matrix`` or ``translation``/``rotation``/
    ``scale``) are accumulated; each primitive's accessor ``min``/``max``
    defines a local AABB, and the 8 corners are transformed into scene
    space for a loose-but-correct world AABB.

    Args:
      glb_path:        Path to the hull GLB.
      group_name:      Top-level node to aggregate under. Defaults to ``"Hull"``.
      native_scale_m:  Multiplier applied to raw GLB extents. Defaults to
                       1.0 (new metric GLBs). Use 15.0 for legacy native GLBs.
      waterline_y:     Passed through into ``geometry.hull.waterline_y``.
      keel_y:          If ``None``, computed as the AABB min-Y (metric).
      _gltf:           Pre-parsed glTF dict (private). Passed by
                       :func:`geometry_and_hitbox_from_hull_glb` to avoid
                       re-reading the GLB header + JSON chunk twice.

    Returns:
      A full ``geometry`` section dict with ``bounds`` populated. If the
      GLB has no matching group or mesh data, returns an all-zeros
      geometry (same as :func:`make_geometry` with defaults).
    """
    import math

    gltf = _gltf if _gltf is not None else _load_glb_json_chunk(glb_path)

    nodes = gltf.get("nodes", [])
    accessors = gltf.get("accessors", [])
    meshes = gltf.get("meshes", [])
    scenes = gltf.get("scenes", [])
    if not nodes or not scenes:
        return make_geometry(waterline_y=waterline_y)
    root_nodes = scenes[gltf.get("scene", 0)].get("nodes", [])
    group_idx = next(
        (i for i in root_nodes if nodes[i].get("name") == group_name),
        None,
    )
    if group_idx is None:
        return make_geometry(waterline_y=waterline_y)

    _IDENT = [1.0, 0.0, 0.0, 0.0,
              0.0, 1.0, 0.0, 0.0,
              0.0, 0.0, 1.0, 0.0,
              0.0, 0.0, 0.0, 1.0]

    def _trs_to_matrix(t: list[float], r: list[float], s: list[float]) -> list[float]:
        # glTF: column-major, quaternion (qx,qy,qz,qw), M = T * R * S.
        qx, qy, qz, qw = r
        sx, sy, sz = s
        tx, ty, tz = t
        m = [0.0] * 16
        m[0]  = (1 - 2*(qy*qy + qz*qz)) * sx
        m[1]  = 2*(qx*qy + qz*qw) * sx
        m[2]  = 2*(qx*qz - qy*qw) * sx
        m[4]  = 2*(qx*qy - qz*qw) * sy
        m[5]  = (1 - 2*(qx*qx + qz*qz)) * sy
        m[6]  = 2*(qy*qz + qx*qw) * sy
        m[8]  = 2*(qx*qz + qy*qw) * sz
        m[9]  = 2*(qy*qz - qx*qw) * sz
        m[10] = (1 - 2*(qx*qx + qy*qy)) * sz
        m[12] = tx
        m[13] = ty
        m[14] = tz
        m[15] = 1.0
        return m

    def _node_local_matrix(n: dict) -> list[float]:
        if "matrix" in n:
            return list(n["matrix"])
        if any(k in n for k in ("translation", "rotation", "scale")):
            return _trs_to_matrix(
                list(n.get("translation", [0.0, 0.0, 0.0])),
                list(n.get("rotation",    [0.0, 0.0, 0.0, 1.0])),
                list(n.get("scale",       [1.0, 1.0, 1.0])),
            )
        return _IDENT

    def _mat_mul(a: list[float], b: list[float]) -> list[float]:
        out = [0.0] * 16
        for col in range(4):
            for row in range(4):
                s = 0.0
                for k in range(4):
                    s += a[k*4 + row] * b[col*4 + k]
                out[col*4 + row] = s
        return out

    def _transform_point(m: list[float], x: float, y: float, z: float) -> tuple:
        return (
            m[0]*x + m[4]*y + m[8]*z  + m[12],
            m[1]*x + m[5]*y + m[9]*z  + m[13],
            m[2]*x + m[6]*y + m[10]*z + m[14],
        )

    aabb_min = [math.inf]  * 3
    aabb_max = [-math.inf] * 3

    def _walk(node_idx: int, world: list[float]) -> None:
        n = nodes[node_idx]
        local = _node_local_matrix(n)
        world_here = _mat_mul(world, local) if local is not _IDENT else world
        mi = n.get("mesh")
        if mi is not None and 0 <= mi < len(meshes):
            for prim in meshes[mi].get("primitives", []):
                pos_idx = prim.get("attributes", {}).get("POSITION")
                if pos_idx is None:
                    continue
                acc = accessors[pos_idx]
                lo = acc.get("min")
                hi = acc.get("max")
                if not lo or not hi or len(lo) < 3 or len(hi) < 3:
                    continue
                for cx in (lo[0], hi[0]):
                    for cy in (lo[1], hi[1]):
                        for cz in (lo[2], hi[2]):
                            x, y, z = _transform_point(world_here, cx, cy, cz)
                            if x < aabb_min[0]:
                                aabb_min[0] = x
                            if y < aabb_min[1]:
                                aabb_min[1] = y
                            if z < aabb_min[2]:
                                aabb_min[2] = z
                            if x > aabb_max[0]:
                                aabb_max[0] = x
                            if y > aabb_max[1]:
                                aabb_max[1] = y
                            if z > aabb_max[2]:
                                aabb_max[2] = z
        for c in n.get("children", []):
            _walk(c, world_here)

    _walk(group_idx, _IDENT)

    if aabb_min[0] == math.inf:
        return make_geometry(waterline_y=waterline_y)

    extent = [(aabb_max[i] - aabb_min[i]) * native_scale_m for i in range(3)]
    # Y-up → height is Y; pick length = longer of remaining two axes.
    height_m = extent[1]
    length_m = max(extent[0], extent[2])
    beam_m   = min(extent[0], extent[2])
    keel = (aabb_min[1] * native_scale_m) if keel_y is None else keel_y
    waterline = waterline_y
    draft_m = max(0.0, waterline - keel)

    return make_geometry(
        length_m     = length_m,
        beam_m       = beam_m,
        height_m     = height_m,
        waterline_y  = waterline,
        keel_y       = keel,
        draft_m      = draft_m,
    )


def hitbox_from_hull_glb(
    glb_path: str | Path,
    *,
    source_glb: str | None = None,
    _gltf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract the "Hitboxes" group from a hull GLB and build a sidecar
    ``hitbox`` section.

    The toolkit emits splash-box AABBs as cube meshes under a top-level
    ``Hitboxes`` node (see ``export/gltf_export.rs::export_ship_glb``).
    Each child keeps its raw ``CM_SB_<zone>_<n>[_<m>]`` name. This helper
    walks the scene graph, normalises each name via
    :func:`normalise_hitbox_token`, and returns a dict suitable for
    :func:`make_hitbox`.

    Args:
      glb_path: Path to the hull GLB.
      source_glb: Filename to store in ``hitbox.source_glb`` (defaults to
        the GLB's basename).
      _gltf: Pre-parsed glTF dict (private). Passed by
        :func:`geometry_and_hitbox_from_hull_glb` to avoid re-reading
        the GLB header + JSON chunk twice.

    Returns:
      A ``hitbox`` section dict. Empty regions / region_count=0 if the
      GLB has no Hitboxes group (older hulls that lacked ``.splash``
      files).
    """
    p = Path(glb_path)
    gltf = _gltf if _gltf is not None else _load_glb_json_chunk(p)

    nodes = gltf.get("nodes", [])
    scenes = gltf.get("scenes", [])
    if not nodes or not scenes:
        return make_hitbox(source_glb=source_glb or p.name)
    root_nodes = scenes[gltf.get("scene", 0)].get("nodes", [])
    hitbox_group_idx = next(
        (i for i in root_nodes if nodes[i].get("name") == "Hitboxes"),
        None,
    )
    if hitbox_group_idx is None:
        return make_hitbox(source_glb=source_glb or p.name)

    children = nodes[hitbox_group_idx].get("children", [])
    # Aggregate per canonical zone. Track raw tokens per zone to expose
    # the alias (useful for zones where the raw name is non-obvious, e.g.
    # "cit" → "citadel", "ruder" → "steeringgear").
    regions: dict[str, dict[str, Any]] = {}
    raws_by_zone: dict[str, set[str]] = {}
    total = 0
    for child_idx in children:
        name = nodes[child_idx].get("name", "")
        if not name.startswith("CM_SB_"):
            continue
        stem = name[len("CM_SB_"):]
        # Strip trailing instance suffixes: ``_1``, ``_1_1``, ``_12`` etc.
        raw = stem
        while raw and (raw[-1].isdigit() or raw.endswith("_")):
            raw = raw.rstrip("0123456789").rstrip("_")
        canonical = normalise_hitbox_token(name)
        entry = regions.setdefault(canonical, {"box_count": 0})
        entry["box_count"] += 1
        if raw:
            raws_by_zone.setdefault(canonical, set()).add(raw)
        total += 1

    # Only expose raw alias when it differs from the canonical zone name.
    for zone, raws in raws_by_zone.items():
        non_trivial = sorted(r for r in raws if r != zone)
        if not non_trivial:
            continue
        if len(non_trivial) == 1:
            regions[zone]["raw_name"] = non_trivial[0]
        else:
            regions[zone]["raw_names"] = non_trivial

    return make_hitbox(
        source_glb=source_glb or p.name,
        region_count=total,
        regions=regions,
    )


def geometry_and_hitbox_from_hull_glb(
    glb_path: str | Path,
    *,
    geometry_kwargs: dict[str, Any] | None = None,
    hitbox_source_glb: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk a hull GLB once and return both the ``geometry`` and ``hitbox``
    sections.

    Equivalent to calling :func:`geometry_from_hull_glb` and
    :func:`hitbox_from_hull_glb` separately, but reads + parses the GLB
    file's JSON chunk only once. The two walks inspect disjoint top-level
    groups (``Hull`` and ``Hitboxes``), so they don't interfere.

    Args:
      glb_path:           Path to the hull GLB.
      geometry_kwargs:    Optional kwargs forwarded to
                          :func:`geometry_from_hull_glb` (e.g.
                          ``waterline_y``, ``native_scale_m``).
      hitbox_source_glb:  Forwarded to :func:`hitbox_from_hull_glb` as
                          ``source_glb``.

    Returns:
      ``(geometry, hitbox)`` — two dicts ready to land at
      ``doc["geometry"]`` / ``doc["hitbox"]``.
    """
    gltf = _load_glb_json_chunk(glb_path)
    geometry = geometry_from_hull_glb(
        glb_path, _gltf=gltf, **(geometry_kwargs or {}),
    )
    hitbox = hitbox_from_hull_glb(
        glb_path, source_glb=hitbox_source_glb, _gltf=gltf,
    )
    return geometry, hitbox


def new_document_from_placements(
    placements: str | Path | dict[str, Any],
    *,
    class_override: str | None = None,
    auto_derived_class: str | None = None,
    ship_key_suffix: str | None = None,
    stages_completed: list[int] | None = None,
) -> dict[str, Any]:
    """Build an initial sidecar document from a placements JSON — combines
    :func:`new_document`, :func:`make_pipeline`, :func:`ship_from_placements`,
    and :func:`absorb_placements_json` into one call.

    Typical usage::

        doc = sidecar.new_document_from_placements(
            "Fletcher/models/Fletcher_accessories.json",
        )
        # optional: merge armor table, materials, skins, etc.
        sidecar.write(doc, "Fletcher/Fletcher.meta.json")

    After this call, ``doc`` has a filled-in ``pipeline`` + ``ship`` section
    and every typed placement section populated from the toolkit output.
    Hand-authored overrides should be merged on top via
    :func:`merge_preserving`.

    See :func:`ship_from_placements` for the ``class_override`` /
    ``auto_derived_class`` precedence rules.
    """
    if isinstance(placements, (str, Path)):
        with open(placements, encoding="utf-8") as f:
            placements_data = json.load(f)
    else:
        placements_data = placements

    ship = ship_from_placements(
        placements_data,
        class_override=class_override,
        auto_derived_class=auto_derived_class,
        ship_key_suffix=ship_key_suffix,
    )
    doc = new_document(
        pipeline=make_pipeline(stages_completed=stages_completed or [0, 7]),
        ship=ship,
    )
    # Pass the parsed data dict through so absorb doesn't reopen the file.
    doc = absorb_placements_json(doc, placements_data)
    return doc
