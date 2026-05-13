"""Layer 3 — pure transforms.

Take structured input, return structured output. No I/O, no subprocess.
Cheap (ms-class), trivially testable with synthetic fixtures.

Public symbols are added here as the corresponding lift lands. The
scaffold ships an empty namespace; see `migration/PIPELINE_API.md` §"Layer
3: resolve" for the planned shape.
"""

from __future__ import annotations

__all__: list[str] = []
