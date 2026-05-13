// Shared per-camo-category masks + mat_camo full-ship albedos.
//
// These are SHARED across meshes (the camo's `Plane_tile_camo_R.dds`
// is referenced by every accessory in the plane / float category), so
// the cache is keyed by basename-stem rather than URL — multiple
// references to the same stem share one decoded GPU texture.
//
// Lives separately from the per-entry `decodedTextureCache` because the
// dispose paths differ: `categoryMaskCache` / `matAlbedoCache` clear on
// ship swap, the entry cache survives.

import type * as THREE from 'three';
import { loadDdsMipChain, resolveDdsMipUrls } from '$lib/dds';
import type { DDSLoader } from '$lib/dds';
import type { Skin } from '$lib/types';

export class CategoryMaskCache {
  private cache = new Map<string, THREE.Texture>();

  constructor(
    private renderer: THREE.WebGLRenderer,
    private ddsLoader: DDSLoader,
    private repoBaseUrl: string,
  ) {}

  /** Decode + cache one `Skin.categories[<cat>].mask`. URL-keyed; idempotent. */
  async ensure(mask: { dds_mips: string[] }): Promise<THREE.Texture | null> {
    if (mask.dds_mips.length === 0) return null;
    const stemBase = mask.dds_mips[0].split(/[\\/]/).pop() ?? '';
    const cacheKey = stemBase.replace(/\.[^.]+$/, '');
    const cached = this.cache.get(cacheKey);
    if (cached) return cached;

    const urls = resolveDdsMipUrls(mask.dds_mips, this.repoBaseUrl);
    if (urls.length === 0) return null;
    const tex = await loadDdsMipChain(urls, /* sRGB */ true, this.ddsLoader, this.renderer);
    if (!tex) return null;
    this.cache.set(cacheKey, tex);
    return tex;
  }

  /** Sync lookup; caller must have run {@link ensure} first. */
  get(mask: { dds_mips: string[] }): THREE.Texture | null {
    if (mask.dds_mips.length === 0) return null;
    const stemBase = mask.dds_mips[0].split(/[\\/]/).pop() ?? '';
    const cacheKey = stemBase.replace(/\.[^.]+$/, '');
    return this.cache.get(cacheKey) ?? null;
  }

  /** Pre-decode every Path-A `mask` on the skin. */
  async ensureForSkin(skin: Skin | null): Promise<void> {
    if (!skin?.categories) return;
    const tasks: Promise<unknown>[] = [];
    for (const data of Object.values(skin.categories)) {
      if (data.mask) tasks.push(this.ensure(data.mask));
    }
    await Promise.all(tasks);
  }

  clear(): void {
    for (const t of this.cache.values()) t.dispose();
    this.cache.clear();
  }
}

export class MatAlbedoCache {
  private cache = new Map<string, THREE.Texture>();

  constructor(
    private renderer: THREE.WebGLRenderer,
    private ddsLoader: DDSLoader,
    private repoBaseUrl: string,
  ) {}

  async ensure(albedo: { dds_mips: string[] }): Promise<THREE.Texture | null> {
    if (albedo.dds_mips.length === 0) return null;
    const stemBase = albedo.dds_mips[0].split(/[\\/]/).pop() ?? '';
    const cacheKey = stemBase.replace(/\.[^.]+$/, '');
    const cached = this.cache.get(cacheKey);
    if (cached) return cached;

    const urls = resolveDdsMipUrls(albedo.dds_mips, this.repoBaseUrl);
    if (urls.length === 0) return null;
    // sRGB=true: mat_*_a.dds files are albedos (color textures).
    const tex = await loadDdsMipChain(urls, /* sRGB */ true, this.ddsLoader, this.renderer);
    if (!tex) return null;
    this.cache.set(cacheKey, tex);
    return tex;
  }

  get(albedo: { dds_mips: string[] }): THREE.Texture | null {
    if (albedo.dds_mips.length === 0) return null;
    const stemBase = albedo.dds_mips[0].split(/[\\/]/).pop() ?? '';
    const cacheKey = stemBase.replace(/\.[^.]+$/, '');
    return this.cache.get(cacheKey) ?? null;
  }

  async ensureForSkin(skin: Skin | null): Promise<void> {
    if (!skin?.mat_textures) return;
    const tasks: Promise<unknown>[] = [];
    for (const data of Object.values(skin.mat_textures)) {
      tasks.push(this.ensure(data.albedo));
    }
    await Promise.all(tasks);
  }

  clear(): void {
    for (const t of this.cache.values()) t.dispose();
    this.cache.clear();
  }
}
