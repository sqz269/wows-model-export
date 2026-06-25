"""Parse ``spaces/<space>/space.ubersettings`` — per-weather HDR tonemap +
IBL environment settings.

A space's ``.ubersettings`` is BigWorld "verbose XML": every leaf is a
``<name><value>\\t...\\t</value></name>`` pair, vectors are space-separated
inside the value, booleans are ``true`` / ``false``, and ``null`` marks an
empty ref. The file carries **one ``<Weather>`` block per weather preset** —
the first (no ``user_name`` attribute) is the base / ``Default`` preset, the
rest carry ``user_name="Storm"`` / ``"Cloudy"`` / … joining to the ``<param>``
ids in the sibling ``weathers.xml``.

Three render-parity asset classes live in each ``<Weather>`` block — these are
the producer-side inputs the webview / Unity consumers need to match WG's
final color + IBL (see ``reference/engine/wg_render_hdr_tonemap.md`` and
``wg_render_pmrem_ibl.md``):

* ``HDR`` — the GT (Uchimura) tonemap curve (``middleGray`` + ``gtContrast`` /
  ``gtLinearSectionStart`` / ``gtLinearSectionLength`` / ``gtBlack``), the
  bloom set, the eye-adaptation set, and the environment multipliers. (The
  Uchimura ``P`` / ``b`` params are not authored anywhere — fixed ``P=1,
  b=0``.)
* ``PostFX/ColorGrading`` — WG's port of UE4 per-luminance-range color grading
  (Saturation / Contrast / RGB Gain / RGB Offset for Global / Shadows /
  Midtones / Highlights, blended by the ``shadowsMaxRelLuminance`` /
  ``highlightsMinRelLuminance`` thresholds). Applied in linear HDR **after
  exposure, before the GT tonemap LUT** (RE'd in ``shaders/post_processing/
  hdr_resolve``). A consumer that skips this renders darker / less warm than
  the game (e.g. ``00_CO_ocean`` lifts highlights with a 5.2× red gain).
* ``PBS/settings/cubemapsPath`` — the directory holding the prefiltered PMREM
  reflection cube (conventionally ``main_probe.dds``; a few old docks use a
  numbered ``<n>/PMREM.dds``). The cube is a single-file 6-face DDS with a
  full prefiltered mip chain (mip index = roughness).
* ``PBS/SphericalHarmonics`` — the diffuse-irradiance SH: a base64 blob that
  decodes to 27 little-endian ``float32`` = **9 RGB L2 coefficients**
  (coeff-major, RGB-interleaved). Stored on disk, so no offline cube→SH
  projection is needed.

The ``PbsExtras`` block (``indirectMultShips`` / ``microShadowsIntensityShips``
/ …) carries the per-space IBL modulation knobs and is captured verbatim.

This module is pure Layer-1: it takes a path (or raw XML text) and returns
plain JSON-serialisable dicts. Locating / extracting the file from the VFS is
the caller's job (see ``compose.environment``).
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# The five Uchimura "Gran Turismo" curve knobs that a consumer's tonemap pass
# needs (``middleGray`` is the keyed-exposure target; the other four shape the
# curve). Kept as a named tuple of keys so consumers can pull just the curve.
GT_PARAM_KEYS: tuple[str, ...] = (
    "middleGray",
    "gtContrast",
    "gtLinearSectionStart",
    "gtLinearSectionLength",
    "gtBlack",
)

# Number of RGB spherical-harmonics coefficients (L2 / 3 bands).
SH_COEFF_COUNT = 9
# 9 coeffs * 3 channels * 4 bytes (float32).
_SH_BYTE_LEN = SH_COEFF_COUNT * 3 * 4


def _coerce(raw: str | None) -> Any:
    """Coerce a BigWorld ``<value>`` string into a Python scalar / list.

    ``"0.2040"`` -> ``0.204``; ``"1 1 1"`` -> ``[1.0, 1.0, 1.0]``;
    ``"true"``/``"false"`` -> ``bool``; ``"null"`` -> ``None``; anything that
    isn't all-numeric (paths, base64) is returned verbatim as ``str``.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or s == "null":
        return None
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    toks = s.split()
    try:
        nums = [float(t) for t in toks]
    except ValueError:
        return s
    return nums[0] if len(nums) == 1 else nums


def _settings_dict(settings_elem: ET.Element | None) -> dict[str, Any]:
    """Flatten a ``<settings>`` block into ``{leaf_tag: coerced_value}``.

    Each direct child is a ``<name><value>...</value></name>`` pair; children
    without a ``<value>`` (nested sub-blocks) are skipped — callers descend
    into those explicitly.
    """
    out: dict[str, Any] = {}
    if settings_elem is None:
        return out
    for child in settings_elem:
        value_node = child.find("value")
        if value_node is None:
            continue
        out[child.tag] = _coerce(value_node.text)
    return out


