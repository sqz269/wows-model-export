// Scalar/vector utils + Ramp / Color / ValueGenerator sampling on their
// authored parameterType axes, and spawn-volume prototype sampling.
// Extracted verbatim from the former monolithic particles.ts.

import type * as THREE from 'three';
import type {
  ParticleColor,
  ParticleRamp,
  ParticleSystem,
  ParticleValueGenerator,
  ParticleVariantVg,
  ParticleVgtPrototype,
} from '$lib/types/sidecar';

export function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

export function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

export function particleByteStepCount(value: unknown): number {
  const raw = Math.trunc(finiteNumber(value, 0));
  return raw < 2 ? 0 : Math.min(raw - 1, 255);
}

export function hasNonZeroNumber(value: unknown, eps = 1e-6): boolean {
  return Math.abs(finiteNumber(value, 0)) > eps;
}

export function vectorHasLength(value: unknown, eps = 1e-6): value is [number, number, number] {
  if (!Array.isArray(value) || value.length !== 3) return false;
  const x = finiteNumber(value[0], 0);
  const y = finiteNumber(value[1], 0);
  const z = finiteNumber(value[2], 0);
  return x * x + y * y + z * z > eps * eps;
}

export function normalizedParticleSunColor(color: THREE.Color): THREE.Color {
  // Native particle lightmapping applies colored Reinhard normalization:
  // sunColor / (luma(sunColor) + 1). This keeps white-sun smoke at the old
  // 0.5 attenuation while preserving weather sun hue.
  const luma = color.r * 0.2126 + color.g * 0.7152 + color.b * 0.0722;
  return color.clone().multiplyScalar(1 / (luma + 1));
}

export function systemUsesDetachedCoordinateFrame(system: ParticleSystem): boolean {
  const coord = system.general?.coordinateStyle ?? 2;
  return coordinateStyleUsesDetachedFrame(coord);
}

export function coordinateStyleUsesDetachedFrame(coord: number): boolean {
  return coord < 2 || coord === 3;
}

/** Sample a 1D ``Ramp`` curve at parameter ``t ∈ [0, 1]``. */
export function sampleRamp(ramp: ParticleRamp | undefined, t: number, fallback = 1): number {
  if (!ramp || !ramp.points || ramp.points.length === 0) return fallback;
  const pts = ramp.points;
  if (t <= pts[0].time) return pts[0].value;
  if (t >= pts[pts.length - 1].time) return pts[pts.length - 1].value;
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].time) {
      const a = pts[i - 1];
      const b = pts[i];
      const span = b.time - a.time;
      if (span <= 0) return a.value;
      const u = (t - a.time) / span;
      return a.value + u * (b.value - a.value);
    }
  }
  return pts[pts.length - 1].value;
}

/** Sample a Color (RGBA) curve at parameter ``t ∈ [0, 1]``. Out → 4 floats. */
export function sampleColor(color: ParticleColor | undefined, t: number, out: Float32Array): void {
  if (!color || !color.points || color.points.length === 0) {
    out[0] = 1;
    out[1] = 1;
    out[2] = 1;
    out[3] = 1;
    return;
  }
  const pts = color.points;
  if (t <= pts[0].time) {
    out[0] = pts[0].r;
    out[1] = pts[0].g;
    out[2] = pts[0].b;
    out[3] = pts[0].a;
    return;
  }
  if (t >= pts[pts.length - 1].time) {
    const p = pts[pts.length - 1];
    out[0] = p.r;
    out[1] = p.g;
    out[2] = p.b;
    out[3] = p.a;
    return;
  }
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].time) {
      const a = pts[i - 1];
      const b = pts[i];
      const span = b.time - a.time;
      if (span <= 0) {
        out[0] = a.r;
        out[1] = a.g;
        out[2] = a.b;
        out[3] = a.a;
        return;
      }
      const u = (t - a.time) / span;
      out[0] = a.r + u * (b.r - a.r);
      out[1] = a.g + u * (b.g - a.g);
      out[2] = a.b + u * (b.b - a.b);
      out[3] = a.a + u * (b.a - a.a);
      return;
    }
  }
}

