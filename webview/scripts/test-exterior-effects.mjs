// Native routing is tested in Python; these cases guard consumer selection
// and the lifecycle that used to retain handles to disposed exterior mounts.
import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const tempRoot = resolve(tmpdir());
const temp = await mkdtemp(join(tempRoot, 'wg-exterior-effects-'));
if (dirname(resolve(temp)) !== tempRoot) throw new Error('Unexpected temporary directory');
try {
  const output = join(temp, 'test.mjs');
  await build({
    stdin: { resolveDir: root, contents: `
      import assert from 'node:assert/strict';
      import { exteriorParticleAttachments } from './src/lib/ship/exterior_effects';
      import { ShipViewer } from './src/lib/ship/viewer';
      const base = [{source:'hull',group:'smoke',node:'EP_Smoke',particle_path:'base.xml'}];
      const themed = [{source:'hull',group:'idle',node:'EP_Light',particle_path:'theme.xml'}];
      const doc = {effects:{attachments:base},exteriors:[
        {exterior_id:'default'}, {exterior_id:'theme',effects:{attachments:themed}},
        {exterior_id:'empty',effects:{attachments:[]}}, {exterior_id:'legacy'}]};
      assert.equal(exteriorParticleAttachments(doc,'default'),base);
      assert.equal(exteriorParticleAttachments(doc,'theme'),themed);
      assert.deepEqual(exteriorParticleAttachments(doc,'empty'),[]);
      assert.equal(exteriorParticleAttachments(doc,'legacy'),base);
      assert.equal(exteriorParticleAttachments(doc,'unknown'),base);
      assert.deepEqual(exteriorParticleAttachments(null,'default'),[]);

      const calls=[];
      const v=Object.create(ShipViewer.prototype);
      Object.assign(v, {
        sidecar:doc, activeExteriorId:'default', activeHullExteriorId:null,
        shipCtx:{placementsDoc:{}}, hpByInstanceId:new Map(), mountRecords:new Map(),
        particleBuildToken:1, particleBuildPromise:Promise.resolve(),
        particleLayerEnabled:true, particleMode:'ambient', particleLoopKeys:new Set(['hull:fire1']),
        shipWaterEnabled:false, particleScene:{dispose(){calls.push('dispose')},root:{}},
        particleHandles:[{}],particleAnchorObjects:new Map(),particleAnchorMotion:new Map(),
        env:{scene:{remove(){}},setWaterPlaneVisible(){}},shipRoot:{},
        textures:{setVariantSwappedAssetIds(){},isShowingTextures(){return false}},
        armorViewEnabled:false,colorMode:'off',
        nodeOverlay:{refresh(_root,rows){assert.equal(rows[0]?.particle_path,
          v.getActiveParticleAttachments()[0]?.particle_path);calls.push('overlay')}},
        disposeMounts(){assert.equal(v.particleHandles.length,0);calls.push('mounts')},
        applyAllStates(){},
        async setShipParticlesVisible(on){assert.equal(on,true);calls.push('build:'+v.activeExteriorId)}
      });
      await v.setActiveExterior('theme');
      assert.deepEqual(calls,['dispose','mounts','build:theme','overlay']);
      assert.equal(v.particleBuildToken,2);
      assert.equal(v.particleBuildPromise,null);
      assert.equal(v.shipWaterEnabled,false);
      assert.deepEqual([...v.particleLoopKeys],['hull:fire1']);
      assert.equal(v.getActiveParticleAttachments(),themed);
      await v.setActiveExterior('default');
      assert.equal(v.getActiveParticleAttachments(),base);
      v.particleLayerEnabled=false;
      calls.length=0;
      await v.setActiveExterior('empty');
      assert.equal(v.hasShipParticleData(),false);
      assert.deepEqual(calls,['mounts','overlay']);

      // Hull reload destroys the ship graph; retain user particle controls.
      Object.assign(v,{currentShip:{},shipCtx:{library:{}},particleLayerEnabled:true,
        particleMode:'all',particleLoopKeys:new Set(['artillery:shotEffect']),shipWaterEnabled:false,
        async loadShip(){this.particleLoopKeys.clear();this.particleLayerEnabled=false;
          this.shipWaterEnabled=null;this.particleMode='ambient'}});
      calls.length=0;
      await v.reloadWithExteriorHull('theme');
      assert.equal(v.particleMode,'all');
      assert.deepEqual([...v.particleLoopKeys],['artillery:shotEffect']);
      assert.equal(v.shipWaterEnabled,false);
      assert.equal(calls.length,1);
      console.log('Passed exterior selection, empty/legacy fallback, mount rebind, hidden layer, and hull reload checks.');
    ` },
    alias: { '$lib': join(root, 'src/lib') },
    bundle: true, platform: 'node', format: 'esm', outfile: output,
    loader: { '.glsl': 'text' },
  });
  await import(pathToFileURL(output).href);
} finally {
  await rm(temp, { recursive: true, force: true });
}
