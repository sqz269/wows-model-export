"""Step-0 validation for the exterior-unification data model.

Runs WITHOUT the GameParams pipeline — it exercises the pure core
(:func:`build_exterior_record` / :func:`project_exterior`) that the additive
``exteriors[]`` emit is built on. The regression-gate property is:

    project_exterior(base, build_exterior_record(base, variant)) == variant

for every swapped HP, with non-swapped placements unchanged.

Two layers:
  * a self-contained round-trip on the REAL Baltimore base->Azur delta
    (3 turret swaps: AGM019 -> AGM622_Azur, the schema_v6 Ry180 matrix flip,
    misc_filter [3] -> []), always runs;
  * an optional on-disk parity check against the published Baltimore base +
    ``__Azur_Baltimore`` sidecars, when present.

Run standalone:  python tests/test_exterior_unify.py
Or under pytest: python -m pytest tests/test_exterior_unify.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# src-layout import without installing the package.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wows_model_export.resolve.exterior_unify import (  # noqa: E402
    build_exterior_record,
    build_exteriors_block,
    default_exterior_record,
    project_exterior,
    reanchor_base_placements,
)

# ---------------------------------------------------------------------------
# Real Baltimore base->Azur delta, as inline fixtures (verified on disk).
# ---------------------------------------------------------------------------

_BASE_MF = [
    "MP_AM781_Boat_4_for_AGM019",
    "MP_AM782_Rangefinder_for_AGM019",
    "MP_AM785_Ventitation_3_and_Rangefinder_for_AGM019",
]
# Matrices only need to DIFFER base<->variant (the Ry180 row-0 sign flip is real:
# base HP_AGM_1/2 row0 = [-1,0,0,0], variant = [1,0,0,0]). Full 16-float here.
_BASE_M_FWD = [-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -1, 0, 0, 6.88, -55.18, 1]
_VAR_M_FWD = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 6.88, -55.18, 1]


def _turret(hp, asset, dead, mf, matrix):
    return {
        "instance_id": hp.replace("HP_", "INST_"),
        "asset_id": asset,
        "dead_asset_id": dead,
        "hp_name": hp,
        "transform": {"matrix": list(matrix)},
        "misc_filter": copy.deepcopy(mf),
    }


def _rider(hp, asset, attach_to, matrix, *, dead=None, mf=None):
    """A ship-level sub-mount that rides a turret (composite hp_name +
    ``attach_to`` = host turret instance_id), e.g. a 40 mm Bofors on a main gun."""
    return {
        "instance_id": hp.replace("HP_", "INST_"),
        "asset_id": asset,
        "dead_asset_id": dead,
        "hp_name": hp,
        "transform": {"matrix": list(matrix)},
        "misc_filter": copy.deepcopy(mf),
        "attach_to": attach_to,
    }


def _make_base():
    return {
        "turrets": [
            _turret("HP_AGM_1", "AGM019_8in55_CA68", "AGM019_8in55_CA68_dead", _BASE_MF, _BASE_M_FWD),
            _turret("HP_AGM_2", "AGM019_8in55_CA68", "AGM019_8in55_CA68_dead", _BASE_MF, _BASE_M_FWD),
            _turret("HP_AGM_3", "AGM019_8in55_CA68", "AGM019_8in55_CA68_dead", _BASE_MF, _BASE_M_FWD),
        ],
        # A non-swapped director — must survive projection untouched and never
        # appear in mounts[].
        "accessories": [
            _turret("HP_AD_1", "AD012_Director", None, None, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
        ],
    }


def _make_variant():
    v = _make_base()
    for t in v["turrets"]:
        t["asset_id"] = "AGM622_8in55_CA68_Azur"
        t["dead_asset_id"] = "AGM622_8in55_CA68_Azur_dead"
        t["misc_filter"] = []                       # nodesConfig miscFilter override -> drop bundled MPs
        t["transform"] = {"matrix": list(_VAR_M_FWD)}  # Ry180-baked variant matrix
    return v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_roundtrip_synthetic_baltimore():
    base, variant = _make_base(), _make_variant()
    rec = build_exterior_record(
        "PAES488_Azur_Baltimore", base, variant,
        display_name="Azur Baltimore", species="Skin", peculiarity="azurlane",
        wg_asset_id="asc080_baltimore_1944_azur", camo_scheme_key="mat_Baltimore_Azur",
    )

    # exactly the 3 turret swaps, none from the unchanged director
    assert len(rec["mounts"]) == 3, rec["mounts"]
    assert {m["hp_name"] for m in rec["mounts"]} == {"HP_AGM_1", "HP_AGM_2", "HP_AGM_3"}
    m0 = next(m for m in rec["mounts"] if m["hp_name"] == "HP_AGM_1")
    assert m0["base_asset_id"] == "AGM019_8in55_CA68"
    assert m0["asset_id"] == "AGM622_8in55_CA68_Azur"
    assert m0["dead_asset_id"] == "AGM622_8in55_CA68_Azur_dead"
    assert m0["misc_filter"] == []                       # override captured verbatim
    assert m0["transform"]["matrix"] == _VAR_M_FWD       # Ry180 matrix captured verbatim
    assert rec["variant_swapped_asset_ids"] == [
        "AGM622_8in55_CA68_Azur", "AGM622_8in55_CA68_Azur_dead",
    ]

    # THE REGRESSION GATE: projecting the record onto base reproduces the variant.
    projected = project_exterior(base, rec)
    assert projected["turrets"] == variant["turrets"], "projection != variant turrets"
    # non-swapped director untouched and absent from mounts
    assert projected["accessories"] == base["accessories"]


def test_swap_table_shape():
    rec = build_exterior_record("E", _make_base(), _make_variant())
    st = rec["swap_table"]
    assert st["by_hp_name"]["HP_AGM_1"] == "AGM622_8in55_CA68_Azur"
    assert st["by_asset_id"]["AGM019_8in55_CA68"] == "AGM622_8in55_CA68_Azur"
    assert st["dead_by_hp_name"]["HP_AGM_2"] == "AGM622_8in55_CA68_Azur_dead"
    assert st["misc_filter_by_hp"]["HP_AGM_3"] == []


def test_no_op_when_identical():
    base = _make_base()
    rec = build_exterior_record("noop", base, copy.deepcopy(base))
    assert rec["mounts"] == []
    assert rec["variant_swapped_asset_ids"] == []


def test_default_record_and_block():
    d = default_exterior_record()
    assert d["exterior_id"] == "default" and d["mounts"] == []
    block = build_exteriors_block(
        _make_base(),
        [{"exterior_id": "PAES488_Azur_Baltimore", "variant_placements": _make_variant(),
          "peculiarity": "azurlane"}],
    )
    assert block[0]["exterior_id"] == "default"          # index 0 is always default
    assert block[1]["exterior_id"] == "PAES488_Azur_Baltimore"
    assert len(block[1]["mounts"]) == 3
    # a texture-only "variant" (no diff) is dropped, not emitted as an exterior
    block2 = build_exteriors_block(
        _make_base(), [{"exterior_id": "camo_only", "variant_placements": _make_base()}],
    )
    assert [e["exterior_id"] for e in block2] == ["default"]
    # exactly ONE is_native entry: a native mesh-swap exterior (ARP-style)
    # takes the flag from the synthesised default; otherwise default keeps it
    block3 = build_exteriors_block(
        _make_base(),
        [{"exterior_id": "PJES477_ARP_TAKAO", "variant_placements": _make_variant(),
          "is_native": True}],
    )
    assert [e["is_native"] for e in block3] == [False, True]
    assert [e["is_native"] for e in block] == [True, False]
    # hull-only exterior (no mount swaps, but a hull-swap marker / HullDelta)
    # is KEPT — ~14% of the corpus swaps only the hull
    block4 = build_exteriors_block(
        _make_base(),
        [
            {"exterior_id": "hull_only_marker", "variant_placements": _make_base(),
             "wg_asset_id": "asc999_some_variant"},
            {"exterior_id": "hull_only_delta", "variant_placements": _make_base(),
             "hull": {"hull_glb": "models/exteriors/hull_only_delta_hull.glb"}},
        ],
    )
    assert [e["exterior_id"] for e in block4] == [
        "default", "hull_only_marker", "hull_only_delta",
    ]
    assert block4[2]["hull"]["hull_glb"].endswith("_hull.glb")


def test_reanchor_parked_hp():
    """HullDelta HP re-anchor: a moved/parked HP becomes a transform-only
    mounts[] record (asset unchanged), and stays OUT of the camo opt-out
    set + swap_table (those are strictly asset-swap semantics)."""
    base = _make_base()
    # WG parks the director inside the hull on the variant: same asset,
    # new transform (the engine's hide mechanism — no removal semantics).
    parked = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0.0, 0.0, -2.03, 1]
    anchored = reanchor_base_placements(
        base, {"HP_AD_1": {"matrix": list(parked), "position": [0.0, 0.0, -2.03]}},
    )
    # base copy untouched; anchored director carries the parked transform
    assert base["accessories"][0]["transform"]["matrix"][14] == 0
    assert anchored["accessories"][0]["transform"]["matrix"] == parked
    # turrets untouched (no map entry)
    assert anchored["turrets"] == base["turrets"]

    rec = build_exterior_record("parked", base, anchored)
    assert len(rec["mounts"]) == 1
    m = rec["mounts"][0]
    assert m["hp_name"] == "HP_AD_1"
    assert m["asset_id"] == m["base_asset_id"] == "AD012_Director"
    assert m["transform"]["matrix"] == parked
    # transform-only rows never pollute asset-swap consumers:
    assert rec["variant_swapped_asset_ids"] == []
    assert rec["swap_table"]["by_hp_name"] == {}
    assert rec["swap_table"]["by_asset_id"] == {}
    # projection replays the park
    projected = project_exterior(base, rec)
    assert projected["accessories"][0]["transform"]["matrix"] == parked


def test_reanchor_epsilon_keeps_base_bytes():
    """An hp_transforms entry within epsilon of the base transform must NOT
    touch the placement (float noise from the harvest's matrix algebra)."""
    base = _make_base()
    noisy = list(_BASE_M_FWD)
    noisy[12] += 1e-6  # sub-epsilon jitter
    anchored = reanchor_base_placements(
        base, {"HP_AGM_1": {"matrix": noisy, "position": noisy[12:15]}},
    )
    assert anchored["turrets"][0]["transform"]["matrix"] == _BASE_M_FWD
    rec = build_exterior_record("noop", base, anchored)
    assert rec["mounts"] == []


def test_reanchor_composes_with_swaps():
    """Moved HP + asset swap on the SAME mount: one record carrying both
    the variant asset AND the re-anchored transform; opt-out includes it."""
    base = _make_base()
    moved = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 7.1, -55.18, 1]
    anchored = reanchor_base_placements(
        base, {"HP_AGM_1": {"matrix": list(moved), "position": [0, 7.1, -55.18]}},
    )
    variant = copy.deepcopy(anchored)
    variant["turrets"][0]["asset_id"] = "AGM622_8in55_CA68_Azur"
    rec = build_exterior_record("both", base, variant)
    m = next(m for m in rec["mounts"] if m["hp_name"] == "HP_AGM_1")
    assert m["asset_id"] == "AGM622_8in55_CA68_Azur"
    assert m["transform"]["matrix"] == moved
    assert rec["variant_swapped_asset_ids"] == [
        "AGM019_8in55_CA68_dead", "AGM622_8in55_CA68_Azur",
    ]
    assert rec["swap_table"]["by_hp_name"] == {"HP_AGM_1": "AGM622_8in55_CA68_Azur"}


