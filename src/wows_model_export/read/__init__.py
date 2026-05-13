"""Layer 1 — pure data readers.

Operate on disk paths or already-loaded JSON dicts. Return typed
dataclasses. No subprocess calls, no network, no file writes. Fast (μs to
low-ms per call).

Public symbols are added here as the corresponding lift lands. The
scaffold ships an empty namespace; see `migration/PIPELINE_API.md` §"Layer
1: read" for the planned shape.
"""

from __future__ import annotations

__all__: list[str] = []
