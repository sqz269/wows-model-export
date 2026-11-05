"""Per-projectile ammo profiles builder.

Lifted from ``tools/build_ammo_profiles.py`` (private I:-side repo).
Layer 4 (composer): walks every ``typeinfo.type == "Projectile"``
entity in the cached GameParams dump and emits one per-ammo metadata
record joining ammo_type + projectile-library asset_id link + visual
block + impact-effects block.

Output schema (``<library_root>/ammo_profiles.json``)::

    {
      "version": "YYYY-MM-DD",
      "profile_count": 3000,
      "shell_trail_constants": { ... },     # client-wide trail constants
      "profiles": {
        "PAPA014_Shell_406mm_AP_AP_Mk_8": {
          "ammo_type": "AP",
          "species":   "Artillery",
          "asset_id":  "CPA001_Shell_Main",     # null when no mesh
          "visual":    { ... },                 # shell/torpedo visual extras
          "effects":   { ... }
        },
        ...
      }
    }

Per-projectile blocks come from the shell/torpedo visual + effects
helpers in :mod:`wows_model_export.resolve.gameparams_autofill`. An
artillery shell's ``visual.trail`` block carries its in-flight trail
parameters verbatim (native names, BW units); ``shell_trail_constants``
(:data:`SHELL_TRAIL_CONSTANTS`) carries the client-wide constants the
trail is drawn with, so a consumer hard-codes nothing. The build also
extracts the shell model's shared heat ramp (``incandescenceMap.dds``)
next to the file (:data:`TRAIL_TEXTURES`).
Shell-render and torpedo-render helpers extract disjoint key sets, so
the merged block carries every relevant field for the species (bombs
end up with shell-style sizing/tracer + airdrop parachute fields,
etc.).

asset_id derivation:

* Artillery → ``"CPA001_Shell_Main"`` (engine convention — every gun
  shell shares this single mesh; per-projectile sizing/tint is in
  ``visual``).
* Non-Artillery with non-empty ``model`` field → leaf directory of
  the ``model`` VFS path (e.g.
  ``content/gameplay/usa/projectile/torpedo/APT001_Torpedo_533mm_Mk_15/...``
  → ``"APT001_Torpedo_533mm_Mk_15"``).
* Pure-VFX entities (Laser / Wave / PlaneTracer) and any species with
  empty ``model`` → ``null``.

The composer emits the following canonical :class:`StepEvent` names:

    "load_gameparams"   "resolve_asset_ids"
    "collect_extras"    "write_profiles"
    "extract_trail_textures"
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp_read
from ..resolve import gameparams_autofill as _gp_autofill
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from ..toolkit.vfs import extract as _vfs_extract
from ..types import AmmoProfilesResult, OnEvent
from ._step_runner import StepRunner

# Engine convention: every artillery shell renders the same shared mesh.
_ARTILLERY_ASSET_ID = "CPA001_Shell_Main"

# Shared textures the in-flight shell needs that no per-asset export
# carries: VFS path -> path under the projectile library root. The heat
# ramp (32x1) is bound by the shell model's body and friction materials
# as ``incandescenceMap``.
TRAIL_TEXTURES: dict[str, str] = {
    "content/gameplay/common/projectile/artillery/textures/incandescenceMap.dds":
        "common/artillery/textures/incandescenceMap.dds",
}

# Client-wide constants of the artillery in-flight visuals (tracer
# streak, smoke ribbon, head-on hat, shell model). Python layer build
# 13533378 (ConstantsShip, SHELL_SETTINGS, ShotTracerSettings,
# TerminalFakeShotParameters), native trail code build 12506899, shader
# values from a June-2026 frame capture. Lengths are BW (1 BW = 15 m of
# model geometry), angles radians, times seconds, rates per second.
SHELL_TRAIL_CONSTANTS: dict[str, Any] = {
    "units": "bw",
    # Shot.start runs this long after the shot arrives; nothing (shell or
    # trail) is drawn before it.
    "client_shot_tracer_delay_s": 0.25,
    # The trail alpha rises 0 -> tracerOpacity over this.
    "trail_fade_in_s": 0.5,
    # A new ring point when the shell moved more than max_points_dist_bw
    # since the last one, or more than min_points_dist_bw with a pitch
    # change above max_dogleg_rad (pitch = -atan2(dy, |dxz|)).
    "min_points_dist_bw": 1.0,
    "max_points_dist_bw": 5.0,
    "max_dogleg_rad": 0.008726646,
    # Ring of 64 slots, at most 63 points kept when a point is added;
    # segment lengths are stored as max(len, min_segment_bw) and the
    # cumulative length (texture u source) as tex_per_bw * len.
    "ring_points": 64,
    "min_segment_bw": 0.02,
    "tex_per_bw": 0.1,
    # Fadeout after impact: alpha = clamp(1 - rate * t, 0, 1); the trail
    # is dead once both alphas are below dead_alpha.
    "fadeout_smoke_per_s": 0.85,
    "fadeout_tracer_per_s": 10.0,
    "dead_alpha": 0.0004,
    # Hat: s2 = sin^2 of the angle between the streak and the camera's
    # forward axis, f = clamp(s2 * hat_gain - hat_bias, 0, 1); no hat at
    # f >= 1 (above 5.38 deg), full below 1.98 deg.
    "hat_gain": 131.579,
    "hat_bias": 0.15789,
    # Near-camera fade in the pixel shader (both techniques):
    # saturate((dist - start / z) / (width / z)), z = max(zoom^2, min_zoom_sq).
    "near_fade_start_bw": 0.3,
    "near_fade_width_bw": 0.25,
    "near_fade_min_zoom_sq": 0.04,
    # Streak and hat vertex shader: fade = saturate((dist^2 * zoom -
    # modelHideDistance^2) * shell_hide_fade_per_bw2); vertex alpha unused.
    "shell_hide_fade_per_bw2": 0.001,
    # Smoke vertex shader: k = 1 + gain * saturate(dot(V.xz, -sunDir.xz))^power
    # scales the base colour, the SH ambient (floored at ambient_floor per
    # channel) and the sun term sunColour / (1 + luminance(sunColour)).
    "forward_scatter": {"gain": 4.0, "power": 16.0},
    "ambient_floor": 0.001,
    "sun_luminance_weights": [0.2125, 0.7154, 0.0721],
    # SHELL_SETTINGS (Lesta.shellModelGlobalSettings).
    "shell_settings": {
        "rollingSpeedCoef": 0.005,
        "speedCoefAfterCollision": 4.0,
        "modelShowDistance": 4.5,
        "modelHideDistance": 20.0,
        "modelTransitionDistance": 4.0,
        "defaultShellModels": dict(_gp_autofill.DEFAULT_SHELL_MODELS),
    },
    # Shell model spin: omega = k / shellModelScale * rotationCoeff *
    # bulletSpeed * rollingSpeedCoef * 2pi rad/s, times
    # max(decay_gain * (1 - decay_per_bw * fromMuzzle)^3, decay_floor).
    "spin": {"k": 0.025, "decay_gain": 11.9, "decay_per_bw": 0.001,
             "decay_floor": 0.1},
    # Shell model heat: heat = max(exp(-per_bw * max(fromMuzzle -
    # start_bw, 0)), floor_body); the friction glow floors at floor_friction.
    "heat": {"start_bw": 50.0, "per_bw": 0.01, "floor_body": 0.01,
             "floor_friction": 0.05},
    # Shell model material parameters as captured (emissivePower, blPos).
    "shell_model_materials": {
        "body": {"emissive_power": 10.0},
        "friction": {"emissive_power": 5.0, "bl_pos": 1.0},
        "friction_head": {"emissive_power": 2.5, "bl_pos": 1.0},
    },
    # The model's uniform scale is the firing gun's barrelDiameter (m); a
    # shell created without a gun (the fake continuation shells) keeps this.
    "model_scale_without_gun": 0.5,
    # TerminalFakeShotParameters: the ricochet / over-penetration
    # continuation shells multiply these trail parameters.
    "fake_shot": {"tracer_opacity": 0.5, "shell_length": 0.15,
                  "shell_glow": 0.15},
    "textures": {
        "incandescence_map": next(iter(TRAIL_TEXTURES)),
    },
}


# ---------------------------------------------------------------------------
# asset_id resolution
# ---------------------------------------------------------------------------


def _asset_id_from_model_path(model: str) -> str | None:
    """Parse the leaf directory name from a VFS model path.

    Examples::

        content/gameplay/usa/projectile/torpedo/APT001_Torpedo/APT001_Torpedo.model
        → "APT001_Torpedo"

        content/gameplay/.../bomb/JPB001/JPB001.model
        → "JPB001"

    Returns ``None`` for empty / malformed input. Trims a trailing
    ``.model`` from the leaf if present (some non-standard entries put
    the file at the root rather than in a same-named directory).
    """
    if not isinstance(model, str) or not model:
        return None
    parts = [p for p in model.split("/") if p]
    if len(parts) < 2:
        return None
    # Last part is the .model filename; second-to-last is the asset dir.
    leaf_dir = parts[-2]
    return leaf_dir or None


def _resolve_asset_id(species: str, projectile: dict[str, Any]) -> str | None:
    if species == "Artillery":
        return _ARTILLERY_ASSET_ID
    return _asset_id_from_model_path(projectile.get("model", "") or "")


# ---------------------------------------------------------------------------
# Profiles builder
# ---------------------------------------------------------------------------


def build_profiles(
    *,
    refresh: bool = False,
    config: PipelineConfig | None = None,
) -> tuple[dict[str, dict], dict[str, int], int]:
    """Walk every Projectile entity and emit a profiles dict keyed by
    ammo_id (the GameParams entity name, e.g.
    ``PAPA014_Shell_406mm_AP_AP_Mk_8``).

    Returns ``(profiles, species_counts, asset_id_resolved)`` where the
    profiles dict is ordered alphabetically by ammo_id (so re-runs
    without GameParams changes produce byte-identical output).
    """
    if refresh:
        _ensure_gameparams_dump(refresh=True, config=config)
    # ``load_full`` already unwraps the outer realm key — iterate
    # directly. Thread ``config`` so the warm-cache hit skips a
    # PipelineConfig.load() (user-settings read) per call.
    main = _gp_read.load_full(config=config)

    profiles: dict[str, dict] = {}
    species_counts: dict[str, int] = {}
    asset_id_resolved = 0

    for ammo_id, entity in main.items():
        if not isinstance(entity, dict):
            continue
        typeinfo = entity.get("typeinfo") or {}
        if typeinfo.get("type") != "Projectile":
            continue
        species = typeinfo.get("species") or "?"
        species_counts[species] = species_counts.get(species, 0) + 1

        ammo_type = entity.get("ammoType")
        asset_id = _resolve_asset_id(species, entity)
        if asset_id is not None:
            asset_id_resolved += 1

        # Both family helpers run; their key sets are disjoint by
        # design (shell-style emits {model_scale, tint, glow, tracer,
        # smoke, ...}; torpedo-style emits {tracer_effect,
        # parachute_*, ...}). We already hold the resolved Projectile
        # entity (`entity` IS get_projectile(ammo_id) here — we filtered
        # typeinfo.type == "Projectile" above), so call the *_from_proj
        # cores directly and skip four get_projectile round-trips per
        # projectile (each a get_entity -> load_full -> ensure_dump ->
        # PipelineConfig.load).
        visual: dict = {}
        visual.update(_gp_autofill._shell_visual_extras_from_proj(entity))
        visual.update(_gp_autofill._torpedo_visual_extras_from_proj(entity))
        effects: dict = {}
        effects.update(_gp_autofill._shell_effects_extras_from_proj(entity))
        effects.update(_gp_autofill._torpedo_effects_extras_from_proj(entity))

        entry: dict = {
            "ammo_type": str(ammo_type) if isinstance(ammo_type, str) else None,
            "species": species,
            "asset_id": asset_id,
        }
        if visual:
            entry["visual"] = visual
        if effects:
            entry["effects"] = effects
        profiles[ammo_id] = entry

    # Stable alphabetical order for diffs.
    ordered = {k: profiles[k] for k in sorted(profiles)}
    return ordered, species_counts, asset_id_resolved


def write_profiles_file(
    profiles: dict[str, dict],
    out_path: Path,
    *,
    pretty: bool = False,
) -> Path:
    """Write ``out_path`` containing the wrapped profiles document.

    The default form is compact (the file is a few MB; primary readers
    are downstream consumers). Pass ``pretty=True`` for an indented
    JSON suitable for human inspection.
    """
    doc = {
        "version": time.strftime("%Y-%m-%d", time.gmtime()),
        "profile_count": len(profiles),
        "shell_trail_constants": SHELL_TRAIL_CONSTANTS,
        "profiles": profiles,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    else:
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def extract_trail_textures(
    library_root: Path,
    *,
    config: PipelineConfig | None = None,
) -> list[Path]:
    """Extract :data:`TRAIL_TEXTURES` from the VFS into ``library_root``.

    One extract call; a file is only rewritten when its bytes changed, so
    a re-run leaves an unchanged file's mtime alone (publish copies on
    mtime). Returns the paths now present under ``library_root``.
    """
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="wms-trail-tex-") as td:
        staging = Path(td)
        _vfs_extract([f"**/{vfs}" for vfs in TRAIL_TEXTURES],
                     out_dir=staging, config=config)
        for vfs, rel in TRAIL_TEXTURES.items():
            src = staging / vfs
            if not src.is_file():
                continue
            dst = library_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                shutil.copyfile(src, dst)
            written.append(dst)
    return written


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def build_ammo_profiles(
    *,
    output_path: Path | None = None,
    workspace: Path | None = None,
    config: PipelineConfig | None = None,
    library_root: Path | None = None,
    on_event: OnEvent | None = None,
    refresh_gameparams: bool = False,
    pretty: bool = False,
    cancel: threading.Event | None = None,
) -> AmmoProfilesResult:
    """Build / refresh ``<library_root>/ammo_profiles.json``.

    The composer iterates every ``typeinfo.type == "Projectile"`` entry
    in the cached GameParams JSON dump and emits one record per ammo_id
    joining ammo_type + projectile-library asset_id + visual + effects.

    Parameters:
        output_path          Override output path. Defaults to
                              ``<library_root>/ammo_profiles.json``.
        workspace            ``PipelineConfig.workspace`` when None.
        config               ``PipelineConfig.load()`` when None.
        library_root         ``workspace / "libraries/projectiles"``
                              when None. Used only to derive
                              ``output_path`` when that isn't set.
        on_event             Optional progress callback receiving
                              :class:`StepEvent` notifications.
        refresh_gameparams   When True, rebuild the GameParams JSON
                              dump before reading (use after a game
                              patch).
        pretty               When True, write indented JSON. Defaults
                              to compact (the file is a few MB; primary
                              readers are downstream consumers).
        cancel               Optional :class:`threading.Event` for
                              cooperative cancel; when set, the next
                              step boundary raises
                              :class:`wows_model_export.errors.CancelledError`.

    Returns an :class:`AmmoProfilesResult` with the output path,
    profile count, warnings, and per-step timings.

    Raises :class:`StepError` (with ``step`` set to one of the canonical
    step names) when any step fails. The original exception is
    accessible via ``.underlying``.
    """
    cfg = config or PipelineConfig.load()
    ws = (workspace or cfg.workspace).resolve()
    lib_root = (library_root or (ws / "libraries" / "projectiles")).resolve()
    out_path = (
        output_path or (lib_root / "ammo_profiles.json")
    ).resolve()

    runner = StepRunner(on_event, cancel=cancel)
    warnings: list[str] = []

    # ── Step: load_gameparams ─────────────────────────────────────────
    try:
        with runner.step("load_gameparams") as st:
            if refresh_gameparams:
                _ensure_gameparams_dump(refresh=True, config=cfg)
            main = _gp_read.load_full(config=cfg)
            # Count Projectile entities up front so the step's data
            # payload carries something useful.
            projectile_count = sum(
                1 for v in main.values()
                if isinstance(v, dict)
                and (v.get("typeinfo") or {}).get("type") == "Projectile"
            )
            st.annotate(
                f"{projectile_count} Projectile entit(ies) "
                f"out of {len(main)} GameParams entries",
                data={
                    "projectiles": projectile_count,
                    "entities":    len(main),
                    "refreshed":   refresh_gameparams,
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="load_gameparams", underlying=e, detail=str(e),
        ) from e

    # ── Steps: resolve_asset_ids + collect_extras ─────────────────────
    # ``build_profiles`` does both in a single pass over Projectiles
    # (avoids walking the GameParams dict twice). We emit one canonical
    # step name per logical pass — the same wall time is split between
    # them for diagnostic visibility.
    profiles: dict[str, dict] = {}
    species_counts: dict[str, int] = {}
    asset_id_resolved = 0

    try:
        with runner.step("resolve_asset_ids") as st:
            # First pass: just count + resolve asset_ids without
            # collecting the extras (cheap — pure typeinfo + model
            # field reads). Reusing build_profiles here would double
            # the gameparams_autofill cost; we do an inline lightweight
            # walk first.
            for _ammo_id, entity in main.items():
                if not isinstance(entity, dict):
                    continue
                typeinfo = entity.get("typeinfo") or {}
                if typeinfo.get("type") != "Projectile":
                    continue
                species = typeinfo.get("species") or "?"
                species_counts[species] = species_counts.get(species, 0) + 1
                aid = _resolve_asset_id(species, entity)
                if aid is not None:
                    asset_id_resolved += 1
            st.annotate(
                f"resolved asset_id for {asset_id_resolved} projectile(s) "
                f"across {len(species_counts)} species",
                data={
                    "resolved":  asset_id_resolved,
                    "species":   dict(species_counts),
                    "projectiles": sum(species_counts.values()),
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="resolve_asset_ids", underlying=e, detail=str(e),
        ) from e

    try:
        with runner.step("collect_extras") as st:
            profiles, species_counts, asset_id_resolved = build_profiles(
                refresh=False, config=cfg,
            )
            n_with_visual = sum(1 for v in profiles.values() if "visual" in v)
            n_with_effects = sum(1 for v in profiles.values() if "effects" in v)
            st.annotate(
                f"collected extras for {len(profiles)} profile(s) "
                f"(visual={n_with_visual}, effects={n_with_effects})",
                data={
                    "profiles":     len(profiles),
                    "with_visual":  n_with_visual,
                    "with_effects": n_with_effects,
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="collect_extras", underlying=e, detail=str(e),
        ) from e

    # ── Step: write_profiles ──────────────────────────────────────────
    try:
        with runner.step("write_profiles") as st:
            written = write_profiles_file(profiles, out_path, pretty=pretty)
            try:
                size_kb = written.stat().st_size / 1024.0
            except OSError:
                size_kb = -1.0
            st.annotate(
                f"wrote {written.name} "
                f"({size_kb:.1f} KiB, {len(profiles)} profiles)",
                data={
                    "path":     str(written),
                    "size_kib": size_kb,
                    "profiles": len(profiles),
                },
            )
    except StepError:
        raise
    except Exception as e:
        raise StepError(
            step="write_profiles", underlying=e, detail=str(e),
        ) from e

    # ── Step: extract_trail_textures ──────────────────────────────────
    try:
        with runner.step("extract_trail_textures") as st:
            got = extract_trail_textures(out_path.parent, config=cfg)
            missing = len(TRAIL_TEXTURES) - len(got)
            if missing:
                warnings.append(
                    f"{missing} trail texture(s) not found in the VFS"
                )
            st.annotate(
                f"{len(got)} trail texture(s) under {out_path.parent.name}/",
                data={"written": [str(p) for p in got]},
            )
    except StepError:
        raise
    except Exception as e:
        warnings.append(f"extract_trail_textures failed: {e}")

    return AmmoProfilesResult(
        output_path=out_path,
        profiles_count=len(profiles),
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "SHELL_TRAIL_CONSTANTS",
    "TRAIL_TEXTURES",
    "build_ammo_profiles",
    "build_profiles",
    "extract_trail_textures",
    "write_profiles_file",
]
