// Per-clone camo uniform set. Allocated by `attachCamoChunk`; pushed by
// `updateCamoUniforms` after the active skin / texture-toggle changes.

import * as THREE from 'three';
import { dummyMaskTexture, dummyMatAlbedoTexture } from './dummies';

export interface CamoUniforms {
  camoEnable: { value: number };
  camoColors: { value: THREE.Vector4[] };
  maskMap: { value: THREE.Texture };
  /**
   * World-space waterline. Fragments with `vWorldY < waterlineY` skip
   * the camo overlay and render base albedo unchanged. Default 0
   * (toolkit hull GLBs put y=0 at the waterline). -1e9 disables.
   */
  waterlineY: { value: number };
  /**
   * Per-mesh UV transform applied to the mask sample
   * (`vCamoUv = vMapUv * camoUV.xy + camoUV.zw`). Identity `(1,1,0,0)`
   * for hull meshes; non-identity for accessory tile-pattern masks.
   */
  camoUV: { value: THREE.Vector4 };
  /**
   * BC4 single-channel no-camo region mask (`_nbmask.dds`, derived
   * from the WG normal-map B channel). Gates the camo overlay
   * independently of the normal map; defaults to a 1×1 black dummy so
   * unmapped fragments fail-closed when no mask is bound.
   */
  camoMaskMap: { value: THREE.Texture };
  /** 1.0 if a real `_nbmask` is bound, else 0.0 (apply-everywhere). */
  camoMaskBound: { value: number };

  // ── mat_albedo path ────────────────────────────────────────────────
  matAlbedoEnable: { value: number };
  matAlbedoMap: { value: THREE.Texture };
  matAlbedoUv: { value: THREE.Vector4 };
  /**
   * Path A vs Path B discriminator. Mirrors
   * `Skin.mat_textures[<cat>].params.camo_mode` from the sidecar:
   *   -1.0 → Path A (flat multiplicative atlas overlay)
   *    0.0 → params.camo_mode == 0 (explicit no-override; consumer
   *          forces matAlbedoEnable off)
   *    1.0 → zone-1 RGB blend
   *    2.0 → full RGB lerp by `albedo.a` (Path B's most common mode)
   *    3.0 → split blend
   */
  matAlbedoMode: { value: number };
  /** Path B AO darkening: `aoMod = lerp(1, ca.a, aoInfluence)`. */
  matAlbedoAo: { value: number };

  // ── WG channel-pack overrides ──────────────────────────────────────
  /**
   * 1.0 → reinterpret bound MR texel as raw WG `_mg`
   * (G=metalMask, B=gloss); 0.0 → glTF semantics (G=roughness,
   * B=metalness).
   */
  wgPackMG: { value: number };
  /** 1.0 → reconstruct normal Z from N.xy (WG `_n.dds` packs B = mask). */
  wgPackN: { value: number };
}

export function makeCamoUniforms(): CamoUniforms {
  return {
    camoEnable: { value: 0.0 },
    camoColors: {
      value: [
        new THREE.Vector4(0, 0, 0, 1),
        new THREE.Vector4(1, 0, 0, 1),
        new THREE.Vector4(0, 1, 0, 1),
        new THREE.Vector4(0, 0, 1, 1),
      ],
    },
    maskMap: { value: dummyMaskTexture },
    waterlineY: { value: 0.0 },
    camoUV: { value: new THREE.Vector4(1, 1, 0, 0) },
    camoMaskMap: { value: dummyMaskTexture },
    camoMaskBound: { value: 0.0 },
    matAlbedoEnable: { value: 0.0 },
    matAlbedoMap: { value: dummyMatAlbedoTexture },
    matAlbedoUv: { value: new THREE.Vector4(1, 1, 0, 0) },
    matAlbedoMode: { value: -1.0 },
    matAlbedoAo: { value: 0.0 },
    wgPackMG: { value: 0.0 },
    wgPackN: { value: 0.0 },
  };
}

/** Pull every camo uniform set off a material (or array of materials). */
export function uniformsOf(mat: THREE.Material | THREE.Material[]): CamoUniforms[] {
  const list: CamoUniforms[] = [];
  const collect = (m: THREE.Material) => {
    const data = m.userData as { camoUniforms?: CamoUniforms } | undefined;
    if (data?.camoUniforms) list.push(data.camoUniforms);
  };
  if (Array.isArray(mat)) mat.forEach(collect);
  else collect(mat);
  return list;
}
