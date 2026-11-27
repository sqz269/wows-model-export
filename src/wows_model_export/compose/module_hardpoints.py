"""Placements for upgrade-module mounts the hull's placements do not fit.

A gun or torpedo module can mount at hardpoints its hull's fitted component
leaves empty: Icarus' top torpedo module (``AB2_Torpedoes``) puts its 5-tube
launchers at ``HP_BGT_1`` / ``HP_BGT_3`` while the stock quads sit at
``HP_BGT_2`` / ``HP_BGT_4``. The game builds a ship from the components its
selected modules bring (METADATA_SPEC §11.5), so with that module fitted those
two hardpoints carry the launchers and the stock ones carry nothing. And a
placement's transform has the placed model's ``Rotate_Y_BlendBone`` stripped,
so it turns another model with a different bone the wrong way (Mogami's 203 mm
twins, a Z-mirror bone, at hardpoints whose 155 mm triples have an identity
bone; Clemson's shielded twin 4-inch, identity, where a Z-mirrored single
stood: 180 degrees each). :class:`HardpointResolver` gives a module record at
such a hardpoint the transform the export writes for a placed mount:

    the hull's ``HP_*`` node world matrix (its local matrix composed with
    every parent's in the hull ``.visual`` that holds it) times the inverse
    rotation of the mount model's ``Rotate_Y_BlendBone``, made a proper
    rotation (column 0 negated when the 3x3 determinant is negative), with Z
    negated (BigWorld to glTF axes) and the translation scaled to metres
    (x15)

(the toolkit's ``export-ship --placements-json`` rule for a simple hardpoint;
:mod:`..building_export` writes the same rule for building guns). The node
trees are read with the toolkit's ``dump-bones``. ``parent_section`` is the
hull section whose ``.visual`` holds the node (``Bow`` / ``MidFront`` /
``MidBack`` / ``Stern``, ``Full`` for the bare model), as for a placed mount;
when several of the model folder's visuals hold the node, the last by name
wins, as in the export. A hardpoint no hull visual holds (a compound one, on
another mount) gets no transform; a hull model with no visual in the manifest
at all (another build's manifest, an empty one) raises ``LookupError``, since
then no hardpoint of it can be read. Asked with the placed model, the resolver
answers only when the two models' bones turn differently (their 3x3 rotations
differ; no bone counts as identity); otherwise the placement's transform holds.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import PipelineConfig

#: Native BigWorld unit -> metres (the toolkit's ``NATIVE_TO_METRES``).
NATIVE_TO_METRES: float = 15.0

#: The mount model node whose rest rotation the export strips from a mount.
ROTATE_Y_BLEND_BONE: str = "Rotate_Y_BlendBone"

#: Mount model -> its bone's local matrix, shared by every resolver of one
#: process (a refresh over every sidecar reads each model once).
_BONE_CACHE: dict[str, list[float] | None] = {}

_IDENTITY: tuple[float, ...] = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


def _mul(a: list[float], b: list[float]) -> list[float]:
    """Column-major 4x4 product ``a x b``."""
    out = [0.0] * 16
    for col in range(4):
        for row in range(4):
            out[col * 4 + row] = sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
    return out


def _rotation_inverse(m: list[float]) -> list[float]:
    """The transpose of ``m``'s 3x3, no translation."""
    return [
        m[0], m[4], m[8], 0.0,
        m[1], m[5], m[9], 0.0,
        m[2], m[6], m[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _det3(m: list[float]) -> float:
    return (
        m[0] * (m[5] * m[10] - m[6] * m[9])
        - m[4] * (m[1] * m[10] - m[2] * m[9])
        + m[8] * (m[1] * m[6] - m[2] * m[5])
    )


def placement_transform(hp_world: list[float], bone_local: list[float] | None) -> dict[str, list[float]]:
    """The placement ``transform`` (``matrix``, column-major, glTF axes,
    metres; ``position``) of a mount whose model has ``Rotate_Y_BlendBone``
    local matrix ``bone_local`` (``None``: no such node) at a hull hardpoint
    with native world matrix ``hp_world``."""
    m = [float(v) for v in hp_world]
    if bone_local is not None:
        m = _mul(m, _rotation_inverse([float(v) for v in bone_local]))
    if _det3(m) < 0.0:
        m[0], m[1], m[2] = -m[0], -m[1], -m[2]
    out = [
        m[0], m[1], -m[2], m[3],
        m[4], m[5], -m[6], m[7],
        -m[8], -m[9], m[10], m[11],
        m[12] * NATIVE_TO_METRES, m[13] * NATIVE_TO_METRES, -m[14] * NATIVE_TO_METRES, m[15],
    ]
    return {"matrix": out, "position": out[12:15]}


def world_matrices(bones_doc: dict[str, Any]) -> dict[str, list[float]]:
    """Node name -> world matrix of a ``dump-bones --json`` document (each
    node's local matrix composed with its parents', root last)."""
    nodes = bones_doc.get("nodes") or []
    by_idx = {n.get("idx"): n for n in nodes if isinstance(n, dict)}
    out: dict[str, list[float]] = {}
    for n in nodes:
        if not isinstance(n, dict) or not n.get("name"):
            continue
        m = [float(v) for v in (n.get("local_matrix") or _IDENTITY)]
        parent = n.get("parent_idx")
        seen = {n.get("idx")}
        while parent is not None and parent in by_idx and parent not in seen:
            seen.add(parent)
            p = by_idx[parent]
            m = _mul([float(v) for v in (p.get("local_matrix") or _IDENTITY)], m)
            parent = p.get("parent_idx")
        out[n["name"]] = m
    return out


def _local_matrix(bones_doc: dict[str, Any], name: str) -> list[float] | None:
    for n in bones_doc.get("nodes") or []:
        if isinstance(n, dict) and n.get("name") == name and n.get("local_matrix"):
            return [float(v) for v in n["local_matrix"]]
    return None


def _geometry_of(model_path: str) -> str:
    p = model_path.replace("\\", "/").lstrip("/")
    return p[: -len(".model")] + ".geometry" if p.endswith(".model") else p


class HardpointResolver:
    """``resolver(hull_name, hp_name, model_path, placed_model_path=None)`` ->
    ``{"transform", "parent_section"}`` or ``None``: the placement of the
    mount model ``model_path`` (its GameParams ``.model`` path) at hardpoint
    ``hp_name`` of hull component ``hull_name`` of ``ship`` (a GameParams
    Vehicle); ``None`` also when ``placed_model_path`` (the model the hull
    places there) turns the same way (:meth:`turns_alike`).

    ``manifest_paths`` is the set of VFS file paths (no leading ``/``) the
    hull visuals are looked up in; ``fetch_bones(geometry_vfs_path)`` returns
    a ``dump-bones --json`` document (default: the toolkit's). Each hull model
    and mount model is read once."""

    def __init__(
        self,
        ship: dict[str, Any],
        *,
        manifest_paths: set[str],
        config: PipelineConfig | None = None,
        fetch_bones: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._ship = ship
        self._paths = manifest_paths
        self._config = config
        self._fetch = fetch_bones
        self._hulls: dict[str, dict[str, tuple[list[float], str]]] = {}
        self._bones: dict[str, list[float] | None] = {}

    def _fetch_doc(self, geometry: str) -> dict[str, Any]:
        if self._fetch is not None:
            return self._fetch(geometry)
        from ..toolkit import bones as _bones

        return _bones.fetch_bones(geometry, config=self._config)

    def hull_hardpoints(self, model_path: str) -> dict[str, tuple[list[float], str]]:
        """``HP_*`` node -> (native world matrix, section) over every visual
        of the hull model's folder, in name order (the last holder wins).
        Raises ``LookupError`` when the manifest holds no visual in that
        folder (the manifest is another build's, or empty)."""
        hit = self._hulls.get(model_path)
        if hit is not None:
            return hit
        p = model_path.replace("\\", "/").lstrip("/")
        folder, _, leaf = p.rpartition("/")
        model_dir = leaf[: -len(".model")] if leaf.endswith(".model") else leaf
        stems = sorted(
            q.rsplit("/", 1)[1][: -len(".geometry")]
            for q in self._paths
            if q.endswith(".geometry") and q.rsplit("/", 1)[0] == folder
        )
        if not stems:
            raise LookupError(f"the VFS manifest holds no visual of {model_path}")
        out: dict[str, tuple[list[float], str]] = {}
        prefix = f"{model_dir}_"
        for stem in stems:
            section = stem[len(prefix):] if stem.startswith(prefix) else "Full"
            world = world_matrices(self._fetch_doc(f"{folder}/{stem}.geometry"))
            for name, m in world.items():
                if name.startswith("HP_"):
                    out[name] = (m, section)
        self._hulls[model_path] = out
        return out

    def mount_bone(self, model_path: str) -> list[float] | None:
        """The mount model's ``Rotate_Y_BlendBone`` local matrix, or ``None``."""
        if model_path not in self._bones:
            geometry = _geometry_of(model_path)
            if self._fetch is None and geometry in _BONE_CACHE:
                self._bones[model_path] = _BONE_CACHE[geometry]
            else:
                doc = self._fetch_doc(geometry)
                self._bones[model_path] = _local_matrix(doc, ROTATE_Y_BLEND_BONE)
                if self._fetch is None:
                    _BONE_CACHE[geometry] = self._bones[model_path]
        return self._bones[model_path]

    def turns_alike(self, model_a: str, model_b: str) -> bool:
        """Whether two mount models' ``Rotate_Y_BlendBone`` rotations are
        equal (no bone: identity), so one placement transform fits both."""
        a = self.mount_bone(model_a) or list(_IDENTITY)
        b = self.mount_bone(model_b) or list(_IDENTITY)
        return all(abs(a[i] - b[i]) < 1e-4 for i in (0, 1, 2, 4, 5, 6, 8, 9, 10))

    def __call__(
        self, hull_name: str, hp_name: str, model_path: str, placed_model_path: str | None = None,
    ) -> dict[str, Any] | None:
        hull = self._ship.get(hull_name)
        hull_model = hull.get("model") if isinstance(hull, dict) else None
        if not isinstance(hull_model, str) or not hull_model or not isinstance(model_path, str) or not model_path:
            return None
        if placed_model_path and self.turns_alike(placed_model_path, model_path):
            return None
        hit = self.hull_hardpoints(hull_model).get(hp_name)
        if hit is None:
            return None
        world, section = hit
        return {
            "transform": placement_transform(world, self.mount_bone(model_path)),
            "parent_section": section,
        }


__all__ = [
    "HardpointResolver",
    "NATIVE_TO_METRES",
    "placement_transform",
    "world_matrices",
]
