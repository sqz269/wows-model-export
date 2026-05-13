# wows-model-export

Extraction pipeline for World of Warships assets — a producer that emits
typed artifacts (sidecar JSON, hull GLB, accessory library) that
downstream consumers (Unity, Blender, a web viewer) read directly.

```
┌──────────────────────────────┐
│  PRODUCER (wows-model-export)│
│  Python + Rust toolkit fork  │
│                              │
│  Reads:  WoWS install        │
│  Writes: GLB + sidecar +     │
│          library JSON +      │
│          attached_accessories│
└──────────────┬───────────────┘
               │
       ┌───────┼─────────┐
       ▼       ▼         ▼
   ┌───────┐ ┌─────────┐ ┌───────┐
   │webview│ │ Blender │ │ Unity │
   │  (TS) │ │ add-on  │ │  (C#) │
   └───────┘ └─────────┘ └───────┘
```

Three independent consumers, each in its own ecosystem. The **webview**
in this repo is the reference consumer: every new sidecar field gets
visually verified here first.

## Status

Early — only the webview has landed under this layout. The Python
pipeline is still being lifted from `I:\Models\warships\tools\` per
`migration/PIPELINE_REFACTOR.md` (in the source repo).

### What's here

- `webview/` — local dev dashboard (Svelte 5 + Three.js + Vite + TS). See
  [webview/README.md](webview/README.md) for setup.

### What's coming

- `src/wows_model_export/` — Python pipeline package (renamed from the
  source repo's `tools/`).
- `docs/contracts/` — public artifact schemas.
- `pyproject.toml` + `pip install -e .` → `wows-*` CLI entry points.

See the migration docs in the source repo for the rollout plan.

## License

TBD before public release.
