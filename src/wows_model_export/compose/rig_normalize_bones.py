"""Strip Blender's bone-axis bake from a turret ``rig.glb``.

Lifted from the source pipeline's ``tools/ship/rig_normalize_bones.py``
(private I:-side repo). Blender exports armature bones with a non-
identity rest rotation that encodes the bone's head-to-tail axis
(Blender's local ``+Y`` aligns to the bone direction). For mesh skinning
that's invisible; for runtime turret control it's catastrophic — the
rig spec at ``docs/contracts/turret-rig.md`` declares
``pivots.yaw.axis = "Y"`` with the convention that::

    yaw.localRotation = Quaternion.Euler(0, deg, 0)

cleanly rotates the gun around the world up axis. With Blender's bake
that line snaps the gun to a wrong orientation (overwriting the rest
rotation); the only correct usage is to compose with the baked rest,
which the spec doesn't describe and a TurretPivot consumer wouldn't do.

This post-processor rewrites a ``.rig.glb`` so every joint has identity
local rotation while preserving the mesh's rest-pose appearance. The
mechanism (verbatim from the source):

1. Walk the node tree, recording each node's CURRENT world transform.
2. For every joint (any node listed in any skin's ``joints``):
   set its NEW world rotation to identity, keep its world translation.
3. For every other node: keep its world transform unchanged
   (so static MeshRenderers parented under joints — the ``_Rig_body`` /
   ``_Rig_elev`` helpers Blender emits — get the bake propagated into
   their own local rotation).
4. Recompute every node's local transform from its new world transform
   and its parent's new world transform.
5. Replace the inverseBindMatrices accessor data so skinning still
   reconstructs the rest pose: with identity bone rotations and
   unchanged bone positions, IBM = T(-bone_world_position).

The mesh renders identically at rest. After normalisation,
``yaw.localRotation = Euler(0, deg, 0)`` rotates the gun around world Y
exactly as the spec promises, ``elev.localRotation = Euler(deg, 0, 0)``
elevates around world X (which becomes the local trunnion axis after
the parent yaw applies), and barrel muzzle world positions match
``rig_pivots.json`` exactly.

Idempotent: running on an already-normalised rig is a no-op (every
joint's local rotation is already identity, IBMs already correct).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .._glb import parse_glb, write_glb


# ---------------------------------------------------------------------------
# Quaternion + transform helpers
# ---------------------------------------------------------------------------
# Quaternions are (x, y, z, w) tuples — glTF convention. All math is
# float64 to keep round-trips clean; we narrow to float32 only when
# writing IBM data (glTF accessor is FLOAT).


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton product (a * b) for unit quaternions in (x,y,z,w) order."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw*bx + ax*bw + ay*bz - az*by,
        aw*by - ax*bz + ay*bw + az*bx,
        aw*bz + ax*by - ay*bx + az*bw,
        aw*bw - ax*bx - ay*by - az*bz,
    ], dtype=np.float64)


def quat_conj(q: np.ndarray) -> np.ndarray:
    return np.array([-q[0], -q[1], -q[2], q[3]], dtype=np.float64)


def quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate vec3 v by quat q. v' = q * (v, 0) * conj(q), pulling the
    vec3 out of the result quaternion."""
    vq = np.array([v[0], v[1], v[2], 0.0], dtype=np.float64)
    res = quat_mul(quat_mul(q, vq), quat_conj(q))
    return res[:3]


def transform_compose(parent_t: np.ndarray, parent_r: np.ndarray,
                       local_t: np.ndarray, local_r: np.ndarray
                       ) -> tuple[np.ndarray, np.ndarray]:
    """world = parent * local (TRS, ignoring scale — turret rigs don't use it).

    Returns (world_translation, world_rotation_quat). Decomposed form:
        world_t = parent_t + parent_r * local_t
        world_r = parent_r * local_r
    """
    world_t = parent_t + quat_rotate(parent_r, local_t)
    world_r = quat_mul(parent_r, local_r)
    return world_t, world_r


def transform_invert_compose(parent_new_t: np.ndarray, parent_new_r: np.ndarray,
                              child_world_t: np.ndarray, child_world_r: np.ndarray
                              ) -> tuple[np.ndarray, np.ndarray]:
    """Find local s.t. parent_new * local = child_world.
    => local = inverse(parent_new) * child_world.
    """
    inv_parent_r = quat_conj(parent_new_r)
    local_r = quat_mul(inv_parent_r, child_world_r)
    delta_t = child_world_t - parent_new_t
    local_t = quat_rotate(inv_parent_r, delta_t)
    return local_t, local_r


