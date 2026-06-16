// Clone a MeshStandardMaterial and bind a resolved texture set to its
// PBR slots, then layer on the camo shader chunk. The clone is the SAME
// for every skin (base albedo + PBR maps come from `main`); the camo
// overlay is layered on top via the chunk's `maskMap` + palette uniforms
// (swapped per skin) — so switching skins is a uniform update.

import * as THREE from 'three';
import { attachCamoChunk } from '../camo/shader';
import type { TextureSetResolved } from './types';

export interface MaterialClonePolicy {
  /** Current AO toggle. Drives `aoMapIntensity` on the new clone. */
  aoEnabled: boolean;
  /**
   * Current MR-maps toggle. Off by default — when the bound texture is
   * raw WG `_mg.dd0` (R=gloss, G=metallic, B=paint mask) Three.js's
   * stock glTF MR chunks read the wrong channels and the painted
   * dielectric reads as a shiny conductor. shader.ts overrides the
   * roughness/metalness chunks to read the engine-faithful channels.
   * Toggling on/off re-binds and may trigger a one-time recompile.
   */
  mgMapEnabled: boolean;
  /**
   * Three.js `MeshStandardMaterial.normalScale` factor. Default 2.0 —
   * WG hull normal maps are authored very subtly (mean tilt 2-3°, p95
   * ~16°; intentional art style for smooth steel plating). Engine-
   * faithful is 1.0; bumping to 2-3x makes the surface detail readable
   * under diffuse-dominated lighting without becoming cartoonish. See
   * `tmp/detail_test/probe_normal_intensity.py` for the underlying
   * tilt-angle distribution data.
   */
  normalScale: number;
}

/**
 * Per-material detail-normal blend params (sidecar
 * ``Material.detail_params``). Plumbed alongside the texture set so
 * the manager can apply the right influence triplet + UV scale + fade
 * for each material without re-reading the sidecar at material-build
 * time. All six fields are required — the sidecar emits them as a
 * unit whenever any influence is non-zero (see
 * ``_materials._apply_material_mappings_json`` for the producer).
 */
export interface DetailParams {
  normal_influence: number;
  albedo_influence: number;
  gloss_influence: number;
  fade_distance: number;
  scale_u: number;
  scale_v: number;
}

/**
 * Per-material animated-emission params (sidecar
 * ``Material.emission_anim``) for themed-exterior hulls using
 * ``ship_emissive_material.fx`` (e.g. Azur Kearsarge). v1 renders anim
 * mode 1 (sine) + colour mode 0 by modulating the static ``_emissive``
 * map with a sine envelope. See ``_materials._emission_anim_from_entry``
 * for the producer and ``camo/shader.ts`` for the GLSL.
 */
export interface EmissionAnim {
  /** emissionAnimationMode: 1 sine (rendered) / 2 timeline / 3 scroll. */
  mode: number;
  /** emissionColorMode: 0 diffuse (rendered) / 1 cycle / 2 diff-lerp. */
  color_mode: number;
  /** animEmissionPower — animated-term multiplier. */
  anim_power: number;
  /** emissivePower — static-term multiplier (the synth baked this in). */
  static_power: number;
  /** maskSmooth — pow() shaping of the sine envelope. */
  mask_smooth: number;
  /** maskSpeed (x,y,z,w); x = sine frequency. */
  mask_speed: number[];
}

