# wows-model-export

Extract ship and accessory models from World of Warships and render them
in a local browser-based viewer. One install gets you both — the Python
extraction pipeline and the Svelte 3D viewer ship in the same package.

```
┌──────────────────────────────┐
│  PRODUCER (Python + Rust)    │
│                              │
│  Reads:  WoWS install        │
│  Writes: GLB + sidecar +     │
│          library JSON +      │
│          attached_accessories│
└──────────────┬───────────────┘
               │
               ▼
        ┌──────────────┐
        │   webview    │ ← reference consumer; bundled in the wheel
        │  (Svelte 5)  │
        └──────────────┘
```

The pipeline emits typed JSON artifacts (sidecar v3, library index,
placements) that any consumer can read; the bundled webview is the
reference one.

## Prerequisites

- **World of Warships** installed (any branch — Steam or WG launcher).
- **Python 3.11+** on your `PATH`.
- **wowsunpack** — a Rust binary from the
  [wows-toolkit fork](https://github.com/sqz269/wows-toolkit). Build
  once with `cargo build --release --bin wowsunpack`; the Settings
  page will let you point at the resulting `.exe`.

## Install

Two paths depending on whether you want the CLI tooling or just the
local viewer.

### Standalone Windows exe (just the viewer)

Grab `wows-model-export-webview.exe` from
[GitHub Releases](https://github.com/sqz269/wows-model-export/releases/latest)
and double-click it. A console window opens, the local server boots, and
the URL it prints (`http://127.0.0.1:5180`) opens the UI in your
browser. Nothing to install — Python is bundled inside the exe. First
run takes ~5-10 seconds to extract; subsequent launches are faster.

You still need `wowsunpack.exe` on your `PATH` (or pointed at via the
Settings page) for the Extract tab to work — see Prerequisites above.

### Wheel install (full CLI + viewer)

Grab the latest wheel from
[GitHub Releases](https://github.com/sqz269/wows-model-export/releases/latest),
then:

```bash
pip install "./wows_model_export-<version>-py3-none-any.whl[webview]"
```

This drops the `wows-*` console scripts (`wows-webview-serve`,
`wows-ingest-ship`, `wows-build-accessory-library`, …) on your `PATH`.
The Svelte UI ships pre-built inside the wheel
(`wows_model_export/_static/webview/`) and is served by the same
`wows-webview-serve` process at `http://127.0.0.1:5180/` — no Node
toolchain required to use it.

## First run

1. Launch the UI:
   ```bash
   wows-webview-serve
   ```
   Open <http://localhost:5180>.

2. Click **Settings** in the top nav. Fill in:
   - **Game directory** — the folder containing `WorldOfWarships.exe`.
   - **Toolkit binary** — path to `wowsunpack.exe`.
   - **Workspace (output directory)** — wherever you want extracted
     ships and the accessory library to land. Defaults to the directory
     you launched `wows-webview-serve` from; pick something stable
     instead.

   Click **Save**, then restart `wows-webview-serve` (Ctrl-C and
   re-run) so the new paths take effect.

3. Still on the Settings page, under **Workspace artifacts**, click
   **Build** next to:
   - **GameParams + snapshot cache** — ~30 s. Required by Extract.
   - **Accessory library index** — instant; populates as you extract
     ships.

4. Open the **Extract** tab, pick a ship, click **Extract**. When it
   finishes, the ship appears in **Ships** and its accessories appear
   in **Library**.

## What lands where

```
<workspace>/
├── .cache/
│   ├── gameparams.json            ← ~2.8 GB; the toolkit's GameParams dump
│   └── snapshot.json              ← Vehicles + Permoflages summary
├── ships/
│   └── <Ship>/
│       ├── <Ship>.meta.json       ← sidecar (typed schema)
│       └── models/
│           ├── <Ship>_hull.glb
│           ├── <Ship>_accessories.json
│           └── textures_dds/      ← per-ship DDS mip chains
└── libraries/
    └── accessories/
        ├── index.json             ← fleet-wide accessory index
        └── <asset_id>/            ← GLB + DDS per shared accessory
```

Re-build the accessory library (`Build → Accessory library index` on
the Settings page) after extracting new ships so they surface in the
Library tab.

## CLI reference

`wows-webview-serve` is the only command most users need. The rest are
also available if you'd rather script extractions.

| Command | What it does |
| --- | --- |
| `wows-webview-serve` | Launch the local UI on `127.0.0.1:5180`. |
| `wows-ingest-ship <vehicle>` | Extract one ship from the game install. |
| `wows-build-accessory-library` | Rebuild the fleet-wide accessory index. |
| `wows-snapshot` | Dump GameParams + Vehicles/Permoflages cache. |
| `wows-find-ship-variants` | Enumerate ship variants from GameParams. |

Each accepts `--help` for the full argument list. Run `wows-` then Tab
in a modern shell to discover the rest.

## Configuration

Settings persist to a JSON file the backend reads at startup:

- **Windows:** `%APPDATA%\wows-model-export\config.json`
- **macOS:** `~/Library/Application Support/wows-model-export/config.json`
- **Linux:** `$XDG_CONFIG_HOME/wows-model-export/config.json`
  (defaults to `~/.config/...`)

Precedence (highest first):

1. CLI flag on `wows-webview-serve` (only `--workspace` today).
2. Environment variable — `WOWS_GAME_DIR`, `WOWS_TOOLKIT_BIN`,
   `WOWS_WORKSPACE`, `WOWS_TOOLKIT_TIMEOUT`.
3. The persisted config file (the Settings page writes here).
4. Auto-discovery (`wowsunpack` on `PATH`) / sensible defaults.

The Settings page shows a per-field source badge so you can see which
layer is winning at a glance.

## Status

Alpha. The public artifact schemas are still moving — alpha releases are
cut from `master` (current: `v0.1.0a1`) so binaries are easy to grab,
but expect breaking changes to the sidecar/library JSON between alphas
until `v0.1.0` lands.

## Development

```bash
git clone <repo>
cd wows-model-export
pip install -e ".[webview]"
cd webview && npm install && npm run dev
```

`npm run dev` runs the FastAPI backend AND the Vite dev server with
HMR. The frontend dev guide is in [`webview/README.md`](webview/README.md);
the Python package layout is documented at the top of
[`src/wows_model_export/__init__.py`](src/wows_model_export/__init__.py).

For local dev runs (`wows-webview-serve` from an editable install),
just `cd webview && npm run build` — the static resolver falls back to
`webview/dist` automatically, no copy needed.

Wheel and standalone-exe builds need the dist staged into the package
at `src/wows_model_export/_static/webview/` (git-ignored). CI does this
in [`.github/workflows/release.yml`](.github/workflows/release.yml). To
build a wheel locally:

```bash
cd webview && npm run build && cd ..
rm -rf src/wows_model_export/_static/webview
cp -r webview/dist src/wows_model_export/_static/webview
python -m build --wheel
```

See [`src/wows_model_export/_static/README.md`](src/wows_model_export/_static/README.md)
for the resolution order across install modes.

## License

MIT
