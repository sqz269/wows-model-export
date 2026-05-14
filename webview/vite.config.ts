// Vite dev server for the webview.
//
// All `/api/*` and `/repo/*` requests are proxied to the FastAPI
// backend at `http://localhost:5180` (the `wows-webview-serve` CLI). The
// previous in-process Node middleware was deleted as part of Path A
// Stage 1 of the integration plan — see `INTEGRATION_PLAN.md`.
//
// Dev workflow (npm scripts at webview/package.json):
//   npm run dev          ← runs Vite + wows-webview-serve concurrently
//   npm run dev:frontend ← Vite alone (use if you already have the
//                          backend running in another shell)
//   npm run dev:backend  ← wows-webview-serve alone
//
// Workspace resolution lives on the Python side now
// (wows_model_export.config.PipelineConfig.load). `$WOWS_WORKSPACE`
// still takes precedence; falling back to the CWD of the
// wows-webview-serve invocation.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

const here = path.dirname(fileURLToPath(import.meta.url));

// FastAPI backend address. Override with VITE_API_TARGET when running
// the backend on a different port / host (e.g. against a remote dev
// machine).
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:5180';

export default defineConfig(() => {
  return {
    plugins: [tailwindcss(), svelte()],
    resolve: {
      alias: {
        $lib: path.resolve(here, 'src/lib'),
        $components: path.resolve(here, 'src/components'),
        $routes: path.resolve(here, 'src/routes'),
      },
    },
    server: {
      proxy: {
        '/api': {
          target: API_TARGET,
          changeOrigin: true,
          // The backend serves chunked GLB responses for /repo/*.
          // Keep ws off for /api — no SSE in Path A Stage 1.
          ws: false,
        },
        '/repo': {
          target: API_TARGET,
          changeOrigin: true,
          ws: false,
        },
      },
    },
    build: {
      sourcemap: true,
      target: 'es2022',
    },
  };
});
