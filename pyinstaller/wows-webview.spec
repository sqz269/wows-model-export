# PyInstaller spec for the wows-model-export webview single-file exe.
#
# Build (from the repo root, with the venv active and pyinstaller
# installed):
#
#     pyinstaller pyinstaller/wows-webview.spec --noconfirm
#
# Output: ``dist/wows-model-export-webview.exe``
#
# ---------------------------------------------------------------------------
# Why --onefile and not --onedir?
# ---------------------------------------------------------------------------
#  * --onefile: one downloadable artifact, but the Python interpreter +
#    dependencies are unpacked to ``%TEMP%/_MEIxxxxxx`` on every launch.
#    First-run extraction is ~5-10 s on a typical SSD; subsequent runs
#    benefit from OS file cache. Distribution UX is much better — users
#    grab a single file from GitHub Releases.
#  * --onedir: ~instant startup, but ships a folder full of DLLs. Less
#    friendly for the "download + double-click" flow we're targeting.
#
# Switch to --onedir later if startup becomes a real complaint. Set
# ``onefile = False`` below; the EXE() block already handles both modes.
#
# ---------------------------------------------------------------------------
# Why console=True?
# ---------------------------------------------------------------------------
# ``wows-webview-serve`` is a CLI that prints the localhost URL + uvicorn
# logs to stdout. A windowed (--noconsole) build would swallow that
# output and leave the user staring at an empty taskbar entry. The exe
# spawns a console window; the user reads the URL from there and clicks
# it (or the launcher could open the browser later — TODO).
#
# ---------------------------------------------------------------------------
# Why no UPX?
# ---------------------------------------------------------------------------
# UPX-compressed Python exes get flagged by Windows Defender + a long
# tail of corporate AV vendors (Python's bytecode bootstrap pattern is
# similar to packed malware). The size win isn't worth the false-positive
# noise. ``upx=False`` everywhere below.
#
# ---------------------------------------------------------------------------
# Assumptions about parallel agent work (verify when their branches merge)
# ---------------------------------------------------------------------------
#  1. The webview static-serving agent puts the built Svelte bundle at
#     ``src/wows_model_export/_static/webview/`` so the FastAPI app can
#     mount it from the wheel. We bundle that path into the exe via
#     ``datas`` below. If they pick a different path, update SOURCE_STATIC.
#  2. The scipy → numpy KD-tree agent removes scipy from runtime deps.
#     Nothing here needs to change — the spec doesn't list scipy as a
#     hidden import either way.
#  3. ``wowsunpack.exe`` (Rust binary, separate fork) is NOT vendored.
#     The exe resolves it at runtime via env var → user config file →
#     ``shutil.which("wowsunpack")``. Document for users that they still
#     need to build / drop wowsunpack on PATH separately.

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# The spec file lives at <repo>/pyinstaller/wows-webview.spec; PyInstaller
# runs with cwd=<repo>, so REPO_ROOT is one level up from the spec dir.
# We compute it from os.getcwd() because PyInstaller does not expose the
# spec file's path as a variable inside this exec context.
REPO_ROOT = Path(os.getcwd()).resolve()
ENTRY = str(REPO_ROOT / "pyinstaller" / "entry.py")

# Bundled webview static assets. Source: the static-serving agent's
# package-data dir. Destination inside the frozen app: same relative
# layout under the package, so ``importlib.resources`` /
# ``Path(__file__).parent`` lookups in ``server/main.py`` keep working
# untouched between editable installs and frozen exes.
SOURCE_STATIC = REPO_ROOT / "src" / "wows_model_export" / "_static" / "webview"
TARGET_STATIC = "wows_model_export/_static/webview"

# datas is a list of (source, dest_dir_in_bundle) tuples. PyInstaller
# copies SOURCE_STATIC's contents to TARGET_STATIC inside the frozen
# tree. We only register the entry if the dir exists — during local
# bring-up the static-serving agent's branch may not be merged yet
# (see pyinstaller/NOTES.md for the empty-placeholder workaround).
datas: list[tuple[str, str]] = []
if SOURCE_STATIC.is_dir():
    datas.append((str(SOURCE_STATIC), TARGET_STATIC))

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# PyInstaller's static analysis catches direct ``import`` statements but
# misses dynamic / string-loaded modules. The list below is the
# well-known FastAPI + uvicorn set; each entry has a one-line note on
# WHY it's needed so we can prune later if a uvicorn release stops
# pulling something in.
hiddenimports: list[str] = [
    # uvicorn dispatches protocol/loop choices via ``importlib.import_module``
    # at runtime — it picks an HTTP impl (h11 vs httptools), a websockets
    # impl (websockets vs wsproto), and a loop policy (asyncio vs uvloop)
    # based on what's installed. Static analysis only sees the dispatch
    # call, so we name every leaf the ``auto`` selector might choose.
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    # Some uvicorn releases reach for ``uvicorn.workers`` even though we
    # never multi-process — cheap to include.
    "uvicorn.workers",

    # pydantic v2's compiled core. Usually picked up by hooks, but list
    # it explicitly so we don't wake up to a "ModuleNotFoundError:
    # pydantic_core._pydantic_core" the first time someone hits an API.
    "pydantic_core",

    # FastAPI imports starlette lazily in places (e.g. background tasks,
    # form parsers). collect_submodules pulls in everything to be safe;
    # the size cost is negligible vs the maintenance cost of chasing
    # one-off ImportError reports from the field.
    *collect_submodules("fastapi"),
    *collect_submodules("starlette"),
    *collect_submodules("anyio"),
    *collect_submodules("uvicorn"),
]

# ---------------------------------------------------------------------------
# Excludes
# ---------------------------------------------------------------------------
# Trim modules we know aren't reachable from the webview entry point.
# Saves a few MB and shaves first-run extraction time.
excludes: list[str] = [
    # Test runners / dev tooling that occasionally ride into the bundle
    # via stray imports.
    "pytest",
    "_pytest",
    "mypy",
    "ruff",
    # Don't bake in matplotlib / IPython if a transitive dep references
    # them in a try/except.
    "matplotlib",
    "IPython",
    "tkinter",
]


block_cipher = None


a = Analysis(
    [ENTRY],
    pathex=[str(REPO_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Switch to False here (and add a COLLECT() block after EXE) for --onedir
# distribution. The default --onefile path packs everything into the
# single ``.exe``.
onefile = True

if onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="wows-model-export-webview",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,            # see "Why no UPX?" at the top of this file
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,         # this is a CLI, not a GUI — keep the console
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,            # add when we have one — Path(REPO_ROOT)/'...'
    )
else:
    # --onedir variant. Faster startup, more files. The COLLECT() call
    # writes a ``dist/wows-model-export-webview/`` folder with the exe
    # plus its dependency DLLs alongside.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="wows-model-export-webview",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="wows-model-export-webview",
    )
