// Per-URL decoded-texture cache + the `decodeSlot` resolver.
//
// Cache survives toggle-off → toggle-on so the second activation is
// instant. The wgPackMG/wgPackN sniff stamps userData based on the
// source filename suffix — used at material-build time to drive the
// shader's channel-pack reinterpretation for loose-mod skin packs that
// ship raw WG `_mg.dd?` / `_n.dd?` instead of glTF-conformant siblings.

import type * as THREE from 'three';
import { DDS_SLOT_SRGB, type DDSLoader, loadDdsMipChain } from '$lib/dds';
import type { TextureSet } from '$lib/types';

export class DecodedTextureCache {
  private byFirstUrl = new Map<string, THREE.Texture>();

  constructor(
    private renderer: THREE.WebGLRenderer,
    private ddsLoader: DDSLoader,
  ) {}

  /**
   * Fetch + decode one slot's mip chain into a CompressedTexture (BC7 +
   * classic) or DataTexture (BC4). URL-keyed cache; same chain re-used
   * across materials.
   */
  async decode(slot: keyof TextureSet, urls: string[]): Promise<THREE.Texture | null> {
    if (urls.length === 0) return null;
    const cacheKey = urls[0];
    const cached = this.byFirstUrl.get(cacheKey);
    if (cached) return cached;
    const sRGB = !!DDS_SLOT_SRGB[slot];
    const tex = await loadDdsMipChain(urls, sRGB, this.ddsLoader, this.renderer);
    if (!tex) return null;

    // Channel-pack hint. The shader reinterprets sampled texels for raw
    // WG `_mg.dd*` / `_n.dd*` (loose-mod skins that bypass swizzle);
    // conformant `_mr.dd*` / `_normal.dd*` siblings keep glTF semantics.
    // Detection mirrors the sidecar's `_SUFFIX_PRIORITY` ordering.
    const fname = (cacheKey.split('/').pop() ?? '').toLowerCase();
    const stem = fname.replace(/\.(dd[012]|dds)$/i, '');
    if (slot === 'metallicRoughness') {
      tex.userData.wgPackMG = stem.endsWith('_mg');
    } else if (slot === 'normal') {
      tex.userData.wgPackN = stem.endsWith('_n');
    }

    this.byFirstUrl.set(cacheKey, tex);
    return tex;
  }

  clear(): void {
    for (const t of this.byFirstUrl.values()) t.dispose();
    this.byFirstUrl.clear();
  }

  get size(): number {
    return this.byFirstUrl.size;
  }
}
