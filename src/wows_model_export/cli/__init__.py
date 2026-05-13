"""Layer 5 — argparse wrappers around `compose` entries.

Each submodule defines a `main() -> int` invoked via the `wows-*` entry
points declared in `pyproject.toml`. CLIs translate argv → composer
kwargs, route `on_event` to a printer (plaintext or `--json-events`), and
exit with the composer's exit code.

CLI modules add no logic of their own — that lives in `compose`. If you
find yourself reaching for a helper here that isn't argparse plumbing,
push it down a layer.
"""

from __future__ import annotations

__all__: list[str] = []