/** Sample a scalar ``ValueGenerator``. Returns a plausible scalar with a
 *  fallback for "none" or unknown types. ``t`` is particle age in [0, 1]
 *  for ramp-type generators. */
export function sampleScalarVg(
  vg: ParticleValueGenerator | undefined,
  t = 0,
  fallback = 0,
): number {
  if (!vg) return fallback;
  switch (vg.type) {
    case 'constant':
      return vg.value ?? fallback;
    case 'linear': {
      // Random pick in [from, to]. Caller can re-sample for randomness.
      const f = vg.from ?? 0;
      const tt = vg.to ?? f;
      return f + Math.random() * (tt - f);
    }
    case 'ramp':
      return sampleRamp(vg.ramp, t, fallback);
    default:
      return fallback;
  }
}

/** Per-particle clock state for the PS_VALG_RAMP_PARAMETER axis selection.
 *  Times are SECONDS; velocity axes are m/s magnitudes; particleIndex is the
 *  per-particle u8 spawn counter (0..254). RE 2026-06-04 (build 12506899) —
 *  see memory project-particle-runtime-eval-size-model. */
export interface ParticleClocks {
  particleAge: number;
  systemAge: number;
  systemActiveTime: number;
  particleSpeed: number;
  systemSpeed: number;
  particleIndex: number;
}

export function rampHasNonZeroValue(ramp: ParticleRamp | undefined): boolean {
  return !!ramp?.points?.some((p) => Math.abs(p.value) > 1e-6);
}

/** PS_VALG_RAMP_PARAMETER → the ramp X axis. The shipped order is
 *  {0:systemAge,1:particleAge,2:systemVelocity,3:particleVelocity,
 *  4:systemActiveTime,5:particleIndex}; standalone/unkeyed ramps default to
 *  particle age. Ramp `.time` is SECONDS of this clock, NOT a normalized
 *  [0,1] (the prior consumer + spec were wrong on this). */
function rampAxisX(vg: ParticleValueGenerator, c: ParticleClocks): number {
  switch (vg.parameterType) {
    case 'systemAge':
      return c.systemAge;
    case 'particleAge':
      return c.particleAge;
    case 'systemActiveTime':
      return c.systemActiveTime;
    case 'systemVelocity':
      return c.systemSpeed;
    case 'particleVelocity':
      return c.particleSpeed;
    case 'particleIndex':
      return c.particleIndex;
    default:
      return c.particleAge;
  }
}

/** PS_VALG_RAMP_SAMPLING wrap against the ramp's last-key time:
 *  {0:loop → fmod, 1:pingPong → triangle, 2:once → clamp}. `once`/undefined
 *  rely on sampleRamp's built-in clamp to [t0, tmax]. */
function wrapRampAxis(x: number, tmax: number, sampling: string | number | undefined): number {
  if (!(tmax > 0)) return x;
  if (sampling === 'loop') {
    const m = x % tmax;
    return m < 0 ? m + tmax : m;
  }
  if (sampling === 'pingPong') {
    const period = tmax * 2;
    let m = x % period;
    if (m < 0) m += period;
    return m <= tmax ? m : period - m;
  }
  return x; // 'once' / undefined
}

/** Sample a scalar ValueGenerator on its authored parameterType axis
 *  (seconds-clock / velocity / particleIndex), wrapped by samplingType. The
 *  RE-correct replacement for sampling ramps at a normalized [0,1] age.
 *  ``linear`` is random per call — sample ONCE at spawn for per-particle-fixed
 *  quantities (e.g. the emitter size base). */
