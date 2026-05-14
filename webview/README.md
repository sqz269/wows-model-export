# Webview

Local dev dashboard for the `wows-model-export` pipeline.

The webview is the **reference consumer** for the pipeline's artifacts: it
renders hulls + accessories + damage states straight off the sidecar +
GLB the Python pipeline emits. New sidecar fields land here first so we
can visually verify them before the Unity / Blender consumers pick them
up.

## Quickstart

```bash
cd webview
npm install
# from the repo root, in a sibling shell or once-only:
pip install -e ".[webview]"        # installs wows-webview-serve
npm run dev
```

Then open <http://localhost:5173>.

`npm run dev` starts **two processes** via `concurrently`:

1. `wows-webview-serve --port 5180 --reload` — the FastAPI backend.
2. `vite` — the dev frontend (proxies `/api/*` + `/repo/*` to 5180).

If you'd rather run them in separate terminals:

```bash
npm run dev:backend   # wows-webview-serve alone
npm run dev:frontend  # vite alone
```

The backend reads ship artifacts from a **workspace directory** —
per-ship data lives on the user's disk, never in this repo. It resolves
the workspace in this order:

1. `--workspace PATH` flag on `wows-webview-serve` (highest priority).
2. `$WOWS_WORKSPACE` env var.
3. The current working directory when `wows-webview-serve` was invoked.

The simplest setup is to set `WOWS_WORKSPACE` in your shell rc (or in a
`.env` file your shell sources) so both pipeline CLIs (`wows-ingest-ship`
et al.) and the webview backend pick it up.

A working workspace contains:

```
<workspace>/
├── ships/
│   └── <Ship>/
│       ├── <Ship>.meta.json          ← sidecar
│       ├── models/
│       │   ├── <Ship>_hull.glb
│       │   ├── <Ship>_accessories.json
│       │   └── textures_dds/         ← per-ship DDS
└── libraries/
    └── accessories/
        ├── index.json                ← fleet-wide library index
        └── …                         ← GLB + DDS per asset_id
```

You build these with the Python pipeline:

```bash
wows-ingest-ship Iowa                  # → ships/Iowa/
wows-build-accessory-library           # → libraries/accessories/
```

If the webview shows "No ships in workspace" or "library_index_missing",
run those commands and refresh.

## Scripts

```bash
npm run dev       # vite dev server
npm run build     # type-check + production bundle into dist/
npm run preview   # serve the production bundle locally
npm run check     # svelte-check (TS + Svelte)
npm run lint      # eslint + prettier --check
npm run format    # prettier --write
```

## Tech stack

- **Svelte 5** for UI components. Single-file `.svelte` components keep
  template + style + script close together; Svelte 5's `$state` /
  `$derived` / `$effect` runes drive reactivity.
- **Three.js 0.165** for 3D rendering. Bundled `three/addons/*` provides
  the `GLTFLoader`, `OrbitControls`, `RoomEnvironment` IBL, and post-FX
  passes used by the camo / bloom features.
- **Vite 5** for the dev server + production bundling.
- **TypeScript 5** in strict mode. The pipeline emits typed artifacts;
  the renderer treats them as typed inputs.

Tooling: **Prettier** + **ESLint** + **svelte-check**.

## Project layout

```
webview/
├── index.html                  ← SPA entry; mounts /src/main.ts
├── vite.config.ts              ← Vite + /api/* + /repo/* proxy → FastAPI
├── svelte.config.js            ← Svelte 5 (runes-enabled)
├── tsconfig.json               ← strict TS + path aliases
├── src/
│   ├── main.ts                 ← Svelte mount(App, …)
│   ├── App.svelte              ← top bar + hash router
│   ├── styles/app.css          ← CSS variables + page shell
│   ├── routes/                 ← top-level pages
│   │   ├── Library.svelte      ← (stub) accessory browser
│   │   ├── Ships.svelte        ← ship picker + viewer + controls
│   │   └── Extract.svelte      ← (stub) Vehicle picker
│   ├── components/             ← Svelte components consumed by routes
│   │   ├── ShipPicker.svelte
│   │   ├── ShipViewer.svelte   ← Three.js host (wraps lib/ship)
│   │   └── ShipControls.svelte
│   └── lib/                    ← framework-agnostic library code
│       ├── api/                ← typed HTTP clients
│       ├── router.ts           ← hash-driven route reader
│       ├── three/              ← reusable Three.js scaffolding
│       │   ├── scene.ts        ← scene + IBL + lights + grid + axes
│       │   ├── resize.ts       ← container-size sync
│       │   ├── dispose.ts      ← GPU-resource cleanup walker
│       │   └── render_loop.ts  ← requestAnimationFrame helper
│       ├── ship/               ← ship-viewer subsystem (no Svelte deps)
│       │   ├── viewer.ts       ← ShipViewer class (orchestrator)
│       │   ├── classify_hull.ts
│       │   ├── accessory_loader.ts
│       │   ├── placement.ts
│       │   ├── color_mode.ts
│       │   ├── damage_cascade.ts
│       │   ├── visibility.ts   ← pure resolvers; mirrors C# contract
│       │   └── index.ts        ← public barrel
│       ├── types/              ← shared types (split by concern)
│       │   ├── hull.ts         ← HullSectionKey / SeamKey / seamFor
│       │   ├── library.ts      ← LibraryIndex / LibraryAsset / TextureSet
│       │   ├── ship.ts         ← ShipSummary / ShipPlacement / Doc
│       │   ├── sidecar.ts      ← Sidecar subset (mounts / materials)
│       │   ├── skin.ts         ← Skin / camo / mat_camo
│       │   ├── attached.ts     ← AttachedAccessoriesDoc
│       │   ├── ballistics.ts   ← ShellEntry / BallisticsSection
│       │   ├── categories.ts   ← classify* (pure functions)
│       │   └── index.ts        ← barrel
│       └── util/
│           ├── html.ts         ← escapeHtml / fmtBytes / rgbHex
│           └── colors.ts       ← lightenTowardWhite
└── README.md
```

