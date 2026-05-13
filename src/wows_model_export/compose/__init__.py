"""Layer 4 — orchestrators.

Chain `toolkit` invocations, `resolve` transforms, and `read` passes into
end-to-end procedures. Slow (seconds to minutes), write files, emit
`StepEvent`s through an optional `on_event` callback.

Public symbols are added here as the corresponding lift lands. The
scaffold ships an empty namespace; see `migration/PIPELINE_API.md` §"Layer
4: compose" for the planned shape.
"""

from __future__ import annotations

__all__: list[str] = []
