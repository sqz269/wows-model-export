"""Model animation documents (``wows_animation/v1``).

Layer 4 helper shared by the libraries: the state machines a WoWS model
loads (its ``.model`` record in ``assets.bin``), parsed from their
``.ssm`` files, and every clip their ``animation`` nodes play, decoded
from the ``.anim`` files and converted to the frame of the model's GLB
(metres, right-handed, Z negated from BigWorld). Each channel targets a
GLB node by name and holds that node's local translation, rotation or
scale, so a consumer can build a clip for any scene that keeps the GLB's
node names and rest transforms.

The document is written beside the model's GLB as
``<asset_id>.animation.json``; the library index carries a summary of it.
Specified in ``reference/schemas/METADATA_SPEC.md`` §18.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

from ..config import PipelineConfig
from ..read import bw_animation

#: Document schema identifier (bump on a breaking change).
ANIMATION_SCHEMA = "wows_animation/v1"

#: Cache folder (under the pipeline cache) for the extracted ``.ssm`` and
#: ``.anim`` files, one subfolder per game build.
CACHE_DIR = "animation"

#: A clip "starts / ends at rest" when every channel's first / last key is
#: within these of the node's rest transform in the GLB.
REST_TOLERANCE = {"translation": 0.005, "rotation_deg": 0.5, "scale": 1e-3}


def cache_root(config: PipelineConfig) -> Path:
    from ..toolkit import build as _build
    build = _build.current_build(config)
    return Path(config.cache_dir) / CACHE_DIR / str(build or "unknown")


def fetch_vfs(paths: list[str], root: Path, config: PipelineConfig | None) -> dict[str, Path | None]:
    """Local copies of VFS files under ``root``: extracts the missing ones in
    one toolkit call. Maps each path (no leading slash) to its file, or None
    when the VFS has no such file."""
    rels = sorted({p.replace("\\", "/").lstrip("/") for p in paths if p})
    missing = [r for r in rels if not (root / r).is_file()]
    if missing:
        from ..toolkit import vfs as _vfs
        root.mkdir(parents=True, exist_ok=True)
        # The toolkit matches VFS paths rooted at "/" and writes them without it.
        _vfs.extract(["/" + r for r in missing], root, config=config, record_deps=True)
    return {r: (root / r if (root / r).is_file() else None) for r in rels}


def prefetch(state_machines: list[str], root: Path, config: PipelineConfig | None) -> None:
    """Extract the state machines and every clip they play in two toolkit
    calls, so the documents of many models build from the cache."""
    files = fetch_vfs(state_machines, root, config)
    clips: list[str] = []
    for f in files.values():
        if f is None:
            continue
        try:
            graph = bw_animation.parse_state_machine(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - reported by build_document
            continue
        clips.extend(n["clip_vfs"] for n in graph["nodes"] if n["type"] == "animation" and n.get("clip_vfs"))
    if clips:
        fetch_vfs(clips, root, config)


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    x, y, z, w = node.get("rotation") or (0.0, 0.0, 0.0, 1.0)
    s = np.array(node.get("scale") or (1.0, 1.0, 1.0), dtype=np.float64)
    r = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m = np.eye(4)
    m[:3, :3] = r * s[None, :]
    m[:3, 3] = node.get("translation") or (0.0, 0.0, 0.0)
    return m


def _quat_from_matrix(r: np.ndarray) -> list[float]:
    tr = float(np.trace(r))
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = [(r[2, 1] - r[1, 2]) / s, (r[0, 2] - r[2, 0]) / s, (r[1, 0] - r[0, 1]) / s, 0.25 * s]
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2
        q = [0.25 * s, (r[0, 1] + r[1, 0]) / s, (r[0, 2] + r[2, 0]) / s, (r[2, 1] - r[1, 2]) / s]
    elif r[1, 1] > r[2, 2]:
        s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2
        q = [(r[0, 1] + r[1, 0]) / s, 0.25 * s, (r[1, 2] + r[2, 1]) / s, (r[0, 2] - r[2, 0]) / s]
    else:
        s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2
        q = [(r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s, 0.25 * s, (r[1, 0] - r[0, 1]) / s]
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return [float(x) / n for x in q]


def rest_pose(gltf: dict) -> dict[str, dict[str, list[float]]]:
    """``{node name: {"translation", "rotation", "scale"}}`` of the GLB's
    non-mesh nodes, from their TRS or decomposed from their matrix (a
    negative determinant puts the reflection on scale Z)."""
    out: dict[str, dict[str, list[float]]] = {}
    for n in gltf.get("nodes") or []:
        name = n.get("name")
        if not name or "mesh" in n or name in out:
            continue
        m = _node_matrix(n)
        r = m[:3, :3]
        s = np.linalg.norm(r, axis=0)
        if np.linalg.det(r) < 0:
            s[2] = -s[2]
        r = r / np.where(np.abs(s) > 1e-12, s, 1.0)
        out[name] = {
            "translation": [float(x) for x in m[:3, 3]],
            "rotation": _quat_from_matrix(r),
            "scale": [float(x) for x in s],
        }
    return out


def _near_rest(path: str, v: list[float], rest: dict[str, list[float]]) -> bool:
    if path == "rotation":
        d = abs(sum(a * b for a, b in zip(v, rest["rotation"], strict=True)))
        return 2 * math.degrees(math.acos(min(1.0, d))) <= REST_TOLERANCE["rotation_deg"]
    tol = REST_TOLERANCE["translation" if path == "translation" else "scale"]
    return max(abs(a - b) for a, b in zip(v, rest[path], strict=True)) <= tol


def build_document(
    model_vfs: str,
    state_machines: list[str],
    *,
    gltf: dict,
    root: Path,
    config: PipelineConfig | None,
    glb_name: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The animation document of one model, or None when it loads no state
    machine. Returns ``(document, problems)``; a problem is a state machine
    or clip that could not be read, or a channel whose node the GLB lacks
    (kept, and listed in the clip's ``missing_nodes``)."""
    problems: list[str] = []
    if not state_machines:
        return None, problems
    files = fetch_vfs(state_machines, root, config)
    machines: list[dict[str, Any]] = []
    clips: dict[str, dict[str, Any]] = {}
    pending: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for ssm in state_machines:
        rel = ssm.replace("\\", "/").lstrip("/")
        f = files.get(rel)
        if f is None:
            problems.append(f"state machine {rel} is not in the VFS")
            continue
        try:
            graph = bw_animation.parse_state_machine(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"state machine {rel}: {e}")
            continue
        machines.append({"vfs": rel, **graph})
        for node in graph["nodes"]:
            if node["type"] == "animation" and node.get("clip_vfs"):
                pending.append((machines[-1], node, rel))
    clip_files = fetch_vfs([n["clip_vfs"] for _, n, _ in pending], root, config)
    rest = rest_pose(gltf)
    for machine, node, ssm_rel in pending:
        key = node["name"]
        if key in clips:
            key = f"{Path(ssm_rel).stem}/{node['name']}"
        node["clip"] = key
        src = node["clip_vfs"]
        f = clip_files.get(src)
        if f is None:
            problems.append(f"{machine['vfs']}: clip {src} is not in the VFS")
            node["clip"] = None
            continue
        try:
            clip = bw_animation.parse_anim(f.read_bytes())
            channels, duration = bw_animation.gltf_channels(clip, take=node.get("take"))
        except (bw_animation.AnimFormatError, KeyError) as e:
            problems.append(f"{machine['vfs']}: clip {src}: {e}")
            node["clip"] = None
            continue
        missing = sorted({c["node"] for c in channels if c["node"] not in rest})
        if missing:
            problems.append(f"clip {key}: nodes not in the GLB: {missing}")
        known = [c for c in channels if c["node"] in rest]
        starts = all(_near_rest(c["path"], c["values"][0], rest[c["node"]]) for c in known)
        ends = all(_near_rest(c["path"], c["values"][-1], rest[c["node"]]) for c in known)
        clips[key] = {
            "source_vfs": src,
            "take": node.get("take"),
            "fps": clip.fps,
            "frame_count": clip.frame_count,
            "duration_s": duration,
            "loop": bool(node.get("loop")),
            "nodes": sorted({c["node"] for c in channels}),
            "missing_nodes": missing,
            "starts_at_rest": bool(known) and starts,
            "ends_at_rest": bool(known) and ends,
            "channels": channels,
        }
    if not machines:
        return None, problems
    doc = {
        "schema": ANIMATION_SCHEMA,
        "model_vfs": model_vfs.replace("\\", "/").lstrip("/"),
        "glb": glb_name,
        "frame": {"units": "m", "handedness": "right", "from_bigworld": "z negated, native x 15",
                  "values": "node-local"},
        "state_machines": machines,
        "clips": clips,
    }
    return doc, problems


