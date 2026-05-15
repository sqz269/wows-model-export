"""Tiny pure-numpy KD-tree — drop-in replacement for the narrow slice of
the ``spatial`` KD-tree API that :mod:`mesh_compare` actually uses.

WHY this exists
---------------
``mesh_compare`` was the *only* module in the codebase pulling in the
heavyweight ``spatial`` package. On Linux that wheel adds ~30 MB; on
Windows the PyInstaller-frozen .exe balloons by roughly 100 MB once
the bundled BLAS/LAPACK (OpenBLAS), ``linalg`` submodule, the rest of
``spatial``, and the unused ``special`` ufuncs all get pulled in
transitively. We don't ship any other consumers of that package — we
burn 100 MB to run ``KDTree.query`` and ``KDTree.query_ball_point`` on
meshes that top out around 3 000 vertices per primitive group. Bad
ratio.

This module implements only the two methods + the construction shape we
actually call from ``mesh_compare``:

    KDTree(data: (N, D) ndarray)
    .query(points, k=1)              -> (dists float64, idx intp)
    .query_ball_point(points, r)     -> object array of list[int]

No ball-tree, no periodic boundaries, no k>1, no count_neighbors. If a
future caller needs more, extend deliberately rather than emulating the
whole upstream API surface here.

Algorithm
---------
Standard median-split KD-tree, ``O(N log N)`` build / ``O(log N)`` query
expected. For each internal node we split on the axis of largest spread
(rather than cycling axes deterministically): on tightly-packed mesh
vertex clouds — long, thin turret/director shapes are common — picking
the widest axis halves cell diameters faster and tightens the
bounding-radius prune during query, which is the dominant cost.

Leaf threshold = 16. Below this we stop recursing and store a flat list
of point indices; the leaf scan is then a single vectorised
``np.einsum``-equivalent (squared-distance broadcast) over up to 16
points. Smaller leaves => deeper trees => more Python recursion per
query; larger leaves => more brute-force work per leaf. 16 is the sweet
spot empirically for (N≈few-thousand, D=3) — same default the upstream
implementation uses, for the same reason.

Tie-breaking on equidistant neighbours follows recursion order (which
side gets visited first depends on the query point's position relative
to the split). The upstream implementation resolves ties by its own
internal order which is not necessarily the same. Callers that depend
on a specific tie-break must re-sort by index — none of mesh_compare's
call-sites do.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np

# WHY 16: see module docstring. Keep this private; not a tunable knob.
_LEAF_SIZE = 16


class _Leaf(NamedTuple):
    # Indices into the original data array. We vectorise the brute-force
    # scan over these on every query that descends into the leaf.
    indices: np.ndarray  # (n_leaf,) int64


class _Node(NamedTuple):
    # Internal split node. ``axis`` is which coordinate we split on,
    # ``split`` is the value (median of points along ``axis``). Points
    # with coord < split go left, else right. We don't store the bounding
    # box: the recursive query carries the per-axis distance contribution
    # incrementally instead, which is cheaper than recomputing per-node.
    axis: int
    split: float
    left: _Leaf | _Node
    right: _Leaf | _Node


class KDTree:
    """Minimal KD-tree over an (N, D) point cloud, numpy-only.

    Mirrors the subset of the upstream ``spatial`` KD-tree API that
    :mod:`mesh_compare` consumes. See module docstring for scope.
    """

    __slots__ = ("data", "_n", "_d", "_root")

    def __init__(self, data: np.ndarray) -> None:
        # Coerce to contiguous float64 — query distances are computed in
        # float64 to match the upstream API's output dtype, and a contiguous
        # backing array makes the leaf-scan broadcast a single memcpy-free op.
        # We *do not* copy if the caller already passed float64
        # contiguous; ``ascontiguousarray`` is a no-op in that case.
        self.data = np.ascontiguousarray(data, dtype=np.float64)
        if self.data.ndim != 2:
            raise ValueError(f"KDTree expects 2D (N, D) data, got shape {self.data.shape}")
        self._n, self._d = self.data.shape
        if self._n == 0:
            # Empty tree: build returns a leaf with zero indices. Queries
            # against an empty tree return inf distance / -1 index, which
            # mesh_compare never hits (it always builds from a non-empty
            # MeshSnapshot.positions) but is the safe sentinel.
            self._root: _Leaf | _Node = _Leaf(indices=np.empty(0, dtype=np.int64))
        else:
            all_idx = np.arange(self._n, dtype=np.int64)
            self._root = self._build(all_idx)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self, idx: np.ndarray) -> _Leaf | _Node:
        # Leaf cutoff: cheap brute-force on small chunks beats recursion
        # overhead. See module docstring re: 16.
        if idx.shape[0] <= _LEAF_SIZE:
            return _Leaf(indices=idx)

        pts = self.data[idx]
        # Split on the widest axis (max spread). This is more robust than
        # round-robin axes when the point cloud is anisotropic, which
        # ship-mesh accessory primitives invariably are (long-thin turret
        # barrels, flat directors). A tighter split = tighter prune
        # bounds during query.
        spread = pts.max(axis=0) - pts.min(axis=0)
        axis = int(np.argmax(spread))

        # Median split. ``argpartition`` gives us the median index in
        # O(N) without a full sort; we then partition idx by the split
        # value (not the median index) so equal-coord points behave
        # consistently.
        coords = pts[:, axis]
        mid = idx.shape[0] // 2
        # argpartition guarantees coords[order[mid]] is the median value;
        # everything left of mid is <=, everything right >=. We use the
        # actual coordinate value as the split so ball queries can prune
        # symmetrically.
        order = np.argpartition(coords, mid)
        split_val = float(coords[order[mid]])

        # Re-bucket by split value (not by argpartition's exact midpoint)
        # so duplicate-coord points aren't artificially separated. If
        # everything collapses to one side (all coords identical), force
        # a balanced split to avoid infinite recursion.
        left_mask = coords < split_val
        right_mask = ~left_mask
        if not left_mask.any() or not right_mask.any():
            # All coords equal on this axis — fall back to a positional
            # split. Order is stable enough; we just need both children
            # to be non-empty.
            left_mask = np.zeros(idx.shape[0], dtype=bool)
            left_mask[order[:mid]] = True
            right_mask = ~left_mask

        left_idx = idx[left_mask]
        right_idx = idx[right_mask]

        return _Node(
            axis=axis,
            split=split_val,
            left=self._build(left_idx),
            right=self._build(right_idx),
        )

    # ------------------------------------------------------------------
    # query (k=1 only — that's all mesh_compare uses)
    # ------------------------------------------------------------------

    def query(self, x: np.ndarray, k: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Nearest-neighbour search. Only ``k=1`` is supported.

        Returns ``(distances, indices)`` matching the upstream API shapes:
            * ``x`` shape (D,)         -> scalars (float, int)
            * ``x`` shape (M, D)       -> arrays (float64 (M,), intp (M,))
        """
        if k != 1:
            # Deliberately not implemented — mesh_compare only uses k=1.
            # Raising here keeps the failure mode loud if someone copies
            # this tree to a new caller and assumes upstream parity.
            raise NotImplementedError("KDTree.query only supports k=1")

        x_arr = np.ascontiguousarray(x, dtype=np.float64)
        squeeze = False
        if x_arr.ndim == 1:
            # Single-point query: upstream returns plain scalars. We mimic
            # that by computing as (1, D) then unwrapping.
            if x_arr.shape[0] != self._d:
                raise ValueError(f"query point dim {x_arr.shape[0]} != tree dim {self._d}")
            x_arr = x_arr[None, :]
            squeeze = True
        elif x_arr.ndim == 2:
            if x_arr.shape[1] != self._d:
                raise ValueError(f"query points dim {x_arr.shape[1]} != tree dim {self._d}")
        else:
            raise ValueError(f"query expects (D,) or (M, D), got {x_arr.shape}")

        m = x_arr.shape[0]
        # Output buffers — float64/int64 to match the upstream API's dtypes.
        # We keep best squared distance internally (avoids a sqrt per
        # leaf-vertex) and sqrt once at the end.
        best_d2 = np.full(m, np.inf, dtype=np.float64)
        best_idx = np.full(m, -1, dtype=np.int64)

        # Iterate per-query-point. Could batch-recurse for cache locality
        # but for our N/M sizes (~3k/3k) the Python overhead of a single
        # recursion stack per query is negligible vs. the cost of the
        # leaf-scan numpy ops.
        for qi in range(m):
            self._query_one(x_arr[qi], qi, best_d2, best_idx)

        dists = np.sqrt(best_d2)
        if squeeze:
            return float(dists[0]), int(best_idx[0])
        return dists, best_idx

    def _query_one(
        self,
        q: np.ndarray,
        qi: int,
        best_d2: np.ndarray,
        best_idx: np.ndarray,
    ) -> None:
        # Iterative recursion (Python recursion limit could in theory
        # bite for pathological inputs at ~10^4 verts deep, but our trees
        # bottom-out at log2(N/16) ≈ 7-8 for N=3k — recursion is safe and
        # simpler to read than an explicit stack).
        self._descend(self._root, q, qi, best_d2, best_idx)

    def _descend(
        self,
        node: _Leaf | _Node,
        q: np.ndarray,
        qi: int,
        best_d2: np.ndarray,
        best_idx: np.ndarray,
    ) -> None:
        if isinstance(node, _Leaf):
            if node.indices.shape[0] == 0:
                return
            # Vectorised brute-force over the leaf's points. Single
            # broadcast subtraction + einsum-equivalent sum-of-squares.
            diff = self.data[node.indices] - q
            d2 = np.einsum("ij,ij->i", diff, diff)
            j = int(np.argmin(d2))
            if d2[j] < best_d2[qi]:
                best_d2[qi] = float(d2[j])
                best_idx[qi] = int(node.indices[j])
            return

        # Internal node: recurse into the near child first (the side
        # containing the query) so we tighten ``best_d2`` early and prune
        # the far child more aggressively.
        delta = q[node.axis] - node.split
        if delta < 0.0:
            near, far = node.left, node.right
        else:
            near, far = node.right, node.left

        self._descend(near, q, qi, best_d2, best_idx)

        # Branch prune: the closest point in the far subtree must lie at
        # least |delta| along the split axis from q. If that lower-bound
        # squared distance already exceeds our current best, skip.
        if delta * delta < best_d2[qi]:
            self._descend(far, q, qi, best_d2, best_idx)

    # ------------------------------------------------------------------
    # query_ball_point
    # ------------------------------------------------------------------

    def query_ball_point(self, x: np.ndarray, r: float) -> np.ndarray:
        """All points within Euclidean distance ``r`` of each query point.

        Returns an object-dtype ndarray of Python ``list[int]``, length
        equal to the number of query points (matching the upstream API's
        shape for 2D ``x``). For 1D ``x`` returns a single ``list[int]``.

        ``r`` is the Euclidean radius; we work in squared distance
        internally and compare against ``r*r`` to avoid per-point sqrts.
        """
        x_arr = np.ascontiguousarray(x, dtype=np.float64)
        if x_arr.ndim == 1:
            if x_arr.shape[0] != self._d:
                raise ValueError(f"query point dim {x_arr.shape[0]} != tree dim {self._d}")
            out: list[int] = []
            self._ball_descend(self._root, x_arr, float(r) * float(r), out)
            # Upstream returns a plain list for a single query point.
            return out  # type: ignore[return-value]

        if x_arr.ndim != 2 or x_arr.shape[1] != self._d:
            raise ValueError(f"query_ball_point expects (D,) or (M, D), got {x_arr.shape}")

        m = x_arr.shape[0]
        r2 = float(r) * float(r)
        # Object array of lists — the upstream API's exact return shape for 2D x.
        # Pre-allocating `np.empty(m, dtype=object)` and writing into it
        # is cheaper than building a Python list and converting.
        results = np.empty(m, dtype=object)
        for qi in range(m):
            buf: list[int] = []
            self._ball_descend(self._root, x_arr[qi], r2, buf)
            results[qi] = buf
        return results

    def _ball_descend(
        self,
        node: _Leaf | _Node,
        q: np.ndarray,
        r2: float,
        out: list[int],
    ) -> None:
        if isinstance(node, _Leaf):
            if node.indices.shape[0] == 0:
                return
            diff = self.data[node.indices] - q
            d2 = np.einsum("ij,ij->i", diff, diff)
            hits = np.flatnonzero(d2 <= r2)
            if hits.size:
                # Convert to plain Python ints — the upstream query_ball_point
                # returns lists of Python ints, not numpy scalars, and
                # mesh_compare uses these as fancy-index for dst.uvs
                # (works either way) but matching the type avoids
                # surprises in any downstream consumer.
                out.extend(int(node.indices[h]) for h in hits)
            return

        # For ball queries we have to descend BOTH children whenever the
        # query ball intersects the far child's halfspace. The standard
        # bound: if |q[axis] - split|^2 > r^2 then the ball lies wholly
        # on one side of the split.
        delta = q[node.axis] - node.split
        if delta < 0.0:
            self._ball_descend(node.left, q, r2, out)
            if delta * delta <= r2:
                self._ball_descend(node.right, q, r2, out)
        else:
            self._ball_descend(node.right, q, r2, out)
            if delta * delta <= r2:
                self._ball_descend(node.left, q, r2, out)


__all__ = ["KDTree"]
