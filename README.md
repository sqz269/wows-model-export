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
│   └── <Ship>/                    ← also prop hulls (`pipeline.kind: "prop_hull"`)
│       ├── <Ship>.meta.json       ← sidecar (typed schema)
│       └── models/
│           ├── <Ship>_hull.glb
│           ├── <Ship>_hull.animation.json  ← only when a hull model animates
│           ├── <Ship>_accessories.json
│           └── textures_dds/      ← per-ship DDS mip chains
├── builds/
│   └── <build>/                   ← a workspace of its own (ships/, maps/,
│                                    libraries/, .cache/) for exports made
│                                    with `--build <build>` from an older
│                                    game build; see "Game builds" below
└── libraries/
    ├── accessories/
    │   ├── index.json             ← fleet-wide accessory index
    │   └── <asset_id>/            ← GLB + DDS per shared accessory, and its
    │                                clips (<asset_id>.animation.json) when
    │                                its model animates (radars, propellers)
    ├── aircraft/
    │   ├── index.json             ← airframe index (LODs, parts, racks,
    │   │                            hardpoints, GameParams entries)
    │   ├── stats.json             ← per-plane stats and loadouts (HP,
    │   │                            speeds, heights, ordnance, dispersion)
    │   └── <nation>/<category>/<ID>/  ← GLB + visual JSON + DDS per airframe,
    │                                    and its clips (<ID>.animation.json)
    └── textures_shared/
        └── <key-8>/<file>         ← one copy of each texture set shared
                                     byte-for-byte across export folders
                                     (detail atlas, shared turret / skin /
                                     camo maps, ...)