export function applyTexturesToMaterial(
  original: THREE.Material,
  tex: TextureSetResolved,
  policy: MaterialClonePolicy,
  forceTransparentBlend: boolean = false,
  detailParams: DetailParams | null = null,
  forceAlphaTest: boolean = false,
  emissionAnim: EmissionAnim | null = null,
): THREE.Material {
  const std = original as THREE.MeshStandardMaterial;
  if (!('isMeshStandardMaterial' in std) || !std.isMeshStandardMaterial) return original;
  const c = std.clone();

  if (tex.baseColor) c.map = tex.baseColor;

  // MG-map binding is gated on `mgMapEnabled` — see the policy notes.
  // Cache the texture + original factors either way so the toggle can
  // re-bind without re-resolving from disk.
  const mgTex = tex.metallicRoughness ?? null;
  const origMetalness = c.metalness;
  const origRoughness = c.roughness;
  if (mgTex && policy.mgMapEnabled) {
    c.metalnessMap = mgTex;
    c.roughnessMap = mgTex;
  } else if (mgTex) {
    // MG off but the material expected a texture (PBS factors are 1.0/1.0
    // by toolkit convention so the MR texel modulates them). With the
    // texture unbound, raw 1.0/1.0 → fully rough conductor reflecting the
    // entire IBL → reads as near-white. Override to legacy-dielectric
    // defaults so the silhouette stays readable.
    c.metalness = 0.0;
    c.roughness = 0.8;
  }

  if (tex.normal) {
    c.normalMap = tex.normal;
    // Three.js `normalScale` defaults to (1, 1); we bump per the
    // policy so WG's intrinsically-gentle hull normals (2-3° mean
    // tilt) read at the chosen visibility level. setNormalScale on
    // the TextureManager updates this live across all clones.
    c.normalScale.set(policy.normalScale, policy.normalScale);
  }
  if (tex.occlusion) c.aoMap = tex.occlusion;
  if (tex.emissive) {
    c.emissiveMap = tex.emissive;
    // Default emissive color is black → emissive map × 0 = no contribution.
    // Set white so the map's RGB drives directly; synth_emission.py bakes
    // the .mfm `emissivePower` into the texture so this stays a unit
    // multiplier.
    c.emissive = new THREE.Color(0xffffff);
    c.emissiveIntensity = 1.0;
  }

  // Honor current AO toggle for new clones — setAoEnabled walks existing
  // clones; this catches ones built later.
  c.aoMapIntensity = policy.aoEnabled ? 1.0 : 0.0;

  // Sidecar marked this as `shader_intent: "transparent"` — force three.js
  // to alpha-blend. The toolkit's pre-2026-05-16 glTF emit said
  // `alphaMode: Opaque` for textured-transparent materials (SHIPGLASS,
  // semi-transparent armor), so the GLTFLoader leaves `transparent: false`.
  // Without this flip, base color alpha is ignored and `transparent_glass_alpha_a.dds`
  // renders solid. `depthWrite: false` is the standard transparent-glass
  // pattern (avoids self-occlusion against opaque geometry behind it).
  if (forceTransparentBlend) {
    c.transparent = true;
    c.depthWrite = false;
  } else if (forceAlphaTest) {
    // Sidecar marked this as `shader_intent: "cutout"` — alpha-tested
    // single-map shader (WG `assets.bin shader_id=0x00010000`, used for
    // nets/grids/fences). Mirrors the transparent branch above: the
    // toolkit's glTF emit says `alphaMode: Opaque`, so without this flip
    // baseColor.a is ignored and `<stem>_a.dds`'s alpha=0 texels render
    // as solid (filled with whatever RGB the artist baked into the
    // holes — typically a mid-gray that reads as "the parent radar's
    // own color"). Standard glTF MASK semantics use a 0.5 cutoff.
    c.alphaTest = 0.5;
    c.transparent = false;
    c.depthWrite = true;
  }

  // Always attach the camo chunk. Sidecar-transparent materials still
  // get the chunk, but the manager's dispatch routes their uniform push
  // down the all-disabled branch (mask=null, matTex=null, mgnTex=null),
  // and the shader's `else { diffuseColor *= baseSample; }` catch-all
  // renders identically to stock Three.js <map_fragment>. Skipping the
  // chunk for transparents was a pre-2026-05-17 optimization that
  // became a load-bearing gate; splitting it cleared the way for
  // removing the per-entry acceptsCamo cache (and its retroactive flip
  // in markNoCamoKey) — the manager now reads noCamoKeys.has(e.key)
  // at use-time, no per-entry mirror needed.
  //
  // Engine analog: per the part_index lookup at exe 0x140071a20 (see
  // reference/topics/camo/camo_part_index_table.md), transparent
  // materials carry no enumerated material name, so FUN_14108c360
  // bails out at LAB_14108c60e and makeCamoMaterial is never invoked.
  // MASK (alphaTest > 0, transparent: false) stays through Path A
  // because the engine itself does `discard_nz` on diffuse.a in
  // `ship_camo_material.fx`.
  const camoUniforms = attachCamoChunk(c);

  // Bind the no-camo region mask (toolkit-emitted `_nbmask.dds`). Without
  // it the shader falls back to apply-everywhere (legacy pre-2026-04-30
  // behaviour). Path B 4-threshold deny-formula source.
  if (tex.camoMask) {
    camoUniforms.camoMaskMap.value = tex.camoMask;
    camoUniforms.camoMaskBound.value = 1.0;
  }

  // Bind the Path A binary paint mask (toolkit-emitted `_camomask.dds`,
  // derived from WG `_mg.B`). Without it the shader falls back to the
  // nbPaint factor (pre-2026-05-16 toolkit extracts).
  if (tex.camoExclusionMask) {
    camoUniforms.camoExclusionMap.value = tex.camoExclusionMask;
    camoUniforms.camoExclusionBound.value = 1.0;
  }

  // WG-pack channel reinterpretation. Decoded textures carry
  // `userData.wgPackMG` / `userData.wgPackN` set in DecodedTextureCache
  // based on filename suffix. Loose-mod skins land raw `_mg.dd*` /
  // `_n.dd*` here; the shader's mix() resolves to glTF-MR semantics.
  if (mgTex) {
    camoUniforms.wgPackMG.value = mgTex.userData?.wgPackMG ? 1.0 : 0.0;
  }
  if (tex.normal) {
    camoUniforms.wgPackN.value = tex.normal.userData?.wgPackN ? 1.0 : 0.0;
  }

  // Detail-atlas binding. The sidecar emits `detail_params` and binds
  // the `detail` slot together (both or neither — see
  // ``_materials._apply_material_mappings_json``), so the presence of
  // `detailParams` implies `tex.detail` is also present. The shader
  // sums detail onto the base tangent normal weighted by a distance
  // fade — see `camo/shader.ts` for the engine recipe.
  if (detailParams) {
    camoUniforms.detailMap.value = tex.detail!;
    camoUniforms.detailMapBound.value = 1.0;
    camoUniforms.detailScale.value.set(detailParams.scale_u, detailParams.scale_v);
    camoUniforms.detailInfluence.value.set(
      detailParams.normal_influence,
      detailParams.albedo_influence,
      detailParams.gloss_influence,
    );
    camoUniforms.detailFadeDistance.value = detailParams.fade_distance;
  }

  // Themed-exterior animated emission (ship_emissive_material.fx). v1
  // renders anim mode 1 (sine) + colour mode 0 by modulating the static
  // `_emissive` map with a sine envelope (see camo/shader.ts). Requires a
  // bound emissive map — that map IS the static glow being animated.
  // gain = animEmissionPower / emissivePower (the engine's animated:static
  // power ratio); the synth baked emissivePower into the texture, so this
  // scales the captured static term back up to the animated amplitude.
  if (emissionAnim && emissionAnim.mode === 1 && tex.emissive) {
    const staticPow = emissionAnim.static_power > 1e-4 ? emissionAnim.static_power : 1.0;
    camoUniforms.exEmissiveAnimEnable.value = 1.0;
    camoUniforms.exEmissiveAnimSpeed.value = emissionAnim.mask_speed?.[0] ?? 0.1;
    camoUniforms.exEmissiveAnimSmooth.value = emissionAnim.mask_smooth;
    camoUniforms.exEmissiveAnimGain.value = emissionAnim.anim_power / staticPow;
  }

  c.userData = { ...(c.userData || {}), camoUniforms, mgTex, origMetalness, origRoughness };
  c.needsUpdate = true;
  return c;
}

export function buildTextured(
  original: THREE.Material | THREE.Material[],
  tex: TextureSetResolved,
  policy: MaterialClonePolicy,
  forceTransparentBlend: boolean = false,
  detailParams: DetailParams | null = null,
  forceAlphaTest: boolean = false,
  emissionAnim: EmissionAnim | null = null,
): THREE.Material | THREE.Material[] {
  if (Array.isArray(original)) {
    return original.map((m) =>
      applyTexturesToMaterial(m, tex, policy, forceTransparentBlend, detailParams, forceAlphaTest, emissionAnim),
    );
  }
  return applyTexturesToMaterial(
    original, tex, policy, forceTransparentBlend, detailParams, forceAlphaTest, emissionAnim,
  );
}