def decode_harmonics(b64: str | None) -> list[list[float]] | None:
    """Decode the ``harmonics`` base64 blob into 9 ``[r, g, b]`` SH coeffs.

    Returns ``None`` if the blob is absent, undecodable, or not exactly
    ``9 * 3 * 4`` bytes. Layout is coeff-major, RGB-interleaved, little-endian
    ``float32`` (``[c0.r, c0.g, c0.b, c1.r, ...]``).
    """
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (base64.binascii.Error, ValueError):
        return None
    if len(raw) != _SH_BYTE_LEN:
        return None
    flat = struct.unpack(f"<{SH_COEFF_COUNT * 3}f", raw)
    return [list(flat[i * 3 : i * 3 + 3]) for i in range(SH_COEFF_COUNT)]


def _parse_hdr(weather: ET.Element) -> dict[str, Any]:
    """Flatten the ``<HDR>`` sub-tree (own settings + Bloom / Tonemapping /
    Environment) into one dict of leaf params."""
    hdr_elem = weather.find("HDR")
    if hdr_elem is None:
        return {}
    out: dict[str, Any] = {}
    out.update(_settings_dict(hdr_elem.find("settings")))
    for sub in ("Bloom", "Tonemapping", "Environment"):
        sub_elem = hdr_elem.find(sub)
        if sub_elem is not None:
            out.update(_settings_dict(sub_elem.find("settings")))
    return out


def _parse_color_grading(weather: ET.Element) -> dict[str, Any]:
    """Flatten the ``<PostFX><ColorGrading><settings>`` block.

    This is WG's port of UE4's per-luminance-range color grading (the engine
    binds it as ``g_colorGradingShadows`` / ``g_colorGradingMidtones`` /
    ``g_colorGradingHighlights`` / ``g_colorGradingLumRanges`` — confirmed in
    ``shaders/post_processing/hdr_resolve``). Per range (Global / Shadows /
    Midtones / Highlights) it authors a scalar ``Saturation`` + scalar
    ``Contrast`` + RGB ``Gain`` + RGB ``Offset`` (UE4's model **minus Gamma**),
    plus the ``shadowsMaxRelLuminance`` / ``highlightsMinRelLuminance`` /
    ``maxLogLuminance`` range thresholds and a ``highlightsExposureOffset`` band.

    The grade runs in **linear HDR after exposure and BEFORE the GT tonemap
    LUT** — a consumer must apply it pre-tonemap to match. Returns ``{}`` when
    the weather authors no ColorGrading block. Values arrive coerced (RGB
    triples become 3-float lists).
    """
    cg = weather.find("PostFX/ColorGrading")
    if cg is None:
        cg = weather.find(".//ColorGrading")  # tolerate a moved/flat layout
    if cg is None:
        return {}
    return _settings_dict(cg.find("settings"))


def _parse_pbs(weather: ET.Element) -> dict[str, Any]:
    """Pull cubemapsPath, the SH coeffs, and the PbsExtras knobs from
    ``<PBS>``."""
    pbs_elem = weather.find("PBS")
    if pbs_elem is None:
        return {"cubemaps_path": None, "sh": None, "pbs_extras": {}}

    settings = _settings_dict(pbs_elem.find("settings"))
    cubemaps_path = settings.get("cubemapsPath")

    sh_b64: str | None = None
    sh_elem = pbs_elem.find("SphericalHarmonics")
    if sh_elem is not None:
        sh_settings = _settings_dict(sh_elem.find("settings"))
        raw = sh_settings.get("harmonics")
        sh_b64 = raw if isinstance(raw, str) else None

    extras_elem = pbs_elem.find("PbsExtras")
    pbs_extras = (
        _settings_dict(extras_elem.find("settings"))
        if extras_elem is not None
        else {}
    )

    return {
        "cubemaps_path": cubemaps_path if isinstance(cubemaps_path, str) else None,
        "sh": decode_harmonics(sh_b64),
        "pbs_extras": pbs_extras,
    }


