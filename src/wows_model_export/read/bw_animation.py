"""WoWS animation clips (``.anim``) and animation state machines (``.ssm``).

Layer 1 reader: bytes / text in, plain data out. No VFS access.

A model prototype in ``assets.bin`` lists the state machines it loads
(:mod:`.model_prototypes`). A state machine is XML: trigger and
random-float parameters, then nodes (``sleep``, ``animation`` and
``sequence``) joined by transitions with conditions. An ``animation`` node
names its clip as ``content/animation/.../<stem>.fbx`` (the authoring
file); the game ships the clip as ``<stem>.anim`` beside that path, and an
optional ``clipName`` selects one named take of it.

``.anim`` layout (little-endian; strings are ``u32 length`` + bytes, no
terminator)::

    u32   frame_count
    f32   fps                              30.0 on every clip of build 13533378
    u32   take_count
          take_count x { str name, f32 start_frame, f32 end_frame, u8 flag }
    u8    compressed                       1: codec 1 below; 0: codec 0 (or 2)
    u8    unknown
    f32 x 3                                key-reduction tolerances (0.001, 0.03, 0.01)
    u8    unknown
    u32   bone_count
          bone_count x str bone            node names of the model
    u32   codec                            0 raw, 1 keyed tracks, 2 quantised
    u32   block_size
          block                            block_size bytes

    codec 0 block:  u32 frame_count, u32 bone_count, f32 fps, then
                    frame_count x bone_count x { f32 t[3], f32 q[4], f32 s[3] }
    codec 1 block:  u32 frame_count, u32 bone_count, f32 fps,
                    u32 n_keyed, u32 keyed_offset, u32 n_const, u32 const_offset,
                    u32 n_keyed, u32 offset[n_keyed], u32 n_const, u32 offset[n_const],
                    keyed tracks:    { u8 kind, u32 bone, u32 n, f32 time[n], value[n] }
                    constant tracks: { u8 kind, u32 bone, value }
                    (offsets from the block start / from the start of each section)
    kind:           0 translation (f32 x 3), 1 rotation (quaternion x, y, z, w),
                    2 scale (f32 x 3)

Values are a bone's absolute local transform in the BigWorld frame
(left-handed, native units): the first key of a clip that starts at rest
equals the node's rest transform in the model's visual. Times are seconds;
a clip lasts ``(frame_count - 1) / fps``. Codec 2 (six location props of
build 13533378) holds per-bone ranges and bit-packed samples; it is not
decoded and raises :class:`UnsupportedAnimCodec`.

:func:`gltf_channels` converts a clip to the frame of the toolkit's GLBs:
metres (native x 15), right-handed with Z negated. A translation becomes
``15 * (x, y, -z)`` and a rotation quaternion ``(-x, -y, z, w)`` (the
conjugation by ``diag(1, 1, -1)`` the toolkit applies to node matrices).

Specified in ``reference/schemas/METADATA_SPEC.md`` §18.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

#: Native BigWorld unit -> metres (the toolkit's ``NATIVE_TO_METRES``).
NATIVE_TO_METRES = 15.0

#: Track kinds of codec 1 and the glTF channel path each one drives.
KIND_PATHS: dict[int, str] = {0: "translation", 1: "rotation", 2: "scale"}
_KIND_WIDTH: dict[int, int] = {0: 3, 1: 4, 2: 3}


class AnimFormatError(ValueError):
    """The bytes are not a ``.anim`` clip this reader understands."""


class UnsupportedAnimCodec(AnimFormatError):
    """A ``.anim`` clip in a codec this reader recognises but does not decode."""

    def __init__(self, codec: int) -> None:
        super().__init__(f"anim codec {codec} is not decoded")
        self.codec = codec


@dataclass(frozen=True)
class Take:
    """A named frame range of a clip (``clipName`` in a state machine)."""

    name: str
    start_frame: float
    end_frame: float
    flag: int


@dataclass
class Track:
    """One bone's translation, rotation or scale over time.

    ``times`` is None for a constant track (one value for the whole clip).
    """

    bone: str
    path: str
    times: list[float] | None
    values: list[tuple[float, ...]]


@dataclass
class AnimClip:
    frame_count: int
    fps: float
    takes: list[Take]
    compressed: bool
    tolerances: tuple[float, float, float]
    bones: list[str]
    codec: int
    tracks: list[Track] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.frame_count - 1) / self.fps if self.fps > 0 and self.frame_count > 1 else 0.0

    def take(self, name: str) -> Take | None:
        for t in self.takes:
            if t.name == name:
                return t
        return None


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.b = data
        self.o = 0

    def need(self, n: int) -> None:
        if self.o + n > len(self.b):
            raise AnimFormatError(f"truncated at byte {self.o} (need {n} more of {len(self.b)})")

    def u8(self) -> int:
        self.need(1)
        v = self.b[self.o]
        self.o += 1
        return v

    def u32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<I", self.b, self.o)[0]
        self.o += 4
        return v

    def f32(self) -> float:
        self.need(4)
        v = struct.unpack_from("<f", self.b, self.o)[0]
        self.o += 4
        return v

    def floats(self, n: int) -> tuple[float, ...]:
        self.need(4 * n)
        v = struct.unpack_from(f"<{n}f", self.b, self.o)
        self.o += 4 * n
        return v

    def string(self) -> str:
        n = self.u32()
        if n > 4096:
            raise AnimFormatError(f"string of {n} bytes at byte {self.o - 4}")
        self.need(n)
        raw = self.b[self.o:self.o + n]
        self.o += n
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AnimFormatError(f"string at byte {self.o - n} is not UTF-8") from e


def parse_anim(data: bytes) -> AnimClip:
    """Parse a ``.anim`` clip. Raises :class:`AnimFormatError` on bytes that
    do not follow the layout (every byte must be consumed) and
    :class:`UnsupportedAnimCodec` on codec 2."""
    r = _Reader(data)
    frame_count = r.u32()
    fps = r.f32()
    n_takes = r.u32()
    if n_takes > 1024:
        raise AnimFormatError(f"{n_takes} takes")
    takes = [Take(r.string(), r.f32(), r.f32(), r.u8()) for _ in range(n_takes)]
    compressed = bool(r.u8())
    r.u8()
    tolerances = r.floats(3)
    r.u8()
    n_bones = r.u32()
    if n_bones > 65536:
        raise AnimFormatError(f"{n_bones} bones")
    bones = [r.string() for _ in range(n_bones)]
    codec = r.u32()
    size = r.u32()
    start = r.o
    r.need(size)
    if start + size != len(data):
        raise AnimFormatError(f"block ends at {start + size}, file at {len(data)}")
    clip = AnimClip(frame_count, fps, takes, compressed, (tolerances[0], tolerances[1], tolerances[2]),
                    bones, codec)
    if codec == 2:
        raise UnsupportedAnimCodec(2)
    q = _Reader(data)
    q.o = start
    b_frames, b_bones, b_fps = q.u32(), q.u32(), q.f32()
    if b_frames != frame_count or b_bones != n_bones or b_fps != fps:
        raise AnimFormatError(
            f"block header ({b_frames} frames, {b_bones} bones, {b_fps} fps) differs from "
            f"the file header ({frame_count}, {n_bones}, {fps})")
    if codec == 0:
        n = frame_count * n_bones
        if size != 12 + 40 * n:
            raise AnimFormatError(f"raw block of {size} bytes for {frame_count} x {n_bones}")
        raw = q.floats(10 * n)
        times = [i / fps for i in range(frame_count)]
        for bi, bone in enumerate(bones):
            for path, lo, hi in (("translation", 0, 3), ("rotation", 3, 7), ("scale", 7, 10)):
                vals = [tuple(raw[(f * n_bones + bi) * 10 + lo:(f * n_bones + bi) * 10 + hi])
                        for f in range(frame_count)]
                clip.tracks.append(Track(bone, path, list(times), vals))
        return clip
    if codec != 1:
        raise AnimFormatError(f"unknown codec {codec}")
    n_keyed, keyed_off, n_const, const_off = q.u32(), q.u32(), q.u32(), q.u32()
    if q.u32() != n_keyed:
        raise AnimFormatError("keyed track count differs from its offset table")
    keyed = [q.u32() for _ in range(n_keyed)]
    if q.u32() != n_const:
        raise AnimFormatError("constant track count differs from its offset table")
    const = [q.u32() for _ in range(n_const)]
    if q.o - start != keyed_off:
        raise AnimFormatError(f"keyed section at {keyed_off}, header ends at {q.o - start}")

    def bone_name(i: int) -> str:
        if i >= n_bones:
            raise AnimFormatError(f"track bone {i} of {n_bones}")
        return bones[i]

    ends = ([keyed_off + o for o in keyed[1:]] + [const_off]) if keyed else []
    for o, end in zip(keyed, ends, strict=True):
        t = _Reader(data)
        t.o = start + keyed_off + o
        kind, bone, n = t.u8(), t.u32(), t.u32()
        if kind not in _KIND_WIDTH:
            raise AnimFormatError(f"track kind {kind}")
        times = list(t.floats(n))
        width = _KIND_WIDTH[kind]
        vals = [t.floats(width) for _ in range(n)]
        if t.o - start != end:
            raise AnimFormatError(f"keyed track ends at {t.o - start}, next starts at {end}")
        clip.tracks.append(Track(bone_name(bone), KIND_PATHS[kind], times, vals))
    ends = ([const_off + o for o in const[1:]] + [size]) if const else []
    for o, end in zip(const, ends, strict=True):
        t = _Reader(data)
        t.o = start + const_off + o
        kind, bone = t.u8(), t.u32()
        if kind not in _KIND_WIDTH:
            raise AnimFormatError(f"track kind {kind}")
        vals = [t.floats(_KIND_WIDTH[kind])]
        if t.o - start != end:
            raise AnimFormatError(f"constant track ends at {t.o - start}, next starts at {end}")
        clip.tracks.append(Track(bone_name(bone), KIND_PATHS[kind], None, vals))
    return clip


# ---------------------------------------------------------------------------
# Conversion to the GLB frame
# ---------------------------------------------------------------------------


def _lerp(a: tuple[float, ...], b: tuple[float, ...], u: float) -> tuple[float, ...]:
    return tuple(x + (y - x) * u for x, y in zip(a, b, strict=True))


def _slerp(a: tuple[float, ...], b: tuple[float, ...], u: float) -> tuple[float, ...]:
    d = sum(x * y for x, y in zip(a, b, strict=True))
    if d < 0:
        b, d = tuple(-y for y in b), -d
    if d > 0.9995:
        out = _lerp(a, b, u)
    else:
        th = math.acos(min(1.0, d))
        s = math.sin(th)
        wa, wb = math.sin((1 - u) * th) / s, math.sin(u * th) / s
        out = tuple(wa * x + wb * y for x, y in zip(a, b, strict=True))
    n = math.sqrt(sum(x * x for x in out)) or 1.0
    return tuple(x / n for x in out)


def sample_track(track: Track, t: float) -> tuple[float, ...]:
    """The track's value at ``t`` (seconds), held before the first and after
    the last key, linear in between (slerp for rotations)."""
    if track.times is None or len(track.values) == 1:
        return track.values[0]
    ts = track.times
    if t <= ts[0]:
        return track.values[0]
    if t >= ts[-1]:
        return track.values[-1]
    lo, hi = 0, len(ts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid
        else:
            hi = mid
    span = ts[hi] - ts[lo]
    u = (t - ts[lo]) / span if span > 0 else 0.0
    mix = _slerp if track.path == "rotation" else _lerp
    return mix(track.values[lo], track.values[hi], u)


def to_gltf_value(path: str, v: tuple[float, ...], *, scale: float = NATIVE_TO_METRES) -> list[float]:
    """One BigWorld local-transform value in the toolkit's GLB frame."""
    if path == "translation":
        return [v[0] * scale, v[1] * scale, -v[2] * scale]
    if path == "rotation":
        return [-v[0], -v[1], v[2], v[3]]
    return [v[0], v[1], v[2]]


