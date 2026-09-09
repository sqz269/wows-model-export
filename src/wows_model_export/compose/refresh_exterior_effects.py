"""Refresh only exterior effect payloads in an existing ship sidecar.

No geometry or textures are rewritten. Existing exterior mesh/camo metadata is
preserved. Missing variant EP caches leave explicit name-only attachments.
"""
from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any

from ..read.gameparams import get_exterior, get_ship
from ..resolve.exterior_effects import resolve_exterior_effects
from ..resolve.exterior_unify import build_exterior_record, default_exterior_record
from ..resolve.gameparams_autofill import resolve_components, resolve_variant_model_dir


def apply_loaded_exterior_effects(
    doc: dict[str, Any], exterior: dict[str, Any], exterior_id: str,
    hull_key: str, models_dir: Path,
) -> dict[str, Any]:
    """Apply effects when the top-level GLB already IS the selected exterior.

    Baseline doc.effects comes from raw Vehicle components, not a previous
    exterior payload. The loaded model's EP candidates are authoritative.
    No duplicate geometry swap is generated for a newly introduced record.
    """
    effects = resolve_exterior_effects(
        (doc.get("effects") or {}).get("attachments") or [], exterior, hull_key,
        exterior_id=exterior_id, variant_hull=True,
        skel_ext_json=next(models_dir.glob("*_skel_ext.json"), None),
    )
    out = copy.deepcopy(doc)
    records = out.setdefault("exteriors", [])
    if not records:
        records.append(default_exterior_record())
    rec = next((r for r in records if r.get("exterior_id") == exterior_id), None)
    if rec is None:
        rec = build_exterior_record(
            exterior_id, {}, {}, display_name=exterior.get("title") or exterior.get("name"),
            species=(exterior.get("typeinfo") or {}).get("species"),
            peculiarity=exterior.get("peculiarity"), is_native=True,
            camo_scheme_key=next((s.get("scheme_key") or s.get("skin_id")
                for s in doc.get("skins", []) if s.get("exterior_id") == exterior_id), None),
        )
        records.append(rec)
    rec["effects"] = effects
    rec["is_native"] = True
    for other in records:
        if other is not rec:
            other["is_native"] = False
    return out


def refresh_exterior_effects(sidecar_path: Path, vehicle_id: str) -> dict[str, Any]:
    """Update effects in place, retaining unrelated data; return audit counts."""
    doc = json.loads(sidecar_path.read_text(encoding="utf-8"))
    ship = get_ship(vehicle_id)
    if not ship:
        raise ValueError(f"GameParams Vehicle not found: {vehicle_id}")
    hull_key = ((doc.get("variants") or {}).get("active_hull")
                or resolve_components(ship).get("__active_hull_id__"))
    if not hull_key:
        raise ValueError(f"Active hull could not be resolved: {vehicle_id}")
    model_dir = sidecar_path.parent / "models"
    if sidecar_path.parent.name == "models":
        model_dir = sidecar_path.parent
    native = ship.get("nativePermoflage")
    native_model, _ = resolve_variant_model_dir(vehicle_id, permoflage_id=native) if native else (None, None)
    if native_model and native_model.lower() == str((doc.get("ship") or {}).get("wg_asset_id", "")).lower():
        doc = apply_loaded_exterior_effects(doc, get_exterior(native) or {}, native, hull_key, model_dir)
        rows = next(r for r in doc["exteriors"] if r["exterior_id"] == native)["effects"]["attachments"]
        sidecar_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"vehicle_id": vehicle_id, "hull_key": hull_key, "exteriors_refreshed": 1,
                "loaded_exterior": native,
                "unresolved_hull_attachments": sum(1 for r in rows
                    if r.get("source") == "hull" and "position" not in r)}
    records = doc.setdefault("exteriors", [])
    by_id = {r.get("exterior_id"): r for r in records}
    exterior_ids = list(dict.fromkeys([*(ship.get("permoflages") or []),
                                      *([native] if native else [])]))
    refreshed = unresolved = 0
    for ext_id in exterior_ids:
        ext = get_exterior(ext_id) or {}
        variant_dir, _ = resolve_variant_model_dir(vehicle_id, permoflage_id=ext_id)
        candidates = (model_dir / "exteriors" / f"{ext_id}_effect_points.json"
                      if variant_dir else next(model_dir.glob("*_skel_ext.json"), None))
        effects = resolve_exterior_effects(
            (doc.get("effects") or {}).get("attachments") or [], ext, hull_key,
            exterior_id=ext_id, variant_hull=bool(variant_dir), skel_ext_json=candidates,
        )
        if effects is None:
            if ext_id in by_id:
                by_id[ext_id].pop("effects", None)
            continue
        rec = by_id.get(ext_id)
        if rec is None:
            # Effects-only exterior; do not invent unexported geometry swaps.
            if variant_dir:
                continue
            rec = build_exterior_record(
                ext_id, {}, {}, display_name=ext.get("title") or ext.get("name"),
                species=(ext.get("typeinfo") or {}).get("species"),
                peculiarity=ext.get("peculiarity"), is_native=ext_id == native,
                camo_scheme_key=next((s.get("scheme_key") or s.get("skin_id")
                    for s in doc.get("skins", []) if s.get("exterior_id") == ext_id), None),
            )
            records.append(rec)
        rec["effects"] = effects
        refreshed += 1
        unresolved += sum(1 for r in effects["attachments"]
                          if r.get("source") == "hull" and "position" not in r)
    if any(r.get("is_native") for r in records if r.get("exterior_id") != "default"):
        for rec in records:
            if rec.get("exterior_id") == "default":
                rec["is_native"] = False
    sidecar_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"vehicle_id": vehicle_id, "hull_key": hull_key,
            "exteriors_refreshed": refreshed, "unresolved_hull_attachments": unresolved}
