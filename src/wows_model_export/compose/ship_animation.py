"""Animation documents of a ship's hull GLBs.

Layer 4 composer over :mod:`.model_animation`. A ship's hull is several
models (the main model and its ``_Bow``, ``_MidFront``, ``_MidBack`` and
``_Stern`` sections); the toolkit merges them into one hull GLB whose mesh
nodes are named ``<model> / <render set>``. Some of those models load
animation state machines: carriers fold their radio masts, submarines open
their hatches and raise their periscopes, event hulls play idle loops.

For each hull GLB of a ship folder (``models/<label>_hull.glb`` and every
exterior's ``hull.hull_glb``), this module finds the models the GLB merges,
reads the state machines they load (their ``assets.bin`` records) and
writes one ``wows_animation/v1`` document for the GLB beside it,
``<glb stem>.animation.json``, with every state machine listed once and the
models that load it (``state_machines[].models``). The sidecar's
``animation`` section summarises each document (``METADATA_SPEC.md`` §18.7).

The toolkit draws a ship hull without its node tree (the GLB's nodes are
the meshes under ``Hull``, ``Armor`` and ``Hitboxes``), so no channel's
node is in a hull GLB: every clip lists its nodes in ``missing_nodes`` and
the sidecar block says ``nodes_in_glb: false``. The documents record what
the parts do and how long it takes; playing them needs a hull GLB that
keeps the animated nodes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .. import build_deps as _build_deps
from .._glb import parse_glb
from ..config import PipelineConfig
from ..resolve import sidecar as _sidecar
from . import model_animation

#: The sidecar section this module owns.
SECTION = "animation"

#: Separator between the model name and the render set in a hull GLB's
#: mesh node names (``JSA032_Ryujo_1944_MidBack / MidBack_AntennaShape``).
_NODE_SEP = " / "

_MODEL_INDEX: dict[str, tuple[tuple[float, int] | None, dict[str, list[str]]]] = {}
_MODEL_INDEX_LOCK = threading.Lock()


def _model_index(config: PipelineConfig) -> dict[str, list[str]]:
    """``{model file stem: [VFS paths of <stem>.model]}`` from the VFS
    manifest, cached per manifest file."""
    from ..toolkit import vfs as _vfs
    path = _vfs.ensure_manifest(config=config)
    key = str(Path(path).resolve())
    try:
        st = Path(path).stat()
        stamp: tuple[float, int] | None = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    with _MODEL_INDEX_LOCK:
        hit = _MODEL_INDEX.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    index: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for e in json.load(f):
            p = (e.get("path") or "").lstrip("/") if isinstance(e, dict) else ""
            if p.endswith(".model"):
                index.setdefault(p.rsplit("/", 1)[-1][: -len(".model")], []).append(p)
    with _MODEL_INDEX_LOCK:
        _MODEL_INDEX[key] = (stamp, index)
    return index


def glb_model_stems(gltf: dict) -> list[str]:
    """The models a merged hull GLB holds, from its mesh node names."""
    stems = {
        n["name"].split(_NODE_SEP, 1)[0]
        for n in gltf.get("nodes") or []
        if "mesh" in n and isinstance(n.get("name"), str) and _NODE_SEP in n["name"]
    }
    return sorted(stems)


def _model_paths(stems: list[str], index: dict[str, list[str]], warnings: list[str]) -> list[str]:
    out: list[str] = []
    for stem in stems:
        paths = sorted(index.get(stem) or [])
        if not paths:
            warnings.append(f"model {stem} is not in the VFS manifest")
            continue
        # A ship's models live under content/gameplay/; prefer those when
        # another tree reuses the name.
        gameplay = [p for p in paths if p.startswith("content/gameplay/")]
        out.append((gameplay or paths)[0])
    return out


def _main_model(models: list[str]) -> str | None:
    """The hull's main model: the one whose name prefixes every other."""
    stems = {m: m.rsplit("/", 1)[-1][: -len(".model")] for m in models}
    for m, s in stems.items():
        if all(o == s or o.startswith(s + "_") for o in stems.values()):
            return m
    return None