def _parse_sun(weather: ET.Element) -> dict[str, Any] | None:
    """Pull the directional sun from ``<Sky><Sun>`` — ``yaw`` / ``pitch``
    (azimuth / elevation in degrees) + ``color`` (RGB; alpha dropped). Returns
    ``None`` when the weather authors no Sun block.

    Note: ``Sea/sunLightPow`` (water-specular power) and ``Sky/SunDisk`` (the
    visible sun sprite) are deliberately NOT this — only the directional light.
    """
    sky = weather.find("Sky")
    if sky is None:
        return None
    sun = sky.find("Sun")
    if sun is None:
        return None
    s = _settings_dict(sun.find("settings"))
    yaw = s.get("yaw")
    pitch = s.get("pitch")
    raw_color = s.get("color")
    if isinstance(raw_color, list):
        color = [float(c) for c in raw_color[:3]]
    elif isinstance(raw_color, (int, float)):
        color = [float(raw_color)] * 3
    else:
        color = None
    return {
        "yaw": float(yaw) if isinstance(yaw, (int, float)) else None,
        "pitch": float(pitch) if isinstance(pitch, (int, float)) else None,
        "color": color,
    }


def _parse_wetness(
    weather: ET.Element, pbs_extras: dict[str, Any]
) -> dict[str, Any] | None:
    """Pull the per-weather rain-wetness drivers.

    Two live in the top-level ``<Weather><settings>`` (``puddlesIntensity`` deck
    puddles + ``ripplesIntensity`` water ripples; plus the rarely-authored
    ``falloutIntensity`` map gimmick — note the lowercase ``f``). Two more —
    ``overallWetness`` (hull surface-wetness scalar) + ``wetnessColor`` (tint) —
    live one level down in ``<PBS><PbsExtras><settings>`` and arrive here
    pre-flattened as ``pbs_extras`` (so we don't re-walk ``<PBS>``). All are
    *independent* knobs: a weather can drive deck puddles without bumping the
    hull-wetness tint (e.g. 14_Atlantic/Storm = puddles 0.75 but overallWetness 0).

    Returns ``None`` when the weather has no top-level ``<settings>`` at all
    (defends old/dock spaces; never happens in current builds). Otherwise the
    five fields with missing scalars defaulted to 0.0 and a missing colour to
    ``None``. ``wetnessColor`` is RGBA (4 floats) in WG content — the 4th term is
    an alpha/strength factor, kept verbatim (consumer takes ``[:3]`` for RGB).
    """
    settings_elem = weather.find("settings")
    if settings_elem is None:
        return None
    s = _settings_dict(settings_elem)

    def _scalar(d: dict[str, Any], key: str) -> float:
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0

    raw_color = pbs_extras.get("wetnessColor")
    if isinstance(raw_color, list):
        wetness_color: list[float] | None = [float(c) for c in raw_color]
    elif isinstance(raw_color, (int, float)):
        wetness_color = [float(raw_color)]
    else:
        wetness_color = None

    return {
        "puddlesIntensity": _scalar(s, "puddlesIntensity"),
        "ripplesIntensity": _scalar(s, "ripplesIntensity"),
        "falloutIntensity": _scalar(s, "falloutIntensity"),
        "overallWetness": _scalar(pbs_extras, "overallWetness"),
        "wetnessColor": wetness_color,
    }


def parse_ubersettings_text(xml_text: str) -> dict[str, Any]:
    """Parse ubersettings XML text into a per-weather environment dict.

    Returns::

        {
          "version": <int|None>,
          "weather_order": ["Default", "Storm", ...],
          "weathers": {
            "Default": {
              "cubemaps_path": "content/location/skybox/.../Default/",
              "hdr": {middleGray, gtContrast, gtLinearSectionStart,
                      gtLinearSectionLength, gtBlack, brightThreshold,
                      bloomAmount, bloomRadius, bloomTint, adaptationSpeed,
                      eyeDarkLimit, eyeLightLimit, skyLumMultiplier,
                      ambientLumMultiplier, hdrMapExposureOffset, ...},
              "sh": [[r, g, b], ... x9] | None,
              "pbs_extras": {indirectMultShips, microShadowsIntensityShips,
                             overallWetness, wetnessColor, ...},
              "sun": {yaw, pitch, color} | None,
              "wetness": {puddlesIntensity, ripplesIntensity, falloutIntensity,
                          overallWetness, wetnessColor} | None,
            },
            "Storm": {...}, ...
          },
        }

    The base ``<Weather>`` (no ``user_name``) is keyed ``"Default"``.
    """
    root = ET.fromstring(xml_text)
    # Tolerate being handed either the document root (<space.ubersettings>)
    # or a pre-located <Root> element.
    root_elem = root.find("Root")
    if root_elem is None:
        root_elem = root if root.tag == "Root" else root

    version_raw = root.find("version")
    version = None
    if version_raw is not None:
        v = _coerce(version_raw.text)
        version = int(v) if isinstance(v, (int, float)) else None

    weathers: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for weather in root_elem.findall("Weather"):
        name = weather.get("user_name") or "Default"
        # Defend against an unexpected second nameless block.
        if name in weathers:
            name = f"{name}_{len(weathers)}"
        pbs = _parse_pbs(weather)
        weathers[name] = {
            "cubemaps_path": pbs["cubemaps_path"],
            "hdr": _parse_hdr(weather),
            "color_grading": _parse_color_grading(weather),
            "sh": pbs["sh"],
            "pbs_extras": pbs["pbs_extras"],
            "sun": _parse_sun(weather),
            "wetness": _parse_wetness(weather, pbs["pbs_extras"]),
        }
        order.append(name)

    return {"version": version, "weather_order": order, "weathers": weathers}