def _window(track: Track, t0: float, t1: float) -> tuple[list[float] | None, list[tuple[float, ...]]]:
    if track.times is None:
        return None, list(track.values)
    times = [t for t in track.times if t0 < t < t1]
    keys = [t0, *times, t1] if t1 > t0 else [t0]
    return [k - t0 for k in keys], [sample_track(track, k) for k in keys]


def _constant(values: list[list[float]], eps: float = 1e-6) -> bool:
    return all(max(abs(a - b) for a, b in zip(v, values[0], strict=True)) <= eps for v in values[1:])


def gltf_channels(clip: AnimClip, *, take: str | None = None,
                  scale: float = NATIVE_TO_METRES, digits: int = 6) -> tuple[list[dict[str, Any]], float]:
    """The clip's tracks as channels in the GLB frame, and the duration in seconds.

    Each channel is ``{"node", "path", "times", "values"}``; a constant
    track has one key at time 0. ``take`` cuts the clip to that named frame
    range, with keys interpolated at its ends and times starting at 0.
    Rotations are kept on one hemisphere (each key's dot product with the
    previous key is >= 0), so a linear or spherical interpolation between
    two keys takes the short way. Raw (codec 0) tracks whose value never
    changes are written as constant tracks.
    """
    t0, t1 = 0.0, clip.duration_s
    if take is not None:
        tk = clip.take(take)
        if tk is None:
            raise KeyError(f"take {take!r} not in the clip ({[t.name for t in clip.takes]})")
        t0, t1 = tk.start_frame / clip.fps, tk.end_frame / clip.fps
    out: list[dict[str, Any]] = []
    for tr in clip.tracks:
        times, vals = _window(tr, t0, t1)
        conv = [to_gltf_value(tr.path, v, scale=scale) for v in vals]
        if tr.path == "rotation":
            for i in range(1, len(conv)):
                if sum(a * b for a, b in zip(conv[i - 1], conv[i], strict=True)) < 0:
                    conv[i] = [-x for x in conv[i]]
        if times is not None and len(conv) > 1 and _constant(conv):
            times, conv = None, conv[:1]
        out.append({
            "node": tr.bone,
            "path": tr.path,
            "times": [round(t, digits) for t in (times if times is not None else [0.0])],
            "values": [[round(x, digits) for x in v] for v in conv],
        })
    return out, round(t1 - t0, digits)


