// 1×1 dummy textures bound to the camo shader's optional sampler slots so
// `onBeforeCompile` can declare them at compile time. The shader gates on
// `camoEnable` / `camoMaskBound` / `matAlbedoEnable` and skips sampling
// when off; the dummies exist only to satisfy "uniform must be bound".

import * as THREE from 'three';

function makeDummy(rgba: [number, number, number, number]): THREE.DataTexture {
  const t = new THREE.DataTexture(new Uint8Array(rgba), 1, 1, THREE.RGBAFormat);
  t.needsUpdate = true;
  return t;
}

/** Opaque black — bound to `maskMap` + `camoMaskMap` defaults. */
export const dummyMaskTexture = makeDummy([0, 0, 0, 255]);
/** Opaque white — bound to `matAlbedoMap` default. */
export const dummyMatAlbedoTexture = makeDummy([255, 255, 255, 255]);
/**
 * Neutral camo MGN default — R=0 (no gloss), G=0 (no metallic),
 * B=128 (normal axis offset = 0 after 2x-1 remap), A=128 (same).
 * Renders as "flat surface, no MGN modulation". The shader also gates
 * on `catMgnBound > 0.5` so this dummy is only sampled when bound; the
 * neutral defaults are a defense against accidental sampling.
 */
export const dummyMgnTexture = makeDummy([0, 0, 128, 128]);
