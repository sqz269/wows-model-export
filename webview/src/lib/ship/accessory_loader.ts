// Accessory GLB cache: one fetch + parse per library asset_id, regardless
// of how many placements reference it. Concurrent clones share the same
// in-flight promise so the network/parse work happens once.
//
// Templates are kept across `clearShip()` calls — most ship swaps reuse
// the same library accessories (USA-USA, JP-JP, …) — and disposed only
// when the viewer itself disposes.

import type * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { repoUrl } from '$lib/api';
import { disposeTree } from '$lib/three/dispose';

export class AccessoryCache {
  private loader = new GLTFLoader();
  private byPath = new Map<string, Promise<THREE.Object3D | null>>();

  /**
   * Resolve and cache a library accessory GLB by its accessories-relative
   * path (`libEntry.glb`). Returns `null` (and warns) when the GLB fails
   * to fetch or parse.
   */
  async load(libRelPath: string): Promise<THREE.Object3D | null> {
    let p = this.byPath.get(libRelPath);
    if (!p) {
      p = this.loadInner(libRelPath);
      this.byPath.set(libRelPath, p);
    }
    return p;
  }

  private async loadInner(libRelPath: string): Promise<THREE.Object3D | null> {
    const url = repoUrl(`libraries/accessories/${libRelPath}`);
    try {
      const gltf = await this.loader.loadAsync(url);
      return gltf.scene;
    } catch (err) {
      console.warn(`[ship] failed to load accessory ${url}:`, err);
      return null;
    }
  }

  /** Dispose every cached template (GPU resources). Idempotent. */
  async dispose(): Promise<void> {
    const entries = Array.from(this.byPath.values());
    this.byPath.clear();
    for (const p of entries) {
      try {
        const tpl = await p;
        if (tpl) disposeTree(tpl);
      } catch {
        // ignore — failed loads already logged
      }
    }
  }
}