def test_vfs_dir_path_channel():
    """WG-faithful path provenance: a variant placement stamped with
    `vfs_dir` (the GameParams model path's directory) flows into the
    mounts[] record + swap_table + projection; transform-only records
    never carry it."""
    base = _make_base()
    variant = _make_variant()
    xd_dir = "content/gameplay/events/director/XD017_Director_Mk51"
    for t in variant["turrets"]:
        t["vfs_dir"] = xd_dir
    rec = build_exterior_record("paths", base, variant)
    assert all(m.get("vfs_dir") == xd_dir for m in rec["mounts"]), rec["mounts"]
    assert rec["swap_table"]["vfs_dir_by_asset_id"] == {
        "AGM622_8in55_CA68_Azur": xd_dir,
    }
    projected = project_exterior(base, rec)
    assert all(p.get("vfs_dir") == xd_dir for p in projected["turrets"])
    # base copy untouched; unswapped director never gains the key
    assert "vfs_dir" not in base["turrets"][0]
    assert "vfs_dir" not in projected["accessories"][0]

    # transform-only record (re-anchor) → no vfs_dir, no swap_table row
    moved = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, -2.03, 1]
    anchored = reanchor_base_placements(
        base, {"HP_AD_1": {"matrix": list(moved), "position": [0, 0, -2.03]}},
    )
    rec2 = build_exterior_record("parked", base, anchored)
    assert "vfs_dir" not in rec2["mounts"][0]
    assert rec2["swap_table"]["vfs_dir_by_asset_id"] == {}


