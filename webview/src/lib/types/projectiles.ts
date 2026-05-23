// Projectile library + ammo profile types. Mirrors the schemas emitted
// by `wows-build-projectile-library` (`libraries/projectiles/index.json`)
// and `wows-build-ammo-profiles` (`libraries/projectiles/ammo_profiles.json`).
//
// The two are joined by `AmmoProfile.asset_id -> ProjectileIndex.assets[id]`.
// Many ammo profiles share one mesh (every artillery shell uses
// `CPA001_Shell_Main`); pure-VFX ammo (lasers, plane tracers) has
// `asset_id: null`.

import type { TextureSet } from './library';

// ── Mesh side (per asset_id) ────────────────────────────────────────────

export interface ProjectileIndex {
  /** Free-form YYYY-MM-DD stamp written by the producer. */
  version: string;
  asset_count: number;
  assets: Record<string, ProjectileMesh>;
}

export interface ProjectileMesh {
  /** Lowercased two/three-letter nation tag (usa / japan / uk / pan_europe / …). */
  nation: string;
  /** Producer-emitted category — shell / torpedo / bomb / depth_charge / mine / …. */
  category: string;
  /** Toolkit VFS path the producer copied geometry from. */
  geometry_vfs: string;
  /** Workspace-relative path under `libraries/projectiles/`. */
  glb: string;
  glb_bytes: number;
  /** Unix ms epoch — preserved across rebuilds (matches accessory pattern). */
  built_at?: number | null;
  /** Workspace-relative directory under `libraries/projectiles/`. */
  textures_dds?: string | null;
  /** Per-material PBR slot map (shape matches `LibraryAsset.materials`). */
  materials?: Array<{
    material_id?: string;
    texture_sets?: Record<string, Record<string, { dds_mips: string[] }>>;
    [key: string]: unknown;
  }>;
  /** Asset-level flat fallback (shape matches `LibraryAsset.texture_sets`). */
  texture_sets?: Record<string, TextureSet>;
}

// ── Ammo profile side (per ammo_id) ─────────────────────────────────────

/**
 * GameParams categorical type. Mirrors the producer's `species` field,
 * which is the engine's discriminator for which `visual` / `effects`
 * fields are populated.
 */
export type AmmoSpecies =
  | 'Artillery'
  | 'Torpedo'
  | 'Bomb'
  | 'DepthCharge'
  | 'Mine'
  | 'Rocket'
  | 'Laser'
  | 'Wave'
  | 'PlaneTracer'
  | string; // catch-all for new GameParams species we haven't seen yet

export interface AmmoProfile {
  /** WG taxonomy: AP / HE / SAP / CS (common shell) / Torpedo / Bomb / …. */
  ammo_type: string;
  species: AmmoSpecies;
  /**
   * Linked mesh in the projectile index, or `null` for pure-VFX entities
   * (lasers, plane tracers, waves) that have no on-screen mesh.
   */
  asset_id: string | null;
  /**
   * Visual extras — exact field set varies by species. Shells carry
   * tracer color + size multipliers; torpedoes carry speed / wake
   * params; bombs carry parachute / drag fields; lasers carry colour
   * + intensity.
   */
  visual?: Record<string, unknown>;
  /**
   * Damage / range / fuse / detonation params. Shape varies by species
   * the same way `visual` does.
   */
  effects?: Record<string, unknown>;
}

export interface AmmoProfilesDoc {
  version: string;
  profile_count: number;
  profiles: Record<string, AmmoProfile>;
}

// ── Merged payload (what `/api/projectiles` returns) ────────────────────

export interface ProjectilesPayload {
  ok: true;
  index: ProjectileIndex;
  ammo_profiles: AmmoProfilesDoc;
}

export interface ProjectilesMissingPayload {
  ok: false;
  error: 'projectiles_artifacts_missing';
  missing: string[];
  hint: string;
}

// ── List-mode UI types ──────────────────────────────────────────────────

/** Which list shape the user is currently browsing. */
export type ProjectileListMode = 'mesh' | 'ammo';

/**
 * Tri-state chip filter — off (in neither set), include (whitelist),
 * exclude (blacklist). Mirrors the Extract page's `ChipFilter<T>`.
 */
export interface ProjectileChipFilter<T> {
  include: Set<T>;
  exclude: Set<T>;
}

export interface ProjectileFilterState {
  text: string;
  nations: ProjectileChipFilter<string>;
  categories: ProjectileChipFilter<string>;
  /** Only meaningful in `ammo` mode. */
  ammoTypes: ProjectileChipFilter<string>;
  species: ProjectileChipFilter<string>;
}
