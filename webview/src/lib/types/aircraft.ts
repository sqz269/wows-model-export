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

/** One clip of a model's animation document, as the index summarises it
 *  (the channels stay in the document). */
export interface AnimationClipSummary {
  source_vfs: string;
  take: string | null;
  fps: number;
  frame_count: number;
  duration_s: number;
  loop: boolean;
  nodes: string[];
  missing_nodes: string[];
  starts_at_rest: boolean;
  ends_at_rest: boolean;
  /** State machine triggers whose transitions enter the clip. */
  triggers: string[];
  /** Aircraft library only: what the clip does. */
  role?: 'bomb_bay_open' | 'bomb_bay_close' | 'gear_down' | 'gear_up' | null;
  /** Only on a clip a sequence plays: the sequence node and the frame's
   *  timing (the clip itself is baked at that speed). */
  sequence?: {
    node: string;
    vfs: string;
    start_s: number;
    duration_s: number | null;
    speed: number;
    clip_start_s: number | null;
    clip_end_s: number | null;
    [key: string]: unknown;
  };
}

/** The `animation` block an index entry (or ship sidecar) carries for one
 *  GLB: its `wows_animation/v1` document and a summary of each clip. */
export interface ModelAnimation {
  /** The document, relative to the library root (or ship folder); null
   *  when the model loads no state machine. */
  file: string | null;
  state_machines: string[];
  clips: Record<string, AnimationClipSummary>;
}

/** The index `animation` block of a WoWS airframe. */
export interface AircraftAnimation extends ModelAnimation {
  bomb_bay: { open: string | null; close: string | null } | null;
  gear: {
    mode: 'switch' | 'clip';
    down?: string | null;
    up?: string | null;
    down_render_sets: string[];
    up_render_sets: string[];
  };
  /** Pivots that spin about their local `axis`; `opposite` turns the other
   *  way from `direct`. */
  propeller_spin: { axis: [number, number, number]; direct: string[]; opposite: string[] };
}

/** One channel of a `wows_animation/v1` clip: a GLB node's local
 *  translation, rotation (quaternion x, y, z, w) or scale over time. */
export interface AnimationChannel {
  node: string;
  path: 'translation' | 'rotation' | 'scale';
  times: number[];
  values: number[][];
}

export interface AnimationDocument {
  schema: string;
  model_vfs: string;
  glb: string | null;
  state_machines: Array<{ vfs: string; entry: string | null; [key: string]: unknown }>;
  clips: Record<string, AnimationClipSummary & { channels: AnimationChannel[] }>;
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
  visual_json?: string | null;
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
  /** Clips, gear rule and propeller spin; absent on entries another
   *  producer added. */
  animation?: AircraftAnimation;
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
