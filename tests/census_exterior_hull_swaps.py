"""Hull-swap census over the mesh-swap exterior corpus (handoff §9b).

Sizes the ship-exterior-unification cutover: for every Vehicle x Exterior
pair that carries a mesh-swap payload, classify it as

* ``mount_only``      — per-HP / per-asset accessory swaps, hull SHARED
                        (no 2nd hull export needed after cutover);
* ``hull_and_mounts`` — accessory swaps PLUS a variant hull model_dir
                        (needs a GLB-only hull export into
                        ``models/exteriors/<id>_hull.glb``);
* ``hull_only``       — a variant hull model_dir with ZERO mount swaps
                        (currently emits NO ``exteriors[]`` row — Step-0
                        drops records with no mounts and no HullDelta, so
                        this bucket measures what HullDelta must cover).

Run:  .venv/Scripts/python.exe tools/exterior_hull_swap_census.py [--json OUT]

Read-only over the GameParams dump; no workspace writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wows_model_export.read.gameparams import load_full  # noqa: E402
from wows_model_export.resolve.gameparams_autofill import (  # noqa: E402
    resolve_variant_accessory_swaps,
    resolve_variant_model_dir,
)


def _has_mesh_swap(swaps: dict) -> bool:
    return any(swaps.get(k) for k in ("by_asset_id", "by_hp_name", "dead_by_hp_name"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=None,
                    help="Also write the full per-pair listing to this path.")
    args = ap.parse_args()

    flat = load_full()
    vehicles = {
        k: v for k, v in flat.items()
        if isinstance(v, dict) and (v.get("typeinfo") or {}).get("type") == "Ship"
    }
    print(f"{len(vehicles)} Vehicles in GameParams", file=sys.stderr)

    rows: list[dict] = []
    kind_counts: Counter[str] = Counter()
    pec_counts: Counter[tuple[str, str]] = Counter()
    species_counts: Counter[str] = Counter()

    for vid, ship in sorted(vehicles.items()):
        permos = list(ship.get("permoflages") or [])
        native = ship.get("nativePermoflage") or None
        if native and native not in permos:
            permos.append(native)
        for ext_id in permos:
            ext = flat.get(ext_id)
            if not isinstance(ext, dict):
                continue
            try:
                swaps = resolve_variant_accessory_swaps(vid, permoflage_id=ext_id)
                model_dir, _ = resolve_variant_model_dir(vid, permoflage_id=ext_id)
            except Exception as exc:  # noqa: BLE001 - census is best-effort
                print(f"  warn: {vid} x {ext_id}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                continue
            has_mounts = _has_mesh_swap(swaps)
            if not has_mounts and not model_dir:
                continue  # texture-only camo — out of scope
            kind = (
                "hull_and_mounts" if (has_mounts and model_dir)
                else "mount_only" if has_mounts
                else "hull_only"
            )
            species = (ext.get("typeinfo") or {}).get("species") or "?"
            pec = ext.get("peculiarity") or "?"
            kind_counts[kind] += 1
            species_counts[species] += 1
            pec_counts[(kind, pec)] += 1
            rows.append({
                "vehicle": vid,
                "exterior": ext_id,
                "kind": kind,
                "species": species,
                "peculiarity": pec,
                "is_native": ext_id == native,
                "model_dir": model_dir,
                "mount_swaps": {k: len(v) for k, v in swaps.items() if v},
            })

    total = sum(kind_counts.values())
    print(f"\nmesh-swap (vehicle x exterior) pairs: {total}")
    for kind, n in kind_counts.most_common():
        print(f"  {kind:16s} {n:5d}  ({n / total:5.1%})")
    print("\nby species:")
    for sp, n in species_counts.most_common():
        print(f"  {sp:16s} {n:5d}")
    print("\nby (kind, peculiarity):")
    for (kind, pec), n in sorted(pec_counts.items()):
        print(f"  {kind:16s} {pec:20s} {n:5d}")
    natives = sum(1 for r in rows if r["is_native"])
    print(f"\nnative mesh-swap exteriors (variant-routed base scaffolds): {natives}")

    if args.json:
        args.json.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        print(f"\nfull listing -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