```

Texture paths in the sidecar and the library indexes are relative to the
model directory that owns them (`models/` for a ship, the asset directory
for a library entry), except paths that start with `libraries/`, which are
relative to the workspace root. The shared texture store is one of
those: every texture set (the split-mip files of one texture) that two or
more export folders would hold byte-for-byte lives once in
`libraries/textures_shared/`, for any slot. Each export moves its shared
sets there right after writing them, leaving a `shared_textures.json`
redirect behind, and repoints the documents of the other folders that
held them (a folder whose own export is still running keeps its copy
until that export ends). `wows-dedup-textures` converts a workspace
exported before this layout.

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
| `wows-ingest-ship (<vehicle> <vehicle> ... \| --ships-file FILE) [--jobs N]` | Extract several new ships in one process, up to three at a time (`--jobs`, default 1). A `--ships-file` line is one ship's part of a single-ship command (`<vehicle> --label L --toolkit-ship T --hull H ...`); options it leaves out come from the command line. Each ship gets the ingest the single-ship command runs, without prompting; `--build-library` builds the accessory library once, after the group. A failed ship is reported and the others go on; `--report FILE` writes the per-ship results and timings as JSON. Run one such process at a time: the library build and the toolkit, camo and particle caches lock only inside one process. |
| `wows-build-accessory-library` | Rebuild the fleet-wide accessory index. |
| `wows-build-aircraft-library [MODEL_OR_KEY ...]` | Export airframes (aircraft model folders, or GameParams aircraft keys) into `libraries/aircraft/`; `--all` for every one, `--validate` to check the index (`reference/schemas/AIRCRAFT_LIBRARY.md`). With the GameParams join it also writes the per-plane stats sidecar `stats.json`; `--stats-only` rewrites only that (`reference/schemas/AIRCRAFT_STATS.md`). |
| `wows-refresh-sidecars (SHIP ... \| --all)` | Re-run single GameParams passes (`--pass air_defense`, the default) over ship sidecars already in the workspace, without a toolkit call or a re-scaffold; only the sections the pass owns change. `--dry-run` reports, writes nothing. |
| `wows-snapshot` | Dump GameParams + Vehicles/Permoflages cache. |
| `wows-find-ship-variants` | Enumerate ship variants from GameParams. |
| `wows-dedup-textures` | Move every texture set shared by two or more export folders into `libraries/textures_shared/` and repoint sidecars + library indexes (older exports). |
| `wows-export-map SPACE [SPACE ...]` | Export spaces (battle maps, docks, operations) into `maps/<name>/`: the GLB, every sidecar manifest, the decal textures, the per-weather skies (`sky/<weather>/env_cube.hdr` + `sky_manifest.json`) and `export.json`. `--skip-glb` re-runs the sidecar post-pass over an exported GLB. `--sky-only` re-runs only the sky step (the skies and `export.json`'s `sky_manifest` block) and touches no other file; it reads the probes from the build the space was exported from. Probes stored as BC6H need `imagecodecs`; a weather whose sky failed is named on stderr and in the `sky_errors` of `--json-events` (`null` when the sky step failed as a whole: `soft_errors` then names `sky_manifest`, and the earlier export's `sky_manifest.json` is not read). |
| `wows-export-building` | Export GameParams Building entries (shore structures) into `buildings/`. |
| `wows-compose-building` | Compose buildings from an old-format base model and GameParams gun mounts, from a spec you supply (`reference/schemas/BUILDING_SIDECAR.md`). |
| `wows-builds status \| snapshot \| diff \| stale \| reexport` | What a game update changed (`diff`), which exports it made stale (`stale`), and re-running those with their original arguments (`reexport`). Per-build manifests live in `.cache/builds/<build>/`. |
| `wows-builds archive` | Copy every install build and package the build archive lacks into it (append-only; see "Game builds"). `--dry-run` reports only; `--install-schedule` runs it at logon and daily. |
| `wows-build-prop-hull` | Wrap a static prop (a WoWS map / port prop by id or model path, or any static `.glb` / `.gltf`) in a minimal ship sidecar under `ships/<id>/`: hull, one armour-less hit box, no mounts, buoyancy dimensions. `--spec` builds several, `--validate` checks them (`reference/schemas/METADATA_SPEC.md` §17). |

Each accepts `--help` for the full argument list. Run `wows-` then Tab
in a modern shell to discover the rest. Every command that reads the game
also takes `--build <build>` and `--archive-dir <dir>` (next section).

## Game builds and the build archive

The game install keeps only the newest two builds' index files
(`bin/<build>/idx`); the launcher deletes the older one with the next
update, and an update can replace packages. A **build archive** keeps
every build readable:

```
<archive>/
├── bin/<build>/      ← the install's bin/<build>/ folder, per build
├── res_packages/     ← every .pkg the install has had (never deleted)
├── superseded_packages/<build>/  ← packages an update rewrote (normally empty)
└── logs/             ← one log per copy (`scheduled.log` for timed runs)
```

Point the pipeline at it with the Settings page's **Build archive
directory**, `WOWS_ARCHIVE_DIR`, or `--archive-dir`.

**Filling it.** `wows-builds archive` compares the install with the
archive (every file of each `bin/<build>/` that has idx files, and every
package; the client's runtime `*.log` files are skipped) and copies what
is missing or newer. It only reads the install and never deletes. When the
archive has its own `snapshot.ps1` it runs that (robocopy `/E /XO`),
otherwise a built-in copier that writes each file under a temporary name
and renames it into place. It does nothing when the archive is up to
date, waits while any file it would copy changed in the last 30 minutes
(a game update may still be writing; `--settle-minutes`), and lets one run
at a time hold `logs/.archive.lock`. Packages are copied before a build's
idx files, so an interrupted copy never leaves a build that looks archived
but points into missing packages.

The archive keeps one copy per package name, and a changed install
package replaces it. Older builds stay readable because game updates
append to packages: their idx files point into the unchanged front part.
Before copying, `wows-builds archive` checks that each archived package it
would replace is the start of the install's copy (a sampled compare). A
package an update rewrote instead is moved to
`superseded_packages/<newest archived build>/` first and reported as a
warning in the run's log: its bytes are kept, but the builds up to that
build read it only once it is put back in place of the new one (for
example in a copy of `res_packages/` given as their package folder).

```bash
wows-builds archive --dry-run            # what is not archived yet
wows-builds archive                      # copy it
wows-builds archive --install-schedule   # Windows: at logon (+15 min) and daily at 05:00
wows-builds archive --schedule-status    # last and next run
wows-builds archive --remove-schedule
```

The scheduled task (per user, "wows-model-export build archive") runs
`pythonw -m wows_model_export.cli.builds archive` with the game and
archive paths it was registered with, below normal priority and without a
window, and appends to `<archive>/logs/scheduled.log`. A new build is
archived on the first run after the update has settled; an up-to-date
check takes about two seconds. Elsewhere, run the command from cron or a
systemd timer.

**Exporting an older build.** `--build <build>` on any export command
(`wows-ingest-ship`, `wows-export-map`, the library builders,
`wows-build-prop-hull`, ...) reads that build instead of the install's
current one, from the archive when it is there and else from the install
while it still has the build's idx files (toolkit flags
`--idx-files <root>/bin/<build>/idx --pkg-dir <root>/res_packages`). Its
output goes to `<workspace>/builds/<build>/`, a workspace of its own with
its own caches, accessory library and texture store, so an older build's
exports never overwrite the current ones:

```bash
wows-ingest-ship Montana --build 13357625 --build-library
wows-export-map e02_Halloween_2017 --build 13357625   # a map later builds removed
wows-builds status --build 13357625                   # that workspace's caches and exports
```

The build the install runs now is the plain workspace: `--build` with it
changes nothing. `WOWS_BUILD=<build>` does the same as `--build` for every
command. The per-build manifests that `wows-builds diff` compares stay in
the main workspace's `.cache/builds/`, and `wows-builds snapshot --build
<build>` can fingerprint any archived build. `wows-build-decal-library`
reads a loose `res_unpack/dyndecals` copy, not a build, so it takes no
`--build` and ignores `WOWS_BUILD`.

**Publishing stays on the current build.** A consumer target holds the
current build, and an older build's files (its accessory index, ship
sidecars, GLBs) would overwrite those of the same names. So `wows-publish`
takes no `--build` and ignores `WOWS_BUILD`; publishing refuses a pinned
config or a `<workspace>/builds/<build>/` workspace (`wows-publish
--workspace <ws>/builds/<build> --allow-build-workspace --target <a folder
of its own>` overrides that on purpose); and `wows-ingest-ship --build N
--and-publish` stops before the ingest.

**In the webview** the topnav's **Game build** picker lists every build
the install or the archive holds. Picking an older build shows that
build's workspace on every page (Ships, Maps, Library, Extract,
Updates, ...), and an Extract or map export from there reads that build
and writes into its workspace. The choice is a cookie (`wows_build`), so
it holds for every tab of that browser; the backend serves each `/api` and
`/repo` request from the chosen build and marks the response with
`X-Wows-Build`. The Updates page shows the archive's state, its scheduled
task, and an **Archive now** button. The Consumers page lists its actions
in an archived build's view but refuses to run them (they publish the
current build only; see above). The first visit to a build's view runs
toolkit calls that fill that build's cache (`builds/<build>/.cache`: the
Extract page's ship snapshot, the VFS metadata dump, the map list), a
minute or two; `wows-snapshot --build <build> --output
<workspace>/builds/<build>/.cache/snapshot.json` fills most of it ahead
of time.

## Configuration

Settings persist to a JSON file the backend reads at startup:

- **Windows:** `%APPDATA%\wows-model-export\config.json`
- **macOS:** `~/Library/Application Support/wows-model-export/config.json`
- **Linux:** `$XDG_CONFIG_HOME/wows-model-export/config.json`
  (defaults to `~/.config/...`)

Precedence (highest first):

1. CLI flag (`--workspace`, `--game-dir`, `--toolkit-bin`,
   `--archive-dir`, `--build`; `wows-webview-serve` takes only
   `--workspace`).
2. Environment variable — `WOWS_GAME_DIR`, `WOWS_TOOLKIT_BIN`,
   `WOWS_WORKSPACE`, `WOWS_TOOLKIT_TIMEOUT`, `WOWS_ARCHIVE_DIR`,
   `WOWS_BUILD`.
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

An editable install picks up code changes but not new console scripts:
after a pull that adds a `wows-*` command to `pyproject.toml`, run
`pip install -e .` again in each environment that has the package (stop a
running `wows-webview-serve` from that environment first, or the
reinstall cannot replace its executable), or call the module form,
`python -m wows_model_export.cli.<module>`.

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