# ---------------------------------------------------------------------------
# State machines (.ssm)
# ---------------------------------------------------------------------------


def _text(e: ET.Element | None, tag: str) -> str | None:
    if e is None:
        return None
    v = e.findtext(tag)
    return v.strip() if v is not None else None


def _bool(s: str | None) -> bool | None:
    if s is None or s == "":
        return None
    return s.lower() in ("true", "1")


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def clip_path_for(resource: str) -> str:
    """The shipped clip of an ``animation`` node's resource (``x.fbx`` -> ``x.anim``)."""
    stem = resource.rsplit(".", 1)[0] if "." in resource.rsplit("/", 1)[-1] else resource
    return stem + ".anim"


def parse_state_machine(text: str) -> dict[str, Any]:
    """An ``.ssm`` as a plain graph.

    Returns ``{"params", "entry", "nodes"}``. ``params`` lists
    ``{"name", "type"}`` (``trigger`` or ``random_float``). Each node is
    ``{"name", "type", "transitions", ...}``:

    - ``sleep``: ``time_s`` ``[min, max]`` (the authored pair);
    - ``animation``: ``resource`` (the ``.fbx`` path as authored), ``clip_vfs``
      (the shipped ``.anim``), ``take`` (``clipName``, or None for the whole
      clip), ``loop``;
    - ``sequence``: ``resource`` (a ``.seq``), ``loop`` (``isLoop``) and
      ``loop_count`` (``loop``).

    A transition is ``{"to", "blend_s", "root_motion", "conditions"}``; a
    condition is ``{"type": "trigger", "name"}``, ``{"type": "exit_time",
    "offset"}`` or ``{"type": "random_float", "name", "range": [a, b]}``
    (unknown types keep their child elements as strings). ``entry`` is the
    first node, the state the machine starts in. ``uiPosition`` (editor
    layout) is dropped.
    """
    root = ET.fromstring(text)
    params = [{"name": _text(p, "name"), "type": _text(p, "type")}
              for p in root.findall("./params/param")]
    nodes: list[dict[str, Any]] = []
    for n in root.findall("node"):
        node: dict[str, Any] = {"name": _text(n, "name"), "type": _text(n, "type")}
        ty = node["type"]
        if ty == "sleep":
            parts = (_text(n, "time") or "").split()
            node["time_s"] = [float(x) for x in parts[:2]] if parts else None
        elif ty == "animation":
            res = _text(n, "resource") or ""
            node["resource"] = res
            node["clip_vfs"] = clip_path_for(res) if res else None
            node["take"] = _text(n, "clipName") or None
            node["loop"] = _bool(_text(n, "isLoop"))
        elif ty == "sequence":
            node["resource"] = _text(n, "resource")
            node["loop"] = _bool(_text(n, "isLoop"))
            lc = _text(n, "loop")
            node["loop_count"] = int(lc) if lc not in (None, "") and lc.lstrip("-").isdigit() else lc
        transitions = []
        for tr in n.findall("transition"):
            conds = []
            for c in tr.findall("./conditions/condition"):
                ct = _text(c, "type")
                if ct == "trigger":
                    conds.append({"type": ct, "name": _text(c, "name")})
                elif ct == "exit_time":
                    conds.append({"type": ct, "offset": _float(_text(c, "exitOffset"))})
                elif ct == "random_float":
                    conds.append({"type": ct, "name": _text(c, "name"),
                                  "range": [_float(_text(c, "rangeStart")), _float(_text(c, "rangeEnd"))]})
                else:
                    conds.append({"type": ct, **{x.tag: (x.text or "").strip() for x in c if x.tag != "type"}})
            transitions.append({
                "to": _text(tr, "to"),
                "blend_s": _float(_text(tr, "blendDuration")),
                "root_motion": _bool(_text(tr, "allowRootMotion")),
                "conditions": conds,
            })
        node["transitions"] = transitions
        nodes.append(node)
    return {"params": params, "entry": nodes[0]["name"] if nodes else None, "nodes": nodes}


__all__ = [
    "KIND_PATHS",
    "NATIVE_TO_METRES",
    "AnimClip",
    "AnimFormatError",
    "Take",
    "Track",
    "UnsupportedAnimCodec",
    "clip_path_for",
    "gltf_channels",
    "parse_anim",
    "parse_state_machine",
    "sample_track",
    "to_gltf_value",
]
