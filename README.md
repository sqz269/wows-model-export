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

Alpha — the Python pipeline and the webview are both in-tree.
Public API still moving while we shake out drift; once stable, this
will tag v0.1.0.

### What's here

- `src/wows_model_export/` — Python pipeline package, layered into
  `read` / `toolkit` / `resolve` / `compose` / `cli`. See
  `src/wows_model_export/__init__.py` for the layer rationale.
- `pyproject.toml` — `pip install -e .` lands the `wows-*` console
  entry points (`wows-ingest-ship`, `wows-scaffold-ship`,
  `wows-build-accessory-library`, `wows-publish`, …).
- `webview/` — local dev dashboard (Svelte 5 + Three.js + Vite + TS).
  See [webview/README.md](webview/README.md) for setup.

### What's coming

- `docs/contracts/` — public artifact schemas (sidecar v3,
  attached-accessories v2, library index).
- A first batch of smoke tests against the schema authority in
  `resolve.sidecar`.

## License

TBD before public release.
