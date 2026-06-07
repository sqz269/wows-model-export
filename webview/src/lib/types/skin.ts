// Skin / camo types. Mirrors `<Ship>.meta.json::skins[]`.
//
// Every ship has at least the mandatory `default` skin (scheme_key =
// "main"). Legacy/non-WG skins can point at a `texture_sets[scheme_key]`
// block on participating materials. Modern WG-authored camos can instead
// carry all paint data in `categories` / `mat_textures`, resolved from
// GameParams + camouflages.xml; in that case materials fall back to `main`
// for base PBR slots.

export interface SkinOverride {
  material_id?: string;
  textures?: Record<string, unknown>;
  factors?: Record<string, unknown>;
}

/**
 * 4×RGBA palette (linear floats) carried per skin when the sidecar
 * resolved the camouflages.xml entry. Consumers composite
 * `mask + palette → albedo` at decode time:
 *   mask zone (r-dominant) → colors[1]
 *   mask zone (g-dominant) → colors[2]
 *   mask zone (b-dominant) → colors[3]
 *   mask zone (black/dim)  → colors[0]
 */
export interface SkinColorScheme {
  name: string;
  colors: [
    [number, number, number, number],
    [number, number, number, number],
    [number, number, number, number],
    [number, number, number, number],
  ];
}

/**
 * Path B paramset (per-category subset of `ship_camo_mgn_material.fx`'s
 * `$Globals` CB). Surfaces from `<Part_mgn>` blocks in camouflages.xml.
 * Presence on a `SkinMatCategoryAlbedo` record is what the consumer
 * dispatches on: absent → Path A flat overlay, present → Path B blend.
 */
export interface SkinMatCategoryParams {
  /** -1 / 0 = no override, 1 = zone-1 RGB blend, 2 = full RGB lerp by mask alpha,
   *  3 = split blend. */
  camo_mode: number;
  use_camo_mask_global: boolean;
  /** (metallic_mix, gloss_mix, normal_mix) — `.w` was DXBC-confirmed dead. */
  mgn_influence: [number, number, number];
  /** 0..1 — controls how strongly the mask alpha darkens base albedo. */
  ao_influence: number;
  emission_anim_mode: number;
  emission_color_mode: number;
  emission_base_power: number;
  emission_anim_max_power: number;
  mask_smooth: number;
  anim_scale: [number, number, number];
  mask_speed: [number, number, number];
  mask_color1: [number, number, number];
  mask_color2: [number, number, number, number];
}

/**
 * Per-camo-category shared mask + UV transform. Modern WG sidecars may
 * include hull-side categories (`tile`, `deckhouse`, `bulge`) here so
 * official camo binding does not depend on per-material filename schemes.
 * See `classifyPartCategory` / `classifyPlacementCategory` for routing.
 */
export interface SkinCategoryMask {
  /**
   * Path A mask. Optional because pure Path B emits omit it.
   */
  mask?: { dds_mips: string[] };
  uv: {
    scale: [number, number];
    offset: [number, number];
  };
  /**
   * Path B optional fields. When `mgn` is present the consumer follows
   * the engine's per-part rule: prefer `mgn` over `mask`.
   */
  mgn?: { dds_mips: string[] };
  anim_map?: { dds_mips: string[] };
  params?: SkinMatCategoryParams;
}

/**
 * Per-category mat_* full-ship albedo + UV transform. Mirrors
 * `SkinCategoryMask` in shape but the texture is a PRE-BAKED ALBEDO
 * that REPLACES the per-stem base color rather than overlaying a
 * zone-classified mask.
 */
export interface SkinMatCategoryAlbedo {
  albedo: { dds_mips: string[] };
  uv: {
    scale: [number, number];
    offset: [number, number];
  };
  mgn?: { dds_mips: string[] };
  anim_map?: { dds_mips: string[] };
  params?: SkinMatCategoryParams;
}

export interface AssetOverrideEntry {
  verdict: string;
  skip_reason?: string;
  fallback?: string;
  texture_sets?: Record<string, Record<string, { dds_mips: string[] }>>;
}

/**
 * Discriminator for `Skin.kind`:
 *   'mat_albedo'   — pre-baked full-ship albedo replacement. The texture
 *                    IS the final paint; skip the camo overlay and both
 *                    N.B + Y gates entirely.
 *   'mask_palette' — implicit default for skins that carry
 *                    `color_scheme` + category or per-stem masks.
 *   undefined      — same as `mask_palette` (legacy sidecars).
 */
export type SkinKind = 'mat_albedo' | 'mask_palette';

export interface Skin {
  skin_id: string;
  display_name: string;
  scheme_key: string;
  camo_pattern?: string | null;
  color_roll?: string | null;
  color_scheme?: SkinColorScheme | null;
  /**
   * Per-camo-category overrides. Keys are canonical lowercase categories
   * (`tile`, `deckhouse`, `bulge`, `gun`, `director`, `plane`, `float`,
   * `misc`, `wire`).
   */
  categories?: Record<string, SkinCategoryMask>;
  /** Discriminates renderer recipe. Unset = legacy mask + palette. */
  kind?: SkinKind;
  /** Provenance — trace-back only; renderer doesn't read these. */
  exterior_id?: string;
  peculiarity?: string;
  /** Per-category mat_* full-ship albedos. Present iff `kind=mat_albedo`. */
  mat_textures?: Record<string, SkinMatCategoryAlbedo>;
  /** Provenance string (`loose:<dir>`, `vfs:<asset_id> via <exterior_id>`). */
  source?: string;
  /**
   * v3.2 skin packs: per-library-asset texture overrides that ship
   * alongside the hull paint. Maps asset_id → override entry.
   */
  asset_overrides?: Record<string, AssetOverrideEntry>;
  overrides?: SkinOverride[];
  /**
   * Per-skin V-flip override. `null` / undefined = auto (derive from
   * `source` — `loose:` → off, vanilla / VFS extract → on).
   * `true` / `false` = force the convention regardless of source.
   * Mirrors Unity's `SidecarSchema.cs:556` `flip_v` field. Useful for
   * the rare loose mod authored top-down or the unusual VFS variant
   * shipped bottom-up. Consumers don't yet honor this in the webview
   * (Unity does); schema kept in sync so loose-mod sidecars aren't
   * silently lossy on this field.
   */
  flip_v?: boolean | null;
}
