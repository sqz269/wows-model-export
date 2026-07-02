// Decoded per-component action state consumed by the SystemRenderer sim.
// Extracted verbatim from the former monolithic particles.ts.

import type * as THREE from 'three';
import type { ParticleRamp, ParticleValueGenerator, ParticleVariantVg } from '$lib/types/sidecar';
import type { VelocityFieldData } from './velocity-field';

export interface StreamAction {
  vector: THREE.Vector3;
  halfLife: number;
  delay: number;
  switchCoordinateStyle: boolean;
}

export interface JitterAction {
  positionGenerator: ParticleVariantVg | undefined;
  velocityGenerator: ParticleVariantVg | undefined;
  delay: number;
  affectPosition: boolean;
  affectVelocity: boolean;
}

export interface OrbitorAction {
  angularVelocityGenerator: ParticleValueGenerator | undefined;
  point: THREE.Vector3;
  axis: THREE.Vector3;
  delay: number;
  affectPosition: boolean;
  affectVelocity: boolean;
}

export interface MagnetAction {
  attractorPoint: THREE.Vector3;
  delay: number;
  minimalDistance: number;
  strength: number;
}

type BarrierShape = 'sphere' | 'cylinder' | 'box' | 'plane';

export const BARRIER_REACTION_SCALE = 0;
export const BARRIER_REACTION_BOUNCE = 1;
export const BARRIER_REACTION_REMOVE = 2;
export const BARRIER_REACTION_SPAWN = 3;
export const BARRIER_REACTION_WRAP = 4;
export const BARRIER_REACTION_ALPHA = 5;
export const BARRIER_REACTION_DAMP = 6;
export const BARRIER_REACTION_FORCE = 7;

export interface BarrierAction {
  shape: BarrierShape;
  reaction: number;
  strength: number;
  stopAge: number;
  delay: number;
  position: THREE.Vector3;
  radius: number;
  corner: THREE.Vector3;
  opposite: THREE.Vector3;
  planeNormal: THREE.Vector3;
  planeConstant: number;
  useWorldSpace: boolean;
  effectName: string;
}

export interface SpawnerAction {
  spawnRamp?: ParticleRamp;
  effectName: string;
  accum: number;
}

export interface VelocityFieldAction {
  topLeftFront: THREE.Vector3;
  bottomRightBack: THREE.Vector3;
  stopAge: number;
  delay: number;
  velocityScale: number;
  influence: number;
  fieldSourceName: string;
  field: VelocityFieldData | null;
}
