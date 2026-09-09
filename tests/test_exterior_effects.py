from pathlib import Path
import sys
import unittest
import tempfile
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wows_model_export.resolve.exterior_effects import resolve_exterior_effects
from wows_model_export.resolve.exterior_unify import build_exteriors_block, collect_exteriors_for_vehicle
from wows_model_export.compose.refresh_exterior_effects import apply_loaded_exterior_effects
from wows_model_export.resolve.skel_ext_hashes import murmur3_32


class ExteriorEffectsTests(unittest.TestCase):
    def setUp(self):
        self.base = [
            {"source": "hull", "group": "smoke", "node": "EP_Smoke_1", "particle_path": "base", "position": [1, 2, 3]},
            {"source": "hull", "group": "fire1", "node": "EP_Fire_1", "particle_path": "basefire"},
            {"source": "artillery", "group": "shotEffect", "node": "HP_Gun", "particle_path": "shot", "intensity": 6},
        ]

    def test_replace_not_merge_and_preserve_duplicate_nodes(self):
        ext = {"effects": {"A_Hull": {"smoke": [["EP_Smoke_1", "new"], ["EP_Smoke_1", "second"]]}}}
        rows = resolve_exterior_effects(self.base, ext, "A_Hull")["attachments"]
        self.assertEqual([r["particle_path"] for r in rows], ["new", "second", "shot"])
        self.assertEqual(rows[0]["position"], [1, 2, 3])
        self.assertEqual(self.base[0]["particle_path"], "base")

    def test_empty_table_suppresses_only_hull(self):
        rows = resolve_exterior_effects(self.base, {"effects": {"A_Hull": {}}}, "A_Hull")["attachments"]
        self.assertEqual(rows, self.base[2:])

    def test_missing_hull_and_disabled_theme_inherit(self):
        self.assertIsNone(resolve_exterior_effects(self.base, {"effects": {"B_Hull": {}}}, "A_Hull"))
        self.assertIsNone(resolve_exterior_effects(self.base, {"effects": {"A_Hull": {}}}, "A_Hull", themed_enabled=False))

    def test_one_substitution_and_placeholder_suppression(self):
        ext = {"peculiarityEffects": {"shot": "newshot", "newshot": "wrong", "base": "wrong"}}
        rows = resolve_exterior_effects(self.base, ext, "A_Hull")["attachments"]
        self.assertEqual(rows[0]["particle_path"], "base")
        self.assertEqual(rows[-1]["particle_path"], "newshot")
        self.assertEqual(rows[-1]["intensity"], 6)
        ext["peculiarityEffects"]["shot"] = "particles/Empty_FX.xml"
        self.assertEqual(len(resolve_exterior_effects(self.base, ext, "A_Hull")["attachments"]), 2)

    def test_no_base_positions_on_variant_hull(self):
        rows = resolve_exterior_effects(self.base, {}, "A_Hull", variant_hull=True)["attachments"]
        self.assertNotIn("position", rows[0])

    def test_exterior_only_group_is_preserved(self):
        rows = resolve_exterior_effects([], {"effects": {"A_Hull": {
            "themeEvent": [["EP_New", "theme.xml"]]}}}, "A_Hull")["attachments"]
        self.assertEqual(rows[0]["group"], "themeEvent")

    def test_effects_only_record_kept_even_empty(self):
        block = build_exteriors_block({}, [{"exterior_id": "FX", "effects": {"attachments": []}}])
        self.assertEqual(len(block), 2)
        self.assertEqual(block[1]["effects"]["attachments"], [])
        block = collect_exteriors_for_vehicle("Ship", {}, get_ship=lambda _: {"permoflages": ["FX"]},
            resolve_swaps=lambda *a, **k: {}, apply_swaps=lambda b, s: b,
            effects_for=lambda _: {"attachments": []})
        self.assertEqual(len(block), 2)

    def test_munition_and_aura_scope(self):
        rows = [dict(source=s, group=g, particle_path="old") for s, g in
                [("munition", "water"), ("aa_aura", "detonationEffect"), ("artillery", "brokenEffect")]]
        out = resolve_exterior_effects(rows, {"peculiarityEffects": {"old": "new"}}, "A_Hull")["attachments"]
        self.assertEqual([r["particle_path"] for r in out], ["new", "old", "old"])

    def test_native_route_uses_loaded_ep_without_duplicate_geometry(self):
        doc = {"effects": {"attachments": self.base}, "ship": {"wg_asset_id": "native_hull"},
               "skins": [{"exterior_id": "Native", "scheme_key": "native_paint"}]}
        ext = {"effects": {"A_Hull": {"smoke": [["EP_Smoke_1", "native"]]}}}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Ship_skel_ext.json").write_text(json.dumps({"candidates": [{
                "segment": "ship_ep", "p0_hash": hex(murmur3_32("EP_Smoke_1")),
                "record_offset": "0x0", "transform": {"position": [7, 8, 9]},
            }]}), encoding="utf-8")
            result = apply_loaded_exterior_effects(doc, ext, "Native", "A_Hull", root)
            again = apply_loaded_exterior_effects(result, ext, "Native", "A_Hull", root)
        self.assertEqual(result, again)
        self.assertEqual(result["effects"], doc["effects"])
        native = result["exteriors"][1]
        self.assertTrue(native["is_native"])
        self.assertEqual(native["camo_scheme_key"], "native_paint")
        self.assertFalse(result["exteriors"][0]["is_native"])
        self.assertIsNone(native["hull"])
        self.assertEqual(native["mounts"], [])
        self.assertEqual(native["effects"]["attachments"][0]["position"], [7, 8, 9])


if __name__ == "__main__":
    unittest.main()
