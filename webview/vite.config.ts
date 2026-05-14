// Vite dev server for the webview.
//
// Two concerns:
//   1. Build/dev plumbing (Svelte + TypeScript path aliases).
//   2. A thin dev backend exposing three endpoints to the SPA:
//      - GET /api/ships           — per-ship summaries (sidecar + hull GLB discovery)
//      - GET /api/library         — accessory library index
//      - GET /repo/<rel-path>     — static workspace files (hull GLB, DDS, JSON, …)
//
// The 14 other endpoints from the legacy webview (extract jobs, winding audit,
// rig rebuild, …) will come back as we lift the corresponding pages over.
// Per migration/PIPELINE_API.md the endgame is a Python FastAPI backend; this
// file is intentionally minimal so the eventual port has less to replace.
//
// Workspace resolution:
//   $WOWS_WORKSPACE env var > probe candidate paths > error.
//   The workspace holds per-ship dirs (Iowa/, Yamato/, …) and
//   libraries/accessories/. It is user data — the public repo carries none of it.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';
import { devApiPlugin } from './src/server/dev_api';

const here = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => {
  // Forward WOWS_* from .env / .env.local into process.env so the dev
  // backend's workspace resolver picks it up. Vite normally exposes env
  // vars only to client code; the dev plugin runs server-side, so this
  // bridge is the simplest way to let contributors drop a one-line
  // .env.local in webview/ and forget about it.
  const env = loadEnv(mode, here, 'WOWS_');
  for (const [k, v] of Object.entries(env)) {
    if (!process.env[k]) process.env[k] = v;
  }
  return {
    plugins: [tailwindcss(), svelte(), devApiPlugin()],
    resolve: {
      alias: {
        $lib: path.resolve(here, 'src/lib'),
        $components: path.resolve(here, 'src/components'),
        $routes: path.resolve(here, 'src/routes'),
      },
    },
    server: {
      fs: {
        // Workspace lives outside the project root; resolved per-request by
        // the /repo middleware, but Vite's static-asset guard still wants
        // it on the allow-list when the URL is served as a module
        // (shouldn't happen for /repo/*, but doesn't hurt).
        strict: true,
      },
    },
    build: {
      sourcemap: true,
      target: 'es2022',
    },
  };
});