def parse_ubersettings(path: Path | str) -> dict[str, Any]:
    """Parse a ``space.ubersettings`` file from disk. See
    :func:`parse_ubersettings_text`."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_ubersettings_text(text)


def gt_tonemap(weather: dict[str, Any]) -> dict[str, float]:
    """Extract just the GT tonemap curve params from a parsed weather dict.

    Returns the five :data:`GT_PARAM_KEYS` (missing ones default to the
    Uchimura-neutral value) plus a ``P``/``b`` of ``1.0``/``0.0`` so a
    consumer can feed a complete curve. ``hdrMapExposureOffset`` is included
    because the keyed exposure (``middleGray / avgLum * exp2(offset)``) needs
    it.
    """
    hdr = weather.get("hdr") or {}
    out: dict[str, float] = {}
    for key in GT_PARAM_KEYS:
        v = hdr.get(key)
        out[key] = float(v) if isinstance(v, (int, float)) else 0.0
    offset = hdr.get("hdrMapExposureOffset")
    out["hdrMapExposureOffset"] = (
        float(offset) if isinstance(offset, (int, float)) else 0.0
    )
    # Uchimura P (max display brightness) / b (pedestal) are not authored in
    # WG content — fixed.
    out["P"] = 1.0
    out["b"] = 0.0
    return out


# The four luminance ranges WG grades independently (Global applies to all).
COLOR_GRADE_RANGES: tuple[str, ...] = ("Global", "Shadows", "Midtones", "Highlights")


def color_grade(weather: dict[str, Any]) -> dict[str, Any] | None:
    """Group the flat ``color_grading`` params into per-range CDL for consumers.

    Returns ``None`` when the weather authors no grade. Otherwise::

        {
          "ranges": {maxLogLuminance, shadowsMaxRelLuminance,
                     highlightsMinRelLuminance},
          "global":     {saturation, contrast, gain: [r,g,b], offset: [r,g,b]},
          "shadows":    {...}, "midtones": {...}, "highlights": {...},
          "highlightsExposure": {offset, minLum, maxLum},
        }

    Apply order (per the ``hdr_resolve`` RE) is **linear HDR, after exposure,
    before the GT tonemap LUT**: for each pixel, weight Shadows/Midtones/
    Highlights from the pixel luminance via the ``ranges`` thresholds, run the
    UE4 ``ColorCorrect`` (saturation → contrast about 0.18 → gain → offset) per
    active range plus Global, then tonemap. WG omits UE4's per-range Gamma.
    """
    cg = weather.get("color_grading") or {}
    if not cg:
        return None

    def _f(key: str) -> float:
        v = cg.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0

    def _rgb(key: str, default: float) -> list[float]:
        v = cg.get(key)
        if isinstance(v, list):
            return [float(c) for c in (v + [default, default, default])[:3]]
        if isinstance(v, (int, float)):
            return [float(v)] * 3
        return [default, default, default]

    def _range(suffix: str) -> dict[str, Any]:
        return {
            "saturation": _f(f"colorSaturation{suffix}"),
            "contrast": _f(f"colorContrast{suffix}"),
            "gain": _rgb(f"colorGain{suffix}", 1.0),
            "offset": _rgb(f"colorOffset{suffix}", 0.0),
        }

    return {
        "ranges": {
            "maxLogLuminance": _f("maxLogLuminance"),
            "shadowsMaxRelLuminance": _f("shadowsMaxRelLuminance"),
            "highlightsMinRelLuminance": _f("highlightsMinRelLuminance"),
        },
        "global": _range("Global"),
        "shadows": _range("Shadows"),
        "midtones": _range("Midtones"),
        "highlights": _range("Highlights"),
        "highlightsExposure": {
            "offset": _f("highlightsExposureOffset"),
            "minLum": _f("highlightsExposureOffsetMinLum"),
            "maxLum": _f("highlightsExposureOffsetMaxLum"),
        },
    }


__all__ = [
    "GT_PARAM_KEYS",
    "COLOR_GRADE_RANGES",
    "SH_COEFF_COUNT",
    "decode_harmonics",
    "parse_ubersettings",
    "parse_ubersettings_text",
    "gt_tonemap",
    "color_grade",
]
