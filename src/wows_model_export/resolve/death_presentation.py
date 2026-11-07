"""Hull death presentation → the sidecar ``death`` block.

GameParams carries everything the client needs to stage a ship's death, per hull:

* ``effectOnDeath`` / ``effectOnFloodDeath`` — animation state machines (``.ssm``) loaded on
  an invisible carrier model at the crack position. Each names a sequence (``.seq``) whose
  particle tracks play the class death explosion (``particles/vehicles/Battleship_death.xml``
  for 10 s, ...). Resolved here to ``[{path, start_s, end_s}]``.
* ``death*Effect`` — the sinking effects: death foam, seething bubbles, water fountains,
  fuming at flooding, post-fire fuming, the catapult-plane wreck, the torpedo death burst, and
  a few submarine-only slots. ``Empty_FX`` placeholders are dropped.
* ``sinkingEffects`` → a ``Sinking`` entity (``PCXS*``) with per-class depth windows
  ``bubbleDeeps`` / ``foamDeeps`` / ``fountainDeeps`` (BW units, the sinking hull's depth).
* ``deathSettings`` (``PCOS001_Default``, the native sink controller's parameters) and the
  sink rows ``crackCoords`` / ``sinkTime`` / ``sinkStartTime``.

The native controller that consumes the windows and the sink parameters is not decompiled
(wows-re-notes ``53_vehicle_death_sink_fsm.md`` §13): values are native, key meanings inferred.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..read.gameparams import get_entity

# Placeholder effects WG uses to switch a slot off.
EMPTY_EFFECTS: frozenset[str] = frozenset({
    "particles/Empty_FX.xml",
    "particles/NF_Empty_APR.xml",
})

# sidecar key → GameParams hull field
HULL_EFFECT_FIELDS: dict[str, str] = {
    "foam": "deathFoamEffect",
    "bubbles": "deathBubblesEffect",
    "fountains": "deathFountainsEffect",
    "fuming": "deathFumingEffect",
    "post_fire": "deathPostFireEffect",
    "plane": "deathPlaneEffect",
    "torpedo": "deathTorpedoEffect",
    "fire": "deathFireEffect",
    "fault": "deathFaultEffect",
    "shell": "deathShellEffect",
    "cauldron_explosion": "effectCauldronExplosion",
    "boiler_explosion": "boilerExplosionPostDeath",
    "cracks_and_oil": "effectShipCracksAndOilPuddle",
}

# sidecar key → Sinking entity field (depth window, BW units)
SINKING_WINDOW_FIELDS: dict[str, str] = {
    "bubbles": "bubbleDeeps",
    "foam": "foamDeeps",
    "fountains": "fountainDeeps",
}

SEQUENCE_CACHE_DIR = "death_sequences"


def _effect(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    path = path.strip()
    return path if path and path not in EMPTY_EFFECTS else None


def _window(v: Any) -> list[float] | None:
    if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int | float) for x in v):
        return [float(v[0]), float(v[1])]
    return None


def _rows(v: Any) -> list[list[float]] | None:
    if not isinstance(v, list) or not v:
        return None
    out = []
    for row in v:
        if not isinstance(row, list) or not all(isinstance(x, int | float) for x in row):
            return None
        out.append([float(x) for x in row])
    return out


# ── .ssm / .seq ─────────────────────────────────────────────────────────────────

_RESOURCE_RE = re.compile(r"<resource>\s*([^<\s][^<]*?)\s*</resource>")
_OBJECT_RE = re.compile(r"<sequenceObject>(.*?)</sequenceObject>", re.S)
_TYPE_RE = re.compile(r"<type>\s*([^<]*?)\s*</type>")
_TRACK_RE = re.compile(r"<track>(.*?)</track>", re.S)
_NAME_RE = re.compile(r"<name>\s*([^<]*?)\s*</name>")
_FRAME_RE = re.compile(r"<frame>.*?<startTime>\s*([-0-9.]+)\s*</startTime>.*?<value>\s*([^<]*?)\s*</value>.*?</frame>", re.S)
_DURATION_RE = re.compile(r"<duration>\s*([-0-9.]+)\s*</duration>")


def parse_state_machine(text: str) -> list[str]:
    """The ``.seq`` resources a state machine's sequence nodes play, in order."""
    return [r for r in _RESOURCE_RE.findall(text) if r.endswith(".seq")]


def parse_sequence(text: str) -> dict[str, Any]:
    """A ``.seq``: its duration and each particle object's visible interval(s).

    Visibility is a bool track of keyframes; each ``true`` run becomes one
    ``{path, start_s, end_s}`` (``end_s`` = the next ``false`` key, else the duration).
    """
    m = _DURATION_RE.search(text)
    duration = float(m.group(1)) if m else 0.0
    particles: list[dict[str, Any]] = []
    for obj in _OBJECT_RE.findall(text):
        t = _TYPE_RE.search(obj)
        r = _RESOURCE_RE.search(obj)
        if not t or t.group(1) != "particle" or not r:
            continue
        path = r.group(1)
        frames: list[tuple[float, bool]] = []
        for track in _TRACK_RE.findall(obj):
            n = _NAME_RE.search(track)
            if n and n.group(1) == "visibility":
                frames = [(float(a), b.strip().lower() == "true") for a, b in _FRAME_RE.findall(track)]
        if not frames:
            particles.append({"path": path, "start_s": 0.0, "end_s": duration})
            continue
        on: float | None = None
        for at, visible in sorted(frames):
            if visible and on is None:
                on = at
            elif not visible and on is not None:
                particles.append({"path": path, "start_s": round(on, 4), "end_s": round(at, 4)})
                on = None
        if on is not None:
            particles.append({"path": path, "start_s": round(on, 4), "end_s": duration})
    return {"duration_s": duration, "particles": particles}


