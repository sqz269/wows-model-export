// Camo shader chunks: monkey-patch a MeshStandardMaterial's GLSL via
// `onBeforeCompile` to add the WG camo overlay (mask + palette OR
// mat_albedo atlas), the per-pixel no-camo gate, and WG channel-pack
// reinterpretation for raw `_mg.dd?` / `_n.dd?` slots.
//
// The GLSL is preserved verbatim from the legacy webview — the rendering
// recipe is documented inline. Don't reflow comments inside the chunk
// strings; line-by-line provenance matters when re-RE'ing a regression.

import type * as THREE from 'three';
import { makeCamoUniforms, type CamoUniforms } from './uniforms';

/**
 * Attach the camo overlay chunk to a freshly-cloned material. Returns
 * the per-clone uniform set the caller (typically `updateCamoUniforms`)
 * will push values into.
 */
export function attachCamoChunk(mat: THREE.MeshStandardMaterial): CamoUniforms {
  const uniforms = makeCamoUniforms();
  mat.onBeforeCompile = (shader) => {
    // Wire the per-clone uniform set into the shader. Three.js's
    // `onBeforeCompile` runs once per program compile; uniforms must be
    // declared at this point. After compile, callers update `value`s.
    shader.uniforms.camoEnable = uniforms.camoEnable;
    shader.uniforms.camoColors = uniforms.camoColors;
    shader.uniforms.maskMap = uniforms.maskMap;
    shader.uniforms.waterlineY = uniforms.waterlineY;
    shader.uniforms.camoUV = uniforms.camoUV;
    shader.uniforms.camoMaskMap = uniforms.camoMaskMap;
    shader.uniforms.camoMaskBound = uniforms.camoMaskBound;
    shader.uniforms.camoExclusionMap = uniforms.camoExclusionMap;
    shader.uniforms.camoExclusionBound = uniforms.camoExclusionBound;
    shader.uniforms.matAlbedoEnable = uniforms.matAlbedoEnable;
    shader.uniforms.matAlbedoMap = uniforms.matAlbedoMap;
    shader.uniforms.matAlbedoUv = uniforms.matAlbedoUv;
    shader.uniforms.matAlbedoMode = uniforms.matAlbedoMode;
    shader.uniforms.matAlbedoAo = uniforms.matAlbedoAo;
    shader.uniforms.catMgnMap = uniforms.catMgnMap;
    shader.uniforms.catMgnBound = uniforms.catMgnBound;
    shader.uniforms.catMgnInfluence = uniforms.catMgnInfluence;
    shader.uniforms.catUseCamoMaskGlobal = uniforms.catUseCamoMaskGlobal;
    shader.uniforms.wgPackMG = uniforms.wgPackMG;
    shader.uniforms.wgPackN = uniforms.wgPackN;

    // Vertex: compute world-space Y + per-mesh camo UV. Toolkit hull GLBs
    // are emitted in metric world space with y=0 at the waterline.
    // `camoUV` packs (scale.xy, offset.xy); `vCamoUv` is what the
    // fragment shader samples the mask at — identity (1,1,0,0) for hull
    // meshes, per-camo authored values for accessories on a shared mask.
    shader.vertexShader =
      'varying float vWorldY;\n' +
      'varying vec2 vCamoUv;\n' +
      'uniform vec4 camoUV;\n' +
      shader.vertexShader.replace(
        '#include <project_vertex>',
        `#include <project_vertex>
  vWorldY = ( modelMatrix * vec4( transformed, 1.0 ) ).y;
#ifdef USE_MAP
  vCamoUv = vMapUv * camoUV.xy + camoUV.zw;
#else
  vCamoUv = vec2( 0.0 );
#endif`,
      );

    shader.fragmentShader =
      'uniform float camoEnable;\n' +
      'uniform vec4 camoColors[4];\n' +
      'uniform sampler2D maskMap;\n' +
      'uniform float waterlineY;\n' +
      'uniform float matAlbedoEnable;\n' +
      'uniform sampler2D matAlbedoMap;\n' +
      'uniform vec4 matAlbedoUv;\n' +
      'uniform float matAlbedoMode;\n' +
      'uniform float matAlbedoAo;\n' +
      'uniform sampler2D camoMaskMap;\n' +
      'uniform float camoMaskBound;\n' +
      'uniform sampler2D camoExclusionMap;\n' +
      'uniform float camoExclusionBound;\n' +
      'uniform sampler2D catMgnMap;\n' +
      'uniform float catMgnBound;\n' +
      'uniform vec4 catMgnInfluence;\n' +
      'uniform float catUseCamoMaskGlobal;\n' +
      'uniform float wgPackMG;\n' +
      'uniform float wgPackN;\n' +
      'varying float vWorldY;\n' +
      'varying vec2 vCamoUv;\n' +
      shader.fragmentShader
        .replace(
          '#include <map_fragment>',
          `
// ── Path B MGN function-scope vars ─────────────────────────────────────
// Declared OUTSIDE the USE_MAP guard so the downstream roughnessmap /
// metalnessmap / normal_fragment_maps chunks can read them (they have
// their own USE_* guards, not USE_MAP). Defaults are neutral: when
// 'catMgnBound' is 0 the downstream chunks lerp by 0 and pass through
// unchanged.
//   catPaintMask  — per-pixel scalar in [0,1] driving the MGN mix
//                   (= nbPaint from the _n.B deny formula, optionally
//                   gated by mg.B via 'catUseCamoMaskGlobal')
//   catMgnSample  — RGBA texel from camoMGN at the camo UV. Channels per
//                   reference/topics/camo/camo_mgn_texture_channels.md:
//                     .R = camo gloss override  → roughnessmap chunk
//                     .G = camo metallic override → metalnessmap chunk
//                     .B = normal axis (tangent Y per texture-side decode)
//                     .A = normal axis (tangent X per texture-side decode)
float catPaintMask = 1.0;
vec4 catMgnSample = vec4( 0.0, 0.0, 0.5, 0.5 );

#ifdef USE_MAP
  vec4 baseSample = texture2D( map, vMapUv );

  // ── nbmask paint factor (Path B only) ────────────────────────────────
  // The ship's _n.B channel is repacked at toolkit emit time as the BC4
  // 'camoMaskMap'. Path B ('ship_camo_mgn_material.fx') samples it as a
  // per-pixel paint multiplier via 4 quadratic soft bands around the
  // reserved deny values u8 {136, 187, 221, 238} — verified from DXBC
  // chunk001 lines 613/617/623/627/631/635. See
  // 'reference/topics/camo/normal_b_deny_list_re.md'.
  //
  //     float4 t = (-0.533330, -0.733330, -0.866660, -0.933330);
  //     float4 d = abs(_n.b - t); d = min(1, d*d*1000);
  //     float paint = d.x * d.y * d.z * d.w;     // 0 = SKIP, 1 = PAINT
  //
  // Path A ('ship_camo_material.fx') does NOT sample _n.B — its gate is
  // metallicGlossMap.B (mg.B = the WG MG texture's binary paint mask).
  // The Path A branch below uses nbPaint as a FALLBACK gate when the
  // conformant _mr.dds is bound (mg.B is dropped during toolkit swizzle);
  // when raw _mg.dd? is bound (wgPackMG=1.0) it reads mg.B directly.
  // 'camoMaskBound' defaults the factor to 1 (apply-everywhere) when no
  // nbmask is bound (legacy exports).
  float nbPaint = 1.0;
  if ( camoMaskBound > 0.5 ) {
    float nb = texture2D( camoMaskMap, vMapUv ).r;
    vec4 d = abs( vec4( nb ) - vec4( 0.5333, 0.7333, 0.8666, 0.9333 ) );
    vec4 dsq = min( vec4( 1.0 ), d * d * 1000.0 );
    nbPaint = dsq.x * dsq.y * dsq.z * dsq.w;
  }
  catPaintMask = nbPaint;

  // Underwater gate — separate aesthetic (preserves the wet/dirty base
  // below the waterline). Applies to both Path A and Path B.
  bool aboveWaterline = ( vWorldY >= waterlineY );

  // ── Path B MGN sample + useCamoMaskGlobal gate ───────────────────────
  // Engine recipe per camo_path_b_render_re.md §1 stages 2 + 4:
  //   paintMask = useCamoMaskGlobal ? mg.B * nbPaint : nbPaint
  //   cm = sample( camoMGN, camoUv )
  // Sampled at the same UV transform as camoAlbedo (matAlbedoUv) since
  // the engine treats them as a paired texture pair. For hull_palette
  // hybrid (Path B-only, no camoAlbedo), matAlbedoUv defaults to
  // identity (1,1,0,0) → sample at vMapUv directly.
  if ( catMgnBound > 0.5 && aboveWaterline ) {
    if ( catUseCamoMaskGlobal > 0.5 ) {
      #ifdef USE_METALNESSMAP
        // WG _mg.B = gloss = the mask (per project_wg_emissive_mg_b_channel.md
        // — the same channel WG reuses for the emissive mask gate).
        // glTF-converted _mr.G = roughness = 1 - gloss. Pick the right
        // channel using the existing wgPackMG flag.
        vec4 mrTexel = texture2D( metalnessMap, vMetalnessMapUv );
        float glossMask = mix( 1.0 - mrTexel.g, mrTexel.b, wgPackMG );
        catPaintMask *= glossMask;
      #endif
    }
    vec2 mgnUv = vMapUv * matAlbedoUv.xy + matAlbedoUv.zw;
    catMgnSample = texture2D( catMgnMap, mgnUv );
  }

  if ( matAlbedoEnable > 0.5 && aboveWaterline ) {
    // mat_* permoflage paint (Path B). Two recipes:
    //   matAlbedoMode <  0.5  → Path A-style multiplicative atlas overlay
    //                            (tile / mat_camo without <Part_mgn>).
    //   matAlbedoMode >= 1.5  → Path B alpha-weighted RGB replace.
    //                            Mode 2 most common (AzurNJ, ARP, Sabaton,
    //                            Aegir AL); modes 1/3 use the same lerp.
    // Blend the per-pixel camo contribution with the natural diffuse by
    // 'nbPaint' — engine-faithful soft falloff around the 4 deny bands.
    vec2 matUv = vMapUv * matAlbedoUv.xy + matAlbedoUv.zw;
    vec4 matSample = texture2D( matAlbedoMap, matUv );
    vec3 natural = diffuseColor.rgb * baseSample.rgb;
    vec3 painted;
    if ( matAlbedoMode < 0.5 ) {
      painted = diffuseColor.rgb * matSample.rgb;
    } else {
      float coverage = matSample.a;
      float aoMod = mix( 1.0, coverage, matAlbedoAo );
      painted = mix( diffuseColor.rgb * aoMod, matSample.rgb, coverage );
    }
    diffuseColor.rgb = mix( natural, painted, nbPaint );
    diffuseColor.a   = baseSample.a;
  } else if ( camoEnable > 0.5 && aboveWaterline ) {
    // Path A — sequential 4-row palette lerp weighted by mask.RGB,
    // gated by mg.B (the WG metallic-gloss texture's B = paint mask).
    // Engine recipe per chunk001:18-42 of ship_camo_material.fx
    // (DXBC RE 2026-05-16). See:
    //   reference/topics/camo/wg_camo_shader_reference.md §"Path A"
    //   reference/topics/camo/CAMO_SOURCE_OF_TRUTH.md §3.3
    //   memory/project_camo_path_a_zoned_mask_refuted.md
    //
    // The previous "dominant-channel zone-pick with threshold 0.12 +
    // blend by mask.a*color.a" recipe was fabricated — the engine has
    // no such instruction. The real algorithm:
    //
    //   1. Pre-mix each of the 4 palette rows against the base by the
    //      row's own .a (= color.a): alpha=0 → row passes base through,
    //      alpha=1 → row fully replaces base with palette[i].rgb.
    //   2. Sequential lerp through all 4 rows weighted by mask.R, then
    //      mask.G, then mask.B — a CONTINUOUS 3-channel weight, not a
    //      thresholded zone classifier.
    //   3. Final paint/no-paint gate by mg.B (binary 0/255 mask channel
    //      of metallicGlossMap in WG MG layout).
    //
    // mg.B source priority (engine-faithful → approximate fallback):
    //   (a) camoExclusionMap (post-2026-05-16 toolkit): a BC4 single-
    //       channel sibling _camomask.dd? carrying the original mg.B
    //       byte-for-byte. This is the canonical engine input.
    //   (b) Raw _mg.dd? bound to metalnessMap with wgPackMG=1 (loose-
    //       mod skin packs): the slot is the raw WG mg layout so
    //       mrTexel.b == mg.B directly.
    //   (c) nbPaint from _n.B 4-threshold deny formula: same gate as
    //       Path B. Engine-different but visually similar — used as a
    //       last-resort fallback when neither (a) nor (b) is available
    //       (pre-2026-05-16 toolkit extracts with conformant _mr.dds).
    //
    // mask.a is NEVER sampled (engine writes r0.xyz only at line 18).
    // texture2D samples in linear space (Three.js auto-converts sRGB)
    // and camoColors are linear floats from <colorN> in camouflages.xml.
    vec4 mask = texture2D( maskMap, vCamoUv );
    vec3 baseRgb = diffuseColor.rgb * baseSample.rgb;
    vec3 P0 = mix( baseRgb, camoColors[0].rgb, camoColors[0].a );
    vec3 P1 = mix( baseRgb, camoColors[1].rgb, camoColors[1].a );
    vec3 P2 = mix( baseRgb, camoColors[2].rgb, camoColors[2].a );
    vec3 P3 = mix( baseRgb, camoColors[3].rgb, camoColors[3].a );
    vec3 step1 = mix( P0,    P1, mask.r );
    vec3 step2 = mix( step1, P2, mask.g );
    vec3 step3 = mix( step2, P3, mask.b );
    // Pick the best mg.B source available. Higher priority overrides
    // lower; nbPaint is the universal fallback.
    float pathAGate = nbPaint;
    #ifdef USE_METALNESSMAP
      // Tier (b): raw WG _mg.dd? bound (loose-mod mode).
      vec4 mgTexel = texture2D( metalnessMap, vMetalnessMapUv );
      pathAGate = mix( pathAGate, mgTexel.b, wgPackMG );
    #endif
    // Tier (a): dedicated _camomask.dd? sibling (preferred). Bound
    // when the post-2026-05-16 toolkit extracted the asset; otherwise
    // camoExclusionBound = 0 and the lower-tier source is kept.
    if ( camoExclusionBound > 0.5 ) {
      pathAGate = texture2D( camoExclusionMap, vMapUv ).r;
    }
    diffuseColor.rgb = mix( baseRgb, step3, pathAGate );
    diffuseColor.a    = baseSample.a;
  } else {
    diffuseColor *= baseSample;
  }
#endif
`,
        )
        // WG-pack metallicRoughness override. Three.js's stock chunks
        // sample G→roughness, B→metalness per glTF spec. WG packs the
        // same texture as G=metalMask, B=gloss — semantics invert, so
        // a painted dielectric reads as a shiny conductor under IBL.
        // Loose-mod skin packs ship the raw `_mg.dd*` form; set
        // `wgPackMG=1.0` on the per-clone uniform to reinterpret.
        .replace(
          '#include <roughnessmap_fragment>',
          `float roughnessFactor = roughness;
#ifdef USE_ROUGHNESSMAP
  vec4 texelRoughness = texture2D( roughnessMap, vRoughnessMapUv );
  // glTF: roughness ← G; WG-pack: roughness ← (1 - B)  (gloss → 1−x)
  float roughTexel = mix( texelRoughness.g, 1.0 - texelRoughness.b, wgPackMG );
  // Path B gloss override: blend roughness toward (1 - camoMGN.R) by
  //   factor = paintMask * Influence_g * catMgnBound
  // Engine recipe (chunk001:97-99): r0.y = lerp(base_gloss, cm.r, mask*infl.y)
  // then roughness = 1 - gloss. Lerp by 0 leaves roughTexel untouched
  // (factor = 0 when catMgnBound = 0 or Influence_g = 0).
  float catGlossMix = catPaintMask * catMgnInfluence.y * catMgnBound;
  roughTexel = mix( roughTexel, 1.0 - catMgnSample.r, catGlossMix );
  roughnessFactor *= roughTexel;
#endif
`,
        )
        .replace(
          '#include <metalnessmap_fragment>',
          `float metalnessFactor = metalness;
#ifdef USE_METALNESSMAP
  vec4 texelMetalness = texture2D( metalnessMap, vMetalnessMapUv );
  // glTF: metalness ← B; WG-pack: metalness ← G (binary mask)
  float metalTexel = mix( texelMetalness.b, texelMetalness.g, wgPackMG );
  // Path B metallic override: blend metalness toward camoMGN.G by
  //   factor = paintMask * Influence_m * catMgnBound
  // Engine recipe (chunk001:80): metal_mix = cm.g * mgnInfluence.x.
  // (The engine then feeds metal_mix into the F0/Lambert split; we
  // approximate by lerping the metalness factor directly — Three.js's
  // metalness uniform feeds the same split downstream.)
  float catMetalMix = catPaintMask * catMgnInfluence.x * catMgnBound;
  metalTexel = mix( metalTexel, catMgnSample.g, catMetalMix );
  metalnessFactor *= metalTexel;
#endif
`,
        )
        // WG-pack normal override. Stock three.js samples (R,G,B) and
        // remaps to (-1..1) — when B is the WG no-camo mask (already
        // consumed by `camoMaskMap`) instead of a reconstructed Z,
        // surface normals point inward and shading tilts wrong.
        // Reconstruct Z from the unit-vector identity when wgPackN=1.0.
        .replace(
          '#include <normal_fragment_maps>',
          `#ifdef OBJECTSPACE_NORMALMAP
  normal = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
  #ifdef FLIP_SIDED
    normal = - normal;
  #endif
  #ifdef DOUBLE_SIDED
    normal = normal * faceDirection;
  #endif
  normal = normalize( normalMatrix * normal );
#elif defined( TANGENTSPACE_NORMALMAP )
  vec3 mapN = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
  // WG _n.dds: B = no-camo mask (not Z). Reconstruct Z when packed.
  float nzRecon = sqrt( max( 0.0, 1.0 - mapN.x * mapN.x - mapN.y * mapN.y ) );
  mapN.z = mix( mapN.z, nzRecon, wgPackN );
  // Path B normal perturbation: camoMGN packs two signed tangent-space
  // axis offsets in .B and .A (each via 2x-1). Engine recipe
  // (chunk001:96 / 103-105) adds the camo perturbation to the vertex
  // normal weighted by 'paintMask * Influence_n'. We approximate the
  // engine's cross-product reconstruction with a tangent-space lerp:
  // blend the base tangent normal toward the camo's, then re-derive Z.
  // X/Y AXIS CAVEAT: per camo_path_b_render_re.md §8 and
  // camo_mgn_texture_channels.md, .A → tangent X and .B → tangent Y
  // is the texture-side hypothesis. If normal-map detail appears
  // rotated 90° in-game vs reference, swap '.a' and '.b' below.
  float catNormalMix = catPaintMask * catMgnInfluence.z * catMgnBound;
  vec2 camoAxisAB = catMgnSample.ab * 2.0 - 1.0;  // .A → X, .B → Y
  mapN.xy = mix( mapN.xy, camoAxisAB, catNormalMix );
  mapN.z = mix( mapN.z, sqrt( max( 0.0, 1.0 - dot( mapN.xy, mapN.xy ) ) ), catNormalMix );
  mapN.xy *= normalScale;
  #ifdef USE_TANGENT
    normal = normalize( vTBN * mapN );
  #else
    normal = perturbNormal2Arb( - vViewPosition, normal, mapN, faceDirection );
  #endif
#elif defined( USE_BUMPMAP )
  normal = perturbNormalArb( - vViewPosition, normal, dHdxy_fwd(), faceDirection );
#endif
`,
        );
  };
  return uniforms;
}
