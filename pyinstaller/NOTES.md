# PyInstaller packaging notes

Living notebook of decisions and assumptions baked into
`wows-webview.spec`, `entry.py`, and the two GitHub Actions workflows
(`.github/workflows/release.yml`, `.github/workflows/build-validation.yml`).

## End-state UX

User downloads `wows-model-export-webview.exe` from GitHub Releases →
double-clicks → console window opens, uvicorn boots, the CLI prints a
`http://127.0.0.1:5180` URL → user opens it in their browser.

That's the only happy path this packaging is designed for. Everything
below exists to support that flow.

## Build it locally

```pwsh
# from repo root, with the venv active
pip install pyinstaller
pyinstaller pyinstaller/wows-webview.spec --noconfirm
./dist/wows-model-export-webview.exe --help
```

## Assumptions about parallel-agent work

These were unmerged when this packaging was added. After their branches
land, audit each item:

1. **Webview static-serving agent** is expected to put the built Svelte
   bundle at `src/wows_model_export/_static/webview/` so the FastAPI
   app can mount it from the wheel as package data.
   - The spec's `SOURCE_STATIC` constant points there.
   - Both workflows have a "Stage webview bundle into package data"
     step that copies `webview/dist/*` into that path. **Drop those
     steps once the agent's branch handles the copy itself** (likely
     via setuptools `package_data` or a build hook).
   - If they pick a different path, update `SOURCE_STATIC` in
     `pyinstaller/wows-webview.spec`.

2. **scipy → numpy KD-tree agent** removes scipy from runtime deps.
   The spec doesn't list scipy as a hidden import either way, so this
   shouldn't need changes — but PyInstaller will stop pulling in
   `scipy.libs` after the dep drops, which trims the exe a little.

3. **`wowsunpack.exe`** (Rust binary, separate fork) is NOT vendored.
   The webview locates it at runtime via `WOWS_TOOLKIT_BIN` env var →
   user-config file → `shutil.which("wowsunpack")`. End-users still
   need to build it separately (or drop it on PATH manually). README
   already covers this in the Prerequisites section; no action needed
   in packaging.

## The placeholder workaround (local dev)

If the static-serving agent's branch isn't merged, the spec's
`SOURCE_STATIC` directory won't exist. The spec handles this gracefully
(it skips the `datas=` entry rather than failing), so the build will
succeed but the exe won't actually serve the UI.

For local validation runs, mkdir an empty placeholder:

```pwsh
New-Item -ItemType Directory -Force src/wows_model_export/_static/webview
```

This is a temporary workaround — once the static-serving agent's
branch lands, the dir will exist for real with the built bundle inside.

## Spec design choices (why they're what they are)

- **`--onefile` over `--onedir`**: one downloadable artifact for the
  GitHub Releases UX. Trade-off: ~5-10 s first-run extraction tax. The
  spec has an `onefile = True` switch with both `EXE()` blocks present,
  flip to `False` if startup speed beats single-file convenience.

- **`console=True`**: the CLI prints the localhost URL + uvicorn logs
  to stdout. A windowed (`--noconsole`) build would swallow that and
  give the user nothing to look at.

- **No UPX**: UPX-packed Python exes look enough like packed malware
  to trigger Defender + a long tail of corporate AV vendors. The size
  win (~30%) isn't worth the false-positive noise.

- **Hidden imports**: the FastAPI/uvicorn standard set, each with a
  one-liner explaining why. PyInstaller's static analysis misses
  uvicorn's `importlib.import_module(...)` dispatch for HTTP / WS /
  loop choices, so we name every leaf the `auto` selector might pick.

- **No icon**: TBD when the project has visual identity. Add via
  `icon=` in the `EXE()` block when ready.

## Smoke test choices (CI)

`release.yml` boots the exe, polls `/api/openapi.json` (always-200,
no workspace dependency) for up to 45 s, then kills the process. If
this proves flaky on Windows runners (port races, AV scans on first
run, etc.), downgrade to a pure `Test-Path` + size check.

`build-validation.yml` only does the lightweight checks: file exists,
size > 10 MB, `--help` succeeds. The full HTTP smoke test would be
overkill on every PR — release.yml is the one with skin in the game.

## Things we explicitly did NOT do

- **No code-signing**: separate decision involving certs + identity.
  Defender will SmartScreen-warn the first few downloads until the
  reputation builds.
- **No Linux/macOS exe**: WoWS itself is Windows-only, no point
  shipping packages for OSes that can't use them.
- **No installer / MSI**: the single .exe IS the install. If we ever
  add things like start-menu shortcuts or auto-update, revisit.
- **No wheel publish to PyPI from this workflow**: `pip install` from
  GitHub Releases works for now. PyPI is its own decision tree.
