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
    shader.uniforms.matAlbedoEnable = uniforms.matAlbedoEnable;
    shader.uniforms.matAlbedoMap = uniforms.matAlbedoMap;
    shader.uniforms.matAlbedoUv = uniforms.matAlbedoUv;
    shader.uniforms.matAlbedoMode = uniforms.matAlbedoMode;
    shader.uniforms.matAlbedoAo = uniforms.matAlbedoAo;
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
      'uniform float wgPackMG;\n' +
      'uniform float wgPackN;\n' +
      'varying float vWorldY;\n' +
      'varying vec2 vCamoUv;\n' +
      shader.fragmentShader
        .replace(
          '#include <map_fragment>',
          `
#ifdef USE_MAP
  vec4 baseSample = texture2D( map, vMapUv );
  // WG bakes per-pixel surface classification into the BLUE channel of
  // the ship's NORMAL MAP, repacked at toolkit emit-time as the BC4
  // 'camoMaskMap' (99.6% within ±4 of the original BC7 B bytes).
  // Empirical clusters (sample_nb.py, 2026-05-08):
  //
  //   PAINT (default — apply camo):
  //     u8 ~119      universal default
  //     u8 ~115/118/120  BC7 spread around 119
  //     u8 ~99/100/102/107  paint sub-clusters
  //     u8 ~51, ~68  paint sub-clusters (turret roofs)
  //     u8 ~17       AGS010 shield accent
  //   SKIP (preserve base):
  //     u8 ~0        unmapped UV regions
  //     u8 ~34       boot-topping / detail strips
  //     u8 ~85       IJN deck-skip / some accessory regions
  //     u8 ~136-140  AA gun MAGAZINE / breech (Bofors variants)
  //     u8 ~153      US-capital deck-top
  //     u8 ~187-189  GUN MANTLET / dark gun face
  //     u8 ~204      Baltimore-hull-specific
  //
  // DENY-LIST gate. Same gate applies to BOTH the mask+palette path AND
  // the mat_albedo path: WG samples one no-camo classification regardless
  // of paint source.
  //
  // 'camoMaskBound' flips off when no nbmask is bound (legacy exports
  // pre-2026-04-30). In that case we have no classification signal so
  // default to apply-everywhere.
  float nbSkip = 0.0;
  if ( camoMaskBound > 0.5 ) {
    float nb = texture2D( camoMaskMap, vMapUv ).r;
    // Band widths chosen to absorb BC4 quantization (±2/255) plus
    // residual BC7-source spread; centred on observed skip-cluster modes.
    bool inSkipBand =
        ( nb <= 0.020 ) ||                       // u8 ~0    unmapped
        ( nb >= 0.110 && nb <= 0.157 ) ||        // u8 ~34   boot-top / detail
        ( nb >= 0.314 && nb <= 0.353 ) ||        // u8 ~85   IJN deck / accessory
        ( nb >= 0.514 && nb <= 0.569 ) ||        // u8 ~136-140 AA magazine
        ( nb >= 0.580 && nb <= 0.620 ) ||        // u8 ~153  US deck top
        ( nb >= 0.714 && nb <= 0.757 ) ||        // u8 ~187-189 gun mantlet
        ( nb >= 0.781 && nb <= 0.820 );          // u8 ~204  Baltimore-specific
    if ( inSkipBand ) {
      nbSkip = 1.0;
    }
  }
  bool inPaintZone = ( vWorldY >= waterlineY ) && ( nbSkip < 0.5 );
  if ( matAlbedoEnable > 0.5 && inPaintZone ) {
    // mat_* permoflage paint. Two recipes:
    //   matAlbedoMode <  0.5  → Path A: multiplicative atlas overlay
    //                            (tile / mat_camo skins without an
    //                            authored <Part_mgn> block).
    //   matAlbedoMode >= 1.5  → Path B: alpha-weighted RGB replace.
    //                            Mode 2 is the most common (AzurNJ, ARP,
    //                            Sabaton, Aegir AL); modes 1 and 3 use
    //                            the same lerp here — exact channel
    //                            routing is rare and visually close.
    vec2 matUv = vMapUv * matAlbedoUv.xy + matAlbedoUv.zw;
    vec4 matSample = texture2D( matAlbedoMap, matUv );
    if ( matAlbedoMode < 0.5 ) {
      diffuseColor.rgb *= matSample.rgb;
    } else {
      float coverage = matSample.a;
      float aoMod = mix( 1.0, coverage, matAlbedoAo );
      vec3 camoRgb = matSample.rgb;
      diffuseColor.rgb = mix( diffuseColor.rgb * aoMod, camoRgb, coverage );
    }
  } else if ( camoEnable > 0.5 && inPaintZone ) {
    // Mask sampling uses vCamoUv (= vMapUv * camoUV.xy + camoUV.zw) so
    // tile-pattern accessory masks repeat at WG's per-camo authored
    // scale. Hull masks have identity camoUV → vCamoUv == vMapUv.
    vec4 maskSample = texture2D( maskMap, vCamoUv );
    float r = maskSample.r;
    float g = maskSample.g;
    float b = maskSample.b;
    float threshold = 0.12;
    // Recipe: straight mix(base, palette[zone], alpha) per the
    // Armored Patrol "Camouflage: Decoded" 2015 blog. alpha is the
    // saturation knob — alpha=0 leaves base untouched, alpha=1 fully
    // replaces with the palette colour. texture2D samples in linear
    // space (Three.js auto-converts sRGB), and camoColors are linear
    // floats from <colorN> in camouflages.xml — recipe runs entirely
    // in linear space.
    vec4 zoneColor;
    if ( r > g && r > b && r > threshold ) {
      zoneColor = camoColors[1];
    } else if ( g > r && g > b && g > threshold ) {
      zoneColor = camoColors[2];
    } else if ( b > r && b > g && b > threshold ) {
      zoneColor = camoColors[3];
    } else {
      zoneColor = camoColors[0];
    }
    vec3 layered = mix( baseSample.rgb, zoneColor.rgb, zoneColor.a );
    diffuseColor.rgb *= layered;
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
