"""PyInstaller entry script for the bundled webview launcher.

PyInstaller's ``--onefile`` mode wraps a single Python file as the
program entry. We can't point it directly at the console-script
``wows-webview-serve`` defined in ``pyproject.toml`` — that script
only exists as an entry point in installed metadata, not as a real
``.py`` file on disk, and PyInstaller's analysis pass needs the latter.

So this stub exists purely to forward to the actual CLI ``main()``.
Keep it tiny: every import here ends up frozen into the final exe,
and we want the import surface area to match what the runtime needs.

End-state UX (the whole reason this file exists):
  user double-clicks ``wows-model-export-webview.exe``
    → Python interpreter unpacks itself + the wheel into ``%TEMP%``
    → this module runs as ``__main__``
    → ``webview_serve.main()`` boots uvicorn at 127.0.0.1:5180
    → user opens the localhost URL the CLI prints
"""

from __future__ import annotations

import sys

from wows_model_export.cli.webview_serve import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
