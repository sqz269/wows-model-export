"""Compose `turret_autorig` — per-asset turret rig pivot extractor.

Lifted from ``tools/ship/turret_autorig.py`` on the I:-side warships repo
(closes the second dangling dependency on the public package — the
accessory library composer's ``build_rigs`` step previously shelled out
to the I:-side script via subprocess).

Layer 4 (composer): chains :mod:`wows_model_export.toolkit.bones` and
emits one ``<asset_id>.rig_pivots.json`` next to the library asset GLB.
The intermediate JSON is consumed by the downstream rigger to build the
actual armature.

Extraction path: reads pivots from the asset's ``.visual`` node tree via
``wowsunpack dump-bones --json`` (wrapped by
:func:`wows_model_export.toolkit.bones.fetch_bones`). WG's universal
naming convention (``Rotate_Y`` → ``Rotate_X`` → ``Roll_Back<N>`` →
``HP_gunFire<N>``) lets us pick pivots by name. AA mounts (Bofors /
Oerlikon — no ``Rotate_Y``) emit yaw=identity and surface only muzzle
hardpoints; their rotation is driven by the parent ship's AA
controller.

Coordinate convention:
  Pivots are recorded in TURRET-LOCAL space, with the turret's ship-
  placement transform stripped off at the root. Matches
  ``turret_rig_spec.md``: ``+Y`` up, ``+Z`` forward (DCC-agnostic).
  Toolkit-emitted bones are in BigWorld native units (1 unit ≈ 15 m);
  ``bw_to_pipeline`` scales to metres and matches the library GLB's
  vertex frame directly.

Canonical step names emitted via ``on_event``:

    "resolve_asset"   "fetch_bones"   "extract_pivots"
    "validate_rig"    "write_rig_json"

Each emits ``started`` → ``completed`` (or ``skipped`` / ``failed``).
Step failures are wrapped in :class:`StepError` with ``step=`` set to
one of the names above.
"""

from __future__ import annotations

import json
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PipelineConfig
from ..errors import StepError, ToolkitError
from ..toolkit.bones import fetch_bones
from ..types import OnEvent, TurretRigResult
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# GLB parsing — minimal JSON+BIN extractor used by the geometric validation
# pass.
# ---------------------------------------------------------------------------


def parse_glb_full(path: Path) -> tuple[dict, bytes]:
    """Like :func:`parse_glb_json` but also returns the BIN chunk for vertex reads."""
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ValueError(f"not a GLB: {path}")
    pos = 12
    gltf: dict | None = None
    bin_data = b""
    while pos + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, pos)
        pos += 8
        payload = data[pos:pos + chunk_len]
        pos += chunk_len
        if chunk_type == b"JSON":
            gltf = json.loads(payload)
        elif chunk_type == b"BIN\x00":
            bin_data = bytes(payload)
    if gltf is None:
        raise ValueError(f"no JSON chunk in {path}")
    return gltf, bin_data


def read_positions_lod0(gltf: dict, bin_data: bytes) -> list[tuple[float, float, float]]:
    """Read all VEC3 float POSITION vertices from the top-LOD primitive.

    Returns a flat list of (x, y, z) tuples. Skips lower-LOD meshes by
    name suffix (``_lod1/2/3/4``). Used for geometric rig validation —
    we only need the high-res mesh's vertices to test muzzle proximity.
    """
    out: list[tuple[float, float, float]] = []
    accessors = gltf.get("accessors") or []
    buffer_views = gltf.get("bufferViews") or []
    for m in gltf.get("meshes") or []:
        nm = (m.get("name") or "").lower()
        if any(t in nm for t in ("_lod1", "_lod2", "_lod3", "_lod4")):
            continue
        for prim in m.get("primitives") or []:
            pos_idx = (prim.get("attributes") or {}).get("POSITION")
            if pos_idx is None:
                continue
            a = accessors[pos_idx]
            if a.get("type") != "VEC3" or a.get("componentType") != 5126:
                continue  # only handle float VEC3 positions
            bv = buffer_views[a["bufferView"]]
            offset = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
            count = a["count"]
            stride = bv.get("byteStride", 12)
            if stride == 12:
                # Tightly packed — one struct.unpack_from for the whole run.
                buf = struct.unpack_from(f"<{count * 3}f", bin_data, offset)
                for i in range(0, len(buf), 3):
                    out.append((buf[i], buf[i + 1], buf[i + 2]))
            else:
                for i in range(count):
                    base = offset + i * stride
                    x, y, z = struct.unpack_from("<fff", bin_data, base)
                    out.append((x, y, z))
    return out


