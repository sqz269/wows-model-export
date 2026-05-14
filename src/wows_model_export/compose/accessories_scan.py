"""Compose `scan_legacy_glb` -- legacy gamemodels3d.com GLB scanner.

Lifted from ``tools/ship/accessories_scan.py`` on the I:-side warships
repo.  This is the Layer 4 orchestrator that walks a legacy ``*_visual.glb``
(produced by the retired gamemodels3d.com extractor) and reports every
shared BigWorld asset placement (bollards, ventilators, hatches,
capstans, rangefinders, ...) along with its per-instance world transform.

The output JSON (``<Ship>_accessories_scan.json``) used to feed the
``legacy-direct`` / ``legacy-anchor`` modes of
:mod:`wows_model_export.compose.skel_ext_resolve`.  Those legacy modes
were retired 2026-05-10 once every in-tree ship migrated to hash mode;
the scan composer is kept around so old or out-of-tree ships that still
need the legacy anchor can produce the input.

Canonical :class:`StepEvent` names emitted at step boundaries:

    "parse_glb"          -- read the GLB JSON chunk + build the scene tree
    "walk_hardpoints"    -- iterate nodes; match the asset-path naming
                            convention; compose per-node world transforms
    "write_scan_json"    -- emit the per-ship scan JSON

Per-step failures are wrapped in :class:`StepError` with ``step=`` set
to one of the names above; ``raise ... from e`` preserves the chain.

Input node-name convention (see
``tools/reference/ships/accessories_findings.md`` on the I: side):

    common/{visual|portvisual}/<scope>/<category>/<asset_id>/<asset_id>

with an optional depth-7 form ``.../<category>/<subcategory>/<asset_id>/
<asset_id>`` for guns split by mount type and radars split by role.
Nodes whose ``category == 'ship'`` are the ship's own hull segments and
are filtered out.
"""
from __future__ import annotations

import json
import re
import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..errors import StepError
from ..types import OnEvent, StepEvent

# glTF 2.0 chunk-type codes; JSON chunk is mandatory, BIN is optional.
CHUNK_JSON = 0x4E4F534A  # "JSON" little-endian

# Asset-path node shape.  Final two components must be identical -- BigWorld
# convention: the node's directory name equals its leaf name.  Depth-6 for
# bollards / directors / catapults; depth-7 for guns + radars split by
# subcategory.
PATH_RE = re.compile(
    r"^common/(?:visual|portvisual)/"
    r"(?P<scope>[^/]+)/"
    r"(?P<category>[^/]+)/"
    r"(?:(?P<subcategory>[^/]+)/)?"
    r"(?P<asset_id>[^/]+)/"
    r"(?P=asset_id)$"
)

SHIP_CATEGORY = "ship"  # excluded -- those are hull segments


# ---------------------------------------------------------------------------
# Step emitter (mirrors the convention in other compose modules)
# ---------------------------------------------------------------------------


class _StepRunner:
    """Wraps ``on_event`` + step timing + :class:`StepError` raising."""

    def __init__(self, on_event: OnEvent | None) -> None:
        self.on_event = on_event
        self.t0 = time.monotonic()
        self.step_timings_ms: dict[str, float] = {}

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0

    def emit(
        self,
        step: str,
        state: str,
        *,
        detail: str = "",
        step_ms: float | None = None,
        data: dict | None = None,
    ) -> None:
        if self.on_event is None:
            return
        ev = StepEvent(
            step=step,
            state=state,  # type: ignore[arg-type]
            detail=detail,
            elapsed_ms=self._elapsed_ms(),
            step_ms=step_ms,
            data=data,
        )
        try:
            self.on_event(ev)
        except Exception:
            pass

    def step(self, step: str, detail: str = "") -> _StepCtx:
        return _StepCtx(self, step, detail)


