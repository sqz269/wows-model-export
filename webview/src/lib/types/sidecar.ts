// Subset of `<Ship>.meta.json` the webview consumes. The full schema
// lives in `docs/contracts/sidecar-schema.md` (TODO); only the fields
// actively read by ship-page features live here.

import type { BallisticsSection } from './ballistics';
import type { Skin } from './skin';

export interface SidecarSlot {
  dds_mips?: string[];
}

export interface SidecarTextureScheme {
  baseColor?: SidecarSlot;
  metallicRoughness?: SidecarSlot;
  normal?: SidecarSlot;
  occlusion?: SidecarSlot;
  emissive?: SidecarSlot;
  /**
   * BC4 single-channel mask carrying the categorical no-camo region
   * marker that used to live in the normal map's B channel.
   */
  camoMask?: SidecarSlot;
}

export interface SidecarMaterial {
  material_id?: string;
  shader_intent?: string;
  texture_sets?: Record<string, SidecarTextureScheme>;
}

/**
 * Per-mount subset. The real sidecar carries more (display_name,
 * attach_to, transform, parent_section, …) — we only need what joins
 * to `ballistics.shells` and what the attached-accessories composer
 * needs.
 *
 * `misc_filter` is the per-HP WHITELIST the WG runtime uses to select
 * which of the asset's bundled miscs render on this hardpoint (verified
 * 2026-05-08 from `MiscsController._getMiscsForLoading`). Three-state
 * semantics: null = render all; `[]` = drop all non-isStyle; non-empty
 * = whitelist.
 */
export interface SidecarMount {
  instance_id?: string;
  hp_name?: string;
  asset_id?: string;
  ammo_ids?: string[];
  misc_filter?: string[];
  /**
   * Diagnostic flag stamped by `apply_variant_asset_swaps` when the host
   * placement matrix was Ry(180°)-corrected to absorb a bone mismatch.
   * No longer acted on by the renderer — schema_v6 attached_accessories
   * bakes the convention-B basis conjugation into the child matrices.
   */
  attached_y_flip?: boolean;
}

export interface SidecarShip {
  /**
   * Asset_ids that were rewritten by the variant peculiarityModels /
   * nodesConfig swap. Consumers gate the variant mat-overlay fold
   * per-asset against this list so bespoke variant albedos win over
   * the flat `mat_camo/<variant>.dds` tile.
   */
  variant_swapped_asset_ids?: string[];
}

export interface SidecarDoc {
  ship?: SidecarShip;
  materials?: SidecarMaterial[];
  turrets?: SidecarMount[];
  secondaries?: SidecarMount[];
  antiair?: SidecarMount[];
  torpedoes?: SidecarMount[];
  accessories?: SidecarMount[];
  ballistics?: BallisticsSection;
  skins?: Skin[];
}

/** Per-material scheme inventory surfaced on the Camos tab. */
export interface MaterialSchemeEntry {
  material_id: string;
  /** Includes "main" and every camo_NN / dead key seen. */
  schemes: string[];
}