# ---------------------------------------------------------------------------
# Accessor I/O — specifically for the inverseBindMatrices accessor.
# We know it's component_type=FLOAT (5126), type="MAT4", count=joint_count.
# We don't touch any other accessors.
# ---------------------------------------------------------------------------


def _replace_ibm_data(gltf: dict, bin_bytes: bytearray,
                       skin_index: int, ibm_matrices: list[np.ndarray]) -> None:
    """Replace the binary data for skin[skin_index].inverseBindMatrices.

    Each matrix is a (4,4) float64 in row-major numpy order; we transpose
    on write since glTF MAT4 is column-major.

    Asserts that the new byte length equals the existing accessor's byte
    length so we can write in-place without reflowing buffer views.
    """
    skin = gltf["skins"][skin_index]
    acc = gltf["accessors"][skin["inverseBindMatrices"]]
    bv = gltf["bufferViews"][acc["bufferView"]]

    expected_count = acc["count"]
    if len(ibm_matrices) != expected_count:
        raise ValueError(
            f"IBM count mismatch: accessor expects {expected_count}, "
            f"got {len(ibm_matrices)}"
        )

    if acc.get("componentType") != 5126:  # FLOAT
        raise ValueError("IBM accessor component_type must be FLOAT (5126)")
    if acc.get("type") != "MAT4":
        raise ValueError("IBM accessor type must be MAT4")

    new_bytes = bytearray()
    for m in ibm_matrices:
        if m.shape != (4, 4):
            raise ValueError(f"IBM must be 4x4, got {m.shape}")
        # glTF MAT4 is column-major.
        col_major = np.asarray(m.T, dtype=np.float32)
        new_bytes.extend(col_major.tobytes())

    expected_bytes = expected_count * 64
    if len(new_bytes) != expected_bytes:
        raise ValueError(
            f"IBM byte size mismatch: {len(new_bytes)} vs {expected_bytes}"
        )

    offset = bv.get("byteOffset", 0)
    bin_bytes[offset:offset + expected_bytes] = new_bytes


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizeStats:
    """Counters returned by :func:`normalize`."""

    joints: int
    joints_normalised: int
    non_joints_propagated: int
    skins_updated: int

    def to_dict(self) -> dict:
        return {
            "joints":               self.joints,
            "joints_normalised":    self.joints_normalised,
            "non_joints_propagated": self.non_joints_propagated,
            "skins_updated":        self.skins_updated,
        }


