import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const tempRoot = resolve(tmpdir());
const temp = await mkdtemp(join(tempRoot, 'wg-particle-temporal-'));
if (dirname(resolve(temp)) !== tempRoot) throw new Error('Unexpected temporary directory');
try {
  const output = join(temp, 'fixture.mjs');
  await build({
    stdin: {
      resolveDir: root,
      contents: `
    export { integrateRamp } from './src/lib/three/particles/sampling';
    export { SystemRenderer } from './src/lib/three/particles/system-renderer';
    export { buildParticleMaterial } from './src/lib/three/particles/material';
  `,
    },
    bundle: true,
    platform: 'node',
    format: 'esm',
    outfile: output,
    logLevel: 'silent',
  });
  const { integrateRamp, SystemRenderer, buildParticleMaterial } = await import(
    pathToFileURL(output)
  );
  const ramp = {
    points: [
      { time: 0, value: 0 },
      { time: 0.1, value: 10 },
      { time: 0.2, value: 0 },
    ],
  };
  const close = (actual, expected) =>
    assert.ok(Math.abs(actual - expected) < 1e-5, `${actual} != ${expected}`);
  close(integrateRamp(ramp, 0, 0.25), 1);
  close(integrateRamp(ramp, 0, 0.075) + integrateRamp(ramp, 0.075, 0.25), 1);
  close(
    integrateRamp(
      {
        points: [
          { time: 0, value: 10 },
          { time: 0.1, value: 10 },
        ],
      },
      0.1,
      5,
    ),
    0,
  );
  for (const aux of [0.99, 1, 2]) {
    const renderer = { blendType: 'BLENDED' };
    const system = {
      renderer,
      general: { capacity: 1, maxParticleAge: 4, coordinateStyle: 2 },
      emitter: {
        rateGenerator: { type: 'constant', value: 60 },
        ageScaleAuxGenerator: { type: 'constant', value: aux },
      },
      animation: { frameRateRamp: ramp, framesRangeEnd: 49, animationType: 'motionVectors' },
      components: [
        {
          kind: 'PCAT',
          action: 'tint',
          body: {
            tint: {
              points: [
                { time: 0, r: 1, g: 1, b: 1, a: 1 },
                { time: 2, r: 1, g: 1, b: 1, a: 0 },
              ],
            },
            repeat: false,
            delay: 0,
            period: 2,
          },
        },
      ],
    };
    const sim = new SystemRenderer(system, buildParticleMaterial(renderer), 1, {
      loopOneShot: false,
    });
    sim.setActive(true);
    sim.tick(1 / 60);
    sim.tick(0.25);
    const particle = sim.debugSnapshot()[0];
    close(particle.alpha, 1 - particle.age / aux / 2);
    close(particle.framePhase, 1);
    sim.dispose();
  }
  // Fire_medium's Unity flicker regression: emission happens after survivor
  // updates, but a newborn must already have its authored age-zero appearance.
  for (const initialAlpha of [0, 1]) {
    const renderer = { blendType: 'BLENDED' };
    const system = {
      renderer,
      general: { capacity: 1, maxParticleAge: 4, coordinateStyle: 2 },
      emitter: {
        rateGenerator: { type: 'constant', value: 60 },
        sizeGenerator: { type: 'constant', value: 2 },
      },
      components: [
        {
          kind: 'PCAT', action: 'tint',
          body: { tint: { points: [
            { time: 0, r: 8, g: 8, b: 0, a: initialAlpha },
            { time: 1, r: 1, g: 1, b: 1, a: 1 },
          ] }, period: 1, repeat: false, delay: 0 },
        },
        {
          kind: 'PCAT', action: 'scaler',
          body: { delay: 0,
            sizeGenerator: { type: 'constant', value: 0.0878 },
            scaleXGenerator: { type: 'constant', value: 0.3 },
          },
        },
      ],
    };
    const sim = new SystemRenderer(system, buildParticleMaterial(renderer), 1, {
      loopOneShot: false,
    });
    try {
      sim.setActive(true);
      sim.tick(1 / 60);
      const newborn = sim.debugSnapshot()[0];
      assert.ok(newborn, 'Expected a particle on the first emission step');
      close(newborn.age, 0);
      close(newborn.alpha, initialAlpha);
      close(newborn.size, 2 * 0.0878);
      close(newborn.scaleX, 0.3);
    } finally {
      sim.dispose();
    }
  }
  console.log(
    'Particle temporal checks passed: knot crossing, end clamp, aux tint clock, independent flipbook clock, newborn appearance.',
  );
} finally {
  await rm(temp, { recursive: true, force: true });
}
