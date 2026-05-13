// GET /repo/<rel-path> — static workspace file service.
//
// The webview reads hull GLBs, sidecar JSON, DDS mip chains, and accessory
// library GLBs from the user's workspace. Browsers can't `fetch('file://')`,
// so this middleware proxies workspace files through the dev server.
//
// Security: traversal is blocked by an `isChildOf` check before reading.
// Symlinks are followed (so workspace junctions on Windows work), but the
// resolved path must still land inside the workspace root.

import fs from 'node:fs';
import path from 'node:path';
import type { Stats } from 'node:fs';
import type { ViteDevServer } from 'vite';
import { isChildOf } from '../workspace';

const MIME: Record<string, string> = {
  '.json': 'application/json',
  '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json',
  '.dds': 'image/vnd.ms-dds',
  '.dd0': 'application/octet-stream',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.bin': 'application/octet-stream',
};

function mimeForExt(ext: string): string {
  return MIME[ext.toLowerCase()] ?? 'application/octet-stream';
}

export function mountRepoStatic(server: ViteDevServer, workspace: string): void {
  server.middlewares.use('/repo', (req, res, next) => {
    try {
      const url = req.url ?? '/';
      const decoded = decodeURIComponent(url.split('?')[0]);
      const rel = decoded.replace(/^\/+/, '');
      if (!rel) return next();

      const abs = path.resolve(workspace, rel);
      // Allow `abs === workspace` only when the request was a literal dir;
      // the file branch below will 404 it. Strict child-of for everything
      // else so `..` traversal cannot escape.
      if (abs !== workspace && !isChildOf(abs, workspace)) {
        res.statusCode = 403;
        res.end('forbidden');
        return;
      }

      fs.stat(abs, (err: NodeJS.ErrnoException | null, stat: Stats) => {
        if (err || !stat.isFile()) {
          res.statusCode = 404;
          res.end('not found');
          return;
        }
        res.setHeader('Content-Type', mimeForExt(path.extname(abs)));
        res.setHeader('Content-Length', String(stat.size));
        res.setHeader('Cache-Control', 'no-cache');
        fs.createReadStream(abs).pipe(res);
      });
    } catch (err) {
      console.error('[/repo]', err);
      res.statusCode = 500;
      res.end('internal error');
    }
  });
}
