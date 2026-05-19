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
    shader.uniforms.detailMap = uniforms.detailMap;
    shader.uniforms.detailMapBound = uniforms.detailMapBound;
    shader.uniforms.detailScale = uniforms.detailScale;
    shader.uniforms.detailInfluence = uniforms.detailInfluence;
    shader.uniforms.detailFadeDistance = uniforms.detailFadeDistance;

    // Vertex: compute per-mesh camo UV. `camoUV` packs (scale.xy,
    // offset.xy); `vCamoUv` is what the fragment shader samples the
    // mask at — identity (1,1,0,0) for hull meshes, per-camo authored
    // values for accessories on a shared mask.
    shader.vertexShader =
      'varying vec2 vCamoUv;\n' +
      'uniform vec4 camoUV;\n' +
      shader.vertexShader.replace(
        '#include <project_vertex>',
        `#include <project_vertex>
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
      'uniform vec3 catMgnInfluence;\n' +
      'uniform float catUseCamoMaskGlobal;\n' +
      'uniform float wgPackMG;\n' +
      'uniform float wgPackN;\n' +
      'uniform sampler2D detailMap;\n' +
      'uniform float detailMapBound;\n' +
      'uniform vec2 detailScale;\n' +
      'uniform vec3 detailInfluence;\n' +
      'uniform float detailFadeDistance;\n' +
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
//                     .B = camo normal X (nx, along-U tangent)
//                     .A = camo normal Y (ny, along-V tangent)
//                   Axis assignment resolved 2026-05-17 via gradient-
//                   anisotropy probe across 5/5 sampled textures.
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

  // ── mg.B paint-mask source picker (engine paint gate) ────────────────
  // metallicGlossMap.B is the WG MG texture's binary 0/255 paint mask,
  // authored per-pixel by artists. Both shader variants reach for it:
  //   • Path A (ship_camo_material.fx)     — always gates by mg.B
  //   • Path B (ship_camo_mgn_material.fx) — gates by mg.B when the
  //                                          per-material useCamoMaskGlobal
  //                                          bool is set (default for hulls
  //                                          + most accessory categories).
  //
  // Source priority (engine-faithful → approximate fallback):
  //   (a) camoExclusionMap — BC4 sibling _camomask.dd? carrying mg.B
  //       byte-for-byte (post-2026-05-16 toolkit). Canonical engine input.
  //   (b) Raw _mg.dd? bound to metalnessMap with wgPackMG=1.0
  //       (loose-mod skin packs). mrTexel.b == mg.B directly.
  //   (c) None — hasMgB == 0. Each consumer picks its own fallback:
  //         Path A → nbPaint   (engine-different but visually similar)
  //         Path B → 1.0       (drop the mg.B factor; paint per nbPaint
  //                             alone — avoids nbPaint² over-exclusion).
  float mgB = 1.0;
  float hasMgB = 0.0;
  #ifdef USE_METALNESSMAP
    vec4 mgTexelB = texture2D( metalnessMap, vMetalnessMapUv );
    mgB    = mix( mgB,    mgTexelB.b, wgPackMG );  // tier (b)
    hasMgB = max( hasMgB, wgPackMG );
  #endif
  if ( camoExclusionBound > 0.5 ) {
    mgB    = texture2D( camoExclusionMap, vMapUv ).r;  // tier (a)
    hasMgB = 1.0;
  }

  // Path B engine paintMask (chunk001:53-54 of ship_camo_mgn_material.fx):
  //   paintMask = useCamoMaskGlobal ? (mg.B * nbPaint) : nbPaint
  // Drives BOTH the mat_albedo diffuse blend below AND the per-channel
  // MGN overrides in roughnessmap / metalnessmap / normal_fragment_maps.
  // When mg.B source is unavailable (hasMgB=0), mgB defaults to 1.0 → the
  // formula degenerates to nbPaint regardless of useCamoMaskGlobal, which
  // drops the engine's mg.B factor rather than approximating with
  // nbPaint² (over-exclusion).
  catPaintMask = mix( nbPaint, nbPaint * mgB, catUseCamoMaskGlobal );

  // Underwater hull is gated at the texel level by the engine paint mask
  // (mg.B == 0 on the anti-fouling region — empirically ~99% of the
  // underwater UV cluster on Montana hull; verified 2026-05-19). Both
  // Path A (`pathAGate = mgB`) and Path B with `useCamoMaskGlobal=1`
  // (hull default, `catPaintMask = nbPaint * mgB`) inherit that for free,
  // so we don't carry a separate world-Y gate. Previously a viewer-side
  // "preserve underwater hull" Y-gate stood in for the missing engine
  // recipe; the new toolkit binds `_camomask.dd?` everywhere, making the
  // gate redundant.

  // ── Path B MGN texture sample ────────────────────────────────────────
  // Sampled at the same UV transform as camoAlbedo (matAlbedoUv) since
  // the engine treats them as a paired texture pair. For hull_palette
  // hybrid (Path B-only, no camoAlbedo), matAlbedoUv defaults to identity
  // (1,1,0,0) → sample at vMapUv directly.
  if ( catMgnBound > 0.5 ) {
    vec2 mgnUv = vMapUv * matAlbedoUv.xy + matAlbedoUv.zw;
    catMgnSample = texture2D( catMgnMap, mgnUv );
  }

  if ( matAlbedoEnable > 0.5 ) {
    // mat_* permoflage paint (Path B) — engine 4-way dispatch on camoMode
    // per ship_camo_mgn_material.fx chunk001:72-79 (DXBC RE), see
    // reference/topics/camo/camo_path_b_render_re.md §4. Mode 1 bypasses
    // useCamoMaskGlobal (raw nbPaint, not catPaintMask); modes 0/2/3 gate
    // by catPaintMask. Mode 2 dominates the corpus; mode 3 is rare/zero.
    vec2 matUv = vMapUv * matAlbedoUv.xy + matAlbedoUv.zw;
    vec4 matSample = texture2D( matAlbedoMap, matUv );
    vec3 natural = diffuseColor.rgb * baseSample.rgb;
    float coverage = matSample.a;

    vec3 painted;
    float blendT;
    if ( matAlbedoMode > 0.5 && matAlbedoMode < 1.5 ) {
      // Mode 1: paint*ca.a — engine bypasses useCamoMaskGlobal here.
      painted = matSample.rgb;
      blendT  = nbPaint * coverage;
    } else if ( matAlbedoMode > 2.5 ) {
      // Mode 3: paintMask*ca.a (rare).
      painted = matSample.rgb;
      blendT  = catPaintMask * coverage;
    } else if ( matAlbedoMode > 1.5 ) {
      // Mode 2: full body color, gated by paintMask. Most common.
      painted = matSample.rgb;
      blendT  = catPaintMask;
    } else {
      // Mode -1 / 0: aoMod-modulated base, gated by paintMask.
      float aoMod = mix( 1.0, coverage, matAlbedoAo );
      painted = mix( natural * aoMod, matSample.rgb, coverage );
      blendT  = catPaintMask;
    }
    diffuseColor.rgb = mix( natural, painted, blendT );
    diffuseColor.a   = baseSample.a;
  } else if ( camoEnable > 0.5 ) {
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
    //   3. Final paint/no-paint gate by mg.B — see picker above.
    //      Falls back to nbPaint when no mg.B source is bound.
    //
    // mask.a is NEVER sampled (engine writes r0.xyz only at line 18).
    // texture2D samples in linear space (Three.js auto-converts sRGB)
    // and camoColors are linear floats from <colorN> in camouflages.xml.
    vec4 mask = texture2D( maskMap, vCamoUv );
    // WG _mg.G is the metallic mask. The engine pre-mixes against
    // baseLambert = baseAlbedo * (1 - mg.G) per chunk001:24 of
    // ship_camo_material.fx, so the camo body color isn't applied on
    // metallic regions (those move to the F0 specular path instead).
    // For non-metallic surfaces (mg.G == 0) the factor is 1 and this
    // is a no-op. Source picker parallels the mg.B picker above:
    //   wgPackMG=1: metalnessMap.g (raw _mg.G)
    //   wgPackMG=0: metalnessMap.b (conformant _mr.B ≈ mg.G mask)
    // When no metalnessMap is bound, mgG stays 0 → identity factor 1.
    float mgG = 0.0;
    #ifdef USE_METALNESSMAP
      vec4 mrTexelG = texture2D( metalnessMap, vMetalnessMapUv );
      mgG = mix( mrTexelG.b, mrTexelG.g, wgPackMG );
    #endif
    vec3 baseRgb = diffuseColor.rgb * baseSample.rgb * ( 1.0 - mgG );
    vec3 P0 = mix( baseRgb, camoColors[0].rgb, camoColors[0].a );
    vec3 P1 = mix( baseRgb, camoColors[1].rgb, camoColors[1].a );
    vec3 P2 = mix( baseRgb, camoColors[2].rgb, camoColors[2].a );
    vec3 P3 = mix( baseRgb, camoColors[3].rgb, camoColors[3].a );
    vec3 step1 = mix( P0,    P1, mask.r );
    vec3 step2 = mix( step1, P2, mask.g );
    vec3 step3 = mix( step2, P3, mask.b );
    float pathAGate = mix( nbPaint, mgB, hasMgB );
    diffuseColor.rgb = mix( baseRgb, step3, pathAGate );
    diffuseColor.a    = baseSample.a;
  } else {
    diffuseColor *= baseSample;
  }
#endif
`,
        )
        // WG-pack metallicRoughness override. WG `_mg.dds` ACTUAL layout
        // (verified 2026-05-17 from ship_camo_material chunk001 DXBC at
        // lines 23-45 + empirical channel histograms on the Montana hull
        // _mg.dds):
        //   R = gloss          (continuous; chunk001:43-45 computes
        //                       roughness = 1 - R for the BRDF path)
        //   G = metallic       (chunk001:24-26 feeds the F0/Lambert split)
        //   B = paint mask     (binary 0/255; chunk001:42 is the camo
        //                       paint gate. Also reused by `*_emissive.mfm`
        //                       and Path B's `useCamoMaskGlobal`.)
        //   A = unused         (BC1 no-alpha)
        //
        // The toolkit's "MG→MR" swizzler (fixed 2026-05-17 in
        // `crates/wowsunpack/src/export/texture.rs`) emits the conformant
        // `_mr.dds` as (R=gloss preserved, G=`1-gloss`=roughness,
        // B=metallic, A=255). Pre-fix builds emitted G=`1-paintMask` by
        // mistake (the original pbr_textures.md §"Phase B" inspection
        // had R/B swapped). **Re-extract libraries with a post-fix
        // toolkit to pick up correct rendering** — this shader trusts
        // the swizzler is the source of truth, not a stack of
        // shader-side workarounds.
        //
        // glTF mode (`_mr.dds`, wgPackMG=0): roughness comes from `.g`
        // per glTF MR convention.
        // WG-pack mode (raw `_mg.dds`, wgPackMG=1, loose-mod skin packs):
        // gloss is in `.R`, so roughness = 1 - `.r`.
        .replace(
          '#include <roughnessmap_fragment>',
          `float roughnessFactor = roughness;
#ifdef USE_ROUGHNESSMAP
  vec4 texelRoughness = texture2D( roughnessMap, vRoughnessMapUv );
  // glTF mode (_mr.dds): .g = roughness directly (toolkit's swizzler
  // emits 255 - mg.R into G).
  // WG-pack mode (raw _mg.dds): .r = gloss → roughness = 1 - .r.
  float roughTexel = mix( texelRoughness.g, 1.0 - texelRoughness.r, wgPackMG );
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
  // WG _mg.G is the metallic source. Toolkit's swizzler moves it to
  // _mr.B (mr.B = mg.G). Pick the right channel per pack mode.
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
          `#ifdef USE_NORMALMAP_OBJECTSPACE
  normal = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
  #ifdef FLIP_SIDED
    normal = - normal;
  #endif
  #ifdef DOUBLE_SIDED
    normal = normal * faceDirection;
  #endif
  normal = normalize( normalMatrix * normal );
#elif defined( USE_NORMALMAP_TANGENTSPACE )
  vec3 mapN = texture2D( normalMap, vNormalMapUv ).xyz * 2.0 - 1.0;
  // WG _n.dds: B = no-camo mask (not Z). Reconstruct Z when packed.
  float nzRecon = sqrt( max( 0.0, 1.0 - mapN.x * mapN.x - mapN.y * mapN.y ) );
  mapN.z = mix( mapN.z, nzRecon, wgPackN );
  // ── Detail-normal overlay (shared atlas) ──────────────────────────────
  // Sample WG's 'ship_atlas_detail.dds' at the per-material UV scale.
  // RG decoded as signed tangent XY (×2-1). The engine adds it to the
  // base tangent normal, weighted by 'detailInfluence.x' × a
  // view-distance fade. Without the exact engine DXBC for the fade we
  // approximate with 'saturate(1 - |viewPos|/detailFadeDistance)' —
  // close-up gives full detail, far approaches zero. Gated on
  // 'detailMapBound' so materials whose MFM didn't opt in pay no
  // sample cost.
  if ( detailMapBound > 0.5 ) {
    vec2 detailUv = vMapUv * detailScale;
    vec2 detailTangent = texture2D( detailMap, detailUv ).xy * 2.0 - 1.0;
    float detailDist = length( vViewPosition );
    float detailFade = clamp( 1.0 - detailDist / detailFadeDistance, 0.0, 1.0 );
    float detailNormalWeight = detailInfluence.x * detailFade;
    mapN.xy += detailTangent * detailNormalWeight;
    mapN.z = sqrt( max( 0.0, 1.0 - dot( mapN.xy, mapN.xy ) ) );
  }
  // Path B normal perturbation: camoMGN packs two signed tangent-space
  // axis offsets in .B and .A (each via 2x-1). Engine recipe
  // (chunk001:96 / 103-105) adds the camo perturbation to the vertex
  // normal weighted by 'paintMask * Influence_n'. We approximate the
  // engine's cross-product reconstruction with a tangent-space lerp:
  // blend the base tangent normal toward the camo's, then re-derive Z.
  // X/Y AXIS — RESOLVED 2026-05-17: .B = tangent X (nx, along-U),
  // .A = tangent Y (ny, along-V). Decided via gradient-anisotropy probe
  // across 5/5 WG '_mgn.dds' textures (synthetic-control-validated): A
  // varies dominantly in V direction, B dominantly in U. See
  // reference/topics/camo/camo_mgn_texture_channels.md §"Axis resolution".
  float catNormalMix = catPaintMask * catMgnInfluence.z * catMgnBound;
  vec2 camoNormalXY = catMgnSample.ba * 2.0 - 1.0;  // .B → X (nx), .A → Y (ny)
  mapN.xy = mix( mapN.xy, camoNormalXY, catNormalMix );
  mapN.z = mix( mapN.z, sqrt( max( 0.0, 1.0 - dot( mapN.xy, mapN.xy ) ) ), catNormalMix );
  mapN.xy *= normalScale;
  // r165: 'tbn' is defined in <normal_fragment_begin> as
  //   mat3(vTangent, vBitangent, normal)   when USE_TANGENT is set
  //   getTangentFrame(-vViewPosition, normal, vNormalMapUv)   otherwise
  // (the legacy perturbNormal2Arb / vTBN identifiers were removed —
  // see node_modules/three/src/renderers/shaders/ShaderChunk/normal_fragment_begin.glsl.js)
  normal = normalize( tbn * mapN );
#elif defined( USE_BUMPMAP )
  normal = perturbNormalArb( - vViewPosition, normal, dHdxy_fwd(), faceDirection );
#endif
`,
        );
  };
  return uniforms;
}
