"""wows-model-export: extraction pipeline for World of Warships assets.

The package is organised into five layers; **import depth tells you what
the operation costs**:

    read     — pure data readers (μs, no side effects)
    toolkit  — subprocess wrappers around the Rust wowsunpack CLI (slow,
               writes files)
    resolve  — pure transforms over structured input (ms, no I/O)
    compose  — orchestrators that chain toolkit + resolve + read passes
               (slow, writes files, emits StepEvents)
    cli      — argparse wrappers around compose entries

Each layer may only depend on layers above it in the list above.

The top-level package re-exports a small public surface for consumers:

    from wows_model_export import read, toolkit, resolve, compose
    from wows_model_export.types import StepEvent, IngestResult, PipelineConfig
    from wows_model_export.errors import PipelineError, ToolkitError

See `docs/architecture.md` for the producer/consumer model and the layered
API design rationale.
"""

from __future__ import annotations

__version__ = "0.1.0a1"

__all__ = ["__version__"]