class _StepCtx:
    def __init__(self, runner: _StepRunner, step: str, detail: str) -> None:
        self.runner = runner
        self.step = step
        self.detail = detail
        self.t_start = 0.0
        self.completed_detail = ""
        self.completed_data: dict | None = None

    def __enter__(self) -> _StepCtx:
        self.t_start = time.monotonic()
        self.runner.emit(self.step, "started", detail=self.detail)
        return self

    def annotate(self, detail: str, data: dict | None = None) -> None:
        self.completed_detail = detail
        if data is not None:
            self.completed_data = data

    def __exit__(self, exc_type, exc, tb) -> bool:
        step_ms = (time.monotonic() - self.t_start) * 1000.0
        self.runner.step_timings_ms[self.step] = step_ms
        if exc is None:
            self.runner.emit(
                self.step, "completed",
                detail=self.completed_detail or self.detail,
                step_ms=step_ms, data=self.completed_data,
            )
            return False
        self.runner.emit(
            self.step, "failed",
            detail=f"{type(exc).__name__}: {exc}",
            step_ms=step_ms,
        )
        if isinstance(exc, StepError):
            return False
        raise StepError(
            step=self.step,
            underlying=exc,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# GLB reading + scene-walk helpers (lifted verbatim)
# ---------------------------------------------------------------------------


def load_gltf_json(glb_path: Path | str) -> dict:
    """Parse the JSON chunk from a binary ``.glb`` file.  Ignores the BIN
    chunk (we only need node metadata, not vertex buffers).
    """
    with open(glb_path, "rb") as f:
        magic, version, _total_len = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF":
            raise ValueError(f"not a glTF binary: {glb_path}")
        if version != 2:
            raise ValueError(f"unsupported glTF version {version}: {glb_path}")
        chunk_len, chunk_type = struct.unpack("<II", f.read(8))
        if chunk_type != CHUNK_JSON:
            raise ValueError(f"first chunk is not JSON: {glb_path}")
        return json.loads(f.read(chunk_len).decode("utf-8"))


# 4x4 column-major matrix math.  glTF convention: ``elements[col * 4 + row]``.


def _identity() -> list[float]:
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _mat4_mul(a: list[float], b: list[float]) -> list[float]:
    """Return ``a @ b`` in column-major layout."""
    out = [0.0] * 16
    for i in range(4):         # result row
        for j in range(4):     # result col
            s = 0.0
            for k in range(4):
                s += a[k * 4 + i] * b[j * 4 + k]
            out[j * 4 + i] = s
    return out


def _compose_trs(t: list[float], r: list[float], s: list[float]) -> list[float]:
    """Build column-major ``M = T * R * S`` from glTF (translation, quat
    xyzw, scale).
    """
    x, y, z, w = r
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        (1 - 2 * (yy + zz)) * s[0], (2 * (xy + wz)) * s[0], (2 * (xz - wy)) * s[0], 0.0,
        (2 * (xy - wz)) * s[1], (1 - 2 * (xx + zz)) * s[1], (2 * (yz + wx)) * s[1], 0.0,
        (2 * (xz + wy)) * s[2], (2 * (yz - wx)) * s[2], (1 - 2 * (xx + yy)) * s[2], 0.0,
        t[0], t[1], t[2], 1.0,
    ]


def _node_local_matrix(node: dict) -> list[float]:
    """A node's local transform.  glTF spec: ``matrix`` (col-major 16)
    takes priority; otherwise assemble from translation / rotation
    (quat xyzw) / scale.
    """
    mtx = node.get("matrix")
    if mtx and len(mtx) == 16:
        return list(mtx)
    t = node.get("translation") or [0.0, 0.0, 0.0]
    r = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]
    s = node.get("scale") or [1.0, 1.0, 1.0]
    return _compose_trs(t, r, s)


def _build_parent_map(nodes: list) -> dict[int, int]:
    parent: dict[int, int] = {}
    for i, n in enumerate(nodes):
        for c in n.get("children") or ():
            parent[c] = i
    return parent


def _ancestor_chain(idx: int, parent: dict[int, int]) -> list[int]:
    chain: list[int] = []
    cur: int | None = idx
    while cur is not None:
        chain.append(cur)
        cur = parent.get(cur)
    chain.reverse()
    return chain


def _compose_world(idx: int, nodes: list, parent: dict[int, int]) -> list[float]:
    world = _identity()
    for i in _ancestor_chain(idx, parent):
        world = _mat4_mul(world, _node_local_matrix(nodes[i]))
    return world


def _scan_gltf(gltf: dict) -> list[dict[str, Any]]:
    """Walk a parsed glTF dict; return a list of per-instance accessory
    records.  Same shape as the I:-side ``scan_glb`` -- callers can drop
    the result into the legacy-mode JSON unchanged.
    """
    nodes = gltf.get("nodes", [])
    parent = _build_parent_map(nodes)

    out: list[dict[str, Any]] = []
    per_asset_count: dict[str, int] = defaultdict(int)
    for i, n in enumerate(nodes):
        m = PATH_RE.match(n.get("name", "") or "")
        if not m:
            continue
        scope = m["scope"]
        category = m["category"]
        subcategory = m["subcategory"]  # may be None (depth-6)
        asset_id = m["asset_id"]
        if category == SHIP_CATEGORY:
            continue

        world = _compose_world(i, nodes, parent)
        instance_index = per_asset_count[asset_id]
        per_asset_count[asset_id] += 1

        mesh_idx = None
        for c in n.get("children") or ():
            cn = nodes[c]
            if "mesh" in cn:
                mesh_idx = cn["mesh"]
                break

        rec: dict[str, Any] = {
            "asset_id": asset_id,
            "scope": scope,
            "category": category,
            "instance_index": instance_index,
            "node_idx": i,
            "node_path": n.get("name", ""),
            "mesh": mesh_idx,
            "world_position": [
                round(world[12], 4), round(world[13], 4), round(world[14], 4),
            ],
            "world_matrix": [round(v, 6) for v in world],
        }
        if subcategory:
            rec["subcategory"] = subcategory
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Public composer entry
# ---------------------------------------------------------------------------


