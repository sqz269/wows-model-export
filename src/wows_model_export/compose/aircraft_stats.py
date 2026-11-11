"""Aircraft stats sidecar: per-plane stats and loadouts of the aircraft library.

Layer 4 (composer). The aircraft library (:mod:`.aircraft_library`) holds
one airframe per model folder; several GameParams ``Aircraft`` entries fly
each airframe (stock / top / consumable / air-support variants). This
module writes, next to the library index, one stats block per entry that
flies a library airframe::

    <library_root>/stats.json      schema "wows_aircraft_stats/v1"

A block holds the plane's HP, speeds, climb and dive angles, heights,
endurance, detectability, squadron, attack-flight and hangar sizes, the
attack-run timing and accuracy, and the ordnance it carries: the
GameParams projectile with its numeric id, ammo type, count per plane and
dispersion. The block gives every number in one unit system:

* lengths and heights in metres of the game's displayed scale
  (``BW × 30``, the ballistic/display scale; the GLB frame is ``BW × 15``),
* speeds in knots as GameParams writes them (the client turns a plane's
  knots into world speed with the ship constant ``BW_KNOTS_TO_MPS``),
* times in seconds, angles in degrees, HP in GameParams points.

The dispersion fields are also kept as GameParams writes them; the block
resolves them into metres (ellipse semi-axes, the torpedo fan's width at
the aim distance) by the client's own formulas (``AirPlanes.AirplaneUtils``),
plus a point-origin half-angle of the fan. The schema, the field-by-field
sources and the client rules are in ``reference/schemas/AIRCRAFT_STATS.md``.

The composer emits these canonical :class:`StepEvent` names::

    "load_gameparams"  "compute_stats"  "write_stats"
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..types import AircraftStatsResult, OnEvent
from ._step_runner import StepRunner

#: Stats sidecar schema identifier (bump on a breaking change).
STATS_SCHEMA = "wows_aircraft_stats/v1"
STATS_FILE = "stats.json"

#: Metres of the displayed (ballistic) scale per BigWorld unit. GameParams
#: plane heights, salvo sizes and the torpedo aim distance are BW lengths.
BW_TO_M = 30.0

#: ``returnHeight`` at or above this many BW is GameParams' "no cap".
#: ``returnHeight`` is the height a returning flight reaches; the client holds
#: both escape thresholds just under it (``AirplaneUtils.getVisibilityLimit``).
RETURN_HEIGHT_NO_CAP_BW = 9999.0

#: GameParams `model` path of an aircraft entry → its model folder name.
_MODEL_DIR_RE = re.compile(r"/aircraft/[^/]+/(?P<asset_id>[^/]+)/[^/]+\.model$")
#: GameParams `model` path of a projectile → its model folder name (the
#: projectile library's asset id).
_PROJECTILE_DIR_RE = re.compile(r"/(?P<asset_id>[^/]+)/[^/]+\.model$")

#: Projectile ``typeinfo.species`` → ordnance kind.
ORDNANCE_KIND: dict[str, str] = {
    "Torpedo": "torpedo",
    "Bomb": "bomb",
    "Rocket": "rocket",
    "SkipBomb": "skip_bomb",
    "DepthCharge": "depth_charge",
    "PlaneSeaMine": "mine",
    "PlaneDrop": "supply_drop",
}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(float(v), nd)


def _m(bw: Any) -> float | None:
    """A BW length in displayed metres."""
    v = _num(bw)
    return None if v is None else _r(v * BW_TO_M)


def _pair(v: Any) -> list[float] | None:
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) is not None for x in v):
        return [float(v[0]), float(v[1])]
    return None


def airframe_of(entry: dict) -> str | None:
    """The model folder (library asset id) a GameParams aircraft entry flies."""
    m = _MODEL_DIR_RE.search(entry.get("model") or "")
    return m.group("asset_id") if m else None


def _dispersion(entry: dict, kind: str) -> dict | None:
    """The entry's spread fields as GameParams has them, resolved to metres.

    Client rules (``AirplaneUtils``; reference/schemas/AIRCRAFT_STATS.md):

    * the current spread is ``lerp(maxSpread, minSpread, aimAccuracy)``;
      ``aimAccuracy`` runs from 0 at the start of the run to 1 fully aimed;
    * bombs, rockets, skip bombs, depth charges, mines and drops land in
      two ellipses whose semi-axes ``[across, along]`` the flight path are
      ``salvoSize × spread`` (BW); ``minSpread`` / ``maxSpread`` are
      per-axis factors and carry no unit;
    * a torpedo flight's fan is ``spread`` wide (BW) at ``torpedoAimDist``
      ahead of the drop line (``getTorpedoingArea``). The fan is a
      trapezoid: its edges start at the ends of the drop line, which is as
      wide as the attack formation (not in the block). ``fan_half_angle_deg``
      is ``atan(spread / 2 / torpedoAimDist)``, the point-origin
      approximation that ignores the drop line's width and so overstates the
      client's edge angle, about ``atan((spread - width) / 2 /
      torpedoAimDist)``. Consumers build the trapezoid from ``fan_width_m``,
      ``aim_dist_m`` and the formation's width.
    """
    mn, mx = entry.get("minSpread"), entry.get("maxSpread")
    if kind == "torpedo":
        lo, hi, aim = _num(mn), _num(mx), _num(entry.get("torpedoAimDist"))
        if lo is None or hi is None or not aim:
            return None
        return {
            "model": "torpedo_fan",
            "min_spread": lo,
            "max_spread": hi,
            "torpedo_aim_dist": aim,
            "aim_dist_m": _m(aim),
            "fan_width_m": {"aimed": _m(lo), "unaimed": _m(hi)},
            "fan_half_angle_deg": {
                "aimed": _r(math.degrees(math.atan(lo / 2.0 / aim))),
                "unaimed": _r(math.degrees(math.atan(hi / 2.0 / aim))),
            },
        }
    lo, hi = _pair(mn), _pair(mx)
    inner, outer = _pair(entry.get("innerSalvoSize")), _pair(entry.get("outerSalvoSize"))
    if lo is None or hi is None or outer is None:
        return None

    def axes(salvo: list[float] | None, spread: list[float]) -> list[float] | None:
        if salvo is None:
            return None
        return [_r(salvo[0] * spread[0] * BW_TO_M), _r(salvo[1] * spread[1] * BW_TO_M)]

    return {
        "model": "ellipse",
        "min_spread": lo,
        "max_spread": hi,
        "inner_salvo_size": inner,
        "outer_salvo_size": outer,
        "inner_percent": _num(entry.get("innerBombsPercentage")),
        "ellipse_m": {
            "outer_aimed": axes(outer, lo),
            "outer_unaimed": axes(outer, hi),
            "inner_aimed": axes(inner, lo),
            "inner_unaimed": axes(inner, hi),
        },
    }


def _custom_rack(entry: dict, projectile: str | None) -> str | None:
    """The rack render set ``customProjRenderSets`` names for the entry's own
    projectile (most entries list only ``stub`` placeholders)."""
    if not projectile:
        return None
    for pair in entry.get("customProjRenderSets") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == projectile:
            return pair[1]
    return None


def plane_stats(key: str, entry: dict, gp: dict[str, Any]) -> tuple[dict, list[str]]:
    """One entry's stats block, and the problems met building it."""
    problems: list[str] = []
    ti = entry.get("typeinfo") or {}
    cruise = _num(entry.get("speedMoveWithBomb"))
    smin, smax = _num(entry.get("speedMin")), _num(entry.get("speedMax"))
    if cruise is None or smin is None or smax is None:
        problems.append(f"{key}: speedMoveWithBomb / speedMin / speedMax missing")

    ret = _num(entry.get("returnHeight"))
    cap = None if ret is None or ret >= RETURN_HEIGHT_NO_CAP_BW else ret

    def escape(field: str) -> float | None:
        # An escape threshold, not a height the flight climbs to: the client's
        # getVisibilityLimit gives an attack flight escapeHeightAttack (sent to
        # drop) or escapeHeightLanding (any other order), held just under
        # returnHeight, and Attacker counts the flight as escaped above it.
        v = _num(entry.get(field))
        if v is None:
            return None
        return _m(min(v, cap) if cap is not None else v)

    bomb = entry.get("bombName") or None
    proj = gp.get(bomb) if bomb else None
    if bomb and not isinstance(proj, dict):
        problems.append(f"{key}: projectile {bomb} not in GameParams")
        proj = None
    pspecies = ((proj or {}).get("typeinfo") or {}).get("species")
    if proj is None:
        kind = "none"
    else:
        kind = ORDNANCE_KIND.get(pspecies or "")
        if kind is None:
            problems.append(f"{key}: projectile species {pspecies!r} has no ordnance kind")
            kind = (pspecies or "unknown").lower()
    m = _PROJECTILE_DIR_RE.search((proj or {}).get("model") or "")
    drops = int(entry.get("attackCount") or 0)
    per_drop = int(entry.get("projectilesPerAttack") or 0)
    hs = entry.get("hangarSettings") or {}
    subtypes = entry.get("planeSubtype")
    subtypes = list(subtypes) if isinstance(subtypes, list) else []

    ordnance: dict[str, Any] = {
        "kind": kind,
        "projectile": bomb if proj is not None else None,
        "projectile_id": (proj or {}).get("id"),
        "projectile_index": (proj or {}).get("index"),
        "projectile_species": pspecies,
        "ammo_type": (proj or {}).get("ammoType"),
        "asset_id": m.group("asset_id") if m else None,
        "custom_rack": _custom_rack(entry, bomb),
        "per_drop": per_drop if proj is not None else 0,
        "drops": drops if proj is not None else 0,
        "per_plane": drops * per_drop if proj is not None else 0,
        "fall_time_s": _num(entry.get("bombFallingTime")),
        "dispersion": _dispersion(entry, kind) if proj is not None else None,
    }

    block: dict[str, Any] = {
        "airframe": airframe_of(entry),
        "model": entry.get("model"),
        "id": entry.get("id"),
        "index": entry.get("index"),
        "species": ti.get("species"),
        "nation": ti.get("nation"),
        "level": entry.get("level"),
        "plane_subtype": subtypes,
        "hp": _num(entry.get("maxHealth")),
        "speed_kn": {
            "cruise": cruise,
            "min": _r(smin * cruise) if cruise is not None and smin is not None else None,
            "max": _r(smax * cruise) if cruise is not None and smax is not None else None,
        },
        "speed_factors": {
            "min": smin,
            "max": smax,
            "climb": _num(entry.get("climbSpeedCoef")),
            "dive": _num(entry.get("diveSpeedCoef")),
            "attack": _num(entry.get("attackSpeedMultiplier")),
            "empty_return": _num(entry.get("emptyReturnSpeedMultiplier")),
            "preparation_limits": _pair(entry.get("preparationSpeedLimits")),
            "aiming_limits": _pair(entry.get("aimingSpeedLimits")),
        },
        "climb_angle_deg": _num(entry.get("angleOfClimb")),
        "dive_angle_deg": _num(entry.get("angleOfDive")),
        "altitudes_m": {
            "cruise": _m(entry.get("flightHeight")),
            "preparation": _m(entry.get("preparationHeight")),
            "aiming": _m(entry.get("aimingHeight")),
            # escape thresholds (see escape() above)
            "escape_after_attack": escape("escapeHeightAttack"),
            "escape_to_land": escape("escapeHeightLanding"),
            # returnHeight: the height a returning flight reaches; null = no cap
            "return_cap": _m(cap) if cap is not None else None,
        },
        "endurance_s": _num(entry.get("fuelTime")),
        "detection": {
            "visibility_km": _num(entry.get("visibilityFactor")),
            "visibility_by_plane_km": _num(entry.get("visibilityFactorByPlane")),
        },
        "squadron": {
            "planes": entry.get("numPlanesInSquadron"),
            "attackers": entry.get("attackerSize"),
            "hangar": {
                "start": hs.get("startValue"),
                "max": hs.get("maxValue"),
                "restore_planes": hs.get("restoreAmount"),
                "restore_s": _num(hs.get("timeToRestore")),
            } if hs else None,
        },
        "attack": {
            "preparation_s": _num(entry.get("preparationTime")),
            "aiming_s": _num(entry.get("aimingTime")),
            "accuracy_gain_per_s": {
                "preparation": _num(entry.get("preparationAccuracyIncreaseRate")),
                "aiming": _num(entry.get("aimingAccuracyIncreaseRate")),
            },
            "accuracy_turn_loss_per_s": {
                "preparation": _num(entry.get("preparationAccuracyDecreaseRate")),
                "aiming": _num(entry.get("aimingAccuracyDecreaseRate")),
            },
            "drop_interval_s": _num(entry.get("attackInterval")),
            "cooldown_s": _num(entry.get("attackCooldown")),
            "attack_angle_deg": _num(entry.get("attackAngle")),
            "max_attack_angle_deg": _num(entry.get("maxAttackAngle")),
        },
        "ordnance": ordnance,
    }
    return block, problems


