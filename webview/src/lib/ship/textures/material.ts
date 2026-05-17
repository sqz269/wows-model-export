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
   * Current waterline gate. 0 = preserve underwater (toolkit y=0 at
   * waterline), -1e9 = disable gate.
   */
  waterlineY: number;
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

export function applyTexturesToMaterial(
  original: THREE.Material,
  tex: TextureSetResolved,
  policy: MaterialClonePolicy,
  forceTransparentBlend: boolean = false,
  detailParams: DetailParams | null = null,
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

  if (tex.normal) c.normalMap = tex.normal;
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
  camoUniforms.waterlineY.value = policy.waterlineY;

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
): THREE.Material | THREE.Material[] {
  if (Array.isArray(original)) {
    return original.map((m) =>
      applyTexturesToMaterial(m, tex, policy, forceTransparentBlend, detailParams),
    );
  }
  return applyTexturesToMaterial(original, tex, policy, forceTransparentBlend, detailParams);
}
