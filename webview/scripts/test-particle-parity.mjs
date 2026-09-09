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

        // Raw input TEXCOORD5.z and actual post-VS TEXCOORD10.z from
        // gk_frame39398.rdc E6984/I0 and E7004/I0. Native VS instructions
        // 993-995 use reciprocal(.3 * v5.z); the extent is NOT final height.
        const softFixtures = [
          {payload: 1.9035370349884033, soft: 1.5, nativeSlope: 1.7511260509490967},
          {payload: 0.06630577147006989, soft: 2, nativeSlope: 50.27214431762695},
        ];
        let softChecked = 0;
        for (const fixture of softFixtures) {
          for (const blendType of ['BLENDED', 'SHIMMER']) {
            for (const orientation of ['camera', 'axial', 'fixed']) {
              for (const [drawScale, sizeFactor, distanceSize] of [[0, .5, 2], [.25, .5, 1], [3, 1, 3]]) {
                const renderer = {blendType, softParticleDepthScale: fixture.soft,
                  billboard: orientation === 'axial',
                  explicitOrientation: orientation === 'camera' ? [0, 0, 0] : [0, 0, 1]};
                const config = (flags, value) => ({flags, ramp: {points: [{time: 0, value}]}});
                const system = {renderer,
                  general: {capacity: 1, maxParticleAge: 2, coordinateStyle: 2},
                  emitter: {rateGenerator: {type: 'constant', value: 60},
                    // SIZE intensity and distance SIZE both belong in B.
                    sizeGenerator: {type: 'constant', value: fixture.payload / fixture.soft}},
                  intensities: {channels: [{configs: [config([23], sizeFactor), config([3, 16], drawScale)]}]},
                  distance: {configs: [config([23], distanceSize), config([3, 16], 2)]},
                  components: [{action: 'scaler', body: {sizeGenerator: {type: 'ramp',
                    parameterType: 'particleAge', samplingType: 'once',
                    ramp: {points: [{time: 0, value: 1}, {time: .2, value: 2}]}}}}],
                };
                const material = buildParticleMaterial(renderer);
                const sim = new SystemRenderer(system, material, 1, {loopOneShot: false});
                const group = new THREE.Group(); group.add(sim.points);
                const camera = new THREE.PerspectiveCamera(); camera.position.z = 100;
                sim.setSortCamera(camera); sim.setActive(true);
                const close = (a, b, label) => assert.ok(Math.abs(a-b) < 1e-5 * Math.max(1, Math.abs(b)),
                  label + ': ' + a + ' != ' + b);
                try {
                  for (const [dt, growth] of [[1/60, 1], [.1, 1.5]]) {
                    sim.tick(dt);
                    if (sim.composedMainBucket) composeMainBucket([sim], camera);
                    const mesh = group.children.find(x => x.geometry?.instanceCount === 1);
                    assert.ok(mesh, 'soft-depth fixture must pack one particle');
                    const baseFactor = sizeFactor * distanceSize;
                    const extent = mesh.geometry.getAttribute('softDepthExtent').getX(0);
                    close(extent, fixture.payload * baseFactor * growth * 15, 'native soft extent in metres');
                    const slope = 1 / (.3 * extent);
                    close(slope, fixture.nativeSlope / (15 * baseFactor * growth), 'captured reciprocal slope');
                    close(mesh.geometry.getAttribute('size').getX(0),
                      fixture.payload / fixture.soft * baseFactor * growth * 30 * drawScale * 2,
                      'draw SCALE_Y affects height only, including zero');
                    // Half a native fade band must produce alpha .5 in metre space.
                    close((.15 * fixture.payload * baseFactor * growth * 15) * slope, .5, 'intersection half alpha');
                  }
                  assert.equal(material.uniforms.uSoftParticleEnabled.value, 1);
                  assert.ok(material.vertexShader.includes('1.0 / (0.3 * softDepthExtent)'));
                  assert.ok(material.fragmentShader.includes('(sceneLinear - particleLinear) * vSoftDepthSlope'));
                } finally { sim.dispose(); }
                softChecked++;
              }
            }
          }
        }
        for (const softParticleDepthScale of [undefined, 0, -1, NaN]) {
          const material = buildParticleMaterial({softParticleDepthScale});
          assert.equal(material.uniforms.uSoftParticleEnabled.value, 0, 'no authored fade remains opt-out');
          material.dispose();
        }
        console.log('Passed ' + softChecked + ' native soft-depth fixtures (spawn/update, draw paths, orientations, size/distance and zero-height factors).');

        // Native zero-active source ordering/count fixtures. GK_Lens is a
        // delayed water-deformation burst, not a rate-1-per-second emitter.
        const burstCases = [
          {label: 'creation before retirement', rate: {type: 'constant', value: 1}, delay: 0,
            steps: [.125, .125], counts: [1, 1]},
          {label: 'delay look-ahead allows two ticks', rate: {type: 'constant', value: 1}, delay: .25,
            steps: [.125, .125, .125, .125], counts: [0, 1, 2, 2]},
          {label: 'variable dt source age', rate: {type: 'constant', value: 1}, delay: .125,
            steps: [.05, .125, .025], counts: [0, 1, 1]},
          {label: 'GK_Lens capacity', rate: {type: 'constant', value: 1}, delay: .1, capacity: 1,
            steps: Array(12).fill(1/60), counts: [0,0,0,0,0,1,1,1,1,1,1,1]},
          {label: 'linear reads first raw value', rate: {type: 'linear', from: 3, to: 99}, delay: 0,
            steps: [.01], counts: [3]},
          {label: 'ramp reads first value before time zero', rate: {type: 'ramp', ramp: {points: [
            {time: -1, value: 3}, {time: 0, value: 99}]}}, delay: 0,
            steps: [.01], counts: [3]},
          {label: 'fraction is never accumulated', rate: {type: 'constant', value: .9}, delay: 0, sleep: 0,
            steps: [.1,.1,.1,.1], counts: [0,0,0,0]},
          {label: 'count has no rate-per-second cap', rate: {type: 'constant', value: 300}, delay: 0, capacity: 400,
            steps: [.01], counts: [300]},
          {label: 'rate intensity before truncation', rate: {type: 'constant', value: 1.9}, multiplier: 2, delay: 0,
            steps: [.01], counts: [3]},
          {label: 'positive sleep phase advance', rate: {type: 'constant', value: 1}, delay: 0, sleep: .25,
            steps: [.125,.125,.125,.125,.125], counts: [1,2,2,3,3]},
          {label: 'strict epsilon inside', rate: {type: 'constant', value: 200}, delay: 0,
            period: Math.fround(.0004) * .5, capacity: 256, steps: [.001], counts: [200]},
          {label: 'strict epsilon edge is continuous', rate: {type: 'constant', value: 200}, delay: 0,
            period: Math.fround(.0004), capacity: 256, steps: [.001], counts: [0]},
        ];
        for (const fixture of burstCases) {
          const renderer = {blendType: 'SHIMMER'};
          const system = {renderer, components: [],
            general: {capacity: fixture.capacity ?? 8, maxParticleAge: 4},
            emitter: {rateGenerator: fixture.rate, activePeriod: fixture.period ?? 0,
              delayGenerator: {type: 'constant', value: fixture.delay},
              sleepPeriodGenerator: {type: 'constant', value: fixture.sleep ?? -1}},
            intensities: {channels: [{configs: [{flags: [19], ramp: {points: [
              {time: 0, value: fixture.multiplier ?? 1} ]}}]}]},
          };
          const sim = new SystemRenderer(system, buildParticleMaterial(renderer), 2, {loopOneShot: false});
          try {
            sim.setActive(true);
            fixture.steps.forEach((dt, i) => {
              sim.tick(dt);
              assert.equal(sim.debugSnapshot().length, fixture.counts[i], fixture.label + ' tick ' + i);
            });
            sim.restart(); sim.tick(fixture.steps[0]);
            assert.equal(sim.debugSnapshot().length, fixture.counts[0], fixture.label + ' restart');
          } finally {sim.dispose();}
        }
        console.log('Passed ' + burstCases.length + ' native near-zero-active burst fixtures.');

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