def nearest_vertex_distance(
    positions: list[tuple[float, float, float]],
    target: tuple[float, float, float],
) -> float:
    """Min Euclidean distance from ``target`` to any vertex in ``positions``.

    Returns ``float('inf')`` on empty input.
    """
    tx, ty, tz = target
    best = float("inf")
    for x, y, z in positions:
        d2 = (x - tx) ** 2 + (y - ty) ** 2 + (z - tz) ** 2
        if d2 < best:
            best = d2
    return best ** 0.5 if best != float("inf") else best


def flip_180_y(p: tuple[float, float, float]) -> tuple[float, float, float]:
    """Rotate by Ry(180°): (x, y, z) → (-x, y, -z)."""
    return (-p[0], p[1], -p[2])


#: Ratio threshold for the muzzle-tip vs Y-flipped-tip vote. A vote
#: registers when one distance is at least ``1/MUZZLE_VOTE_RATIO``× the
#: other (i.e. 2× closer). Below this margin the vote is a tie.
_MUZZLE_VOTE_RATIO: float = 0.5


def validate_against_mesh(pivots: dict, library_glb: Path) -> dict:
    """Geometric sanity check: each muzzle should land near the alive
    library mesh. If the 180°-flipped pose lands closer at all barrels,
    the rig was extracted in WG's pre-aim-rotation pose and needs a
    Ry(180°) flip applied (OI-6).

    Returns:
        {
          "verdict":    "ok" | "needs_flip" | "ambiguous" | "no_mesh",
          "muzzle_dists":      [...],   # nearest vertex distance per muzzle
          "muzzle_dists_flip": [...],   # same after flipping
          "votes":      {"ok": int, "flip": int, "tie": int},
        }
    """
    if not library_glb.is_file():
        return {"verdict": "no_mesh"}
    try:
        gltf, bin_data = parse_glb_full(library_glb)
        positions = read_positions_lod0(gltf, bin_data)
    except Exception as e:
        return {"verdict": "no_mesh", "error": str(e)}
    if not positions:
        return {"verdict": "no_mesh", "error": "empty position buffer"}

    tips = pivots.get("muzzle_tips") or pivots.get("barrels") or []
    if not tips:
        return {"verdict": "no_mesh", "error": "no muzzle_tips"}

    dists_ok: list[float] = []
    dists_flip: list[float] = []
    votes_ok = votes_flip = votes_tie = 0
    for tip in tips:
        d_ok = nearest_vertex_distance(positions, tuple(tip))
        d_flip = nearest_vertex_distance(positions, flip_180_y(tuple(tip)))
        dists_ok.append(round(d_ok, 4))
        dists_flip.append(round(d_flip, 4))
        # 2× ratio — strong signal vs noise. If neither is meaningfully
        # closer, treat as a tie.
        if d_flip < d_ok * _MUZZLE_VOTE_RATIO:
            votes_flip += 1
        elif d_ok < d_flip * _MUZZLE_VOTE_RATIO:
            votes_ok += 1
        else:
            votes_tie += 1

    n = len(tips)
    if votes_flip == n:
        verdict = "needs_flip"
    elif votes_ok == n:
        verdict = "ok"
    elif votes_flip > votes_ok and votes_flip + votes_tie >= n - 1:
        verdict = "needs_flip"      # majority + at most one tie
    elif votes_ok > votes_flip and votes_ok + votes_tie >= n - 1:
        verdict = "ok"
    else:
        verdict = "ambiguous"

    return {
        "verdict": verdict,
        "muzzle_dists":      dists_ok,
        "muzzle_dists_flip": dists_flip,
        "votes": {"ok": votes_ok, "flip": votes_flip, "tie": votes_tie},
    }