_RIDER_M = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1.2, 3.4, -50.0, 1]


def test_rider_rehost_kept_when_host_swapped():
    """A sub-mount the exterior leaves UNCHANGED but whose host turret it
    SWAPS must be emitted as a kept rider carrying the host HP, so the consumer
    re-hosts it onto the variant turret (else it render-hides with the base)."""
    base = _make_base()
    base["antiair"] = [_rider("HP_AGM_2_HP_AGA_1", "AGA056_40mm_Bofors", "INST_AGM_2", _RIDER_M)]
    variant = _make_variant()                       # swaps HP_AGM_1/2/3; AA untouched
    variant["antiair"] = copy.deepcopy(base["antiair"])
    rec = build_exterior_record("E", base, variant)

    riders = [m for m in rec["mounts"] if m["hp_name"] == "HP_AGM_2_HP_AGA_1"]
    assert len(riders) == 1, rec["mounts"]
    rm = riders[0]
    assert rm["asset_id"] == rm["base_asset_id"] == "AGA056_40mm_Bofors"
    assert rm["attach_to"] == "HP_AGM_2"
    assert rm.get("rehost_kept") is True
    assert rm["transform"]["matrix"] == _RIDER_M
    # a kept rider (base asset) stays OUT of camo opt-out + asset-swap table
    assert "AGA056_40mm_Bofors" not in rec["variant_swapped_asset_ids"]
    assert "HP_AGM_2_HP_AGA_1" not in rec["swap_table"]["by_hp_name"]