def _read_vfs_text(vfs_path: str, cache_root: Path, config: PipelineConfig | None) -> str | None:
    """Read a small text file from the game VFS through the toolkit, cached on disk."""
    rel = vfs_path.lstrip("/")
    local = cache_root / rel
    if not local.is_file():
        from ..toolkit import vfs as _vfs
        # The toolkit matches VFS paths rooted at "/" and writes them without it.
        _vfs.extract(["/" + rel], cache_root, config=config, record_deps=True)
    if not local.is_file():
        return None
    return local.read_text(encoding="utf-8", errors="replace")


def resolve_sequences(
    state_machines: list[str],
    *,
    cache_root: Path,
    config: PipelineConfig | None = None,
) -> list[dict[str, Any]]:
    """``effectOnDeath``-style state machine paths → their sequences' particle tracks."""
    out: list[dict[str, Any]] = []
    for ssm in state_machines:
        if not isinstance(ssm, str) or not ssm.strip():
            continue
        ssm = ssm.strip()
        text = _read_vfs_text(ssm, cache_root, config)
        if text is None:
            out.append({"state_machine": ssm, "unresolved": True})
            continue
        for seq_path in parse_state_machine(text):
            seq_text = _read_vfs_text(seq_path, cache_root, config)
            if seq_text is None:
                out.append({"state_machine": ssm, "sequence": seq_path, "unresolved": True})
                continue
            seq = parse_sequence(seq_text)
            out.append({"state_machine": ssm, "sequence": seq_path, **seq})
    return out


# ── the block ───────────────────────────────────────────────────────────────────

def collect_death_block(
    hull: dict[str, Any],
    *,
    cache_root: Path | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, Any] | None:
    """Build the sidecar ``death`` block from an active hull's GameParams dict.

    ``cache_root`` holds extracted ``.ssm`` / ``.seq`` files (None skips sequence
    resolution: the block then carries the state machine paths only).
    """
    if not isinstance(hull, dict) or "deathFoamEffect" not in hull:
        return None
    block: dict[str, Any] = {}

    effects = {k: p for k, f in HULL_EFFECT_FIELDS.items() if (p := _effect(hull.get(f)))}
    if effects:
        block["effects"] = effects
    uw = [p for p in (hull.get("deathUnderWaterBubblesEffects") or []) if _effect(p)]
    if uw:
        block["underwater_bubbles"] = uw
    nodes = [n for n in (hull.get("deathNodes") or []) if isinstance(n, str) and n]
    if nodes:
        block["nodes"] = nodes
    for key, field in (("time_before_death_effect_s", "timeBeforeDeathEffect"),
                       ("time_before_cauldron_explosion_s", "timeBeforeCauldronExplosion")):
        v = hull.get(field)
        if isinstance(v, int | float):
            block[key] = float(v)

    for key, field in (("sequence", "effectOnDeath"), ("flood_sequence", "effectOnFloodDeath"),
                       ("underwater_sequence", "effectOnUnderWaterDeath")):
        ssms = [s for s in (hull.get(field) or []) if isinstance(s, str) and s.strip()]
        if not ssms:
            continue
        block[key] = (resolve_sequences(ssms, cache_root=cache_root, config=config)
                      if cache_root is not None else [{"state_machine": s} for s in ssms])

    sinking_id = hull.get("sinkingEffects")
    if isinstance(sinking_id, str) and sinking_id:
        block["sinking_effects_id"] = sinking_id
        sinking = get_entity(sinking_id)
        if isinstance(sinking, dict):
            windows = {k: w for k, f in SINKING_WINDOW_FIELDS.items() if (w := _window(sinking.get(f)))}
            if windows:
                block["depth_windows_bw"] = windows
    settings = hull.get("deathSettings")
    if isinstance(settings, str) and settings:
        block["death_settings_id"] = settings

    crack = hull.get("crackCoords")
    if isinstance(crack, list) and all(isinstance(x, int | float) for x in crack):
        block["crack_coords"] = [float(x) for x in crack]
    for key, field in (("sink_time_s", "sinkTime"), ("sink_start_time_s", "sinkStartTime")):
        rows = _rows(hull.get(field))
        if rows:
            block[key] = rows
    return block or None


def sequence_cache_root(config: PipelineConfig) -> Path:
    """Where extracted death state machines / sequences are cached, per game build."""
    from ..toolkit import build as _build
    build = _build.current_build(config)
    return Path(config.cache_dir) / SEQUENCE_CACHE_DIR / str(build or "unknown")


__all__ = [
    "EMPTY_EFFECTS",
    "HULL_EFFECT_FIELDS",
    "SINKING_WINDOW_FIELDS",
    "collect_death_block",
    "parse_sequence",
    "parse_state_machine",
    "resolve_sequences",
    "sequence_cache_root",
]