def apply_y_flip_to_pivots(pivots_pipeline: dict) -> dict:
    """Return a NEW pivots dict with all positions flipped 180° around Y.

    Yaw is at the origin so it's invariant; elev + every muzzle gets
    ``(x, y, z) → (-x, y, -z)``.
    """
    return {
        "yaw":     pivots_pipeline["yaw"],
        "elev":    flip_180_y(pivots_pipeline["elev"]),
        "barrels": [flip_180_y(b) for b in pivots_pipeline["barrels"]],
        "muzzles": [flip_180_y(m) for m in pivots_pipeline["muzzles"]],
    }


# ---------------------------------------------------------------------------
# Matrix helpers — glTF uses column-major 4×4; we only need translations
# for pivot extraction (BigWorld hardpoint chains rarely encode rotations
# other than axis flips).
# ---------------------------------------------------------------------------


def mat_translation(m: list[float] | None) -> tuple[float, float, float]:
    """Extract translation from a column-major 4×4 matrix. ``None`` → origin."""
    if not m:
        return (0.0, 0.0, 0.0)
    return (m[12], m[13], m[14])


def add(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


# ---------------------------------------------------------------------------
# Coordinate conversion — BigWorld to pipeline convention.
# ---------------------------------------------------------------------------

# Matches ``skel_ext_resolve`` and the toolkit's ``gltf_export``: 1 BW unit
# ≈ 15 m. The accessory library GLBs are emitted in this metric frame.
NATIVE_TO_METRES = 15.0


def bw_to_pipeline(p: tuple[float, float, float]) -> tuple[float, float, float]:
    """Convert a BigWorld-native pivot to the accessory-library GLB frame:
    metric, +Z forward (raw BW native handedness, just scaled).

    Historical note: an earlier version of this function also Z-negated
    to match the toolkit's HULL path (``export-ship`` applies
    ``negate_z_transform``). The ACCESSORY path (``export-model``, which
    emits the library GLBs this rig is for) does NOT apply Z-flip — the
    mesh ships in raw BW frame scaled to metres. Z-negation at the pivot
    side landed pivots at -Z while the mesh barrels are at +Z. Downstream
    consumers must handle the left-handed-at-rest frame of the library
    GLBs — same situation as the mesh itself. See
    ``rig.json.bind.forward_axis = "+Z"``.
    """
    return (
        p[0] * NATIVE_TO_METRES,
        p[1] * NATIVE_TO_METRES,
        p[2] * NATIVE_TO_METRES,
    )


# ---------------------------------------------------------------------------
# Toolkit-driven pivot extraction (default since 2026-05-10)
# ---------------------------------------------------------------------------
#
# The toolkit's ``wowsunpack dump-bones --json`` emits the asset's full visual
# node tree from ``.visual`` directly. WG's universal naming convention lets
# us pick pivots by name instead of inferring topology by walking empty-node
# chains:
#
#   Rotate_Y          turret yaw pivot (root of rotation rig)
#   Rotate_X          shared elev pivot
#   Roll_Back<N>      per-barrel recoil pivot   (children of Rotate_X)
#   HP_gunFire<N>     muzzle / projectile spawn (children of Roll_Back<N>)
#   HP_gunFireEffect  shared muzzle-flash anchor (skipped — not a barrel)
#
# AA mounts (Bofors / Oerlikon — no Rotate_Y) deviate: they have a
# weapon-named root node (e.g. ``Bofors``, ``OerlikonTwin``) with
# HP_gunFire<N> children directly. Yaw/elev rotation is driven by the
# parent ship's AA controller, not by this asset's own bones, so we treat
# yaw=identity and emit only muzzle hardpoints.

# Bone names that always identify the rig structure regardless of asset.
_BONE_YAW = "Rotate_Y"
_BONE_ELEV = "Rotate_X"
_BONE_ROLLBACK_PREFIX = "Roll_Back"     # Roll_Back1, Roll_Back2, …
_BONE_MUZZLE_PREFIX = "HP_gunFire"      # HP_gunFire1, HP_gunFire2, …
_BONE_MUZZLE_EFFECT = "HP_gunFireEffect"  # shared flash anchor; skip


def _muzzle_index(name: str) -> int | None:
    """Extract the trailing integer from ``'HP_gunFire7'`` → ``7``.

    Used to sort muzzles numerically (``HP_gunFire10`` must follow
    ``HP_gunFire9``, not ``1``).
    """
    suffix = name[len(_BONE_MUZZLE_PREFIX):]
    if suffix and suffix.isdigit():
        return int(suffix)
    return None


def _rollback_index(name: str) -> int | None:
    """Extract the trailing integer from ``'Roll_Back2'`` → ``2``.

    Skip ``Roll_Back<N>_BlendBone`` (animation helper, not a pivot point).
    """
    if name.endswith("_BlendBone"):
        return None
    suffix = name[len(_BONE_ROLLBACK_PREFIX):]
    if suffix and suffix.isdigit():
        return int(suffix)
    return None


def extract_pivots_from_bones(bones_doc: dict) -> dict:
    """Pure-data pivot extraction from ``wowsunpack dump-bones --json`` output.

    Returns ``yaw_world``, ``elev_world``, ``barrel_worlds``,
    ``muzzle_worlds``, ``shared_elev``, ``chain_depth`` (synthetic;
    ``len(name → parent)`` chain to yaw).

    Coordinates are in raw BigWorld native units (1 unit ≈ 15 m). The
    caller applies :func:`bw_to_pipeline` to scale to metres.
    """
    nodes = bones_doc.get("nodes") or []
    by_name: dict[str, dict] = {n["name"]: n for n in nodes}

    yaw_node = by_name.get(_BONE_YAW)
    elev_node = by_name.get(_BONE_ELEV)

    rollbacks: list[tuple[int, dict]] = []
    muzzles: list[tuple[int, dict]] = []
    for n in nodes:
        nm = n["name"]
        if nm.startswith(_BONE_ROLLBACK_PREFIX):
            ridx = _rollback_index(nm)
            if ridx is not None:
                rollbacks.append((ridx, n))
        elif nm.startswith(_BONE_MUZZLE_PREFIX) and nm != _BONE_MUZZLE_EFFECT:
            midx = _muzzle_index(nm)
            if midx is not None:
                muzzles.append((midx, n))

    rollbacks.sort(key=lambda kv: kv[0])
    muzzles.sort(key=lambda kv: kv[0])

    # Yaw / elev fall back to identity for AA-style mounts that lack the
    # standard rotation rig (Bofors / Oerlikon). Emitting (0,0,0) is the
    # right thing — these mounts pivot via the parent ship's AA rig, so
    # the asset-local rotation centre IS the origin.
    yaw_world = tuple(yaw_node["world_translation"]) if yaw_node else (0.0, 0.0, 0.0)
    elev_world = tuple(elev_node["world_translation"]) if elev_node else yaw_world

    barrel_worlds = [tuple(n["world_translation"]) for _, n in rollbacks]
    muzzle_worlds = [tuple(n["world_translation"]) for _, n in muzzles]

    # Modern WG content always shares Rotate_X across all barrels; per-
    # barrel elev is encoded only via the BlendBone animation helpers we
    # ignore. Keep the field for output-schema documentation.
    shared_elev = True

    # When fewer Roll_Back nodes than HP_gunFire (e.g. twin secondaries
    # share one Roll_Back across both barrels) — or none at all (AA
    # mounts) — promote each muzzle to also serve as a barrel-base, so
    # downstream consumers see one barrel entry per gun.
    if len(barrel_worlds) < len(muzzle_worlds):
        barrel_worlds = list(muzzle_worlds)

    # Synthetic chain depth: yaw → elev → barrel → muzzle = 4 for full
    # turret rigs, 1 for static AA mounts.
    if yaw_node and elev_node and rollbacks and muzzles:
        chain_depth = 4
    elif yaw_node and elev_node:
        chain_depth = 3
    elif muzzle_worlds:
        chain_depth = 1
    else:
        chain_depth = 0

    return {
        "yaw_world":      yaw_world,
        "elev_world":     elev_world,
        "barrel_worlds":  barrel_worlds,
        "muzzle_worlds":  muzzle_worlds,
        "shared_elev":    shared_elev,
        "chain_depth":    chain_depth,
        # Diagnostic: preserve which bones we found so consumers can
        # tell turret-style from AA-style without re-parsing.
        "rig_kind": (
            "turret" if (yaw_node and elev_node and rollbacks)
            else "aa_mount" if muzzle_worlds and not yaw_node
            else "minimal"
        ),
    }


@dataclass
class ToolkitSource:
    """Pivot data sourced from ``wowsunpack dump-bones --json``."""

    asset_id: str
    vfs_geometry_path: str
    bones_doc: dict


def vfs_geometry_path_for_asset(asset_id: str, library_root: Path) -> str | None:
    """Derive the VFS ``.geometry`` path for an ``asset_id`` from its
    library entry.

    Returns
    ``content/gameplay/<scope>/<category>/<subcategory>/<asset>/<asset>.geometry``
    (or the ``content/styles/`` equivalent for style-resident assets) or
    ``None`` if the asset is not in the library yet (e.g. brand-new
    ingest that hasn't run ``build_accessory_library``).
    """
    idx_path = library_root / "index.json"
    if not idx_path.is_file():
        return None
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = (idx.get("assets") or {}).get(asset_id)
    if not entry:
        return None
    glb_rel = entry.get("glb")  # e.g. "usa/gun/main/AGM034.../AGM034....glb"
    if not glb_rel:
        return None
    parts = Path(glb_rel).with_suffix("").as_posix().split("/")
    scope = entry.get("scope")
    if scope == "style":
        # ["<StyleName>", "<asset_id>", "<asset_id>"]
        if len(parts) < 3:
            return None
        return f"content/styles/{'/'.join(parts)}.geometry"
    # ["usa","gun","main","AGM034_16in50_Mk7","AGM034_16in50_Mk7"]
    if len(parts) < 5:
        return None
    return f"content/gameplay/{'/'.join(parts)}.geometry"


# ---------------------------------------------------------------------------
# Per-asset extraction → ExtractionResult
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Internal pivot-extraction record — collapses to :class:`TurretRigResult`
    at the public boundary."""

    asset_id: str
    source_ship: str
    source_glb: str
    source_node_idx: int
    pivots_bw: dict            # pivots in raw BigWorld frame (debug)
    pivots_pipeline: dict      # pivots in pipeline frame, post auto-flip
    shared_elev: bool
    barrel_count: int
    warnings: list[str] = field(default_factory=list)
    # Geometric validation against the alive library mesh. None when the
    # library GLB hasn't been built yet (validation is best-effort).
    geometric_check: dict | None = None
    auto_flipped: bool = False
    rig_kind: str = "turret"   # 'turret' / 'aa_mount' / 'minimal'


def extract_for_asset(
    asset_id: str,
    src: ToolkitSource,
    library_glb: Path | None = None,
) -> ExtractionResult:
    """Reads pivots from the bones JSON the toolkit emits via
    ``dump-bones --json``.
    """
    pivots = extract_pivots_from_bones(src.bones_doc)

    warns: list[str] = []
    rig_kind = pivots["rig_kind"]
    if rig_kind == "minimal":
        warns.append(
            "no rig topology detected — neither Rotate_Y nor HP_gunFire* "
            "found; check the asset's .visual"
        )
    elif rig_kind == "aa_mount":
        warns.append(
            "AA-mount style rig (no Rotate_Y / Rotate_X) — yaw/elev "
            "default to identity; rotation is parent-driven"
        )
    n = len(pivots["barrel_worlds"])
    if n == 0 and rig_kind != "minimal":
        warns.append("no barrels detected")
    elif n > 16:
        warns.append(f"unusual barrel_count={n} (sanity check)")

    pivots_pipe = {
        "yaw":     bw_to_pipeline(pivots["yaw_world"]),
        "elev":    bw_to_pipeline(pivots["elev_world"]),
        "barrels": [bw_to_pipeline(p) for p in pivots["barrel_worlds"]],
        "muzzles": [bw_to_pipeline(p) for p in pivots["muzzle_worlds"]],
    }

    geo: dict | None = None
    auto_flipped = False
    if library_glb is not None and pivots_pipe["muzzles"]:
        geo = validate_against_mesh(pivots_pipe, library_glb)
        if geo.get("verdict") == "needs_flip":
            pivots_pipe = apply_y_flip_to_pivots(pivots_pipe)
            auto_flipped = True
            warns.append(
                "auto-flipped 180° around yaw based on geometric "
                "validation against the alive library mesh"
            )
        elif geo.get("verdict") == "ambiguous":
            warns.append(
                f"geometric validation ambiguous "
                f"(votes={geo.get('votes')}); pivots left as-extracted, "
                f"verify manually in the webview"
            )

    return ExtractionResult(
        asset_id=asset_id,
        source_ship="<toolkit>",
        source_glb=src.vfs_geometry_path,
        source_node_idx=-1,         # not meaningful for the by-name extractor
        pivots_bw={
            "yaw":     pivots["yaw_world"],
            "elev":    pivots["elev_world"],
            "barrels": pivots["barrel_worlds"],
            "muzzles": pivots["muzzle_worlds"],
        },
        pivots_pipeline=pivots_pipe,
        shared_elev=pivots["shared_elev"],
        barrel_count=n,
        warnings=warns,
        geometric_check=geo,
        auto_flipped=auto_flipped,
        rig_kind=rig_kind,
    )


# ---------------------------------------------------------------------------
# Rig-pivots JSON writer
# ---------------------------------------------------------------------------


def write_intermediate(res: ExtractionResult, out_path: Path) -> None:
    """Emit the ``wows_turret_rig_pivots/v1`` JSON next to the library GLB.

    Consumed by the downstream rigger that builds the actual armature
    and exports the rigged GLB.
    """
    def v(p: tuple[float, float, float]) -> list[float]:
        return [round(float(p[0]), 5), round(float(p[1]), 5), round(float(p[2]), 5)]

    doc = {
        "schema": "wows_turret_rig_pivots/v1",
        "asset_id": res.asset_id,
        "source": {
            "kind": "toolkit",
            "ship": res.source_ship,
            "glb":  res.source_glb,
            "node_idx": res.source_node_idx,
        },
        "rig_kind": res.rig_kind,           # 'turret' / 'aa_mount' / 'minimal'
        "frame": {
            "units": "m",
            "forward_axis": "+Z",
            "up_axis":      "+Y",
            "note": (
                "Accessory-library GLB frame: metric, +Z forward (raw "
                "BigWorld native handedness scaled to metres). Matches "
                "<asset_id>.glb vertex positions directly — apply with "
                "no additional transform in Three.js. Consumers whose "
                "glTF import applies Rx(+90°) should place empties at "
                "(x, -z, y) relative to the raw pivot."
            ),
        },
        "shared_elev":  res.shared_elev,
        "barrel_count": res.barrel_count,
        "pivots": {
            # Turret-local yaw + elev pivots (BW hardpoint tree, stripped
            # of the asset's ship-space placement).
            "yaw":     v(res.pivots_pipeline["yaw"]),
            "elev":    v(res.pivots_pipeline["elev"]),
            # Per-barrel MUZZLE-END positions. BigWorld stores projectile
            # spawn points here — it does NOT store barrel-base positions.
            # The rigger reconstructs each barrel's base as
            # ``(muzzle[i].x, elev.y, elev.z)`` (elev pivot offset
            # laterally to the muzzle's X) and spans from that base to
            # this tip to form the Recoil_NN bone.
            "muzzle_tips":     [v(b) for b in res.pivots_pipeline["barrels"]],
            "muzzle_tips_alt": [v(m) for m in res.pivots_pipeline["muzzles"]],
        },
        "pivots_note": (
            "muzzle_tips[] are top-level hardpoints under elev. "
            "muzzle_tips_alt[] are deeper nested leaves (rare; often "
            "coincident with the top-level hardpoint, occasionally offset "
            "by 10-30 cm for a secondary spawn marker). Rigger should "
            "default to muzzle_tips[]."
        ),
        "warnings": res.warnings,
        "auto_flipped_180_around_yaw": res.auto_flipped,
        "geometric_check": res.geometric_check,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Library lookup helpers
# ---------------------------------------------------------------------------


def _output_dir_for_asset(library_root: Path, asset_id: str) -> Path | None:
    """Find ``libraries/accessories/.../<asset_id>/`` from the library index.

    Returns ``None`` if the asset hasn't been built yet (the caller can
    still emit an intermediate JSON to the workspace as a standalone).
    """
    idx_path = library_root / "index.json"
    if not idx_path.is_file():
        return None
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = (idx.get("assets") or {}).get(asset_id)
    if not entry or not entry.get("glb"):
        return None
    return (library_root / entry["glb"]).parent


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def autorig_asset(
    asset_id: str,
    *,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    output_path: Path | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Extract turret rig pivots for a single library asset.

    Parameters:
        asset_id        WG asset identifier (e.g. ``"AGM034_16in50_Mk7"``).
        config          ``PipelineConfig.load()`` when ``None``.
        library_root    Defaults to ``config.workspace / "libraries/accessories"``.
        output_path     Defaults to ``<asset_dir>/<asset_id>.rig_pivots.json``
                        (or ``<workspace>/<asset_id>.rig_pivots.json`` when
                        the asset isn't in the library index yet).
        on_event        Optional progress callback receiving
                        :class:`StepEvent` notifications. Canonical step
                        names: ``"resolve_asset"``, ``"fetch_bones"``,
                        ``"extract_pivots"``, ``"validate_rig"``,
                        ``"write_rig_json"``.
        cancel          Optional :class:`threading.Event` for
                        cooperative cancel; when set, the next step
                        boundary raises
                        :class:`wows_model_export.errors.CancelledError`.

    Returns the written ``rig_pivots.json`` path. We return a bare
    :class:`Path` because the accessory-library composer currently
    consumes the file via on-disk path lookup; consumers that need the
    rig kind / barrel count can call :func:`autorig_asset_full` to get
    a :class:`TurretRigResult` instead.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    step names) when the asset isn't found, the toolkit subprocess
    fails, or the JSON write fails. The original exception is
    accessible via ``.underlying``.
    """
    return autorig_asset_full(
        asset_id,
        config=config,
        library_root=library_root,
        output_path=output_path,
        on_event=on_event,
        cancel=cancel,
    ).rig_pivots_path