def test_rider_not_emitted_when_host_unswapped():
    """A rider on a turret the exterior does NOT swap is left alone (no emit)."""
    base = _make_base()
    base["antiair"] = [_rider("HP_AD_1_HP_AGA_1", "AGA056_40mm_Bofors", "INST_AD_1", _RIDER_M)]
    variant = _make_variant()                       # swaps only HP_AGM_*; HP_AD_1 kept
    variant["antiair"] = copy.deepcopy(base["antiair"])
    rec = build_exterior_record("E", base, variant)
    assert not [m for m in rec["mounts"] if (m.get("asset_id") or "").startswith("AGA")]


def test_swapped_rider_carries_attach_to():
    """A rider whose OWN model the exterior swaps must still re-host onto the
    variant turret — it carries attach_to, but is a real swap (not kept)."""
    base = _make_base()
    base["antiair"] = [_rider("HP_AGM_2_HP_AGA_1", "AGA056_40mm_Bofors", "INST_AGM_2", _RIDER_M)]
    variant = _make_variant()
    v_rider = copy.deepcopy(base["antiair"][0])
    v_rider["asset_id"] = "AGA554_40mm_Bofors_HW19"
    variant["antiair"] = [v_rider]
    rec = build_exterior_record("E", base, variant)
    rm = next(m for m in rec["mounts"] if m["hp_name"] == "HP_AGM_2_HP_AGA_1")
    assert rm["asset_id"] == "AGA554_40mm_Bofors_HW19"
    assert rm["attach_to"] == "HP_AGM_2"
    assert "rehost_kept" not in rm
    assert "AGA554_40mm_Bofors_HW19" in rec["variant_swapped_asset_ids"]


