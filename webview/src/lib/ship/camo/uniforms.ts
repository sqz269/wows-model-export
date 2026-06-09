// Per-clone camo uniform set. Allocated by `attachCamoChunk`; pushed by
// `updateCamoUniforms` after the active skin / texture-toggle changes.

import * as THREE from 'three';
import {
  dummyMaskTexture,
  dummyMatAlbedoTexture,
  dummyMgnTexture,
  dummyDetailTexture,
} from './dummies';

export interface CamoUniforms {
  camoEnable: { value: number };
  camoColors: { value: THREE.Vector4[] };
  maskMap: { value: THREE.Texture };
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
   * unmapped fragments fail-closed when no mask is bound. This is the
   * Path B 4-threshold deny-formula source (nbPaint = `f(_n.B)`).
   */
  camoMaskMap: { value: THREE.Texture };
  /** 1.0 if a real `_nbmask` is bound, else 0.0 (apply-everywhere). */
  camoMaskBound: { value: number };
  /**
   * BC4 single-channel Path A paint mask (`_camomask.dds`, derived
   * from the WG metallic-gloss-map B channel). Engine-faithful Path A
   * exclusion gate: where `_mg.B == 1` paint applies, where `_mg.B == 0`
   * the base albedo passes through unchanged. See
   * `reference/topics/camo/wg_camo_shader_reference.md` §"Path A".
   * Defaults to the same 1×1 black dummy as `maskMap`. When unbound
   * (no `_camomask.dds` shipped — pre-2026-05-16 toolkit extracts),
   * the consumer falls back to the `nbPaint`-derived gate.
   */
  camoExclusionMap: { value: THREE.Texture };
  /** 1.0 if a real `_camomask` is bound, else 0.0 (fall back to nbPaint). */
  camoExclusionBound: { value: number };

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
  // (reference/topics/camo/camo_path_b_render_re.md §3):
  //   .R = camo gloss override   → blended into roughness via Influence_g
  //   .G = camo metallic override → blended into metalness via Influence_m
  //   .B = tangent X (nx, along-U) | .A = tangent Y (ny, along-V) — each
  //        signed via 2x-1, added to base normal via Influence_n. Axis
  //        assignment resolved 2026-05-17 via gradient-anisotropy probe;
  //        see reference/topics/camo/camo_mgn_texture_channels.md.
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
   */
  catMgnInfluence: { value: THREE.Vector3 };
  /**
   * Per-pixel mg.B gate. When true, paintMask = mg.B × nbPaint instead
   * of just nbPaint — artists get a second binary mask channel
   * independent of the `_n.B` 4-threshold deny list. WG art convention
   * uses values clustered at 0 or 1 (binary). Implemented for WG-pack
   * mode (wgPackMG=1); glTF-mode behavior is conservative (treats the
   * gate as off — see shader.ts comments).
   */
  catUseCamoMaskGlobal: { value: number };

  // ── WG channel-pack overrides ──────────────────────────────────────
  /**
   * 1.0 → bound MR texel is the raw WG `_mg.dds` (R=gloss, G=metallic,
   * B=binary paint mask, A=unused). 0.0 → it's the toolkit-swizzled
   * conformant `_mr.dds` (R=gloss preserved, G=`1-paintMask`,
   * B=metallic). Gloss is always in `.R` in both modes; this flag only
   * routes the metalness + mg.B reads to the right channel.
   */
  wgPackMG: { value: number };
  /** 1.0 → reconstruct normal Z from N.xy (WG `_n.dds` packs B = mask). */
  wgPackN: { value: number };
  /**
   * 1.0 → apply the `PBS_ship.fx` ("legacy") runtime texel remaps on top
   * of the sampled `_mg` values (engine §6b,
   * `reference/engine/wg_render_deferred_gbuffer.md`, build 12506899):
   *   roughness = 1 − pow(gloss, g_legacyGlossRemap = 0.75)
   *   metallic  = min(1, (m·g_legacySpecularMul)^g_legacySpecularPow)
   *             = min(1, (3m)^4)            (γ = g_gammaCorrection = 1)
   * `ship_camo_material.fx` — the family this shader chunk models — does
   * NOT apply them (zero `g_legacy*` constants across all 48 camo DXBC
   * chunks), so the default is 0.0. Wire per-material once the sidecar
   * carries the MFM fx family (producer TODO — mfms are hash-packed, the
   * toolkit must resolve the fx reference).
   */
  wgLegacyRemap: { value: number };

  // ── Detail-normal atlas overlay ────────────────────────────────────
  //
  // WG's ``ship_atlas_detail.dds`` (2048² BC7) is bound by every PBS
  // material whose MFM declares a non-zero ``g_detail*Influence``.
  // Engine recipe (PBS_ship_metallic.win.dx11): sample at
  // ``vMapUv × (scale.x, scale.y)``, decode RG as signed tangent XY,
  // add to the base ``mapN.xy`` weighted by
  // ``influence.x × distanceFade(fadeDistance)`` and re-derive Z.
  // Albedo / gloss variants apply the same texel's other channels with
  // their own influences.
  /** Detail-normal atlas. 1×1 neutral default. */
  detailMap: { value: THREE.Texture };
  /** 1.0 when a real detail atlas is bound + the material opts in. */
  detailMapBound: { value: number };
  /** Per-material UV scale (``g_detailScaleU``, ``g_detailScaleV``). */
  detailScale: { value: THREE.Vector2 };
  /**
   * (normal, albedo, gloss) influence triplet from the MFM —
   * ``g_detailNormalInfluence`` / ``g_detailAlbedoInfluence`` /
   * ``g_detailGlossInfluence``. Each in [0,1]; defaults zero so an
   * always-bound shared map sums to no-op when the material has no
   * detail.
   */
  detailInfluence: { value: THREE.Vector3 };
  /**
   * View-distance fade threshold (``g_detailFadeDistance``, world
   * units). Engine convention seems to be linear falloff from
   * full-influence at the camera to zero at this distance; without
   * the exact DXBC for the fade we approximate with
   * ``saturate(1 - |viewPos| / fadeDistance)`` which is visually
   * close to the engine's behaviour.
   */
  detailFadeDistance: { value: number };
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
    camoUV: { value: new THREE.Vector4(1, 1, 0, 0) },
    camoMaskMap: { value: dummyMaskTexture },
    camoMaskBound: { value: 0.0 },
    camoExclusionMap: { value: dummyMaskTexture },
    camoExclusionBound: { value: 0.0 },
    matAlbedoEnable: { value: 0.0 },
    matAlbedoMap: { value: dummyMatAlbedoTexture },
    matAlbedoUv: { value: new THREE.Vector4(1, 1, 0, 0) },
    matAlbedoMode: { value: -1.0 },
    matAlbedoAo: { value: 0.0 },
    catMgnMap: { value: dummyMgnTexture },
    catMgnBound: { value: 0.0 },
    catMgnInfluence: { value: new THREE.Vector3(0, 0, 0) },
    catUseCamoMaskGlobal: { value: 0.0 },
    wgPackMG: { value: 0.0 },
    wgPackN: { value: 0.0 },
    wgLegacyRemap: { value: 0.0 },
    detailMap: { value: dummyDetailTexture },
    detailMapBound: { value: 0.0 },
    detailScale: { value: new THREE.Vector2(1, 1) },
    detailInfluence: { value: new THREE.Vector3(0, 0, 0) },
    detailFadeDistance: { value: 1.0 },
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