def scan_legacy_glb(
    legacy_glb: Path,
    *,
    output_json: Path,
    config: PipelineConfig | None = None,
    on_event: OnEvent | None = None,
) -> Path:
    """Walk a legacy gamemodels3d.com GLB and emit the hardpoint /
    accessory scan JSON used by legacy-mode decoratives resolution.

    Inputs:
        legacy_glb
            Path to a ``<Ship>_visual.glb`` produced by the retired
            gamemodels3d.com extractor (Stage 2 of the old pipeline).
        output_json
            Output path for the scan JSON.  Convention is
            ``<Ship>_accessories_scan.json`` next to the input GLB but
            any path is accepted.
        config
            Optional :class:`PipelineConfig`; accepted for API symmetry
            with other composers and reserved for future use.  This
            composer is pure JSON parsing -- it neither shells out to
            the toolkit nor reads any cache.
        on_event
            Optional progress callback.  Receives :class:`StepEvent`s
            at the step boundaries listed in the module docstring.

    Returns the resolved output path.  Raises :class:`StepError` with
    the step name set when any step fails.
    """
    # ``config`` is reserved for parity with other composers (it's part
    # of the public Round-4 signature).  No path resolution is required
    # for this pure-JSON walk; the explicit reference keeps the lint
    # rule for unused-arguments quiet without changing semantics.
    _ = config or PipelineConfig.load()
    legacy_glb = Path(legacy_glb)
    output_json = Path(output_json)

    runner = _StepRunner(on_event)

    # ── Step: parse_glb ───────────────────────────────────────────────
    with runner.step("parse_glb", detail=legacy_glb.name) as st:
        if not legacy_glb.is_file():
            raise FileNotFoundError(f"legacy GLB not found: {legacy_glb}")
        gltf = load_gltf_json(legacy_glb)
        nodes = gltf.get("nodes", []) or []
        st.annotate(
            f"parsed {len(nodes)} nodes",
            data={"node_count": len(nodes)},
        )

    # ── Step: walk_hardpoints ─────────────────────────────────────────
    with runner.step("walk_hardpoints") as st:
        instances = _scan_gltf(gltf)
        distinct = {i["asset_id"] for i in instances}

        scope_category_counts: dict[tuple[str, str], int] = defaultdict(int)
        for inst in instances:
            scope_category_counts[(inst["scope"], inst["category"])] += 1

        ship_name = legacy_glb.stem
        if ship_name.endswith("_visual"):
            ship_name = ship_name[:-len("_visual")]

        report: dict[str, Any] = {
            "source": str(legacy_glb.resolve()),
            "ship": ship_name,
            "instance_count": len(instances),
            "distinct_asset_count": len(distinct),
            "instances": instances,
        }
        st.annotate(
            f"{len(instances)} placements, {len(distinct)} distinct asset_ids",
            data={
                "instance_count":          len(instances),
                "distinct_asset_count":    len(distinct),
                "by_scope_category":       {
                    f"{s}/{c}": n for (s, c), n in
                    sorted(scope_category_counts.items(), key=lambda x: -x[1])
                },
            },
        )

    # ── Step: write_scan_json ─────────────────────────────────────────
    with runner.step("write_scan_json", detail=output_json.name) as st:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        try:
            sz = output_json.stat().st_size
        except OSError:
            sz = 0
        st.annotate(
            f"wrote {output_json.name} ({sz:,} bytes)",
            data={"bytes": sz},
        )

    return output_json


__all__ = [
    # Public composer entry
    "scan_legacy_glb",
    # Public helpers exposed for callers that want to peek into the
    # GLB without invoking the full composer (mirrors the I:-side
    # public surface).
    "load_gltf_json",
    # Constants
    "PATH_RE",
    "SHIP_CATEGORY",
    "CHUNK_JSON",
]
