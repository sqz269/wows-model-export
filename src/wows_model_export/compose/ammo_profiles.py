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
helpers in :mod:`wows_model_export.resolve.gameparams_autofill`.
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
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp_read
from ..resolve import gameparams_autofill as _gp_autofill
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from ..types import AmmoProfilesResult, OnEvent
from ._step_runner import StepRunner

# Engine convention: every artillery shell renders the same shared mesh.
_ARTILLERY_ASSET_ID = "CPA001_Shell_Main"


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
    # directly.
    main = _gp_read.load_full()

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
        # parachute_*, ...}).
        visual: dict = {}
        visual.update(_gp_autofill.shell_visual_extras(ammo_id))
        visual.update(_gp_autofill.torpedo_visual_extras(ammo_id))
        effects: dict = {}
        effects.update(_gp_autofill.shell_effects_extras(ammo_id))
        effects.update(_gp_autofill.torpedo_effects_extras(ammo_id))

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
        "profiles": profiles,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    else:
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


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

    runner = StepRunner(on_event)
    warnings: list[str] = []

    # ── Step: load_gameparams ─────────────────────────────────────────
    try:
        with runner.step("load_gameparams") as st:
            if refresh_gameparams:
                _ensure_gameparams_dump(refresh=True, config=cfg)
            main = _gp_read.load_full()
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

    return AmmoProfilesResult(
        output_path=out_path,
        profiles_count=len(profiles),
        warnings=tuple(warnings),
        step_timings_ms=dict(runner.step_timings_ms),
    )


__all__ = [
    "build_ammo_profiles",
    "build_profiles",
    "write_profiles_file",
]
