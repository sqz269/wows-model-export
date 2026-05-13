// GET /api/library — accessory library index.
//
// Reads `<workspace>/libraries/accessories/index.json` on every request so
// the SPA stays in sync with library rebuilds without a manual refresh.
// File missing → 404 with a hint at how to generate it.

import fs from 'node:fs';
import path from 'node:path';
import type { ViteDevServer } from 'vite';

export function mountLibraryApi(server: ViteDevServer, workspace: string): void {
  const indexPath = path.join(workspace, 'libraries', 'accessories', 'index.json');

  server.middlewares.use('/api/library', (_req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-cache');
    try {
      if (!fs.existsSync(indexPath)) {
        res.statusCode = 404;
        res.end(
          JSON.stringify({
            error: 'library_index_missing',
            path: indexPath,
            hint: 'Run `wows-build-accessory-library` to generate it.',
          }),
        );
        return;
      }
      res.end(fs.readFileSync(indexPath, 'utf-8'));
    } catch (err) {
      console.error('[/api/library]', err);
      res.statusCode = 500;
      res.end(JSON.stringify({ error: 'internal_error', detail: String(err) }));
    }
  });
}
