// `/api/library` client: accessory library index.
//
// Dedupes parallel callers via a module-level promise cache. With the
// router keeping every route mounted at once (App.svelte), the Library
// and Ships pages call this on mount simultaneously; without the cache
// the dev backend would build the same index twice on first load.

import { fetchJson } from './client';
import type { LibraryIndex } from '$lib/types';

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
