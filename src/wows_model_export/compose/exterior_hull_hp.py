"""Variant-hull HP-node transform harvest (HullDelta companion).

WG hides base accessories on a hull-swap exterior by **moving the HP
nodes on the variant hull model** — the variant keeps the full base HP
roster (no adds/removes; verified 123/123 on JSB403_Yamato_StarTrek vs
JSB039_Yamato_1945) but parks unused decorative HPs at one degenerate
point inside the hull (36 ``HP_JD_*``/``HP_JRS_*``/``HP_JF_*`` nodes all
collapse to ship-space ``(0, 0, 0.135)`` on the Star Trek hull) and
nudges the kept ones to fit the variant superstructure. There are NO
removal semantics in the engine; "hidden" is literally "anchored inside
the mesh".

The toolkit cannot emit a placements JSON for a variant model dir (it
isn't a GameParams Vehicle, so HP→asset resolution comes up empty —
that's why the legacy ``__Variant`` folders silently kept BASE hull
placements). This module recovers the per-HP **transforms** directly
from the variant hull part ``.visual`` node trees via ``dump-bones`` and
converts them into the exact convention the sidecar placements use, so
``exterior_unify.reanchor_base_placements`` can diff them into ordinary
transform-only ``mounts[]`` records that existing consumers replay
verbatim.

Convention (validated against the base ship's own placements JSON —
the toolkit emits ``transform.matrix`` byte-identical into the sidecar):

- ``dump-bones`` matrices are row-vector local transforms; world =
  ``local @ parent_world``.
- BigWorld→glTF: conjugate by ``diag(1, 1, -1)`` (negates m[0][2],
  m[1][2], m[2][0], m[2][1]) and scale translation by
  ``NATIVE_TO_METRES = 15`` with Z negated.
- Hull parts are modeled in ship space (part anchors at identity), so
  per-part world composition is already ship space.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import PipelineConfig
from ..toolkit.bones import fetch_bones

#: BigWorld native units → metres (the pipeline-wide ×15).
NATIVE_TO_METRES = 15.0

#: Part-suffix candidates are derived from the full model's ``HP_<Part>``
#: anchor nodes; this shape filter keeps real part names (``Bow``,
#: ``MidFront``, ``Stern``, ``Full``…) and rejects accessory hardpoints
#: (``HP_JD_14``) and specials (``HP_Ship_death_1``).
_PART_NAME_RE = re.compile(r"^[A-Z][A-Za-z]+$")


def _world_matrices(nodes: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Compose per-node world matrices (row-vector: ``local @ parent``)
    from a ``dump-bones`` node list. Returns ``{name: 16-float}`` for
    every named node; later duplicates of a name are ignored (WG visuals
    occasionally repeat helper names; the first is the canonical one).
    """
    import numpy as np

    worlds: dict[int, Any] = {}

    def world(idx: int) -> Any:
        cached = worlds.get(idx)
        if cached is not None:
            return cached
        n = nodes[idx]
        local = np.array(n.get("local_matrix"), dtype=float).reshape(4, 4)
        parent = n.get("parent_idx")
        if parent is None or parent == idx:
            w = local
        else:
            w = local @ world(int(parent))
        worlds[idx] = w
        return w

    out: dict[str, list[float]] = {}
    for i, n in enumerate(nodes):
        name = n.get("name")
        if not isinstance(name, str) or not name or name in out:
            continue
        lm = n.get("local_matrix")
        if not isinstance(lm, list) or len(lm) != 16:
            continue
        out[name] = [float(x) for x in world(i).reshape(-1)]
    return out


def _bw_to_gltf_transform(m16: list[float]) -> dict[str, Any]:
    """Convert a BigWorld-space row-vector matrix into the sidecar's
    placements ``transform`` shape (glTF axes, metres): conjugate by
    ``diag(1,1,-1)`` and scale the translation row by ×15 with Z negated.
    """
    m = list(m16)
    for i in (2, 6, 8, 9):  # m[0][2], m[1][2], m[2][0], m[2][1]
        m[i] = -m[i]
    m[12] = m[12] * NATIVE_TO_METRES
    m[13] = m[13] * NATIVE_TO_METRES
    m[14] = -m[14] * NATIVE_TO_METRES
    # Normalise -0.0 → 0.0 so emitted JSON diffs cleanly.
    m = [0.0 if x == 0.0 else x for x in m]
    return {"matrix": m, "position": [m[12], m[13], m[14]]}


