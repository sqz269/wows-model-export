// Shared tunables, unit conversions, and engine enum constants.
// Extracted verbatim from the former monolithic particles.ts.

import * as THREE from 'three';

export const DEFAULT_PARTICLE_LIFETIME = 4.0; // seconds, when WG didn't author one
export const ABSOLUTE_MAX_CAPACITY = 512; // hard cap per system
// The toolkit exports ship geometry/bones/rig pivots scaled by this factor
// (compose/turret_autorig.py:254 NATIVE_TO_METRES) so the GLB is in metres
// (Baltimore hull = 205.8 m, ~15× its native BigWorld-unit length). Particle
// records, however, carry RAW native BigWorld-unit lengths (the faithful
// engine decode). The native engine has no ×15 — it renders everything in one
// BW-unit space (RE: FUN_1406d29c0 builds the billboard corner as
// worldPos + billboardAxis·(size·tiling), so SIZE is a world-space length in
// the SAME unit as position). Because the consumer's world is ×15 metres,
// every length-dimensioned particle quantity — size, spawn offset, velocity,
// and all velocity-derived displacement — must be ×15 to sit correctly on the
// ship. The sim runs in raw record units and this factor is applied to its
// OUTPUT (and the few world-frame INPUTS are divided back). NOT applied to
// times, dimensionless multipliers (dampfer/ageScale), sprite-space offsets
// (customCenterOffset, tiling), or colour.
export const NATIVE_TO_METRES = 15;
// Native per-particle update substep ceiling, seconds (FUN_140718f00 clamps
// every integration substep to DAT_142556548 = 0.25; RE 2026-06-09).
export const NATIVE_SUBSTEP_MAX_S = 0.25;
export const DEFAULT_SIZE = 0.02; // native BW units (≈0.3 m after NATIVE_TO_METRES) —
// sane baseline if the particle didn't author a size generator
export const HARD_MAX_EMIT_RATE_HZ = 200; // safety clamp on the per-frame
// particles-emitted count
export const PARTICLE_POINT_LIGHT_BUDGET = 24;
export const CHILD_EFFECT_DEPTH_LIMIT = 3;
export const CHILD_EFFECT_BUDGET = 256;
export const CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK = 8;
export const SEA_LEVEL_Y = 0;
export const DEFAULT_PARTICLE_SUN_DIR = new THREE.Vector3(50, 80, 50).normalize();
export const DEFAULT_PARTICLE_SUN_COLOR_NORM = new THREE.Color(0.5, 0.5, 0.5);

export const PS_IC_PARTICLE_TILING_U = 0;
export const PS_IC_LIGHT_TINT_R = 1;
export const PS_IC_PARTICLE_STREAMER_X = 2;
export const PS_IC_PARTICLE_SCALE_X = 3;
export const PS_IC_PARTICLE_VEL_Z = 4;
export const PS_IC_LIGHT_RADIUS = 5;
export const PS_IC_LIGHT_TINT_B = 6;
export const PS_IC_PARTICLE_COLOR_R = 7;
export const PS_IC_AGE_SCALE = 8;
export const PS_IC_PARTICLE_COLOR_B = 9;
export const PS_IC_PARTICLE_VEL_Y = 10;
export const PS_IC_PARTICLE_TILING_V = 11;
export const PS_IC_PARTICLE_COLOR_A = 12;
export const PS_IC_PARTICLE_TINT_G = 13;
export const PS_IC_AGE_AUX_SCALE = 14;
export const PS_IC_PARTICLE_TINT_B = 15;
export const PS_IC_PARTICLE_SCALE_Y = 16;
export const PS_IC_PARTICLE_STREAMER_Y = 17;
export const PS_IC_PARTICLE_TINT_R = 18;
export const PS_IC_EMITTER_RATE = 19;
export const PS_IC_PARTICLE_VEL_X = 20;
export const PS_IC_PARTICLE_STREAMER_Z = 21;
export const PS_IC_PARTICLE_COLOR_G = 22;
export const PS_IC_PARTICLE_SIZE = 23;
export const PS_IC_PARTICLE_TINT_A = 24;
export const PS_IC_LIGHT_TINT_G = 25;
export const PS_RBT_DEPTH_SORT_MODES = new Set([
  'BLENDED_UNDERWATER',
  'UNDERWATER_GRADIENT_MAP',
  'BLENDED_GLOW',
  'GRADIENT_MAP',
  'BLENDED',
]);
