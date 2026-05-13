// Per-host `<asset>.attached_accessories.json` cache.
//
// The pipeline's asset_attachments_resolve emits this file for every
// host accessory whose source `.skel_ext` carries non-trivial bundled
// placements (main turrets, secondary mounts, complex directors). Most
// assets have none — for those `libEntry.attached_accessories` is
// absent and `load()` short-circuits to null.
//
// Time-busted URL (`?t=…`) on every fetch so dev-side regenerated
// sidecars don't get stuck behind a stale 304.

import type { AttachedAccessoriesDoc, LibraryAsset } from '$lib/types';
import { repoUrl } from '$lib/api';

export class AttachedDocCache {
  private byPath = new Map<string, Promise<AttachedAccessoriesDoc | null>>();

  /**
   * Resolve and cache the attached_accessories.json for a library asset.
   * Returns null when the asset has no attached siblings (or the fetch
   * fails — logged, non-fatal).
   */
  async load(libEntry: LibraryAsset): Promise<AttachedAccessoriesDoc | null> {
    const rel = libEntry.attached_accessories;
    if (!rel) return null;
    let p = this.byPath.get(rel);
    if (!p) {
      p = this.loadInner(rel);
      this.byPath.set(rel, p);
    }
    return p;
  }

  private async loadInner(rel: string): Promise<AttachedAccessoriesDoc | null> {
    const url = repoUrl(`libraries/accessories/${rel}`) + `?t=${Date.now()}`;
    try {
      const resp = await fetch(url);
      if (!resp.ok) return null;
      return (await resp.json()) as AttachedAccessoriesDoc;
    } catch (err) {
      console.warn(`[ship] failed to load attached_accessories ${url}:`, err);
      return null;
    }
  }

  clear(): void {
    this.byPath.clear();
  }
}
