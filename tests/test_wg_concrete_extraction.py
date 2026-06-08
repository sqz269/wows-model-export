from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wows_model_export.compose.scaffold_ship import _merged_path_a_b_categories
from wows_model_export.resolve import camo as wg_camo
from wows_model_export.resolve.sidecar._absorb import apply_variant_asset_swaps
from wows_model_export.resolve.sidecar._materials import _apply_material_mappings_json


class WgConcreteExtractionTests(unittest.TestCase):
    def test_variant_swap_missing_glb_warning_does_not_abort_when_stderr_fails(self) -> None:
        class BrokenStderr:
            def write(self, _text: str) -> int:
                raise OSError(22, "Invalid argument")

            def flush(self) -> None:
                raise OSError(22, "Invalid argument")

        doc = {
            "turrets": [
                {
                    "instance_id": "HP_AGM_1",
                    "hp_name": "HP_AGM_1",
                    "asset_id": "AGM019",
                    "scope": "ship",
                    "category": "gun",
                    "subcategory": "main",
                    "transform": {"matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]},
                }
            ]
        }
        swaps = {
            "by_asset_id": {"AGM019": "AGM622"},
            "by_hp_name": {},
            "dead_by_hp_name": {},
            "misc_filter_by_hp": {},
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stderr", BrokenStderr()):
            swapped, n_swapped, unused = apply_variant_asset_swaps(
                doc,
                swaps,
                library_root=Path(tmp),
            )

        self.assertEqual(n_swapped, 1)
        self.assertEqual(unused, set())
        self.assertEqual(swapped["turrets"][0]["asset_id"], "AGM622")
        self.assertNotIn("attached_y_flip", swapped["turrets"][0])

    def test_material_mapping_replaces_png_guess_with_exact_mfm_dds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dds_dir = root / "textures_dds"
            dds_dir.mkdir()
            for name in (
                "ExactHull_a.dd0",
                "ExactHull_a.dd1",
                "ExactHull_normal.dd0",
                "WrongName_a.dd0",
            ):
                (dds_dir / name).write_bytes(b"")

            mapping_path = root / "material_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "materials": [
                            {
                                "material_identifier": "SHIPMAT_PBS_Hull",
                                "mfm_stem": "ExactHull",
                                "textures": {
                                    "diffuseMap": {"stem": "ExactHull"},
                                    "normalMap": {"stem": "ExactHull"},
                                },
                                "floats": {},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            materials = [
                {
                    "material_id": "SHIPMAT_PBS_Hull",
                    "shader_intent": "opaque_pbr",
                    "render_queue": "opaque",
                    "texture_sets": {
                        "main": {
                            "baseColor": {"png": "textures/WrongName.png"},
                            "normal": {"png": "textures/WrongName_normal.png"},
                        }
                    },
                }
            ]

            resolved = _apply_material_mappings_json(materials, mapping_path, dds_dir)

            self.assertEqual(resolved, 1)
            main = materials[0]["texture_sets"]["main"]
            self.assertEqual(main["baseColor"]["png"], "textures/WrongName.png")
            self.assertEqual(
                main["baseColor"]["dds_mips"],
                ["textures_dds/ExactHull_a.dd0", "textures_dds/ExactHull_a.dd1"],
            )
            self.assertNotIn("WrongName_a.dd0", main["baseColor"]["dds_mips"])
            self.assertEqual(
                main["normal"]["dds_mips"],
                ["textures_dds/ExactHull_normal.dd0"],
            )

    def test_library_mapping_preserves_alpha_render_intent_without_name_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dds_dir = root / "textures_dds"
            dds_dir.mkdir()
            (dds_dir / "transparent_glass_alpha_a.dd0").write_bytes(b"")

            mapping_path = root / "material_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "materials": [
                            {
                                "material_identifier": "SHIPGLASS_PBS_Hull",
                                "mfm_stem": "transparent_glass_alpha",
                                "textures": {
                                    "diffuseMap": {"stem": "transparent_glass_alpha"},
                                },
                                "floats": {},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            materials = [
                {
                    "material_id": "SHIPGLASS_PBS_Hull",
                    "shader_intent": "opaque_pbr",
                    "render_queue": "opaque",
                    "texture_sets": {},
                }
            ]

            resolved = _apply_material_mappings_json(materials, mapping_path, dds_dir)

            self.assertEqual(resolved, 1)
            self.assertEqual(materials[0]["shader_intent"], "transparent")
            self.assertEqual(materials[0]["render_queue"], "transparent")
            self.assertEqual(
                materials[0]["texture_sets"]["main"]["baseColor"]["dds_mips"],
                ["textures_dds/transparent_glass_alpha_a.dd0"],
            )

    def test_camouflages_xml_categories_include_hull_side_masks(self) -> None:
        entry = wg_camo.CamoEntry(
            name="camo_permanent_1",
            tiled=False,
            textures={
                "Hull": "content/gameplay/common/camouflage/textures/HullMask.dds",
                "DeckHouse": "content/gameplay/common/camouflage/textures/DeckMask.dds",
                "Gun": "content/gameplay/common/camouflage/textures/GunMask.dds",
            },
        )
        masks_mip_index = {
            "HullMask": ["HullMask.dds"],
            "DeckMask": ["DeckMask.dds"],
            "GunMask": ["GunMask.dds"],
        }

        categories = _merged_path_a_b_categories(entry, masks_mip_index, {})

        self.assertEqual(
            categories["tile"]["mask"]["dds_mips"],
            ["libraries/camo_masks/HullMask.dds"],
        )
        self.assertEqual(
            categories["deckhouse"]["mask"]["dds_mips"],
            ["libraries/camo_masks/DeckMask.dds"],
        )
        self.assertEqual(
            categories["gun"]["mask"]["dds_mips"],
            ["libraries/camo_masks/GunMask.dds"],
        )


if __name__ == "__main__":
    unittest.main()
