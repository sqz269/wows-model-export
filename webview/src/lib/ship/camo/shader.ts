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

  // ── nbmask paint factor (Path B only) ────────────────────────────────
  // The ship's _n.B channel is repacked at toolkit emit time as the BC4
  // 'camoMaskMap'. Path B ('ship_camo_mgn_material.fx') samples it as a
  // per-pixel paint multiplier via 4 quadratic soft bands around the
  // reserved deny values u8 {136, 187, 221, 238} — verified from DXBC
  // chunk001 lines 613/617/623/627/631/635. See
  // 'reference/investigations/normal_b_deny_list_re.md'.
  //
  //     float4 t = (-0.533330, -0.733330, -0.866660, -0.933330);
  //     float4 d = abs(_n.b - t); d = min(1, d*d*1000);
  //     float paint = d.x * d.y * d.z * d.w;     // 0 = SKIP, 1 = PAINT
  //
  // Path A ('ship_camo_material.fx') does NOT sample _n.B — its gate is
  // the camoColorMask R/G/B/Black zone alpha (zoneColor.a). So nbPaint
  // multiplies the mat_albedo branch only; the palette branch keeps its
  // own zone-alpha gate. 'camoMaskBound' defaults the factor to 1
  // (apply-everywhere) when no nbmask is bound (legacy exports).
  float nbPaint = 1.0;
  if ( camoMaskBound > 0.5 ) {
    float nb = texture2D( camoMaskMap, vMapUv ).r;
    vec4 d = abs( vec4( nb ) - vec4( 0.5333, 0.7333, 0.8666, 0.9333 ) );
    vec4 dsq = min( vec4( 1.0 ), d * d * 1000.0 );
    nbPaint = dsq.x * dsq.y * dsq.z * dsq.w;
  }

  // Underwater gate — separate aesthetic (preserves the wet/dirty base
  // below the waterline). Applies to both Path A and Path B.
  bool aboveWaterline = ( vWorldY >= waterlineY );

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
    // Path A — palette + zoned mask. No nbmask gate; zoneColor.a (zero
    // in the authored "no-paint" zone) is the engine's deny mechanism.
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