def _placement_sections(doc):
    return {k: doc.get(k) or [] for k in ("turrets", "secondaries", "antiair", "torpedoes", "accessories")}


def test_onfile_parity_baltimore():
    """Optional: real published base + __Azur_Baltimore sidecars. Skips if absent.

    Looks in ``WB_PIPELINE_DIR`` first, then the Unity publish target, then the
    producer's own workspace (``ships/`` under ``J:/ExtractedModels``).
    """
    candidates = [
        Path(p) for p in (
            os.environ.get("WB_PIPELINE_DIR"),
            r"H:/UnityProjects/ProjectWB_URP/Assets/Ships/Pipeline",
            r"J:/ExtractedModels/ships",
        ) if p
    ]
    pipeline = next(
        (
            p for p in candidates
            if (p / "Baltimore_1944" / "Baltimore_1944.meta.json").exists()
            and (p / "Baltimore_1944__Azur_Baltimore"
                 / "Baltimore_1944__Azur_Baltimore.meta.json").exists()
        ),
        None,
    )
    if pipeline is None:
        print(f"  [skip] on-disk Baltimore sidecars not found under any of {candidates}")
        return "skipped"
    base_p = pipeline / "Baltimore_1944" / "Baltimore_1944.meta.json"
    var_p = pipeline / "Baltimore_1944__Azur_Baltimore" / "Baltimore_1944__Azur_Baltimore.meta.json"

    base = _placement_sections(json.loads(base_p.read_text(encoding="utf-8")))
    variant = _placement_sections(json.loads(var_p.read_text(encoding="utf-8")))
    rec = build_exterior_record("PAES488_Azur_Baltimore", base, variant)
    assert rec["mounts"], "expected at least one swapped mount on Baltimore->Azur"
    projected = project_exterior(base, rec)

    # Gate is scoped to the SWAPPED mounts (the exterior's intent) — NOT the full
    # placement arrays. Comparing every placement would trip on the re-extraction
    # noise the design explicitly refuses to bake (non-intent accessory deltas:
    # base-only decoratives, sub-mm transform jitter). For each swapped HP, the
    # projected placement must equal the variant placement byte-for-byte.
    def _by_hp(secs):
        return {p.get("hp_name"): p for sec in secs.values() for p in sec if p.get("hp_name")}

    proj_by_hp, var_by_hp = _by_hp(projected), _by_hp(variant)
    for m in rec["mounts"]:
        hp = m["hp_name"]
        proj, var = proj_by_hp[hp], var_by_hp[hp]
        for field in ("asset_id", "dead_asset_id", "misc_filter"):
            assert proj.get(field) == var.get(field), f"{hp} {field}: {proj.get(field)!r} != {var.get(field)!r}"
        assert (proj.get("transform") or {}).get("matrix") == (var.get("transform") or {}).get("matrix"), \
            f"{hp} transform"
    print(f"  on-disk parity OK: {len(rec['mounts'])} swapped mounts reproduced byte-for-byte")
    return "ok"


if __name__ == "__main__":
    tests = [
        test_roundtrip_synthetic_baltimore,
        test_swap_table_shape,
        test_no_op_when_identical,
        test_default_record_and_block,
        test_reanchor_parked_hp,
        test_reanchor_epsilon_keeps_base_bytes,
        test_reanchor_composes_with_swaps,
        test_vfs_dir_path_channel,
        test_rider_rehost_kept_when_host_swapped,
        test_rider_not_emitted_when_host_unswapped,
        test_swapped_rider_carries_attach_to,
        test_onfile_parity_baltimore,
    ]
    failed = 0
    for t in tests:
        try:
            res = t()
            print(f"PASS  {t.__name__}" + (f"  ({res})" if res else ""))
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