def normalize(gltf: dict, bin_bytes: bytearray) -> NormalizeStats:
    """Identity-rotation every joint while preserving rest-pose visuals.

    Mutates ``gltf`` (node ``translation`` / ``rotation`` fields) and
    ``bin_bytes`` (inverseBindMatrices accessor data) in place. Returns
    a :class:`NormalizeStats` for logging.
    """
    nodes = gltf["nodes"]
    n_nodes = len(nodes)

    # Discover joints (any node referenced from any skin's joint list).
    joints: set[int] = set()
    for skin in gltf.get("skins", []) or []:
        joints.update(skin.get("joints", []))

    # Build parent map: child_idx -> parent_idx. Roots have no entry.
    parent_of: dict[int, int] = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []) or []:
            parent_of[c] = i

    # Discover root nodes (in scene OR with no parent — the scene's
    # nodes[] is the canonical entry, but we also handle stray roots).
    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    scene_roots = list(scene.get("nodes", []) or [])
    root_set = set(scene_roots)
    for i in range(n_nodes):
        if i not in parent_of and i not in root_set:
            scene_roots.append(i)
            root_set.add(i)

    # Pass 1 — current world transforms via DFS from the scene roots.
    world_t = np.zeros((n_nodes, 3), dtype=np.float64)
    world_r = np.tile(np.array([0, 0, 0, 1], dtype=np.float64), (n_nodes, 1))

    def get_local(idx: int) -> tuple[np.ndarray, np.ndarray]:
        n = nodes[idx]
        t = np.array(n.get("translation", [0, 0, 0]), dtype=np.float64)
        r = np.array(n.get("rotation", [0, 0, 0, 1]), dtype=np.float64)
        if "matrix" in n:
            raise NotImplementedError(
                f"node {idx} ({n.get('name')!r}) uses matrix form — "
                "rig normaliser only handles TRS nodes")
        return t, r

    def dfs_world(idx: int, parent_t: np.ndarray, parent_r: np.ndarray) -> None:
        lt, lr = get_local(idx)
        wt, wr = transform_compose(parent_t, parent_r, lt, lr)
        world_t[idx] = wt
        world_r[idx] = wr
        for c in nodes[idx].get("children", []) or []:
            dfs_world(c, wt, wr)

    identity_t = np.zeros(3, dtype=np.float64)
    identity_r = np.array([0, 0, 0, 1], dtype=np.float64)
    for r in scene_roots:
        dfs_world(r, identity_t, identity_r)

    # Pass 2 — desired NEW world transforms.
    #   joints   → keep position, rotation := identity
    #   others   → keep both (preserves mesh visuals)
    new_world_t = world_t.copy()
    new_world_r = world_r.copy()
    for j in joints:
        new_world_r[j] = identity_r

    # Pass 3 — derive new local TRS for every node from new world + new parent world.
    # Walk in parent-before-child order via BFS from scene roots.
    bfs_order: list[int] = []
    queue = list(scene_roots)
    seen = set(scene_roots)
    while queue:
        cur = queue.pop(0)
        bfs_order.append(cur)
        for c in nodes[cur].get("children", []) or []:
            if c not in seen:
                seen.add(c)
                queue.append(c)

    joints_normalised = 0
    others_propagated = 0
    for idx in bfs_order:
        n = nodes[idx]
        if idx in parent_of:
            p = parent_of[idx]
            parent_new_t = new_world_t[p]
            parent_new_r = new_world_r[p]
        else:
            parent_new_t = identity_t
            parent_new_r = identity_r

        new_local_t, new_local_r = transform_invert_compose(
            parent_new_t, parent_new_r,
            new_world_t[idx], new_world_r[idx])

        old_t, old_r = get_local(idx)
        if idx in joints:
            joints_normalised += 1
        elif not (np.allclose(new_local_t, old_t, atol=1e-7)
                  and np.allclose(new_local_r, old_r, atol=1e-7)):
            others_propagated += 1

        # Omit fields that are at their default. Keeps diffs small for
        # nodes that didn't change.
        if np.allclose(new_local_t, [0, 0, 0], atol=1e-9):
            n.pop("translation", None)
        else:
            n["translation"] = [float(x) for x in new_local_t]
        if np.allclose(new_local_r, [0, 0, 0, 1], atol=1e-9):
            n.pop("rotation", None)
        else:
            n["rotation"] = [float(x) for x in new_local_r]

    # Pass 4 — recompute inverseBindMatrices for every skin. With identity
    # bone world rotation and unchanged bone position, the bone's
    # rest-world matrix is T(bone_world_pos), and its inverse is
    # T(-bone_world_pos).
    for skin_index in range(len(gltf.get("skins", []) or [])):
        ibm: list[np.ndarray] = []
        for j in gltf["skins"][skin_index]["joints"]:
            wt = new_world_t[j]
            mat = np.eye(4, dtype=np.float64)
            mat[0, 3] = -wt[0]
            mat[1, 3] = -wt[1]
            mat[2, 3] = -wt[2]
            ibm.append(mat)
        _replace_ibm_data(gltf, bin_bytes, skin_index, ibm)

    return NormalizeStats(
        joints=len(joints),
        joints_normalised=joints_normalised,
        non_joints_propagated=others_propagated,
        skins_updated=len(gltf.get("skins", []) or []),
    )


def normalize_file(path: Path, *, output: Path | None = None) -> NormalizeStats:
    """High-level entry: parse, normalise, and write back atomically.

    Args:
        path: Input ``.rig.glb`` to normalise.
        output: Optional output path. When ``None``, ``path`` is
            overwritten (the common case after a Blender export).

    Returns:
        :class:`NormalizeStats` with per-pass counters.
    """
    data = path.read_bytes()
    gltf, bin_immutable = parse_glb(data)
    bin_bytes = bytearray(bin_immutable)
    stats = normalize(gltf, bin_bytes)
    out_path = output if output is not None else path
    write_glb(gltf, bytes(bin_bytes), out_path)
    return stats


__all__ = [
    "NormalizeStats",
    "normalize",
    "normalize_file",
    "quat_mul",
    "quat_conj",
    "quat_rotate",
    "transform_compose",
    "transform_invert_compose",
]
