"""Ship / sub-model export subcommands of `wowsunpack`.

Three callables:

- `export_ship`     — full ship → single GLB (+ optional placements,
                      skel_ext candidates, textures, material mappings,
                      raw DDS dir).
- `export_model`    — single accessory / sub-model from a VFS
                      `.geometry` path → GLB.
- `batch_export_model` — many sub-models in ONE wowsunpack invocation;
                      amortizes the per-call `assets.bin` parse cost
                      across all items.

`export_ship` applies a post-process winding flip to `Armor_*` and
`CM_SB_*` meshes in the output GLB — those are emitted by the toolkit
with inverted triangle order, and downstream raycasters expect the
WG/glTF convention. The flip is local to ship exports; `export_model`
does not run it (single-asset GLBs don't carry armor / hitbox meshes).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from .._glb import flip_winding, parse_glb, write_glb
from ..config import PipelineConfig
from ..errors import ToolkitError
from ..types import ToolkitResult
from ._subprocess import run_toolkit


def export_ship(
    ship: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
    hull: str | None = None,
    lod: int = 0,
    no_textures: bool = True,
    damaged: bool = False,
    all_render_sets: bool = False,
    accessories: Literal["embed", "main-only", "exclude"] = "embed",
    placements_json: Path | str | os.PathLike | None = None,
    skel_ext_candidates_json: Path | str | os.PathLike | None = None,
    textures_dir: Path | str | os.PathLike | None = None,
    textures_uri_prefix: str | None = None,
    raw_dds_dir: Path | str | os.PathLike | None = None,
    material_mappings_json: Path | str | os.PathLike | None = None,
) -> ToolkitResult:
    """Export a full ship to a single GLB.

    Hull parts keep their WG names (``ASB017_Montana_1945_Bow``); accessory
    mounts are named by hardpoint (``HP_AGM_1 (AGM034_16in50_Mk7)``);
    armor models are ``Armor_Bow``, ``Armor_Citadel``, etc.

    ``ship`` may be a model-dir name (``JSB039_Yamato_1945``) or a
    translated display name (``Yamato``) — wowsunpack does fuzzy lookup.

    ``accessories`` controls which mounts are embedded in the GLB:
        - ``"embed"`` (default): every mount baked in. Largest output.
        - ``"main-only"``: keep Main Battery turrets embedded
          (per-ship rigging case); leave secondaries / AA / directors
          / radar to the shared accessory library at runtime.
        - ``"exclude"``: hull + armor only; no accessory meshes. Every
          mount comes from the shared library.

    ``all_render_sets``: when True, each sub-model emits one named mesh
    per render set — every LOD + intact + damaged variant — scoped
    under the sub-model name. Ignores ``lod`` / ``damaged``.

    Post-process: ``Armor_*`` and ``CM_SB_*`` meshes get their triangle
    winding reversed in-place after export (toolkit emits them with
    inverted order; downstream collider raycasts expect the standard
    convention). Idempotent at the call site: re-running `export_ship`
    rebuilds the GLB from scratch, so the flip is applied once per
    fresh export.

    `ToolkitResult.output_paths` includes the GLB and any opt-in side
    files that were requested.
    """
    if accessories not in ("embed", "main-only", "exclude"):
        raise ValueError(
            f"accessories={accessories!r}; expected one of "
            f"'embed', 'main-only', 'exclude'"
        )
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "--game-dir", str(cfg.require_game_dir()),
        "export-ship", ship,
        "--output", str(out),
        "--lod", str(lod),
        "--accessories", accessories,
    ]
    if hull is not None:
        argv += ["--hull", hull]
    if no_textures:
        argv.append("--no-textures")
    if damaged:
        argv.append("--damaged")
    if all_render_sets:
        argv.append("--all-render-sets")

    expected: list[Path] = [out]

    if placements_json is not None:
        placements_out = Path(placements_json).resolve()
        placements_out.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--placements-json", str(placements_out)]
        expected.append(placements_out)
    if skel_ext_candidates_json is not None:
        skel_ext_out = Path(skel_ext_candidates_json).resolve()
        skel_ext_out.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--skel-ext-candidates-json", str(skel_ext_out)]
        expected.append(skel_ext_out)
    if textures_dir is not None:
        tex_dir = Path(textures_dir).resolve()
        tex_dir.mkdir(parents=True, exist_ok=True)
        argv += ["--textures-dir", str(tex_dir)]
        if textures_uri_prefix is not None:
            argv += ["--textures-uri-prefix", textures_uri_prefix]
    if raw_dds_dir is not None:
        dds_dir = Path(raw_dds_dir).resolve()
        dds_dir.mkdir(parents=True, exist_ok=True)
        argv += ["--raw-dds-dir", str(dds_dir)]
    if material_mappings_json is not None:
        mat_map_out = Path(material_mappings_json).resolve()
        mat_map_out.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--material-mappings-json", str(mat_map_out)]
        expected.append(mat_map_out)

    result = run_toolkit(argv, config=cfg, expect_outputs=tuple(expected))

    # Post-export: flip winding on armor + hitbox cube meshes (see
    # module docstring for rationale). Re-pack timing into the returned
    # ToolkitResult so callers can see total wall time, not just the
    # subprocess slice.
    import time as _t
    t0 = _t.perf_counter()
    _flip_armor_hitbox_winding(out)
    flip_ms = (_t.perf_counter() - t0) * 1000.0

    return ToolkitResult(
        output_paths=result.output_paths,
        stderr=result.stderr,
        elapsed_ms=result.elapsed_ms + flip_ms,
    )


def export_model(
    geometry_vfs_path: str,
    out_path: Path | str | os.PathLike,
    *,
    config: PipelineConfig | None = None,
    lod: int = 0,
    no_textures: bool = True,
    damaged: bool = False,
    all_render_sets: bool = False,
    textures_dir: Path | str | os.PathLike | None = None,
    textures_uri_prefix: str | None = None,
    raw_dds_dir: Path | str | os.PathLike | None = None,
    material_mappings_json: Path | str | os.PathLike | None = None,
    skel_ext_candidates_json: Path | str | os.PathLike | None = None,
) -> ToolkitResult:
    """Export a single accessory / sub-model to GLB.

    ``geometry_vfs_path`` is the VFS path to the ``.geometry`` file,
    e.g.
    ``content/gameplay/usa/gun/main/AGM034_16in50_Mk7/AGM034_16in50_Mk7.geometry``.
    The toolkit auto-resolves the paired ``.visual`` in ``assets.bin``
    and applies its VisualPrototype node transforms to the output.

    ``skel_ext_candidates_json``: when set, the toolkit also writes a
    per-asset skel_ext candidates JSON describing accessory-attached
    misc placements bundled into this asset (rangefinders / periscopes
    / ammo boxes / cranes / etc. mounted on a turret roof, director
    housing, …). Silent no-op when the source asset has no sibling
    ``<stem>.skel_ext``.
    """
    cfg = config or PipelineConfig.load()

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "--game-dir", str(cfg.require_game_dir()),
        "export-model", geometry_vfs_path,
        "--output", str(out),
        "--lod", str(lod),
    ]
    if no_textures:
        argv.append("--no-textures")
    if damaged:
        argv.append("--damaged")
    if all_render_sets:
        argv.append("--all-render-sets")

    expected: list[Path] = [out]

    if textures_dir is not None:
        tex_dir = Path(textures_dir).resolve()
        tex_dir.mkdir(parents=True, exist_ok=True)
        argv += ["--textures-dir", str(tex_dir)]
        if textures_uri_prefix is not None:
            argv += ["--textures-uri-prefix", textures_uri_prefix]
    if raw_dds_dir is not None:
        dds_dir = Path(raw_dds_dir).resolve()
        dds_dir.mkdir(parents=True, exist_ok=True)
        argv += ["--raw-dds-dir", str(dds_dir)]
    if material_mappings_json is not None:
        mm_path = Path(material_mappings_json).resolve()
        mm_path.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--material-mappings-json", str(mm_path)]
        expected.append(mm_path)
    if skel_ext_candidates_json is not None:
        sx_path = Path(skel_ext_candidates_json).resolve()
        sx_path.parent.mkdir(parents=True, exist_ok=True)
        argv += ["--skel-ext-candidates-json", str(sx_path)]
        # NOTE: skel_ext candidates JSON is *only* written when the
        # source `.geometry` has a sibling `.skel_ext`. Many accessories
        # don't (the file is per-host, not per-asset). So we don't add
        # it to expect_outputs — a missing file isn't a failure here.

    return run_toolkit(argv, config=cfg, expect_outputs=tuple(expected))


def batch_export_model(
    items: list[dict],
    *,
    shared: dict | None = None,
    config: PipelineConfig | None = None,
    keep_going: bool = True,
) -> ToolkitResult:
    """Export many sub-models in ONE wowsunpack invocation.

    Amortizes the per-invocation ``assets.bin`` parse cost (~5-10s)
    across all items — turns O(N × 10s) sequential `export_model` calls
    into O(10s + N × 0.3s).

    Item shape:
        {
          "geometry":                 "<VFS path to .geometry>",
          "output":                   "<disk path for output GLB>",
          "textures_dir":             "<disk path or null>",
          "raw_dds_dir":              "<disk path or null>",
          "material_mappings_json":   "<disk path or null>",
          "skel_ext_candidates_json": "<disk path or null>"
        }

    ``shared`` is an optional dict of flags applied to every item:
        {
          "all_render_sets":     True/False,
          "no_textures":         True/False,
          "damaged":             True/False,
          "lod":                 0,
          "textures_uri_prefix": "textures/"
        }

    Writes the manifest to a temp JSON, invokes
    ``batch-export-model``, raises ``ToolkitError`` on overall failure.
    Per-item failures are logged to the toolkit's stdout but do not
    abort when ``keep_going=True``.

    The returned `ToolkitResult.output_paths` lists every requested
    output GLB (NOT the side files). Callers can inspect on-disk for
    detailed per-item status; future versions may parse the toolkit's
    per-item status lines into structured data.
    """
    cfg = config or PipelineConfig.load()

    # Normalize paths to strings — WG VFS is case-sensitive, so we pass
    # through without mangling.
    normalized_items: list[dict] = []
    output_paths: list[Path] = []
    for it in items:
        entry: dict = {
            "geometry": str(it["geometry"]),
            "output":   str(it["output"]),
        }
        output_paths.append(Path(it["output"]).resolve())
        if it.get("textures_dir"):
            entry["textures_dir"] = str(it["textures_dir"])
            Path(entry["textures_dir"]).mkdir(parents=True, exist_ok=True)
        if it.get("raw_dds_dir"):
            entry["raw_dds_dir"] = str(it["raw_dds_dir"])
            Path(entry["raw_dds_dir"]).mkdir(parents=True, exist_ok=True)
        if it.get("material_mappings_json"):
            entry["material_mappings_json"] = str(it["material_mappings_json"])
            Path(entry["material_mappings_json"]).parent.mkdir(parents=True, exist_ok=True)
        if it.get("skel_ext_candidates_json"):
            entry["skel_ext_candidates_json"] = str(it["skel_ext_candidates_json"])
            Path(entry["skel_ext_candidates_json"]).parent.mkdir(parents=True, exist_ok=True)
        normalized_items.append(entry)

    manifest = {
        "shared": shared or {},
        "items":  normalized_items,
    }

    # Manifest goes to a temp file; toolkit reads + reports per-item.
    fd, manifest_path = tempfile.mkstemp(suffix=".batch.json", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False)
        argv = [
            "--game-dir", str(cfg.require_game_dir()),
            "batch-export-model", manifest_path,
        ]
        if not keep_going:
            argv += ["--keep-going", "false"]
        # We don't pass expect_outputs because per-item failures with
        # keep_going=True still exit 0; callers should walk
        # output_paths themselves to see what actually landed.
        return run_toolkit(argv, config=cfg)
    finally:
        try:
            os.unlink(manifest_path)
        except OSError:
            pass


# ── post-export winding flip ──────────────────────────────────────────


_HULL_FLIP_PREFIXES = ("Armor_", "CM_SB_")


def _flip_armor_hitbox_winding(glb_path: Path) -> None:
    """Reverse triangle winding for `Armor_*` and `CM_SB_*` meshes in a
    hull GLB. Called from `export_ship` after a successful export.

    The toolkit emits armor + splash-box hitbox cubes with inverted
    triangle winding; downstream raycasters expect the standard glTF
    convention. Flip in place so downstream consumers don't each
    implement the same fixup.

    Same algorithm as the webview's runtime `flipWinding(mesh)`:
    index[1] ↔ index[2] per triangle.

    NOT idempotent at the call site (no marker is written), so this
    must only be invoked on a fresh export. `export_ship` is the only
    caller and runs it exactly once per successful run.
    """
    data = glb_path.read_bytes()
    gltf, bin_data = parse_glb(data)
    new_bin, _report = flip_winding(
        gltf, bin_data,
        mesh_filter=lambda m: (m.get("name") or "").startswith(_HULL_FLIP_PREFIXES),
    )
    write_glb(gltf, new_bin, glb_path)


def _missing(path: Path | None) -> bool:
    """Internal: True iff `path` is not None and doesn't exist on disk."""
    return path is not None and not Path(path).is_file()


# Re-exported for symmetry with the I:-side import surface.
__all__ = ["export_ship", "export_model", "batch_export_model"]

# Defensive: surface ToolkitError for `import *` consumers that catch it.
_ = ToolkitError
