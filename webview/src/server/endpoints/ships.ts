// GET /api/ships — per-ship summary list.
//
// Walks `<workspace>/ships/` for entries that have both a hull GLB and a
// placements JSON, returning enough metadata for the ship picker sidebar
// to render without a second fetch (section counts, sidecar-derived nation
// / class / tier, hull mtime for cache-busting).

import fs from 'node:fs';
import path from 'node:path';
import type { ViteDevServer } from 'vite';

interface SidecarSubset {
  ship?: { display_name?: string; nation?: string; class?: string; tier?: number };
}

interface PlacementsSubset {
  turrets?: unknown[];
  secondaries?: unknown[];
  antiair?: unknown[];
  torpedoes?: unknown[];
  accessories?: unknown[];
}

function countSections(p: string): {
  turrets: number;
  secondaries: number;
  antiair: number;
  torpedoes: number;
  accessories: number;
} {
  const empty = { turrets: 0, secondaries: 0, antiair: 0, torpedoes: 0, accessories: 0 };
  try {
    const doc = JSON.parse(fs.readFileSync(p, 'utf-8')) as PlacementsSubset;
    return {
      turrets: Array.isArray(doc.turrets) ? doc.turrets.length : 0,
      secondaries: Array.isArray(doc.secondaries) ? doc.secondaries.length : 0,
      antiair: Array.isArray(doc.antiair) ? doc.antiair.length : 0,
      torpedoes: Array.isArray(doc.torpedoes) ? doc.torpedoes.length : 0,
      accessories: Array.isArray(doc.accessories) ? doc.accessories.length : 0,
    };
  } catch (err) {
    console.warn(`[/api/ships] failed to parse placements at ${p}:`, err);
    return empty;
  }
}

function readSidecar(p: string): SidecarSubset | null {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf-8')) as SidecarSubset;
  } catch (err) {
    console.warn(`[/api/ships] failed to parse sidecar at ${p}:`, err);
    return null;
  }
}

function workspaceRel(p: string, workspace: string): string {
  return path.relative(workspace, p).split(path.sep).join('/');
}

export function mountShipsApi(server: ViteDevServer, workspace: string): void {
  const shipsRoot = path.join(workspace, 'ships');

  server.middlewares.use('/api/ships', (_req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-cache');
    try {
      if (!fs.existsSync(shipsRoot)) {
        res.end(JSON.stringify({ ships: [] }));
        return;
      }
      const entries = fs.readdirSync(shipsRoot, { withFileTypes: true });
      const ships: unknown[] = [];

      for (const e of entries) {
        if (!e.isDirectory() || e.name.startsWith('.')) continue;
        const name = e.name;
        const hull = path.join(shipsRoot, name, 'models', `${name}_hull.glb`);
        const placements = path.join(shipsRoot, name, 'models', `${name}_accessories.json`);
        if (!fs.existsSync(hull) || !fs.existsSync(placements)) continue;

        const hullStat = fs.statSync(hull);
        const sidecarPath = path.join(shipsRoot, name, `${name}.meta.json`);
        const sidecar = fs.existsSync(sidecarPath) ? readSidecar(sidecarPath) : null;

        ships.push({
          name,
          display_name: sidecar?.ship?.display_name ?? name,
          nation: sidecar?.ship?.nation ?? null,
          ship_class: sidecar?.ship?.class ?? null,
          tier: typeof sidecar?.ship?.tier === 'number' ? sidecar.ship.tier : null,
          hull_glb: workspaceRel(hull, workspace),
          accessories_json: workspaceRel(placements, workspace),
          sidecar_json: sidecar ? workspaceRel(sidecarPath, workspace) : null,
          hull_bytes: hullStat.size,
          hull_mtime: Math.floor(hullStat.mtimeMs / 1000),
          section_counts: countSections(placements),
        });
      }

      ships.sort((a, b) =>
        (a as { name: string }).name.localeCompare((b as { name: string }).name),
      );
      res.end(JSON.stringify({ ships }));
    } catch (err) {
      console.error('[/api/ships]', err);
      res.statusCode = 500;
      res.end(JSON.stringify({ error: 'internal_error', detail: String(err) }));
    }
  });
}