def compute_stats(
    gp: dict[str, Any],
    airframes: dict[str, list[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict], list[str]]:
    """Stats blocks over a GameParams dict (unwrapped).

    ``airframes`` limits the output to these model folders (``{folder:
    [GameParams keys]}`` from the library index); None takes every
    aircraft entry with a model folder. Returns ``(airframes, entries,
    problems)``: ``airframes`` maps each folder to the sorted keys that
    fly it, ``entries`` maps each key to its block."""
    keys_by_model: dict[str, list[str]] = {}
    if airframes is None:
        for key, v in gp.items():
            if isinstance(v, dict) and (v.get("typeinfo") or {}).get("type") == "Aircraft":
                aid = airframe_of(v)
                if aid:
                    keys_by_model.setdefault(aid, []).append(key)
    else:
        keys_by_model = {aid: list(keys) for aid, keys in airframes.items()}
    entries: dict[str, dict] = {}
    problems: list[str] = []
    out_airframes: dict[str, list[str]] = {}
    for aid in sorted(keys_by_model):
        kept: list[str] = []
        for key in sorted(set(keys_by_model[aid])):
            v = gp.get(key)
            if not isinstance(v, dict) or (v.get("typeinfo") or {}).get("type") != "Aircraft":
                problems.append(f"{aid}: {key} is not a GameParams aircraft entry")
                continue
            block, probs = plane_stats(key, v, gp)
            if block["airframe"] != aid:
                problems.append(f"{key}: flies {block['airframe']}, listed under {aid}")
            entries[key] = block
            problems.extend(probs)
            kept.append(key)
        out_airframes[aid] = kept
    return out_airframes, entries, problems


def write_stats(
    library_root: Path,
    airframes: dict[str, list[str]],
    entries: dict[str, dict],
    *,
    game_build: int | None = None,
) -> Path:
    """Write ``stats.json`` beside the library index (atomic)."""
    doc = {
        "schema": STATS_SCHEMA,
        "version": time.strftime("%Y-%m-%d", time.gmtime()),
        "game_build": game_build,
        "units": {
            "length": "m (displayed scale: BW x 30)",
            "speed": "kn",
            "time": "s",
            "angle": "deg",
            "hp": "GameParams plane HP points",
            "visibility": "km",
        },
        "airframe_count": len(airframes),
        "entry_count": len(entries),
        # Library airframes no GameParams entry flies (a model kept for its
        # look; a consumer takes its numbers from another entry).
        "airframes_without_entries": sorted(aid for aid, keys in airframes.items() if not keys),
        "airframes": {aid: airframes[aid] for aid in sorted(airframes)},
        "entries": {k: entries[k] for k in sorted(entries)},
    }
    out = Path(library_root) / STATS_FILE
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def index_airframes(library_root: Path) -> dict[str, list[str]]:
    """``{model folder: [GameParams keys]}`` from the library index."""
    idx = json.loads((Path(library_root) / "index.json").read_text(encoding="utf-8"))
    return {
        aid: [g["key"] for g in (a.get("gameparams") or []) if g.get("key")]
        for aid, a in (idx.get("assets") or {}).items()
    }


def validate_stats(library_root: Path) -> list[str]:
    """Check ``stats.json`` against the index beside it and its own rules.

    Every index airframe is in the sidecar with exactly the index's
    GameParams keys (an airframe no entry flies is listed in
    ``airframes_without_entries``), and every listed key has a block that
    flies that airframe; ``min <= cruise <= max`` speed; ``per_plane = drops ×
    per_drop``; a projectile and a positive count unless the kind is
    ``none``; the dispersion matches the kind (an ellipse for dropped and
    fired ordnance, a fan for torpedoes); heights, HP and endurance are
    positive."""
    root = Path(library_root)
    path = root / STATS_FILE
    if not path.is_file():
        return [f"{path} missing"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{path}: unreadable: {e}"]
    problems: list[str] = []
    if doc.get("schema") != STATS_SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {STATS_SCHEMA!r}")
    entries = doc.get("entries") or {}
    airframes = doc.get("airframes") or {}
    try:
        want = index_airframes(root)
    except (OSError, ValueError) as e:
        want = {}
        problems.append(f"index.json unreadable: {e}")
    for aid, keys in want.items():
        if aid not in airframes:
            problems.append(f"{aid}: in the index, not in {STATS_FILE}")
        elif sorted(keys) != sorted(airframes[aid]):
            problems.append(f"{aid}: keys differ from the index ({sorted(keys)} vs {sorted(airframes[aid])})")
    bare = sorted(aid for aid, keys in airframes.items() if not keys)
    if bare != sorted(doc.get("airframes_without_entries") or []):
        problems.append(f"airframes_without_entries is not the airframes with no keys ({bare})")
    for aid in airframes:
        if aid not in want:
            problems.append(f"{aid}: in {STATS_FILE}, not in the index")
    for aid, keys in airframes.items():
        for k in keys:
            e = entries.get(k)
            if e is None:
                problems.append(f"{k}: listed under {aid}, no block")
                continue
            if e.get("airframe") != aid:
                problems.append(f"{k}: block flies {e.get('airframe')}, listed under {aid}")
    if doc.get("entry_count") != len(entries) or doc.get("airframe_count") != len(airframes):
        problems.append("airframe_count / entry_count do not match the maps")
    for k, e in entries.items():
        sp = e.get("speed_kn") or {}
        try:
            if not sp["min"] <= sp["cruise"] <= sp["max"]:
                problems.append(f"{k}: speed min <= cruise <= max does not hold ({sp})")
        except (KeyError, TypeError):
            problems.append(f"{k}: speeds missing ({sp})")
        for f in ("hp", "endurance_s"):
            if not isinstance(e.get(f), (int, float)) or e[f] <= 0:
                problems.append(f"{k}: {f} is {e.get(f)!r}")
        alt = e.get("altitudes_m") or {}
        for f in ("cruise", "aiming", "preparation"):
            if not isinstance(alt.get(f), (int, float)) or alt[f] <= 0:
                problems.append(f"{k}: altitudes_m.{f} is {alt.get(f)!r}")
        o = e.get("ordnance") or {}
        kind = o.get("kind")
        if o.get("per_plane") != (o.get("drops") or 0) * (o.get("per_drop") or 0):
            problems.append(f"{k}: per_plane is not drops x per_drop")
        if kind == "none":
            if o.get("projectile") is not None or o.get("per_plane"):
                problems.append(f"{k}: kind none with a projectile or a count")
            continue
        if kind not in ORDNANCE_KIND.values():
            problems.append(f"{k}: unknown ordnance kind {kind!r}")
        if not o.get("projectile") or not o.get("per_plane"):
            problems.append(f"{k}: kind {kind} without a projectile or a count")
        if o.get("projectile_id") is None or not o.get("ammo_type"):
            problems.append(f"{k}: projectile id or ammo type missing")
        disp = o.get("dispersion")
        want_model = "torpedo_fan" if kind == "torpedo" else "ellipse"
        if not disp or disp.get("model") != want_model:
            problems.append(f"{k}: dispersion model {(disp or {}).get('model')!r}, expected {want_model}")
    return problems


def build_aircraft_stats(
    *,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    gameparams: dict[str, Any] | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> AircraftStatsResult:
    """Write ``stats.json`` for the airframes of the library index.

    ``gameparams`` is an already loaded (unwrapped) GameParams dict; None
    loads the cache (about 40 s) and releases it afterwards. Raises
    :class:`StepError` when the index is missing or a step fails."""
    cfg = config or PipelineConfig.load()
    ws = Path(workspace or cfg.workspace).resolve()
    lib_root = Path(library_root or (ws / "libraries" / "aircraft")).resolve()
    runner = StepRunner(on_event, cancel=cancel)
    loaded_here = False
    with runner.step("load_gameparams") as st:
        if not (lib_root / "index.json").is_file():
            raise FileNotFoundError(f"{lib_root / 'index.json'} missing; build the library first")
        want = index_airframes(lib_root)
        gp = gameparams
        if gp is None:
            from ..read import gameparams as read_gameparams
            gp = read_gameparams.load_full(config=cfg)
            loaded_here = True
        st.annotate(f"{len(want)} airframe(s) in the index")
    try:
        with runner.step("compute_stats") as st:
            airframes, entries, problems = compute_stats(gp, want)
            st.annotate(f"{len(entries)} entr(ies) over {len(airframes)} airframe(s); "
                        f"{len(problems)} problem(s)")
    finally:
        if loaded_here:
            from ..read import gameparams as read_gameparams
            read_gameparams.unload_full()
    with runner.step("write_stats") as st:
        from ..toolkit import build as _build
        try:
            build = _build.current_build(cfg)
        except Exception:  # noqa: BLE001 - a missing game dir only loses the stamp
            build = None
        path = write_stats(lib_root, airframes, entries, game_build=build)
        problems.extend(validate_stats(lib_root))
        st.annotate(f"wrote {path.name}")
    return AircraftStatsResult(
        stats_path=path,
        airframes=len(airframes),
        entries=len(entries),
        warnings=tuple(problems),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "BW_TO_M",
    "ORDNANCE_KIND",
    "STATS_FILE",
    "STATS_SCHEMA",
    "airframe_of",
    "build_aircraft_stats",
    "compute_stats",
    "index_airframes",
    "plane_stats",
    "validate_stats",
    "write_stats",
]
