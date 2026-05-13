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
