"""Layer 2 — subprocess wrappers around the wowsunpack CLI.

Slow (subprocess spawn + Rust-side asset parsing), writes files, returns
typed paths + stderr + elapsed time as `ToolkitResult`. The binary
location is resolved via `PipelineConfig.toolkit_bin` (env var
`WOWS_TOOLKIT_BIN`, then `shutil.which("wowsunpack")`, then a build-time
default).

Public symbols are added here as the corresponding lift lands. The
scaffold ships an empty namespace; see `migration/PIPELINE_API.md` §"Layer
2: toolkit" for the planned shape.
"""

from __future__ import annotations

__all__: list[str] = []
