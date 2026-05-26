// `/api/library` + related client: accessory library index, rig
// overrides + rebuild.
//
// `fetchLibrary` dedupes parallel callers via a module-level promise
// cache. With the router keeping every route mounted at once
// (App.svelte), the Library and Ships pages call this on mount
// simultaneously; without the cache the dev backend would build the
// same index twice on first load.
//
// The rig endpoints have no caching — each call mutates disk state and
// the callers want fresh reads.

import { fetchJson } from './client';
import type {
  LibraryIndex,
  RigOverridesDoc,
  RigPivots,
} from '$lib/types';

let cached: Promise<LibraryIndex> | null = null;

export function fetchLibrary(): Promise<LibraryIndex> {
  if (!cached) {
    cached = fetchJson<LibraryIndex>('/api/library').catch((err) => {
      // Don't pin a failed result — a transient backend hiccup should
      // not prevent a retry on the next mount.
      cached = null;
      throw err;
    });
  }
  return cached;
}

/** Reset the cache. Useful for a "rebuild library" hook later. */
export function invalidateLibrary(): void {
  cached = null;
}

// ── Rig pivots (from /repo/) ────────────────────────────────────────────

import { repoUrl } from './client';

/** Fetch `<asset>.rig_pivots.json` from the repo static server.
 *  Returns `null` when the file isn't on disk (asset hasn't been
 *  rigged yet) so the caller can clear any existing overlay. */
export async function fetchRigPivots(relGlb: string): Promise<RigPivots | null> {
  const rel = relGlb.replace(/\.glb$/i, '.rig_pivots.json');
  try {
    const resp = await fetch(repoUrl(`libraries/accessories/${rel}`));
    if (!resp.ok) return null;
    return (await resp.json()) as RigPivots;
  } catch {
    return null;
  }
}

/** URL for the asset's `.rig.debug.glb`. The viewer's `loadDebugSceneGlb`
 *  consumes this directly. Cache-bust appended so a rebuild reloads. */
export function rigDebugGlbUrl(relGlb: string): string {
  const rel = relGlb.replace(/\.glb$/i, '.rig.debug.glb');
  return repoUrl(`libraries/accessories/${rel}`) + `?t=${Date.now()}`;
}

// ── Rig overrides + rebuild ─────────────────────────────────────────────

export interface RigOverridesResult {
  ok: boolean;
  exists?: boolean;
  doc?: RigOverridesDoc | null;
  error?: string;
}

/** GET `/api/rig-overrides?assetId=X`. `exists=false` is a normal state
 *  (asset has no overrides yet); only treat non-ok as an error. */
export async function fetchRigOverrides(assetId: string): Promise<RigOverridesResult> {
  const resp = await fetch(
    `/api/rig-overrides?assetId=${encodeURIComponent(assetId)}`,
  );
  return (await resp.json()) as RigOverridesResult;
}

export interface SaveRigOverridesResult {
  ok: boolean;
  path?: string;
  deleted?: boolean;
  error?: string;
}

export async function saveRigOverrides(
  assetId: string,
  doc: RigOverridesDoc,
): Promise<SaveRigOverridesResult> {
  const resp = await fetch(
    `/api/rig-overrides?assetId=${encodeURIComponent(assetId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(doc),
    },
  );
  return (await resp.json()) as SaveRigOverridesResult;
}

export async function deleteRigOverrides(assetId: string): Promise<SaveRigOverridesResult> {
  const resp = await fetch(
    `/api/rig-overrides?assetId=${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  );
  return (await resp.json()) as SaveRigOverridesResult;
}

export interface RigRebuildResult {
  ok: boolean;
  stdout?: string;
  stderr?: string;
  error?: string;
}

/** Spawn `wows-turret-autorig <assetId>`. Refreshes both
 *  `<asset>.rig_pivots.json` and (when the autorig CLI grows
 *  `--debug-scene` support) the `<asset>.rig.debug.glb` the rig editor
 *  consumes. */
export async function postRigRebuild(assetId: string): Promise<RigRebuildResult> {
  const resp = await fetch('/api/rig-rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assetId }),
  });
  return (await resp.json()) as RigRebuildResult;
}
