# Webview ↔ pipeline integration plan

Date: 2026-05-14

Scope: ways to deepen the link between the Svelte webview
(`webview/`) and the Python pipeline package
(`src/wows_model_export/`). Written after the Extract page port (commit
`9a8698a`) closed the immediate parity gap with the legacy
`I:/Models/warships/tools/webview/`.

This is a planning document — no code is changed by reading it. Each
item names the affected files and rough effort so triage is cheap.

---

## TL;DR — five most valuable wins, in order

1. **Switch the job runner to parse `--json-events` JSONL instead of
   raw stdout.** Python side is already done
   (`cli/_emit.py::make_json_printer`, `cli/_args.py::build_printer`).
   Node-side change: ~40 LOC in `server/jobs.ts` to split the stdout
   pipe on newlines and emit structured `StepEvent`s. Currently the
   user sees an undifferentiated stdout dump; afterwards the job panel
   can render "step 4/7: scaffolding sidecar… ✓".

2. **Add buttons for the obviously-missing one-click ops.**
   `wows-find-ship-variants --refresh` (resolves the 503 on cold
   GameParams cache), `wows-build-accessory-library` (Library
   rebuild), `wows-publish <Ship>` (from the Ships page or Extract
   preview after a successful run), `wows-teardown-ship`. Each is
   ~4 hours: one endpoint clone, one button.

3. **Lift the legacy webview's winding-audit + viewed-history
   endpoints.** These existed in
   `I:/Models/warships/tools/webview/vite.config.ts` and got dropped on
   migration. ~200 LOC total. Brings the Library page back to legacy
   parity.

4. **Replace HTTP polling with SSE for live job progress.** Combined
   with point 1, sub-second feedback without the 1.5 s tick.
   Implementable inside the current Node middleware before any
   FastAPI move — keeps the door open without committing.

5. **Plan the FastAPI port deliberately, not opportunistically.** The
   endpoints are stable; the move from spawn-and-parse to in-process
   is meaningful only after committing to it for the whole backend.
   Don't half-port.

---

## Short-term wins (each ≤ 1 day, additive only)

### JSONL job runner

Files: `server/jobs.ts`, `server/endpoints/extract.ts`,
`components/ExtractJobPanel.svelte`.

- Add `--json-events` to the spawned `wows-ingest-ship` /
  `wows-ingest-skin-pack` / `wows-snapshot` argv.
- Replace `child.stdout?.on('data', chunk → stdout += chunk)` with a
  line-buffered parser; if a line parses as JSON matching `StepEvent`,
  append to `job.events: StepEvent[]`. Non-JSON lines fall through to
  `job.stdout` as today.
- Surface in `ExtractJobPanel.svelte`: render the current/total step
  + the last 3 messages instead of the raw tail. Keep a "show raw
  output" toggle for debugging.

### Missing one-click ops

Each is a clone of an existing endpoint in
`server/endpoints/extract.ts` + a small button in the affected route.

- `POST /api/gameparams/refresh` → spawn `wows-find-ship-variants
  --refresh`. Wire into `routes/Extract.svelte` so the
  GameParams-cache-missing banner has a button next to it.
- `POST /api/library/rebuild` → spawn `wows-build-accessory-library`.
  Add to `routes/Library.svelte`.
- `POST /api/publish` → spawn `wows-publish <Ship>` (single) or
  `wows-publish --all`. A button in `ShipControls.svelte` or the
  Extract preview's post-run state.
- `POST /api/teardown` → spawn `wows-teardown-ship <Ship>`. A button
  in `ShipPicker.svelte`'s context menu / row hover.

### Library parity-lift from the legacy webview

- `GET /api/winding-audit` → static read of
  `<workspace>/libraries/accessories/winding_audit.json`. Wire
  `AssetList.svelte` to overlay flip/keep/ambiguous badges. ~100 LOC.
- `POST /api/auto-flip-winding` → spawn
  `wows-build-accessory-library --audit-only --auto-flip-winding`.
  Surface as a Library-page button gated on N > 0 flip-verdict assets.
  ~30 LOC.
- `GET/POST /api/viewed` → JSON file under
  `libraries/accessories/viewed.json`; client tracks reviewed assets.
  ~70 LOC.
- Skip rig-rebuild for now: the legacy webview had it but
  `wows-turret-autorig` is per-asset and not commonly user-triggered.
  Add only if asked.

---

## Medium-term (~1 week, structural)

### SSE for job progress

Implementable in Node *or* after the FastAPI port; conceptually the
same in either backend.

- New `GET /api/extract/jobs/:id/events` middleware holds the
  connection open and pushes each new `StepEvent` from the job's
  event queue as `event: progress\ndata: <json>\n\n`.
- Client (`routes/Extract.svelte`): replace the 1.5 s `setInterval`
  poll with `new EventSource('/api/extract/jobs/${id}/events')`.

### FastAPI backend port

Replaces all Node middlewares. The composers' `_StepRunner` event
system already supports this — the wins are in-process composer calls
(no subprocess overhead), real cancellation via
`asyncio.Task.cancel()`, and an authoritative shared workspace
resolver.

Layout:

```
src/wows_model_export/server/
├── main.py        ← FastAPI app + uvicorn entry
├── workspace.py   ← thin re-export of config.PipelineConfig.load
├── jobs.py        ← in-memory job runner (Popen + threads)
├── routes/
│   ├── extract.py
│   ├── ships.py
│   ├── library.py
│   ├── gameparams.py
│   └── repo.py
└── sse.py         ← event streaming + thread→loop bridging (Stage 2)
```

