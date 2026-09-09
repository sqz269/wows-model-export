import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const tempRoot = resolve(tmpdir());
const temp = await mkdtemp(join(tempRoot, 'wg-camo-parity-'));
if (dirname(resolve(temp)) !== tempRoot) throw new Error('Unexpected temporary directory');
try {
  const output = join(temp, 'fixture.mjs');
  await build({
    stdin: { resolveDir: root, contents: `
      export * as THREE from 'three';
      export { TextureManager } from './src/lib/ship/textures/manager';
      export { attachCamoChunk } from './src/lib/ship/camo/shader';
      export { ShipViewer } from './src/lib/ship/viewer';
    ` },
    bundle: true, platform: 'node', format: 'esm',
    tsconfig: join(root, 'tsconfig.json'), outfile: output,
  });
  const { THREE, TextureManager, attachCamoChunk, ShipViewer } = await import(pathToFileURL(output).href);
  const skinChoices = [
    {skin_id:'azur', scheme_key:'azur', exterior_id:'azur-exterior'},
    {skin_id:'standard', scheme_key:'main'},
  ];
  const exteriorChoices = [{exterior_id:'default'},
    {exterior_id:'azur-exterior', camo_scheme_key:'azur'}];
  const calls = [];
  const viewer = Object.create(ShipViewer.prototype);
  Object.assign(viewer, {
    activeExteriorId:'default', getExteriors:() => exteriorChoices,
    textures: {getSkins:() => skinChoices, setActiveSkin:async id => calls.push(['skin', id])},
    setActiveExterior:async id => { calls.push(['exterior', id]); viewer.activeExteriorId = id; },
  });
  await viewer.setActiveSkin('azur');
  assert.deepEqual(calls.splice(0), [['exterior','azur-exterior'], ['skin','azur']],
    'linked skin must load its exterior before applying paint');
  await viewer.setActiveSkin('azur');
  assert.deepEqual(calls.splice(0), [['skin','azur']], 'reselect must not reload hull');
  await viewer.setActiveSkin('standard');
  assert.deepEqual(calls.splice(0), [['exterior','default'], ['skin','standard']],
    'ordinary camouflage must still revert a variant exterior');
  // Exercise the actual uniform dispatcher without network or a WebGL context.
  const manager = Object.create(TextureManager.prototype);
  Object.assign(manager, {
    noCamoKeys: new Set(), variantSwappedAssetIds: new Set(), activeSchemeKey: 'main',
    mgnTextureCache: { get: x => x?.dds_mips[0] === 'missing' ? null : texture },
    matAlbedoCache: { get: () => null }, categoryMaskCache: { get: () => texture },
    animMapCache: { get: () => null },
  });
  const texture = new THREE.Texture();
  const material = new THREE.MeshStandardMaterial();
  const u = attachCamoChunk(material);
  material.userData.camoUniforms = u;
  manager.entries = [{ key: 'hull:fixture', category: 'deckhouse', assetId: null,
    texturedByScheme: new Map([['main', material]]), maskTextureByScheme: new Map() }];
  const params = { mgn_influence: [.4,.6,.8,0], use_camo_mask_global: false,
    camo_mode:2, ao_influence:0, emission_anim_mode:0, emission_base_power:0 };
  const category = { mgn: {dds_mips:['bronze']}, uv: {scale:[8,8],offset:[.1,-.2],rotate:-2.38}, params };
  manager.updateCamoUniforms({ categories: {deckhouse:category} });
  assert.equal(u.catMgnBound.value, 1);
  assert.deepEqual(u.catMgnUv.value.toArray(), [8,8,.1,-.2]);
  assert.equal(u.catMgnRotate.value, -2.38);
  assert.equal(u.matAlbedoEnable.value, 0, 'MGN-only fixture must not need albedo');
  manager.updateCamoUniforms({ categories: {deckhouse:category}, mat_textures: {
    deckhouse: {...category, albedo:{dds_mips:['unavailable']}, uv:{scale:[2,3],offset:[0,0]}}
  }});
  assert.deepEqual(u.catMgnUv.value.toArray(), [2,3,0,0], 'mat entry takes precedence despite missing albedo');
  assert.equal(u.catMgnRotate.value, 0, 'legacy absent rotation defaults to zero');
  manager.updateCamoUniforms(null);
  assert.equal(u.catMgnBound.value, 0);
  assert.deepEqual(u.catMgnUv.value.toArray(), [1,1,0,0]);
  assert.equal(u.catMgnRotate.value, 0);
  assert.equal(u.camoRotate.value, 0);
  assert.equal(u.matAlbedoRotate.value, 0);
  // Assemble the production Three shader; ensure all patch points are consumed.
  const shader = {uniforms:{}, vertexShader:THREE.ShaderLib.standard.vertexShader,
    fragmentShader:THREE.ShaderLib.standard.fragmentShader};
  material.onBeforeCompile(shader, null);
  assert.match(shader.vertexShader, /wgCamoUv\( vMapUv, camoUV, camoRotate \)/);
  assert.match(shader.fragmentShader, /wgCamoUv\( vMapUv, catMgnUv, catMgnRotate \)/);
  assert.match(shader.fragmentShader, /texture2DGradEXT\( detailMap/);
  assert.match(shader.fragmentShader, /\( 1.0 - metalnessFactor \) \+ wgDetailDiffuseDelta/);
  assert.match(shader.fragmentShader, /material.specularColor = mix\( vec3\( 0.04 \), diffuseColor.rgb/,
    'detail albedo must not alter metallic F0');
  assert.doesNotMatch(shader.fragmentShader, /#include <(?:map_fragment|normal_fragment_maps|roughnessmap_fragment|lights_physical_fragment)>/);
  material.dispose(); texture.dispose();
  console.log('Passed camo MGN routing, transform/reset and shader assembly checks.');
} finally {
  await rm(temp, {recursive:true, force:true});
}
