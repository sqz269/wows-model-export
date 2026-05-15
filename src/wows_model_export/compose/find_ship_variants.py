"""Compose `find_ship_variants` -- enumerate Vehicle GameParams + detect
mesh-swap permoflage variants.

Lifted from ``tools/ship/find_ship_variants.py`` on the I:-side warships
repo.  This is the Layer 4 composer that walks the cached GameParams
dump, lists every Vehicle (and optionally every matching Exterior), and
groups vehicles by their hull ``model_dir`` so the caller can detect
collisions (the motivating case: ``PASC017_Baltimore_1944`` and
``PASC108_Baltimore_1944`` share one hull while carrying different stats).

Used during scaffold to flag "did you really mean THAT Vehicle?" when a
ship name resolves ambiguously, and standalone as a fleet-wide variant
inventory.

Canonical :class:`StepEvent` names emitted at step boundaries:

    "ensure_gameparams"     -- ensure (or refresh) the cached GameParams JSON
    "load_gameparams"       -- read the cache into the in-process flat dict
    "enumerate_vehicles"    -- walk Vehicle entries (optionally matching a name)
    "enumerate_exteriors"   -- walk Exterior entries (permoflages + camos)
    "detect_collisions"     -- bucket Vehicles by model_dir; identify multi-hits
    "write_output"          -- (optional) emit the survey as JSON

Per-step failures are wrapped in :class:`StepError` with ``step=`` set
to one of the names above; ``raise ... from e`` preserves the chain.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..read import gameparams as _gp_read
from ..resolve import gameparams_autofill as _gp_autofill
from ..toolkit.gameparams import ensure_dump as _ensure_gameparams_dump
from ..types import OnEvent
from ._step_runner import StepRunner

# ---------------------------------------------------------------------------
# Vehicle / model_dir resolution helpers
# ---------------------------------------------------------------------------


def _model_dir_from_vehicle(v: dict[str, Any]) -> str | None:
    """Extract the hull ``model_dir`` from a Vehicle GameParam record.

    Resolves the chain-end ``_Hull`` upgrade via
    :func:`wows_model_export.resolve.gameparams_autofill.resolve_components`,
    reads the ``model`` field, and returns the directory leaf
    (``ASC017_Baltimore_1944`` from
    ``content/gameplay/usa/ship/cruiser/ASC017_Baltimore_1944/...model``).
    """
    components = _gp_autofill.resolve_components(v, hull_choice="upgraded")
    hull = components.get("hull") if isinstance(components, dict) else None
    if not isinstance(hull, dict):
        return None
    mp = hull.get("model")
    if not isinstance(mp, str):
        return None
    parts = mp.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[-1].endswith(".model"):
        return parts[-2]
    return None


def _vehicles_for_search(
    needle: str,
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every Vehicle entry whose name or index contains
    ``needle`` (case-insensitive).  Empty needle returns every Vehicle.
    """
    low = needle.lower() if needle else ""
    out: list[dict[str, Any]] = []
    for top_key, v in data.items():
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo") or {}
        if ti.get("type") != "Ship":
            continue
        idx = str(v.get("index") or "")
        if low and low not in idx.lower() and low not in str(top_key).lower():
            continue
        out.append({
            "param_index":    idx,
            "top_key":        top_key,
            "model_dir":      _model_dir_from_vehicle(v),
            "tier":           v.get("level"),
            "is_premium":     bool(v.get("isPremium", False)),
            "is_in_test":     bool(v.get("isInTest", False)),
            "permoflages":    list(v.get("permoflages") or []),
            "native_permoflage": v.get("nativePermoflage"),
        })
    out.sort(key=lambda e: e["param_index"])
    return out