#### Stage 1 — structural port (SHIPPED 2026-05-13)

Mechanical port of the Node middleware to FastAPI. No behaviour
changes:

- All endpoints still spawn the existing `wows-*` CLIs via
  `subprocess.Popen` — opaque stdout, no JSONL parsing.
- HTTP polling stays at the existing 1.5 s cadence; no SSE yet.
- Same response shapes byte-for-byte (snake_case job fields,
  validation regexes, error payloads). The TypeScript client
  (`webview/src/lib/api/extract.ts`) and components keep working
  with zero edits.
- New CLI entry point `wows-webview-serve --port 5180 --host
  127.0.0.1 [--workspace PATH] [--reload]` (in
  `src/wows_model_export/cli/webview_serve.py`).
- `webview/vite.config.ts` proxies `/api/*` and `/repo/*` to
  `http://127.0.0.1:5180` (override with `VITE_API_TARGET`); the
  Node `src/server/` middleware is deleted.
- `npm run dev` runs Vite + `wows-webview-serve` concurrently via
  the `concurrently` npm package; `npm run dev:frontend` /
  `dev:backend` run them in isolation.
- Workspace resolution: re-uses
  `wows_model_export.config.PipelineConfig.load` (env var > CWD).
  The Node-side marker walk + `~/wows-workspace` fallback are
  intentionally not duplicated — set `$WOWS_WORKSPACE` or
  `--workspace` explicitly.
- New `webview` extra in `pyproject.toml`:
  `pip install wows-model-export[webview]` pulls FastAPI +
  uvicorn. Pipeline users who don't run the dashboard aren't
  forced into either dep.

#### Stage 2 — JSONL events + SSE (not started)

- Backend: line-buffer the job stdout, parse JSON-per-line via the
  `--json-events` printer.
- Add `GET /api/extract/jobs/{id}/events` SSE stream.
- Client: swap the 1.5 s `setInterval` poll for `EventSource`.

#### Stage 3 — in-process composer calls (not started)

- Replace `subprocess.Popen('wows-ingest-ship …')` with
  `await loop.run_in_executor(None, compose.ingest_ship, …,
  on_event=…)`.
- Real cancellation via task cancellation rather than SIGTERM.
- Drop the `PYTHONUNBUFFERED` trick — events are no longer
  routed through a pipe.

---

## Long-term (architecture)

### `wows-webview serve` as a single distribution entry point

After the FastAPI port lands: ~3 days to bundle `webview/dist/` into
the wheel via `package_data` and serve static files alongside the API.
Result: `pip install wows-model-export && wows-webview serve` — no
Node at runtime, only at build time. Document the dev vs. prod split
(dev still uses Vite for HMR; prod is uvicorn + static).

### Type-sharing snapshot.json ↔ `lib/types/extract.ts`

Pydantic models on the Python side, FastAPI auto-emits OpenAPI, run
`openapi-typescript` in a build hook. Drop this only after the
FastAPI port is in and the contract has stabilised; pre-FastAPI it's
busywork. ~1 day once the prerequisites land.

### Sidecar v3.2 `hulls` block consumer

Hull-tier selector in the Ships page. Roughly 200 LOC + a `<select>`
in `ShipControls.svelte`. Independent of pipeline integration but
worth listing — both webviews (legacy and migrated) are currently
blind to the v3.2 `hulls` block.

---

## Traps to know about

- **PYTHONUNBUFFERED.** `server/jobs.ts` already sets it for
  Node-spawned subprocesses. The FastAPI port must too when running
  composers in an executor — otherwise progress callbacks fire
  immediately but any in-composer `print(...)` is batched. Keep
  `on_event` as the only progress channel; ignore stdout for
  composer-internal logging.
- **Sync `on_event` from a worker thread → async queue.** Use
  `loop.call_soon_threadsafe(queue.put_nowait, event)`, never call
  `queue.put_nowait` directly from the executor thread. Test with a
  deliberately fast-emitting composer to validate ordering.
- **Workspace resolution drift.** `server/workspace.ts` and
  `wows_model_export/config.py` are independent. Today they agree on
  env-var precedence + marker walk + home fallback. Diverge silently
  and you get spooky cross-tool inconsistency. Either commit to
  running through the Python side at Node startup, or document the
  contract as part of the public API.
- **Job locking model.** The in-memory `Map<label, jobId>` is fine
  for a single-user dev tool. Moves cleanly to FastAPI as a
  module-level `dict` with a lock. Don't add SQLite persistence until
  the FastAPI server runs as a real daemon — overkill otherwise.
- **`make_json_printer` writes to stdout by default.** If a composer
  ever does `print(...)` for non-event output (none do today, but
  easy to regress), JSONL parsing on the Node side breaks. Either
  gate the parser behind a `try { JSON.parse } catch → stdout`
  fallback, or switch `make_json_printer` to stderr (symmetric with
  `make_text_printer`) and leave stdout free for real data.

---

## Recommendation if only one thing ships

**Short-term win #1 (JSONL events) is the highest
user-value-per-LOC change.** ~50 LOC of Node-side parsing + a small
`ExtractJobPanel` update, no Python changes, no architecture
commitment. Turns the job tail from "wall of text" into "step 4/7:
scaffolding sidecar… ✓ done". Everything else listed here is
additive, but this one unlocks the perceived quality of every
job-running flow.