def write_document(doc: dict[str, Any], path: Path) -> bool:
    """Write ``doc`` as JSON; returns False when the file already holds it."""
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def clip_summary(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The index summary of each clip: everything but the channels, and the
    state machine trigger(s) whose transitions enter it."""
    triggers: dict[str, list[str]] = {}
    for m in doc.get("state_machines") or []:
        by_node = {n["name"]: n for n in m["nodes"]}
        for n in m["nodes"]:
            for tr in n["transitions"]:
                target = by_node.get(tr["to"]) or {}
                key = target.get("clip")
                if not key:
                    continue
                for c in tr["conditions"]:
                    if c.get("type") == "trigger" and c.get("name") not in triggers.setdefault(key, []):
                        triggers[key].append(c["name"])
    out: dict[str, dict[str, Any]] = {}
    for key, c in (doc.get("clips") or {}).items():
        s = {k: v for k, v in c.items() if k != "channels"}
        s["triggers"] = triggers.get(key, [])
        out[key] = s
    return out


def validate_document(doc: Any, gltf: dict) -> list[str]:
    """Problems of an animation document against its GLB: schema, every
    state machine node's clip present, every channel on a GLB node (or
    listed missing), a known path, rising times inside the clip, values of
    the path's width, unit quaternions."""
    problems: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema") != ANIMATION_SCHEMA:
        return [f"schema is {doc.get('schema') if isinstance(doc, dict) else type(doc).__name__!r}"]
    nodes = {n.get("name") for n in gltf.get("nodes") or [] if "mesh" not in n}
    clips = doc.get("clips") or {}
    for m in doc.get("state_machines") or []:
        names = {n.get("name") for n in m.get("nodes") or []}
        if m.get("entry") not in names:
            problems.append(f"{m.get('vfs')}: entry {m.get('entry')!r} is not a node")
        for n in m.get("nodes") or []:
            if n.get("type") == "animation" and n.get("clip") not in clips:
                problems.append(f"{m.get('vfs')}: node {n.get('name')} plays missing clip {n.get('clip')!r}")
            for tr in n.get("transitions") or []:
                if tr.get("to") not in names:
                    problems.append(f"{m.get('vfs')}: transition to unknown node {tr.get('to')!r}")
    width = {"translation": 3, "rotation": 4, "scale": 3}
    for key, c in clips.items():
        listed = set(c.get("missing_nodes") or [])
        for ch in c.get("channels") or []:
            tag = f"clip {key} {ch.get('node')}.{ch.get('path')}"
            if ch.get("node") not in nodes and ch.get("node") not in listed:
                problems.append(f"{tag}: node not in the GLB")
            if ch.get("path") not in width:
                problems.append(f"{tag}: unknown path")
                continue
            t, v = ch.get("times") or [], ch.get("values") or []
            if not t or len(t) != len(v):
                problems.append(f"{tag}: {len(t)} times for {len(v)} values")
            if any(b < a for a, b in zip(t, t[1:], strict=False)):
                problems.append(f"{tag}: times fall")
            if t and (t[0] < -1e-6 or t[-1] > (c.get("duration_s") or 0) + 1e-4):
                problems.append(f"{tag}: times {t[0]}..{t[-1]} outside the clip")
            if any(len(x) != width[ch["path"]] for x in v):
                problems.append(f"{tag}: value width")
            elif ch["path"] == "rotation" and any(abs(math.sqrt(sum(a * a for a in q)) - 1) > 2e-3 for q in v):
                problems.append(f"{tag}: quaternion not unit length")
    return problems


__all__ = [
    "ANIMATION_SCHEMA",
    "CACHE_DIR",
    "REST_TOLERANCE",
    "build_document",
    "cache_root",
    "clip_summary",
    "fetch_vfs",
    "prefetch",
    "rest_pose",
    "validate_document",
    "write_document",
]