def harvest_hull_hp_transforms(
    model_vfs_dir: str,
    *,
    config: PipelineConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Dump the hull model + its part models for ``model_vfs_dir`` (full
    VFS directory, original case) and return every ``HP_*`` node's
    ship-space transform in sidecar convention:
    ``{hp_name: {"matrix": [...16], "position": [x,y,z]}}``.

    Part enumeration: the whole-ship visual carries one ``HP_<Part>``
    anchor per hull part (``HP_Bow`` / ``HP_MidFront`` / …); each
    matching ``<dir>_<Part>.geometry`` that exists is dumped and its HP
    nodes unioned in (first occurrence wins — the full model's own HP
    anchors don't collide with part-level accessory HPs).

    Raises whatever ``fetch_bones`` raises for the FULL model (a variant
    dir without a readable hull visual is a real failure); individual
    missing part models are skipped silently (``_Full`` render sets often
    live in the main geometry with no standalone part file).
    """
    cfg = config or PipelineConfig.load()
    model_dir = model_vfs_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    base = f"{model_vfs_dir.replace(chr(92), '/').rstrip('/')}/{model_dir}"

    full = fetch_bones(f"{base}.geometry", config=cfg)
    full_nodes = full.get("nodes") or []
    full_worlds = _world_matrices(full_nodes)

    part_names = sorted(
        name[3:]
        for name in full_worlds
        if name.startswith("HP_") and _PART_NAME_RE.fullmatch(name[3:])
    )

    out: dict[str, dict[str, Any]] = {}
    for part in part_names:
        try:
            doc = fetch_bones(f"{base}_{part}.geometry", config=cfg)
        except Exception:
            continue  # part baked into the main geometry (e.g. Full)
        for name, m in _world_matrices(doc.get("nodes") or []).items():
            if name.startswith("HP_") and name not in out:
                out[name] = _bw_to_gltf_transform(m)

    # Main-geometry HPs last: part anchors (HP_Bow…) plus everything a
    # single-model hull carries directly.
    for name, m in full_worlds.items():
        if name.startswith("HP_") and name not in out:
            out[name] = _bw_to_gltf_transform(m)
    return out


def corrected_variant_hp_transforms(
    base_placement_transforms: dict[str, list[float]],
    base_harvest: dict[str, dict[str, Any]],
    variant_harvest: dict[str, dict[str, Any]],
    *,
    epsilon: float = 5e-3,
) -> dict[str, dict[str, Any]]:
    """Re-anchor base placements onto the variant hull's HP nodes,
    self-calibrating against the base hull so the toolkit's per-asset
    frame corrections carry over without re-deriving them.

    The sidecar placement transform is not the raw HP node world matrix:
    the toolkit left-multiplies a per-asset local frame correction
    (Ry(180°) for det<0 bone-frame assets — 24/111 placements on Yamato,
    spanning guns, catapults, directors AND searchlights, so no category
    rule works). Computing ``R_hp = S_base @ inv(W_base)`` per HP from
    the base hull recovers that correction exactly, and
    ``V = R_hp @ W_variant`` applies it to the variant node. For an HP
    the variant didn't move, ``V`` reproduces the base bytes (modulo
    float noise far below ``epsilon``), so only genuinely moved HPs are
    returned — including WG's parked-inside-the-hull "hidden" nodes.

    Inputs: ``base_placement_transforms`` maps ``hp_name`` → the SIDECAR
    transform matrix (16 floats); the two harvests come from
    :func:`harvest_hull_hp_transforms`. HPs missing from either harvest
    are skipped (chained ``HP_<host>_HP_<child>`` placements live on
    accessory models, not the hull — intentionally untouched).
    """
    import numpy as np

    def m44(lst: list[float]) -> Any:
        return np.array(lst, dtype=float).reshape(4, 4)

    out: dict[str, dict[str, Any]] = {}
    for hp, s_base in base_placement_transforms.items():
        if not isinstance(s_base, list) or len(s_base) != 16:
            continue
        bh = base_harvest.get(hp)
        vh = variant_harvest.get(hp)
        if bh is None or vh is None:
            continue
        sb = m44(s_base)
        try:
            r = sb @ np.linalg.inv(m44(bh["matrix"]))
        except np.linalg.LinAlgError:
            continue  # degenerate base node (zero scale) — leave placement alone
        v = r @ m44(vh["matrix"])
        if float(np.abs(v - sb).max()) <= epsilon:
            continue  # unmoved on the variant hull
        flat = [0.0 if x == 0.0 else float(x) for x in v.reshape(-1)]
        out[hp] = {"matrix": flat, "position": [flat[12], flat[13], flat[14]]}
    return out


__all__ = [
    "harvest_hull_hp_transforms",
    "corrected_variant_hp_transforms",
    "NATIVE_TO_METRES",
]
