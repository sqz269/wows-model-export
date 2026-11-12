// Aircraft library types. Mirrors `libraries/aircraft/index.json`
// (`wows-build-aircraft-library`, schema `wows_aircraft_library/v1`;
// reference/schemas/AIRCRAFT_LIBRARY.md).

import type { TextureSet } from './library';

/** Client part a render set belongs to. `airframe` sets are always drawn;
 *  the others are toggled by the plane's state (and the rack by its
 *  ordnance). */
export type AircraftPart =
  | 'airframe'
  | 'gear_down'
  | 'gear_up'
  | 'wire'
  | 'cradle'
  | 'propeller_disc'
  | 'propeller_blades'
  | 'propeller_cap'
  | 'rack';

export interface AircraftMesh {
  /** The node the render set hangs on (the client's render-set name). */
  render_set: string;
  part: AircraftPart;
  /** Authored LODs this mesh draws in (indices into `lods`). */
  lods: number[];
  material: string | null;
  skinned: boolean;
}

export interface AircraftLod {
  /** Camera distance beyond which this LOD stops drawing, in the visual's
   *  own units (BW: × 15 for metres). Null on an entry another producer
   *  added whose LOD rule is not a distance (`source` other than "wows"). */
  extent: number | null;
  casts_shadow: boolean;
  meshes: string[];
}

export interface AircraftRack {
  render_set: string;
  ordnance: string;
  variant: 'default' | 'custom';
  index: number;
}

export interface AircraftPropeller {
  node: string;
  spin: 'direct' | 'opposite';
  render_sets: string[];
}

export interface AircraftGameParamsEntry {
  key: string;
  species: string | null;
  nation: string | null;
  level: number | null;
  plane_subtype: string[];
}

export interface AircraftAsset {
  /** "wows" when absent; another value marks an entry another producer
   *  added (no VFS model, no GameParams entries). */
  source?: string;
  /** "glb": the GLB's own materials carry embedded textures (no DDS). */
  textures?: string;
  /** Stats sidecar keys of an entry another producer added. */
  stats_keys?: string[];
  nation: string;
  category: string;
  geometry_vfs: string | null;
  visual_vfs?: string | null;
  /** Path relative to `<workspace>/libraries/aircraft/`. */
  glb: string;
  glb_bytes: number;
  built_at?: number | null;
  textures_dds?: string | null;
  visual_json?: string;
  material_mappings?: string | null;
  skinned: boolean;
  bounds_m: { min: [number, number, number]; max: [number, number, number] } | null;
  dimensions_m: { span: number; height: number; length: number } | null;
  lods: AircraftLod[];
  meshes: Record<string, AircraftMesh>;
  render_sets: Record<
    string,
    { part: AircraftPart; meshes: string[]; rack?: Omit<AircraftRack, 'render_set'> }
  >;
  parts: Partial<Record<AircraftPart, string[]>>;
  racks: AircraftRack[];
  propellers: AircraftPropeller[];
  moving_parts: Record<string, string[]>;
  hardpoints: Record<string, string[]>;
  materials?: Array<{
    material_id?: string;
    texture_sets?: Record<string, Record<string, { dds_mips: string[] }>>;
    [key: string]: unknown;
  }>;
  texture_sets?: Record<string, TextureSet>;
  gameparams: AircraftGameParamsEntry[];
}

export interface AircraftIndex {
  schema: string;
  version: string;
  asset_count: number;
  assets: Record<string, AircraftAsset>;
}

export interface AircraftPayload {
  ok: true;
  index: AircraftIndex;
}

/** The client's render presets (which toggled parts show in each plane
 *  state); everything of part `airframe` always draws. */
export type AircraftPreset = 'fly' | 'stand' | 'launch';

export const AIRCRAFT_PRESET_PARTS: Record<AircraftPreset, AircraftPart[]> = {
  fly: ['airframe', 'gear_up', 'propeller_disc', 'propeller_cap', 'wire'],
  stand: ['airframe', 'gear_down', 'propeller_blades', 'wire'],
  launch: ['airframe', 'gear_down', 'propeller_disc', 'propeller_cap', 'wire'],
};