Path aliases (`tsconfig.json` + `vite.config.ts`):

- `$lib/…` → `src/lib/…`
- `$components/…` → `src/components/…`
- `$routes/…` → `src/routes/…`

## Architecture

**One viewer class, many Svelte wrappers.** The Three.js scene graph,
GLB loaders, dispose lifecycle, and damage-state cascade live in
`src/lib/ship/`. Nothing in there imports Svelte — the lifecycle is
explicit (`new ShipViewer(container)` → `loadShip` → `dispose()`). Svelte
components wrap the class and feed it props.

This lets contributors swap framework without rewriting the renderer.
If we ever move to React (or back to vanilla TS), `src/lib/ship/` ports
straight across; only the `.svelte` files change.

**Pure resolvers separated from scene state.** `visibility.ts` is plain
functions over data (seam states, mesh names) — no Three.js imports.
This mirrors the C# contract in
`H:\UnityProjects\ProjectWB\Assets\Scripts\Ships\HullDamageState.cs`;
keeping the webview port pure makes it easy to compare side-by-side.

**Two-sided binding pattern (texture pipeline, not yet ported).** When
textures lift in, they'll follow the existing pattern: meshes register
their binding key on load; sidecar schemes bind to the same key on
parse; whichever arrives first stashes a "pending" record the other
side resolves. See `migration/SHIP_TS_INVENTORY.md` C.10 for the legacy
implementation.

## Adding a new feature

1. **Pure / data-shaped logic** → `src/lib/`. Add a new module under
   `ship/`, `util/`, or `types/`. Keep it import-free of Svelte and DOM.
2. **3D state that lives across renders** → add fields to `ShipViewer`
   (or a sibling class), with a clear `dispose()` path.
3. **UI control surface** → new `.svelte` component under
   `src/components/`, props-only API, no business logic. Wire it to a
   `ShipViewer` method.
4. **New page** → add a `Route.svelte` under `src/routes/`, wire it into
   `App.svelte`'s router and `index.html`'s nav.

If a feature touches a sidecar field for the first time, add the field
to `src/lib/types/` first — drives type-safe code everywhere else.

## Adding a new dev-backend endpoint

The backend is a FastAPI app under
`src/wows_model_export/server/` in the Python package. Each endpoint
group lives in `routes/<name>.py` and is wired into
`server/main.py::create_app`. To add one:

1. New file `src/wows_model_export/server/routes/<name>.py` exporting
   `make_router(config: PipelineConfig) -> APIRouter`.
2. Call `app.include_router(<name>.make_router(config), prefix="/api")`
   in `server/main.py`.
3. Add a typed client in `webview/src/lib/api/<name>.ts`.

Keep endpoint handlers thin (≤10 lines of business logic; delegate to
library functions in `wows_model_export.read` / `compose`). The FastAPI
backend deliberately runs the existing `wows-*` CLIs as subprocesses
for now — see `INTEGRATION_PLAN.md` for the path-to-in-process plan.

## Migration status

The legacy webview is `I:\Models\warships\tools\webview\`. This rewrite
follows the **lift-as-needed** plan in
`I:\Models\warships\migration\FRONTEND_REFACTOR.md`. As of v0.1.0:

- Scaffold: package, build, lint, format, router — done.
- Anchor features: hull GLB load, accessory placement, damage state,
  per-section / per-group / per-seam toggles, color modes — done.
- Library + Extract pages: stubs.
- Texture pipeline (DDS, camo shader, skin packs, mat_albedo): not yet.
- Attached accessories (rangefinders / periscopes / ammo boxes): not
  yet.
- Bloom / BC4 histogram / `__shipDebug__` window hook: not yet.

`migration/SHIP_TS_INVENTORY.md` lists every legacy feature with line
ranges — when something's missing, look there first.

## Known limitations (day-one)

- **No texture rendering.** Hulls + accessories appear flat-shaded.
  Damage cascades and section toggles work; the visual diagnostic
  is intentional (it makes the damage state painfully obvious).
- **No attached accessories.** Main turrets render without their bundled
  rangefinders / periscopes / ammo boxes. Use the legacy webview if you
  need those.
- **No camo / skin support.** The active skin is always the default.

These come back in follow-up passes per the migration plan's sequencing.
