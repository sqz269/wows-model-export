// Per-clone camo uniform set. Allocated by `attachCamoChunk`; pushed by
// `updateCamoUniforms` after the active skin / texture-toggle changes.

import * as THREE from 'three';
import { dummyMaskTexture, dummyMatAlbedoTexture, dummyMgnTexture } from './dummies';

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

  // ── Path B MGN override (camoMGN texture, t6 equivalent) ───────────
  //
  // The Path B fragment shader (ship_camo_mgn_material.fx) layers a
  // metallic / gloss / normal override on top of the camoAlbedo paint.
  // Channels of the camoMGN texture, per the DXBC RE
  // (reference/investigations/camo_path_b_render_re.md §3):
  //   .R = camo gloss override   → blended into roughness via Influence_g
  //   .G = camo metallic override → blended into metalness via Influence_m
  //   .B / .A = tangent-space normal axis offsets (each signed via 2x-1)
  //             → added to base normal via Influence_n
  // The mask is in camoAlbedo.a, NOT camoMGN.a — camoMGN has no alpha mask.
  /** Camo MGN override texture. 1×1 neutral default. */
  catMgnMap: { value: THREE.Texture };
  /** 1.0 when a real MGN texture is bound. */
  catMgnBound: { value: number };
  /**
   * Influence scalars from `<*_mgn>` params (mgnInfluence):
   *   .x = metallic mix  (paintMask × .x scales camoMGN.G into metalness)
   *   .y = gloss mix     (paintMask × .y blends mg.R toward camoMGN.R)
   *   .z = normal mix    (paintMask × .z scales the tangent-space offset)
   *   .w = unused (dead slot per DXBC — every chunk; pipeline can drop)
   */
  catMgnInfluence: { value: THREE.Vector4 };
  /**
   * Per-pixel mg.B gate. When true, paintMask = mg.B × nbPaint instead
   * of just nbPaint — artists get a second binary mask channel
   * independent of the `_n.B` 4-threshold deny list. WG art convention
   * uses values clustered at 0 or 1 (binary). Implemented for WG-pack
   * mode (wgPackMG=1); glTF-mode behavior is conservative (treats the
   * gate as off — see shader.ts comments).
   */
  catUseCamoMaskGlobal: { value: number };

  // ── Path B emission animation (PHASE 2 — scaffolded, not yet bound) ─
  //
  // chunks 24-47 only. ~13% of corpus carries non-default emission
  // params. Decoded recipe in camo_path_b_render_re.md §5.
  // Stubbed here so the structure exists for a follow-up; current
  // shader.ts does NOT consume these uniforms yet.
  catAnimMap: { value: THREE.Texture };
  catAnimMapBound: { value: number };
  catEmissionAnimMode: { value: number };
  catEmissionColorMode: { value: number };
  catEmissionBasePower: { value: number };
  catEmissionAnimMaxPower: { value: number };
  catMaskSmooth: { value: number };
  catAnimScale: { value: THREE.Vector3 };
  catMaskSpeed: { value: THREE.Vector3 };
  catCamoMaskColor1: { value: THREE.Vector3 };
  catCamoMaskColor2: { value: THREE.Vector4 };
  /** Seconds since start, set per frame in the render loop (phase 2). */
  catTime: { value: number };

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
    catMgnMap: { value: dummyMgnTexture },
    catMgnBound: { value: 0.0 },
    catMgnInfluence: { value: new THREE.Vector4(0, 0, 0, 0) },
    catUseCamoMaskGlobal: { value: 0.0 },
    catAnimMap: { value: dummyMaskTexture },
    catAnimMapBound: { value: 0.0 },
    catEmissionAnimMode: { value: 0 },
    catEmissionColorMode: { value: 0 },
    catEmissionBasePower: { value: 0.0 },
    catEmissionAnimMaxPower: { value: 0.0 },
    catMaskSmooth: { value: 1.0 },
    catAnimScale: { value: new THREE.Vector3(1, 1, 1) },
    catMaskSpeed: { value: new THREE.Vector3(0.1, 0.1, 0.5) },
    catCamoMaskColor1: { value: new THREE.Vector3(1, 0, 0) },
    catCamoMaskColor2: { value: new THREE.Vector4(1, 1, 0, 1) },
    catTime: { value: 0.0 },
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