def build_hull_document(
    glb: Path,
    *,
    config: PipelineConfig,
    assets_bin: Path,
    warnings: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The animation document of one hull GLB (None when none of its
    models loads a state machine) and counts for the caller's report."""
    from ..read import model_prototypes

    gltf, _bin = parse_glb(glb.read_bytes())
    models = _model_paths(glb_model_stems(gltf), _model_index(config), warnings)
    machines = model_prototypes.model_state_machines(assets_bin, models)
    loaded_by: dict[str, list[str]] = {}
    for m in models:
        for s in machines.get(m) or []:
            loaded_by.setdefault(s.replace("\\", "/").lstrip("/"), []).append(m)
    stats = {"models": len(models), "animated_models": sum(1 for m in models if machines.get(m)),
             "clips": 0, "missing_node_clips": 0}
    if not loaded_by:
        return None, stats
    root = model_animation.cache_root(config)
    model_animation.prefetch(sorted(loaded_by), root, config)
    main = _main_model(models) or next(m for m in models if machines.get(m))
    doc, problems = model_animation.build_document(
        main, sorted(loaded_by), gltf=gltf, root=root, config=config, glb_name=glb.name,
    )
    for p in problems:
        if not (p.startswith("clip ") and ": nodes not in the GLB: " in p):
            warnings.append(f"{glb.name}: animation: {p}")
    if doc is None:
        return None, stats
    # The document's game inputs, read from the pipeline cache when an
    # earlier run extracted them: record them for the export's build deps.
    inputs = set(loaded_by)
    for m in doc["state_machines"]:
        for n in m["nodes"]:
            if n["type"] == "sequence" and n.get("resource"):
                inputs.add(n["resource"])
    inputs.update(c["source_vfs"] for c in doc["clips"].values())
    for path in sorted(inputs):
        _build_deps.record_file(path)
    for m in doc["state_machines"]:
        m["models"] = loaded_by.get(m["vfs"], [])
    stats["clips"] = len(doc["clips"])
    stats["missing_node_clips"] = sum(1 for c in doc["clips"].values() if c["missing_nodes"])
    return doc, stats


def _hull_block(doc: dict[str, Any], doc_path: Path, glb_rel: str, ship_dir: Path) -> dict[str, Any]:
    block = model_animation.animation_block(doc, doc_path.relative_to(ship_dir).as_posix())
    block["glb"] = glb_rel
    block["models"] = sorted({m for sm in doc["state_machines"] for m in sm.get("models") or []})
    block["nodes_in_glb"] = all(not c["missing_nodes"] for c in doc["clips"].values())
    return block


def animate_ship(
    ship_dir: Path,
    *,
    config: PipelineConfig | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Write the animation documents of a ship folder's hull GLBs and the
    sidecar's ``animation`` section (removed when no hull GLB animates).

    Returns ``{"hulls": n documents, "clips": n, "missing_node_clips": n,
    "written": bool}``. Raises on an unreadable sidecar; a hull GLB that
    fails adds a warning and gets no block.
    """
    from ..toolkit import assets_bin as _assets_bin

    cfg = config or PipelineConfig.load()
    warn = warnings if warnings is not None else []
    ship_dir = Path(ship_dir)
    label = ship_dir.name
    sidecar_path = ship_dir / f"{label}.meta.json"
    doc = _sidecar.read(sidecar_path)
    targets: list[tuple[str | None, str]] = [(None, f"models/{label}_hull.glb")]
    for ext in doc.get("exteriors") or []:
        hull = ext.get("hull") if isinstance(ext, dict) else None
        rel = hull.get("hull_glb") if isinstance(hull, dict) else None
        if isinstance(rel, str) and rel and ext.get("exterior_id"):
            targets.append((ext["exterior_id"], rel))
    assets_bin = _assets_bin.ensure_dump(config=cfg)
    section: dict[str, Any] = {}
    totals = {"hulls": 0, "clips": 0, "missing_node_clips": 0, "written": False}
    for ext_id, rel in targets:
        glb = ship_dir / rel
        doc_path = glb.with_name(f"{glb.stem}.animation.json")
        if not glb.is_file():
            continue
        try:
            adoc, stats = build_hull_document(glb, config=cfg, assets_bin=assets_bin, warnings=warn)
        except Exception as e:  # noqa: BLE001 - one hull's failure keeps the others
            warn.append(f"{rel}: animation failed: {e}")
            continue
        if adoc is None:
            doc_path.unlink(missing_ok=True)
            continue
        model_animation.write_document(adoc, doc_path)
        block = _hull_block(adoc, doc_path, rel, ship_dir)
        if ext_id is None:
            section["hull"] = block
        else:
            section.setdefault("exteriors", {})[ext_id] = block
        totals["hulls"] += 1
        totals["clips"] += stats["clips"]
        totals["missing_node_clips"] += stats["missing_node_clips"]
    new = dict(doc)
    if section:
        new[SECTION] = section
    else:
        new.pop(SECTION, None)
    if new != doc:
        _sidecar.write(new, sidecar_path)
        totals["written"] = True
    return totals


__all__ = ["SECTION", "animate_ship", "build_hull_document", "glb_model_stems"]