def autorig_asset_full(
    asset_id: str,
    *,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    output_path: Path | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> TurretRigResult:
    """Same as :func:`autorig_asset` but returns the full
    :class:`TurretRigResult` (rig kind, barrel count, etc.).

    See :func:`autorig_asset` for parameter semantics.
    """
    cfg = config or PipelineConfig.load()
    lib_root = (library_root or (cfg.workspace / "libraries" / "accessories")).resolve()

    runner = StepRunner(on_event, cancel=cancel)

    # ── Step: resolve_asset ───────────────────────────────────────────
    src_toolkit: ToolkitSource | None = None
    out_dir: Path | None = None
    library_glb: Path | None = None
    try:
        with runner.step("resolve_asset", detail=asset_id) as st:
            out_dir = _output_dir_for_asset(lib_root, asset_id)
            if out_dir is not None:
                candidate = out_dir / f"{asset_id}.glb"
                if candidate.is_file():
                    library_glb = candidate

            vfs_path = vfs_geometry_path_for_asset(asset_id, lib_root)
            if vfs_path is None:
                raise FileNotFoundError(
                    f"{asset_id} not in library index at {lib_root / 'index.json'} "
                    f"— run build_accessory_library first"
                )
            src_toolkit = ToolkitSource(
                asset_id=asset_id,
                vfs_geometry_path=vfs_path,
                bones_doc={},  # filled in by the fetch_bones step
            )
            st.annotate(
                f"toolkit: {vfs_path}",
                data={"mode": "toolkit", "vfs_path": vfs_path},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="resolve_asset", underlying=e, detail=str(e)) from e

    # ── Step: fetch_bones ─────────────────────────────────────────────
    try:
        with runner.step(
            "fetch_bones",
            detail=src_toolkit.vfs_geometry_path,
        ) as st:
            bones_doc = fetch_bones(
                src_toolkit.vfs_geometry_path,
                config=cfg,
            )
            src_toolkit.bones_doc = bones_doc
            nodes = bones_doc.get("nodes") or []
            st.annotate(
                f"{len(nodes)} bone(s)",
                data={"node_count": len(nodes)},
            )
    except StepError:
        raise
    except ToolkitError as e:
        raise StepError(
            step="fetch_bones", underlying=e,
            detail=f"dump-bones failed for {asset_id}: {e}",
        ) from e
    except Exception as e:
        raise StepError(step="fetch_bones", underlying=e, detail=str(e)) from e

    # ── Step: extract_pivots ──────────────────────────────────────────
    try:
        with runner.step("extract_pivots", detail=asset_id) as st:
            res = extract_for_asset(
                asset_id, src_toolkit,
                library_glb=None,  # validation runs in its own step
            )
            st.annotate(
                f"{res.rig_kind}, barrels={res.barrel_count}, "
                f"shared_elev={res.shared_elev}",
                data={
                    "rig_kind": res.rig_kind,
                    "barrel_count": res.barrel_count,
                    "shared_elev": res.shared_elev,
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="extract_pivots", underlying=e, detail=str(e)) from e

    # ── Step: validate_rig ────────────────────────────────────────────
    # Only runs when the library GLB exists on disk. Folds the auto-flip
    # correction back into ``res.pivots_pipeline`` + flags ``auto_flipped``.
    if library_glb is None or not res.pivots_pipeline["muzzles"]:
        runner.emit(
            "validate_rig", "skipped",
            detail=("library GLB missing" if library_glb is None
                    else "no muzzles to validate"),
        )
        runner.step_timings_ms["validate_rig"] = 0.0
    else:
        try:
            with runner.step("validate_rig", detail=library_glb.name) as st:
                geo = validate_against_mesh(res.pivots_pipeline, library_glb)
                res.geometric_check = geo
                if geo.get("verdict") == "needs_flip":
                    res.pivots_pipeline = apply_y_flip_to_pivots(res.pivots_pipeline)
                    res.auto_flipped = True
                    res.warnings.append(
                        "auto-flipped 180° around yaw based on geometric "
                        "validation against the alive library mesh"
                    )
                elif geo.get("verdict") == "ambiguous":
                    res.warnings.append(
                        f"geometric validation ambiguous "
                        f"(votes={geo.get('votes')}); pivots left as-extracted, "
                        f"verify manually in the webview"
                    )
                st.annotate(
                    f"verdict={geo.get('verdict')}",
                    data={"verdict": geo.get("verdict"), "votes": geo.get("votes")},
                )
        except StepError:
            raise
        except Exception as e:
            # Validation is best-effort — don't fail the whole composer.
            res.warnings.append(f"validate_rig failed: {e}")

    # ── Step: write_rig_json ──────────────────────────────────────────
    try:
        with runner.step("write_rig_json") as st:
            if output_path is not None:
                out = Path(output_path).resolve()
            elif out_dir is not None:
                out = (out_dir / f"{asset_id}.rig_pivots.json").resolve()
            else:
                # Fallback: write next to the workspace root. The intermediate
                # JSON is still useful standalone (e.g. when the library
                # hasn't been built yet).
                out = (cfg.workspace / f"{asset_id}.rig_pivots.json").resolve()
            write_intermediate(res, out)
            st.annotate(
                str(out),
                data={"output_path": str(out), "auto_flipped": res.auto_flipped},
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(step="write_rig_json", underlying=e, detail=str(e)) from e

    return TurretRigResult(
        asset_id=asset_id,
        rig_pivots_path=out,
        rig_kind=res.rig_kind,  # type: ignore[arg-type]
        barrel_count=res.barrel_count,
        shared_elev=res.shared_elev,
        auto_flipped=res.auto_flipped,
        warnings=tuple(res.warnings),
    )


__all__ = [
    # Public composer entries
    "autorig_asset",
    "autorig_asset_full",
    # Re-exported sources + pivots data structures (mostly for the
    # accessory_library composer and any future per-asset CLI wrapper).
    "ExtractionResult",
    "ToolkitSource",
    # Pure helpers (useful in tests + parity checks)
    "extract_pivots_from_bones",
    "extract_for_asset",
    "validate_against_mesh",
    "apply_y_flip_to_pivots",
    "bw_to_pipeline",
    "vfs_geometry_path_for_asset",
    "write_intermediate",
]