export function sampleGenAxis(
  vg: ParticleValueGenerator | undefined,
  c: ParticleClocks,
  fallback = 0,
): number {
  if (!vg) return fallback;
  if (vg.type === 'constant') return vg.value ?? fallback;
  if (vg.type === 'linear') {
    const f = vg.from ?? 0;
    const t = vg.to ?? f;
    return f + Math.random() * (t - f);
  }
  if (vg.type === 'ramp') {
    const pts = vg.ramp?.points;
    if (!pts || pts.length === 0) return fallback;
    const tmax = pts[pts.length - 1].time;
    return sampleRamp(vg.ramp, wrapRampAxis(rampAxisX(vg, c), tmax, vg.samplingType), fallback);
  }
  return fallback;
}

/** Pick a random point inside the union of all prototypes in the
 *  variant VG (creator.initialPositionGenerator / initialVelocityGenerator).
 *  Writes to `out`. */
export function samplePosFromVariantVg(
  vg: ParticleVariantVg | undefined,
  out: THREE.Vector3,
): void {
  out.set(0, 0, 0);
  if (!vg || vg.count === 0 || !vg.prototypes?.length) return;
  // Pick one prototype at random with uniform weight. WG's distribution
  // semantics aren't fully nailed down; uniform is a reasonable approx.
  const proto = vg.prototypes[Math.floor(Math.random() * vg.prototypes.length)];
  samplePosFromPrototype(proto, out);
}

function samplePosFromPrototype(proto: ParticleVgtPrototype, out: THREE.Vector3): void {
  const body = proto.body;
  if (!body) {
    out.set(0, 0, 0);
    return;
  }
  switch (proto.vgt_type) {
    case 'point': {
      const p = body.position ?? [0, 0, 0];
      out.set(p[0], p[1], p[2]);
      return;
    }
    case 'box': {
      const c = body.corner ?? [0, 0, 0];
      const o = body.opposite ?? c;
      out.set(
        c[0] + Math.random() * (o[0] - c[0]),
        c[1] + Math.random() * (o[1] - c[1]),
        c[2] + Math.random() * (o[2] - c[2]),
      );
      return;
    }
    case 'line': {
      const c = body.corner ?? [0, 0, 0];
      const d = body.difference ?? [0, 0, 0];
      const u = Math.random();
      out.set(c[0] + u * d[0], c[1] + u * d[1], c[2] + u * d[2]);
      return;
    }
    case 'sphere': {
      const ctr = body.center ?? [0, 0, 0];
      const rMin = body.minRadius ?? 0;
      const rMax = body.maxRadius ?? rMin;
      // Pick a random direction; pick a radius uniformly between min and max.
      const u = Math.random() * 2 - 1;
      const phi = Math.random() * Math.PI * 2;
      const sinTheta = Math.sqrt(1 - u * u);
      const r = rMin + Math.random() * Math.max(0, rMax - rMin);
      out.set(
        ctr[0] + r * sinTheta * Math.cos(phi),
        ctr[1] + r * u,
        ctr[2] + r * sinTheta * Math.sin(phi),
      );
      return;
    }
    case 'cylinder': {
      const origin = body.origin ?? [0, 0, 0];
      const basisU = body.basisU ?? [1, 0, 0];
      const basisV = body.basisV ?? [0, 0, 1];
      const diff = body.difference ?? [0, 1, 0];
      const rMin = body.minRadius ?? 0;
      const rMax = body.maxRadius ?? rMin;
      const r = rMin + Math.random() * Math.max(0, rMax - rMin);
      const theta = Math.random() * Math.PI * 2;
      const tHeight = Math.random();
      const cu = Math.cos(theta) * r;
      const cv = Math.sin(theta) * r;
      out.set(
        origin[0] + cu * basisU[0] + cv * basisV[0] + tHeight * diff[0],
        origin[1] + cu * basisU[1] + cv * basisV[1] + tHeight * diff[1],
        origin[2] + cu * basisU[2] + cv * basisV[2] + tHeight * diff[2],
      );
      return;
    }
    default:
      out.set(0, 0, 0);
  }
}
