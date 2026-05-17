// Shared types for the texture pipeline.

import type * as THREE from 'three';
import type { TextureSet } from '$lib/types';

/** Per-slot mip-chain URL list, level-ordered (`.dd0` → `.dds`). */
export type SlotUrls = Partial<Record<keyof TextureSet, string[]>>;

/** Slot → decoded GPU texture. */
export type TextureSetResolved = Partial<Record<keyof TextureSet, THREE.Texture>>;

/**
 * Tracks one mesh participating in the texture toggle. We swap
 * `mesh.material` between `untextured` and the active skin's textured
 * material (built lazily on first activation).
 */
export interface TextureMeshEntry {
  mesh: THREE.Mesh;
  /**
   * Scheme key → slot URLs. Always has a `main` entry once the bind has
   * landed (filled at registration if the key was bound first).
   */
  slotUrlsByScheme: Map<string, SlotUrls>;
  untextured: THREE.Material | THREE.Material[];
  /**
   * Cached textured clone keyed by `main`. The camo shader chunk on this
   * clone reads the base via `map` and the camo mask via a `maskMap`
   * uniform that gets swapped per active skin — so switching schemes
   * is a uniform update, no clone rebuild.
   *
   * EXCEPTION: skin packs (kind=mat_albedo with per-material
   * `texture_sets[<scheme>]` blocks AND/OR per-asset
   * `Skin.asset_overrides[<asset_id>].texture_sets[main]`) replace the
   * base albedo wholesale. Those schemes get their own clone in
   * `texturedByScheme`.
   */
  textured: THREE.Material | THREE.Material[] | null;
  texturedByScheme: Map<string, THREE.Material | THREE.Material[]>;
  /** Per-scheme camo mask textures, decoded lazily. */
  maskTextureByScheme: Map<string, THREE.Texture>;
  isAccessoryEntry: boolean;
  /**
   * Library asset_id when known. Used at uniform-update time to skip
   * the variant mat-overlay fold for accessories carrying bespoke
   * variant albedos (e.g. `GGM3003_..._Azur_a.dds`). Null for hull
   * entries.
   */
  assetId: string | null;
  /**
   * Camo part-category — `tile`/`deckhouse`/`bulge` for hull, or
   * `gun`/`director`/`plane`/`float`/`misc` for accessories. Computed
   * once at registration; drives `Skin.categories[<cat>]` lookup.
   */
  category: string;
  /**
   * False for materials the sidecar marks `shader_intent: "transparent"`
   * (glass, semi-transparent armor visualizers). Engine analog: WG routes
   * these through `ship_transparent_*.fx` which has no camo recipe.
   * Cutout (alpha-tested) stays at true — engine's Path A uses its own
   * `discard_nz` against diffuse.a inside `ship_camo_material.fx`.
   */
  acceptsCamo: boolean;
}
