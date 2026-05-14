"""GNU gettext `.mo` reader for WoWS UI strings.

Lifted from `tools/shared/wg_localization.py`.

Pure read — parses .mo files, returns typed data, no side effects.

WoWS ships its UI text catalogue as standard GNU gettext ``.mo`` files at
``<game_dir>/bin/<version>/res/texts/<lang>/LC_MESSAGES/global.mo``. Each
catalogue carries ~50 000 ``msgid -> translation`` pairs covering every
piece of UI copy: ship display names, achievement titles, modifier
descriptions, signal flags, and -- importantly for this pipeline --
camouflage / permoflage / skin display names.

The catalogue keys for cosmetics follow a stable rule: every Exterior
entity in ``GameParams`` (Camouflage / Permoflage / Skin / MSkin /
ShipDestruction species) is keyed in the catalogue by
``IDS_<exterior.name.uppercase()>``. So:

  GameParams entry                        msgid                              English
  --------------------------------------- ---------------------------------- -----------
  PCEC001_CAMO_1                          IDS_PCEC001_CAMO_1                 "Patches"
  PCEC002_CAMO_2                          IDS_PCEC002_CAMO_2                 "Stripes"
  PCEM001_HALLOWEEN19_MSKIN               IDS_PCEM001_HALLOWEEN19_MSKIN      "Infernal"
  PAES118_AZUR_ENTERPRISE                 IDS_PAES118_AZUR_ENTERPRISE        "Azur Lane"

Hit rate against the 2 216 Exteriors in the current GameParams snapshot
is ~94% via this rule directly; the remainder is split between catalogue-
hidden entries (legacy ``P*EP`` permoflages) and pure-internal entities
(boosts, multiboosts) that have no UI string by design.

Why a manual parser instead of stdlib ``gettext``: WoWS ships ``.mo``
files without a ``Content-Type: ... charset=...`` header, and Python's
``gettext.GNUTranslations._parse`` raises ``UnicodeDecodeError`` when it
falls back to ASCII for the catalogue body (which contains UTF-8
non-ASCII bytes for non-English realms). A 30-line manual parser using
``struct.unpack`` reads the ``.mo`` format directly and decodes message
bodies as UTF-8 unconditionally -- same result the game engine uses.

References:
  - GNU gettext ``.mo`` format spec:
    https://www.gnu.org/software/gettext/manual/html_node/MO-Files.html
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Game-dir + version discovery
# ---------------------------------------------------------------------------

# Bin directories are numeric (build IDs). Pick the largest as "latest".
_BIN_DIR_RE = re.compile(r"^\d+$")


def latest_bin_dir(game_dir: Path | None = None) -> Path:
    """Return the highest-numbered ``bin/<n>`` directory under ``game_dir``.

    Raises ``ValueError`` if ``game_dir`` is ``None`` (callers should pass
    ``PipelineConfig.load().require_game_dir()`` when config-driven
    resolution is desired). Raises ``FileNotFoundError`` if ``game_dir``
    has no ``bin/`` or no numeric subdirs (e.g. fresh-checked-out client
    dir).
    """
    if game_dir is None:
        raise ValueError(
            "game_dir is required; pass an explicit Path or call "
            "PipelineConfig.load().require_game_dir() at the call site."
        )
    root = Path(game_dir)
    bin_root = root / "bin"
    if not bin_root.is_dir():
        raise FileNotFoundError(f"No bin/ directory under {root}")
    candidates = [
        p for p in bin_root.iterdir()
        if p.is_dir() and _BIN_DIR_RE.fullmatch(p.name)
    ]
    if not candidates:
        raise FileNotFoundError(f"No numeric build dirs under {bin_root}")
    return max(candidates, key=lambda p: int(p.name))


def mo_path(
    *,
    game_dir: Path | None = None,
    bin_dir: Path | None = None,
    lang: str = "en",
) -> Path:
    """Resolve the ``.mo`` file path for ``lang`` under the given (or
    auto-discovered latest) ``bin_dir``.

    If both ``game_dir`` and ``bin_dir`` are ``None``, raises
    ``ValueError`` -- callers wanting config-driven resolution should
    pass ``PipelineConfig.load().require_game_dir()`` for ``game_dir``.
    """
    if bin_dir is None:
        bin_dir = latest_bin_dir(game_dir)
    return bin_dir / "res" / "texts" / lang / "LC_MESSAGES" / "global.mo"


# ---------------------------------------------------------------------------
# .mo format parser
# ---------------------------------------------------------------------------

_MO_MAGIC_LE = 0x950412DE
_MO_MAGIC_BE = 0xDE120495


def _parse_mo(path: Path) -> dict[str, str]:
    """Parse a GNU gettext ``.mo`` file -> ``{msgid: translation}`` dict.

    Decodes both message ids and translations as UTF-8 unconditionally.
    WoWS's catalogue lacks a charset header -- the bodies are UTF-8
    regardless of language, matching what the game engine consumes.
    """
    with path.open("rb") as f:
        data = f.read()
    if len(data) < 28:
        raise ValueError(f"{path}: too short to be a .mo file")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == _MO_MAGIC_LE:
        endian = "<"
    elif magic == _MO_MAGIC_BE:
        endian = ">"
    else:
        raise ValueError(f"{path}: bad .mo magic {magic:#x}")

    # Header: magic (skipped), revision, n, msgid-table-off, msgstr-table-off.
    _rev, n, omsg, otmsg = struct.unpack_from(f"{endian}IIII", data, 4)
    out: dict[str, str] = {}
    n_replacement_chars = 0
    n_total_chars = 0
    for i in range(n):
        mlen, moff = struct.unpack_from(f"{endian}II", data, omsg + i * 8)
        tlen, toff = struct.unpack_from(f"{endian}II", data, otmsg + i * 8)
        msg = data[moff:moff + mlen].decode("utf-8", errors="replace")
        tr = data[toff:toff + tlen].decode("utf-8", errors="replace")
        n_replacement_chars += msg.count("�") + tr.count("�")
        n_total_chars += len(msg) + len(tr)
        out[msg] = tr
    # Surface mojibake -- a corrupted catalogue produces U+FFFD substitutions
    # silently, and downstream camo-name lookups would just see "Y?dachi"
    # without any indication of why.
    if n_total_chars > 0 and n_replacement_chars / n_total_chars > 0.001:
        import sys as _sys
        print(
            f"  warn: {path}: {n_replacement_chars} U+FFFD replacement "
            f"char(s) in {n_total_chars} total -- catalogue may be "
            f"corrupted; consider re-extracting.",
            file=_sys.stderr,
        )
    return out


# ---------------------------------------------------------------------------
# Per-process cache + public API
# ---------------------------------------------------------------------------

@dataclass
class LocalizationDb:
    """Loaded catalogue for one (lang, mo_path) pair.

    Construct via :func:`load` to benefit from process-wide caching;
    direct instantiation works too if you've already parsed the table.
    """

    lang: str
    mo_path: Path
    table: dict[str, str]

    def get(self, msgid: str, default: str | None = None) -> str | None:
        """Return the translation for ``msgid`` or ``default`` if not found.

        WG occasionally ships catalogue entries whose translation equals
        the msgid (placeholder rows). We treat those as misses too -- a
        UI label that says ``IDS_PCEC005_CAMO_NY_3`` is no better than
        no translation at all.
        """
        v = self.table.get(msgid)
        if v is None or v == msgid or not v.strip():
            return default
        return v

    def exterior_display_name(
        self,
        exterior_name: str,
        *,
        fallback: str | None = None,
    ) -> str | None:
        """Look up an Exterior's localized display name.

        ``exterior_name`` is the GameParams entity ``name`` field
        (``"PCEC001_CAMO_1"`` / ``"PAES118_AZUR_ENTERPRISE"``). The
        catalogue key is ``f"IDS_{exterior_name.upper()}"``.

        Returns ``fallback`` (default ``None``) on miss; consumers
        typically want :func:`humanize_exterior_name` as the fallback.
        """
        if not exterior_name:
            return fallback
        return self.get(f"IDS_{exterior_name.upper()}", fallback)


_cache: dict[tuple[str, str], LocalizationDb] = {}


def load(
    *,
    game_dir: Path | None = None,
    bin_dir: Path | None = None,
    lang: str = "en",
    refresh: bool = False,
) -> LocalizationDb:
    """Return a cached :class:`LocalizationDb` for ``lang``.

    First call per (lang, bin_dir) pays the ``.mo`` parse cost
    (~100 ms for the en catalogue, ~50 K entries); subsequent calls are
    dict accesses. ``refresh=True`` forces a re-parse -- useful after a
    game patch.

    If neither ``game_dir`` nor ``bin_dir`` is provided, raises
    ``ValueError``; callers wanting config-driven resolution should pass
    ``PipelineConfig.load().require_game_dir()`` for ``game_dir``.
    """
    path = mo_path(game_dir=game_dir, bin_dir=bin_dir, lang=lang)
    key = (lang, str(path))
    if not refresh and key in _cache:
        return _cache[key]
    db = LocalizationDb(lang=lang, mo_path=path, table=_parse_mo(path))
    _cache[key] = db
    return db


# ---------------------------------------------------------------------------
# Humanized fallback (for when .mo lookup misses)
# ---------------------------------------------------------------------------

# WG entity names start with a 4-char index code (PCEC001, PAES118, ...).
# Strip that to get the descriptive part, then turn underscores into
# spaces and Title-Case the result.
_EXTERIOR_INDEX_RE = re.compile(r"^P[A-Z]{2,3}\d{3,4}_(.*)$")


def humanize_exterior_name(exterior_name: str) -> str:
    """Synthesize a passable display name from an Exterior entity name.

    ``"PAEP318_Iowa_1944"`` -> ``"Iowa 1944"``;
    ``"PCEM001_HALLOWEEN19_MSKIN"`` -> ``"Halloween19 Mskin"`` (admittedly
    rough -- the localization lookup is preferred when available).

    The output is deterministic and never empty -- falls back to the raw
    input if the index-prefix regex doesn't match.
    """
    if not exterior_name:
        return ""
    m = _EXTERIOR_INDEX_RE.match(exterior_name)
    descriptive = m.group(1) if m else exterior_name
    # Drop the trailing "_MSKIN" / "_CAMO_N" decorations -- they're
    # internal classifiers, not part of the user-facing name.
    descriptive = re.sub(r"_(MSKIN|CAMO_\d+|CAMO|PERM|PERMOFLAGE)$",
                         "", descriptive, flags=re.IGNORECASE)
    return descriptive.replace("_", " ").strip().title() or exterior_name


__all__ = [
    "LocalizationDb",
    "latest_bin_dir",
    "mo_path",
    "load",
    "humanize_exterior_name",
]
