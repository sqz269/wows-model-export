// Run with node scripts/test-particle-parity.mjs from webview.
// Bundles the CPU simulator only; no browser or GPU is required.
import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const tempRoot = resolve(tmpdir());
const temp = await mkdtemp(join(tempRoot, 'wg-particle-size-'));
if (dirname(resolve(temp)) !== tempRoot) throw new Error('Unexpected temporary directory');
try {
  const output = join(temp, 'test.mjs');
  await build({
    stdin: {
      resolveDir: root,
      contents: `
        import assert from 'node:assert/strict';
        import * as THREE from 'three';
        import { SystemRenderer } from './src/lib/three/particles/system-renderer';
        import { buildParticleMaterial } from './src/lib/three/particles/material';
        import { composeMainBucket } from './src/lib/three/particles/bucket-compositor';
        import { gunShotIntensity } from './src/lib/three/particles/gun-shot-intensity';
        import { ParticleScene } from './src/lib/three/particles/scene';

        assert.equal(gunShotIntensity(420, 1, 1), 7.4);
        assert.equal(gunShotIntensity(420, 3, 1), 10);
        assert.ok(Math.abs(gunShotIntensity(356, 2, .75) - 5.508) < 1e-10);
        assert.equal(gunShotIntensity(50, 3, 1), 0);
        assert.equal(gunShotIntensity(420, 3, 0), 0);
        assert.ok(Math.abs(gunShotIntensity(100, 100, 1) - 2.5) < 1e-10);
        console.log('Passed 6 native gun-intensity fixtures.');
        for (const blendType of ['GRADIENT_MAP', 'UNDERWATER_GRADIENT_MAP', 'BLENDED', 'SHIMMER']) {
          for (const authored of [false, true]) {
            const material = buildParticleMaterial({blendType, useEmissionAlphaFromMV: authored});
            const nativeBit8 = authored || blendType.includes('GRADIENT_MAP');
            assert.equal(material.uniforms.useEmissionAlphaFromMV.value, nativeBit8 ? 1 : 0,
              'native effective MV alpha flag: ' + blendType + '/' + authored);
            material.dispose();
          }
        }
        console.log('Passed 8 native MV alpha flag fixtures.');

        // Captured GK sys5 has v5.xy = .904596 native half-extents.
        // VS geometry uses corner * v5; UV uses (corner + 1) / 2.
        // A native octagon corner at 115/127 must retain its world position.
        const halfSize = .904596;
        const corner = 115 / 127;
        let checked = 0;
        for (const blendType of ['BLENDED', 'SHIMMER']) {
          for (const orientation of ['camera', 'axial', 'fixed']) {
            const renderer = {
              blendType, scaleX: 1, rotationCenter: 'bottom',
              billboard: orientation === 'axial',
              explicitOrientation: orientation === 'camera' ? [0, 0, 0] : [0, 0, 1],
            };
            const system = {
              name: 'native-size-fixture', renderer, components: [],
              general: {capacity: 1, maxParticleAge: 2, coordinateStyle: 2},
              emitter: {
                rateGenerator: {type: 'constant', value: 60},
                sizeGenerator: {type: 'constant', value: halfSize},
                initialPositionGenerator: {outer_type: 1, count: 1, prototypes: [
                  {vgt_type: 'point', body: {position: [1, 2, 3]}}
                ]},
              },
            };
            const material = buildParticleMaterial(renderer);
            const sim = new SystemRenderer(system, material, 1, {loopOneShot: false});
            const group = new THREE.Group(); group.add(sim.points);
            sim.setActive(true); sim.tick(1 / 60);
            if (sim.composedMainBucket) composeMainBucket([sim], null);
            const mesh = group.children.find(x => x.geometry?.instanceCount === 1);
            assert.ok(mesh, 'fixture must emit and pack a particle');
            const diameter = mesh.geometry.getAttribute('size').getX(0);
            const close = (a, b, label) => assert.ok(Math.abs(a-b) < 1e-5, label + ': ' + a + ' != ' + b);
            close(sim.debugSnapshot()[0].size, halfSize, 'simulation remains in native half-extents');
            close(diameter, 27.13788, blendType + '/' + orientation + ' full diameter in metres');
            close(((corner + 1) / 2 - .5) * diameter, corner * halfSize * 15,
              'same texture coordinate occupies the native world position');
            close(diameter * .5, 13.56894, 'anchor half-height in metres');
            const pos = mesh.geometry.getAttribute('iPosition');
            close(pos.getX(0), 15, 'position X');
            close(pos.getY(0), 30, 'position Y');
            close(pos.getZ(0), 45, 'position Z');
            sim.setDrawVisible(false);
            assert.equal(mesh.visible, false, 'hide must affect the actual draw mesh while paused');
            if (sim.composedMainBucket) composeMainBucket([sim], null);
            assert.equal(mesh.visible, false, 'recomposition must preserve hidden state');
            sim.setDrawVisible(true);
            assert.equal(mesh.visible, true, 'show restores buffered draws without advancing time');
            sim.dispose(); checked++;
          }
        }
        console.log('Passed ' + checked + ' particle size fixtures (all orientation modes, both draw paths).');

        const cases = [
          {rate: 11, active: .1, delay: 0, dt: 1/60, count: 1},
          {rate: 11, active: .1, delay: .025, dt: 1/60, count: 1},
          {rate: 11, active: .1, delay: .05, dt: 1/60, count: 1},
          {rate: 11, active: .1, delay: .07, dt: 1/60, count: 1},
          {rate: 22, active: .12, delay: .105, dt: 1/60, count: 2},
          // Fractional last-step clipping: a full extra .05 would emit six.
          {rate: 40, active: .12, delay: 0, dt: .05, count: 4},
          // Native delay lookahead counts the whole crossing step; exact
          // interval intersection would emit four instead of native five.
          {rate: 44, active: .1, delay: .025, dt: .05, count: 5},
        ];
        for (const c of cases) {
          const system = {
            name: 'negative-sleep-fixture', components: [],
            general: {capacity: 100, maxParticleAge: 6, coordinateStyle: 2},
            emitter: {
              rateGenerator: {type: 'constant', value: c.rate},
              sizeGenerator: {type: 'constant', value: 1},
              activePeriod: c.active,
              sleepPeriodGenerator: {type: 'constant', value: -1},
              delayGenerator: {type: 'constant', value: c.delay},
            },
          };
          const sim = new SystemRenderer(system, buildParticleMaterial(), 1.5, {loopOneShot: false});
          sim.setActive(true);
          for(let i=0; i<Math.ceil(1.35/c.dt); i++) sim.tick(c.dt);
          assert.equal(sim.debugSnapshot().length, c.count, JSON.stringify(c));
          for(let i=0; i<Math.ceil(.3/c.dt); i++) sim.tick(c.dt);
          assert.equal(sim.debugSnapshot().length, c.count, 'source must remain stopped');
          sim.dispose();
        }
        console.log('Passed ' + cases.length + ' negative-sleep emission fixtures.');
        const loopSystem = {
          components: [], general: {capacity: 100, maxParticleAge: 6},
          emitter: {
            rateGenerator: {type: 'constant', value: 22},
            activePeriod: .12,
            sleepPeriodGenerator: {type: 'constant', value: -1},
          },
        };
        const loopSim = new SystemRenderer(loopSystem, buildParticleMaterial(), 1.5, {loopOneShot: true});
        loopSim.setActive(true);
        for(let i=0; i<540; i++) loopSim.tick(1/60);
        assert.equal(loopSim.debugSnapshot().length, 2, 'second preview cycle must not double-count its first step');
        loopSim.dispose();
        console.log('Passed preview-loop emission regression.');
        const preview = new ParticleScene();
        const [handle] = preview.build(
          [{node: 'origin', particle_path: 'pause-fixture', source: 'hull'}],
          {'pause-fixture': {maxEmittingDuration: 1.5, systems: [loopSystem]}},
          () => new THREE.Vector3(),
        );
        preview.setAttachmentActive(handle, true);
        preview.tick(0); preview.tick(100);
        const beforePause = handle.systems[0].debugSnapshot();
        assert.ok(beforePause.length > 0);
        preview.tick(10000, true);
        assert.deepEqual(handle.systems[0].debugSnapshot(), beforePause, 'pause preserves particle state');
        assert.equal(handle.group.visible, true, 'pause preserves visible attachment');
        preview.tick(10010);
        assert.ok(Math.abs(handle.systems[0].elapsedSeconds - .11) < 1e-6, 'resume advances only time since paused tick');
        preview.dispose();
        console.log('Passed preview pause/resume regression.');
      `,
    },
    bundle: true,
    platform: 'node',
    format: 'esm',
    tsconfig: join(root, 'tsconfig.json'),
    outfile: output,
  });
  await import(pathToFileURL(output).href);
} finally {
  await rm(temp, { recursive: true, force: true });
}