def _exteriors_for_search(
    needle: str,
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return every Exterior entry whose name contains ``needle``
    (case-insensitive).  Empty needle returns every Exterior.
    """
    low = needle.lower() if needle else ""
    out: list[dict[str, Any]] = []
    for top_key, v in data.items():
        if not isinstance(v, dict):
            continue
        ti = v.get("typeinfo") or {}
        if ti.get("type") != "Exterior":
            continue
        idx = str(v.get("index") or "")
        if low and low not in idx.lower() and low not in str(top_key).lower():
            continue
        out.append({
            "param_index":  idx,
            "top_key":      top_key,
            "camouflage":   v.get("camouflage"),
            "title":        v.get("title"),
        })
    out.sort(key=lambda e: e["top_key"])
    return out


def _group_by_model_dir(
    vehicles: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket Vehicles by their resolved ``model_dir``."""
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in vehicles:
        by_model[v.get("model_dir") or "(unresolved)"].append(v)
    return dict(by_model)


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def find_ship_variants(
    *,
    name: str | None = None,
    include_exteriors: bool = False,
    refresh: bool = False,
    output_json: Path | None = None,
    config: PipelineConfig | None = None,
    on_event: OnEvent | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Enumerate every Vehicle in GameParams + cross-link by mesh-swap
    ``nativePermoflage``.

    Returns a survey dict::

        {
          "search": "Baltimore" | null,        # original needle (None == all)
          "refreshed": True | False,           # whether ensure_dump ran with refresh
          "vehicles":   [{...}, ...],          # matching Vehicle records
          "exteriors":  [{...}, ...],          # matching Exterior records
                                                # (only when include_exteriors=True)
          "by_model_dir": {
              "ASC017_Baltimore_1944": [<Vehicle>, <Vehicle>],
              ...
          },
          "model_dir_collisions": {            # subset where >= 2 share a hull
              "ASC017_Baltimore_1944": [<Vehicle>, <Vehicle>],
              ...
          },
          "step_timings_ms": {...},
        }

    Each Vehicle record carries::

        param_index, top_key, model_dir, tier, is_premium, is_in_test,
        permoflages (list[str]), native_permoflage

    Inputs:
        name
            Optional search fragment (case-insensitive).  When omitted,
            every Vehicle is returned.
        include_exteriors
            When ``True`` also enumerate matching Exterior GameParams
            (permoflages + camos).
        refresh
            Force ``gameparams.ensure_dump(refresh=True)`` -- re-dumps
            the GameParams JSON before reading.  Use after a WoWS patch.
        output_json
            When provided, the survey dict is also written to this path
            as pretty-printed JSON.
        config
            Optional :class:`PipelineConfig`; defaults to
            ``PipelineConfig.load()``.
        on_event
            Optional :class:`StepEvent` callback.
        cancel
            Optional :class:`threading.Event` for cooperative cancel;
            when set, the next step boundary raises
            :class:`wows_model_export.errors.CancelledError`.
    """
    cfg = config or PipelineConfig.load()
    runner = StepRunner(on_event, cancel=cancel)

    # ── Step: ensure_gameparams ───────────────────────────────────────
    with runner.step(
        "ensure_gameparams",
        detail="refresh=True" if refresh else "use cached",
    ) as st:
        cache_path = _ensure_gameparams_dump(refresh=refresh, config=cfg)
        try:
            sz_mb = cache_path.stat().st_size / (1024 * 1024)
        except OSError:
            sz_mb = 0.0
        st.annotate(
            f"{cache_path.name} ({sz_mb:.1f} MB)",
            data={"cache_path": str(cache_path), "size_mb": sz_mb},
        )

    # ── Step: load_gameparams ─────────────────────────────────────────
    with runner.step("load_gameparams") as st:
        data = _gp_read.load_full(refresh=refresh)
        st.annotate(
            f"{len(data):,} top-level entities",
            data={"entity_count": len(data)},
        )

    # ── Step: enumerate_vehicles ──────────────────────────────────────
    with runner.step("enumerate_vehicles", detail=name or "(all)") as st:
        vehicles = _vehicles_for_search(name or "", data)
        st.annotate(
            f"{len(vehicles)} Vehicle(s) matched",
            data={"match_count": len(vehicles)},
        )

    # ── Step: enumerate_exteriors ─────────────────────────────────────
    exteriors: list[dict[str, Any]] = []
    if include_exteriors:
        with runner.step("enumerate_exteriors", detail=name or "(all)") as st:
            exteriors = _exteriors_for_search(name or "", data)
            st.annotate(
                f"{len(exteriors)} Exterior(s) matched",
                data={"match_count": len(exteriors)},
            )
    else:
        runner.emit("enumerate_exteriors", "skipped", detail="include_exteriors=False")

    # ── Step: detect_collisions ───────────────────────────────────────
    with runner.step("detect_collisions") as st:
        by_model = _group_by_model_dir(vehicles)
        collisions = {
            k: g for k, g in by_model.items()
            if len(g) > 1 and k != "(unresolved)"
        }
        st.annotate(
            f"{len(by_model)} hull(s); {len(collisions)} collision(s)",
            data={
                "model_dir_count":    len(by_model),
                "collisions_count":   len(collisions),
            },
        )

    survey: dict[str, Any] = {
        "search":                  name,
        "refreshed":               refresh,
        "vehicles":                vehicles,
        "exteriors":               exteriors,
        "by_model_dir":            by_model,
        "model_dir_collisions":    collisions,
        "step_timings_ms":         dict(runner.step_timings_ms),
    }

    # ── Step: write_output ────────────────────────────────────────────
    if output_json is not None:
        out_path = Path(output_json)
        with runner.step("write_output", detail=out_path.name) as st:
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(survey, f, indent=2, ensure_ascii=False)
            except (OSError, TypeError) as e:
                raise StepError(
                    step="write_output",
                    underlying=e,
                    detail=f"failed to write {out_path}",
                ) from e
            try:
                sz = out_path.stat().st_size
            except OSError:
                sz = 0
            st.annotate(
                f"wrote {out_path.name} ({sz:,} bytes)",
                data={"bytes": sz},
            )
    else:
        runner.emit("write_output", "skipped", detail="output_json=None")

    return survey


__all__ = [
    "find_ship_variants",
]
