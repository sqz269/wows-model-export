"""Layer 1 — pure data readers.

Operate on disk paths or already-loaded JSON dicts. Return typed
dataclasses. No subprocess calls, no network, no file writes. Fast (μs
to low-ms per call).

Lifted so far:

    mfm                — .mfm MaterialPrototype reader (from wg_mfm.py)
"""

from __future__ import annotations

from .mfm import (
    DEFAULT_EMISSIVE_POWER,
    MaterialPrototype,
    get_emissive_power,
    parse_mfm,
)

__all__ = [
    "MaterialPrototype",
    "parse_mfm",
    "get_emissive_power",
    "DEFAULT_EMISSIVE_POWER",
]
