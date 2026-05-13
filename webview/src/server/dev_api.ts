// Vite plugin: dev-only HTTP backend.
//
// Hosts the three endpoints the day-one ship view needs. Each endpoint is in
// its own module under `./endpoints/`; this file is the wiring.
//
// Discipline:
//   - No business logic here. If a handler grows beyond ~10 lines, extract.
//   - Every endpoint reads its data from `$WOWS_WORKSPACE` (resolved once at
//     server start). No hardcoded paths.
//   - Public release direction is FastAPI (migration/PIPELINE_API.md Option B);
//     handlers stay thin so they're easy to translate later.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';
import { resolveWorkspace } from './workspace';
import { mountRepoStatic } from './endpoints/repo';
import { mountShipsApi } from './endpoints/ships';
import { mountLibraryApi } from './endpoints/library';

const here = path.dirname(fileURLToPath(import.meta.url));

export function devApiPlugin(): Plugin {
  return {
    name: 'wows-model-export/dev-api',
    apply: 'serve',
    configureServer(server) {
      const workspace = resolveWorkspace(here);
      console.log(`[webview] workspace: ${workspace}`);

      mountRepoStatic(server, workspace);
      mountShipsApi(server, workspace);
      mountLibraryApi(server, workspace);
    },
  };
}
