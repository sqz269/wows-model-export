// Three.js particle-system renderer driven by the sidecar's `effects`
// block (Parsed Effect records from `assets.bin`).
//
// MVP scope: CPU-driven point-sprite emitter, one Three.Points instance
// per attachment. We read the parsed authoring data
// (rate ramp / initial position volume / size ramp / tint color curve /
// alpha ramp / force XYZ) and approximate WG's runtime semantics. Not
// bit-exact — see `reference/investigations/particle_work/
// particle_format_spec.md` for the canonical schema. The data here is
// authoritative enough for an inspector / preview rendering.
//
// Performance: each instance maintains a fixed-capacity ring buffer
// (one slot per particle). With <= 200 particles per emitter and <= 50
// active emitters, the JS-side cost stays under 1 ms / frame on a
// recent laptop. Heavier scenes (idle wakes + 4 fires) should switch to
// a GPU-driven backend; that's a future iteration.
//
// Coordinates: emitter origin is whatever transform the caller applies
// to the returned `Points` object. Particles are simulated in world
// space.

import * as THREE from 'three';
import type {
  ParticleAttachment,
  ParticleColor,
  ParticleRamp,
  ParticleRecord,
  ParticleSystem,
  ParticleValueGenerator,
  ParticleVariantVg,
  ParticleVgtPrototype,
} from '$lib/types/sidecar';
import { repoUrl } from '$lib/api';
import { loadDdsMipChain } from '$lib/dds';

const DEFAULT_PARTICLE_LIFETIME = 4.0; // seconds, when WG didn't author one
const ABSOLUTE_MAX_CAPACITY = 512; // hard cap per system
const DEFAULT_SIZE_M = 0.3; // metres — sane baseline if the
// particle didn't author a size
// generator
const HARD_MAX_EMIT_RATE_HZ = 200; // safety clamp on the per-frame
// particles-emitted count

/** Sample a 1D ``Ramp`` curve at parameter ``t ∈ [0, 1]``. */
function sampleRamp(ramp: ParticleRamp | undefined, t: number, fallback = 1): number {
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
function sampleColor(color: ParticleColor | undefined, t: number, out: Float32Array): void {
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
function sampleScalarVg(vg: ParticleValueGenerator | undefined, t = 0, fallback = 0): number {
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
interface ParticleClocks {
  particleAge: number;
  systemAge: number;
  systemActiveTime: number;
  particleSpeed: number;
  systemSpeed: number;
  particleIndex: number;
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
function sampleGenAxis(
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
function samplePosFromVariantVg(vg: ParticleVariantVg | undefined, out: THREE.Vector3): void {
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

// ---------------------------------------------------------------------------
// Per-system simulation
// ---------------------------------------------------------------------------

/**
 * One Three.Points emitter wrapping a single System inside a particle's
 * Effect record. Updates a ring buffer of particles each frame.
 */
class SystemRenderer {
  readonly points: THREE.Points;
  private capacity: number;
  private maxAge: number;
  // Record-level emission window (seconds). >0 ⇒ one-shot burst that
  // re-bursts after window+maxAge; <=0 ⇒ continuous emitter. See tick().
  private maxEmittingDuration: number;
  // SIZE model (RE 2026-06-04, build 12506899; memory
  // project-particle-runtime-eval-size-model). Engine:
  //   size = emitter.sizeGenerator (BASE, in METRES, per-particle)
  //        × ageScaleGenerator (per-particle multiplier)
  //        × Π scaler/resizer.sizeGenerator (per-frame multipliers, own axis)
  // NO ×15 on size. `psize[i]` caches the per-particle base × ageScale (both
  // usually linear→random, fixed at spawn); the scaler ramps are evaluated
  // per-frame on their parameterType axis. The prior code had this INVERTED
  // (scaler-as-base, sampled at a normalized [0,1] age).
  private emitterSizeGen: ParticleValueGenerator | undefined;
  private ageScaleGen: ParticleValueGenerator | undefined;
  private scalerGens: ParticleValueGenerator[] = [];
  // dampfer.velocityGenerator — a per-frame drag MULTIPLIER on the velocity's
  // contribution to position (1.0 → ~0). Undefined = no damping.
  private dampGen: ParticleValueGenerator | undefined;
  // Per-system u8 spawn counter → the particleIndex ramp axis (0..254 wrap).
  private spawnCounter = 0;
  // Creator (PSAT idx=12) — additive secondary burst layer, present on
  // ~12% of corpus systems. When present, the simulator uses creator's
  // VGs for spawning AND its rateRamp for emission. When absent, the
  // simulator falls through to the always-on emitter sub-struct below.
  private rateRamp: ParticleRamp | undefined;
  private initialPosVg: ParticleVariantVg | undefined;
  private initialVelVg: ParticleVariantVg | undefined;
  // Emitter sub-struct (System +0x0a0) — canonical always-on emitter
  // per the 2026-05-23 audit (12152 of 13737 systems have NO creator
  // and 100% of those have a populated emitter.rateGenerator). When
  // creator is absent we drive emission from here.
  private emitterRateVg: ParticleValueGenerator | undefined;
  private emitterPosVg: ParticleVariantVg | undefined;
  private emitterVelVg: ParticleVariantVg | undefined;
  private emitterActivePeriod: number;
  // Per-action driver fields.
  private tintColor: ParticleColor | undefined;
  private alphaRamp: ParticleRamp | undefined;
  private forceX: ParticleValueGenerator | undefined;
  private forceY: ParticleValueGenerator | undefined;
  private forceZ: ParticleValueGenerator | undefined;

  // Particle attribute arrays.
  private pos: Float32Array;
  private vel: Float32Array;
  private age: Float32Array; // age in seconds; -1 = empty slot
  private lifetime: Float32Array;
  private colorRGBA: Float32Array;
  private sizeArr: Float32Array;
  // Per-slot (CPU-only) size base (emitter × ageScale, metres) + the u8
  // particleIndex counter, both assigned at spawn. Consumed to produce
  // sizeArr each frame; not packed for the GPU.
  private psize: Float32Array;
  private pidx: Float32Array;
  private alive = 0; // count of currently-alive particles

  // Reusable scratch buffers for the geometry attributes (we update
  // each frame in-place).
  private posAttr: THREE.BufferAttribute;
  private colorAttr: THREE.BufferAttribute;
  private sizeAttr: THREE.BufferAttribute;
  /** Packed (compacted to the front, matching pos/color/size) age values
   *  for the GPU. Drives the fragment shader's atlas grid frame index.
   *  Kept separate from ``age[]`` (which is the per-slot CPU truth
   *  source). */
  private ageGpu: Float32Array;
  private ageAttr: THREE.BufferAttribute;

  // Fractional-particle accumulators, one per emission source. RE
  // (2026-05-29): the always-on emitter.rateGenerator is the PRIMARY source
  // and the PSAT creator is an ADDITIVE secondary burst — BOTH spawn. The
  // prior code used creator-XOR-emitter with creator precedence, which
  // under-emitted any system carrying both (this flak burst spawned 1
  // particle from the creator's ~0.8/s ramp instead of ~12 from the emitter's
  // 11/s). Sources with rate 0 contribute nothing.
  private emitAccum = 0;
  private creatorAccum = 0;
  private elapsed = 0;
  /** Tail-end of the alphaSetter ramp's time domain. Used to detect
   *  ramps that are keyed by system age (extending into 10s of seconds)
   *  vs particle age (within the particle's lifetime). When the tail
   *  is significantly > maxAge we drive the ramp by `elapsed` instead. */
  private alphaSetterIsSystemAge = false;
  private active = true;

  // Tmp scratch — avoids per-frame Vector3 allocations.
  private static readonly TMP_POS = new THREE.Vector3();
  private static readonly TMP_VEL = new THREE.Vector3();
  private static readonly TMP_COL = new Float32Array(4);
  // Reused per-particle clock scratch (mutated in tick/spawn; the per-particle
  // update loop and the emit/spawn phase run sequentially, never concurrently).
  private static readonly TMP_CLOCKS: ParticleClocks = {
    particleAge: 0,
    systemAge: 0,
    systemActiveTime: 0,
    particleSpeed: 0,
    systemSpeed: 0,
    particleIndex: 0,
  };

  /** Resolved ShaderMaterial. Owned per-instance so each system can bind
   *  its own DDS map without uniform clobbering between systems. */
  readonly material: THREE.ShaderMaterial;

  /** Live alive-particle count (read-only). Surface for the inspector
   *  overlay. */
  get aliveCount(): number {
    return this.alive;
  }
  /** Elapsed simulated time in seconds (read-only). */
  get elapsedSeconds(): number {
    return this.elapsed;
  }
  /** Configured ring-buffer capacity, in particles. */
  get particleCapacity(): number {
    return this.capacity;
  }
  /** Configured per-particle max age, in seconds. */
  get particleMaxAge(): number {
    return this.maxAge;
  }

  constructor(system: ParticleSystem, material: THREE.ShaderMaterial, maxEmittingDuration = 0) {
    this.material = material;
    this.maxEmittingDuration = maxEmittingDuration;
    const gen = system.general;
    this.maxAge = Math.max(0.05, gen?.maxParticleAge ?? DEFAULT_PARTICLE_LIFETIME);
    const desiredCap = Math.max(1, gen?.capacity ?? 32);
    this.capacity = Math.min(desiredCap, ABSOLUTE_MAX_CAPACITY);

    // Wire up component-action drivers. WG actions are evaluated in
    // declaration order (PCAT applied per-particle, PSAT applied
    // system-wide); for MVP we just collapse all actions into a single
    // bag of driver fields.
    for (const c of system.components) {
      const body = c.body ?? {};
      if (c.action === 'creator') {
        if (body.rateRamp) this.rateRamp = body.rateRamp as ParticleRamp;
        if (body.initialPositionGenerator)
          this.initialPosVg = body.initialPositionGenerator as ParticleVariantVg;
        if (body.initialVelocityGenerator)
          this.initialVelVg = body.initialVelocityGenerator as ParticleVariantVg;
      } else if (c.action === 'tint') {
        if (body.tint) this.tintColor = body.tint as ParticleColor;
      } else if (c.action === 'alphaSetter') {
        if (body.ramp) this.alphaRamp = body.ramp as ParticleRamp;
      } else if (c.action === 'scaler' || c.action === 'resizer') {
        if (body.sizeGenerator) this.scalerGens.push(body.sizeGenerator as ParticleValueGenerator);
      } else if (c.action === 'dampfer') {
        if (body.velocityGenerator)
          this.dampGen = body.velocityGenerator as ParticleValueGenerator;
      } else if (c.action === 'force') {
        if (body.forceXGenerator) this.forceX = body.forceXGenerator as ParticleValueGenerator;
        if (body.forceYGenerator) this.forceY = body.forceYGenerator as ParticleValueGenerator;
        if (body.forceZGenerator) this.forceZ = body.forceZGenerator as ParticleValueGenerator;
      }
    }
    // Capture the Emitter sub-struct fields. Used when no creator
    // component is present (the 88%-of-corpus case). Sample the rate
    // generator in SECONDS against ``elapsed % activePeriod`` (NOT
    // normalised [0,1] like the legacy creator path) — empirical: ramp
    // tail == activePeriod in 99.8% of corpus emitter ramps.
    this.emitterRateVg = system.emitter?.rateGenerator;
    this.emitterPosVg = system.emitter?.initialPositionGenerator;
    this.emitterVelVg = system.emitter?.initialVelocityGenerator;
    this.emitterActivePeriod = Math.max(0, system.emitter?.activePeriod ?? 0);
    // SIZE base (RE 2026-06-04): the emitter's sizeGenerator is the per-particle
    // BASE size in METRES; ageScaleGenerator is a per-particle life multiplier.
    // Both are typically linear (random) → sampled once at spawn into psize[].
    // The scaler/resizer ramps (scalerGens, captured above) are the per-frame
    // multipliers, evaluated on their own parameterType axes in tick(). NO ×15.
    this.emitterSizeGen = system.emitter?.sizeGenerator;
    this.ageScaleGen = system.emitter?.ageScaleGenerator;

    // Decide whether the alphaSetter ramp is keyed by particle age or
    // system age. Heuristic: if the last keyframe is well past the
    // particle's lifetime, the curve is in system-age seconds (fade-in/
    // out over the whole system lifetime — typical fire/flood DoT
    // pattern at 60s / 500s tails). Otherwise it's particle-age (e.g.
    // the alphaSetter for short impact sparks).
    if (this.alphaRamp?.points && this.alphaRamp.points.length > 0) {
      const last = this.alphaRamp.points[this.alphaRamp.points.length - 1].time;
      this.alphaSetterIsSystemAge = last > this.maxAge * 2;
    }

    this.pos = new Float32Array(this.capacity * 3);
    this.vel = new Float32Array(this.capacity * 3);
    this.age = new Float32Array(this.capacity);
    this.lifetime = new Float32Array(this.capacity);
    this.colorRGBA = new Float32Array(this.capacity * 4);
    this.sizeArr = new Float32Array(this.capacity);
    this.ageGpu = new Float32Array(this.capacity);
    this.psize = new Float32Array(this.capacity);
    this.pidx = new Float32Array(this.capacity);
    for (let i = 0; i < this.capacity; i++) this.age[i] = -1;

    // Geometry: one vertex per particle. Three.js's `Points` renders
    // every vertex as a quad facing the camera (when the material is a
    // PointsMaterial).
    const geom = new THREE.BufferGeometry();
    this.posAttr = new THREE.BufferAttribute(this.pos, 3);
    this.posAttr.setUsage(THREE.DynamicDrawUsage);
    this.colorAttr = new THREE.BufferAttribute(this.colorRGBA, 4);
    this.colorAttr.setUsage(THREE.DynamicDrawUsage);
    this.sizeAttr = new THREE.BufferAttribute(this.sizeArr, 1);
    this.sizeAttr.setUsage(THREE.DynamicDrawUsage);
    this.ageAttr = new THREE.BufferAttribute(this.ageGpu, 1);
    this.ageAttr.setUsage(THREE.DynamicDrawUsage);
    geom.setAttribute('position', this.posAttr);
    geom.setAttribute('color', this.colorAttr);
    geom.setAttribute('size', this.sizeAttr);
    geom.setAttribute('age', this.ageAttr);
    geom.setDrawRange(0, 0);
    this.points = new THREE.Points(geom, material);
    this.points.frustumCulled = false;
  }

  setActive(active: boolean): void {
    this.active = active;
  }

  /** Step the simulation by `dt` seconds. Updates the GPU buffers. */
  tick(dt: number): void {
    if (!this.active) {
      // Even when paused, decay existing particles so they don't sit
      // frozen. Optional — for MVP we just fully freeze.
      return;
    }
    this.elapsed += dt;

    // One-shot emission window + re-burst cycle (RE 2026-05-29). The record's
    // `maxEmittingDuration` bounds how long the effect emits; WoWS flak/
    // explosion effects burst ONCE (e.g. 1.1 s) then their particles fade over
    // `maxAge`. The inspector previously ignored this and emitted forever,
    // showing every flipbook frame at once. We now gate emission to the window
    // and, once the whole burst has dissipated (window + maxAge elapsed), reset
    // for a fresh one-shot so it loops cleanly for inspection.
    // `maxEmittingDuration <= 0` ⇒ continuous emitter (no gate), e.g.
    // persistent fire/smoke.
    const oneShot = this.maxEmittingDuration > 0;
    if (oneShot && this.elapsed >= this.maxEmittingDuration + this.maxAge) {
      for (let i = 0; i < this.capacity; i++) this.age[i] = -1;
      this.alive = 0;
      this.emitAccum = 0;
      this.creatorAccum = 0;
      this.elapsed = 0;
    }
    const emitting = !oneShot || this.elapsed <= this.maxEmittingDuration;

    // Per-particle update. WG's authoring convention: ramp + color
    // curves are keyed by particle age in *seconds*, not normalised
    // [0,1]. A 4.2-second fire particle samples its tint curve at
    // age=2.3s directly (not 2.3/4.2). Force generators usually use a
    // ramp parameter type of "particleAge" too; for the MVP we feed the
    // normalised ratio to those scalar generators and accept the
    // approximation.
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      this.age[i] += dt;
      if (this.age[i] >= this.lifetime[i]) {
        this.age[i] = -1;
        this.alive--;
        continue;
      }
      const age = this.age[i];
      // Per-particle clocks for the parameterType axis (RE 2026-06-04): ramps
      // are sampled on their own clock in SECONDS (or m/s, or the u8 index) —
      // NOT a normalized [0,1] age.
      const vx = this.vel[i * 3 + 0];
      const vy = this.vel[i * 3 + 1];
      const vz = this.vel[i * 3 + 2];
      const clocks = SystemRenderer.TMP_CLOCKS;
      clocks.particleAge = age;
      clocks.systemAge = this.elapsed;
      clocks.systemActiveTime =
        this.emitterActivePeriod > 0 ? this.elapsed % this.emitterActivePeriod : this.elapsed;
      clocks.particleSpeed = Math.hypot(vx, vy, vz);
      clocks.systemSpeed = 0;
      clocks.particleIndex = this.pidx[i];
      // Force integration (constants ignore the axis; ramps use parameterType).
      this.vel[i * 3 + 0] += sampleGenAxis(this.forceX, clocks, 0) * dt;
      this.vel[i * 3 + 1] += sampleGenAxis(this.forceY, clocks, 0) * dt;
      this.vel[i * 3 + 2] += sampleGenAxis(this.forceZ, clocks, 0) * dt;
      // dampfer: a per-frame drag multiplier on the velocity's displacement.
      const damp = this.dampGen ? sampleGenAxis(this.dampGen, clocks, 1) : 1;
      this.pos[i * 3 + 0] += this.vel[i * 3 + 0] * dt * damp;
      this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt * damp;
      this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt * damp;
      // Final opacity = tint.alpha(age) × alphaSetter(t). RE-CONFIRMED on build
      // 12506899 (decompiled FUN_140742af0 + FUN_1407423c0, agent-cross-checked):
      // the tint action does renderRec[0x34..0x40] *= tint.RGBA (alpha @0x40
      // included) and the alphaSetter does renderRec[0x40] *= ramp — BOTH
      // multiply, so the product below is correct. Do NOT make either term
      // "override" the other. (Engine also folds in the base-color alpha + the
      // per-component scaler-alpha, both ≤1 then clamped [0,1]; not modelled here
      // — at worst a slight over-bright vs engine.)
      sampleColor(this.tintColor, age, SystemRenderer.TMP_COL);
      const alphaT = this.alphaSetterIsSystemAge ? this.elapsed : age;
      const alpha = sampleRamp(this.alphaRamp, alphaT, 1) * SystemRenderer.TMP_COL[3];
      this.colorRGBA[i * 4 + 0] = SystemRenderer.TMP_COL[0];
      this.colorRGBA[i * 4 + 1] = SystemRenderer.TMP_COL[1];
      this.colorRGBA[i * 4 + 2] = SystemRenderer.TMP_COL[2];
      this.colorRGBA[i * 4 + 3] = alpha;
      // SIZE (RE 2026-06-04): per-particle base (emitter × ageScale, cached in
      // psize at spawn) × Π scaler multipliers, each on its own axis. Metres.
      let sz = this.psize[i];
      for (let s = 0; s < this.scalerGens.length; s++) {
        sz *= sampleGenAxis(this.scalerGens[s], clocks, 1);
      }
      this.sizeArr[i] = Math.max(0, sz);
    }

    // Emit from BOTH sources (RE-aligned, 2026-05-29): the always-on emitter
    // is the primary spawn source; the PSAT creator is an additive secondary
    // burst. Each carries its own fractional accumulator and its own
    // position/velocity volume generators; they share the capacity cap so the
    // system can't exceed `capacity`. A source whose rate is 0 spawns nothing.
    if (emitting) {
      if (this.emitterRateVg) {
        // Emitter ramp keyed in SECONDS against systemAge, sampled at
        // `elapsed mod activePeriod` (constant/linear VGs ignore t; ramp VGs
        // hold their last value past the tail; activePeriod==0 ⇒ raw elapsed).
        const t =
          this.emitterActivePeriod > 0 ? this.elapsed % this.emitterActivePeriod : this.elapsed;
        const eRate = sampleScalarVg(this.emitterRateVg, t, 0);
        this.emitAccum = this.emitFromSource(
          eRate,
          dt,
          this.emitAccum,
          this.emitterPosVg,
          this.emitterVelVg,
        );
      }
      if (this.rateRamp) {
        // Creator rate on the legacy normalised systemAge axis (an
        // approximation flagged in the RE notes — creator.rateRamp is really
        // in seconds — but visually negligible for the short corpus ramps).
        const period = Math.max(0.01, this.maxAge);
        const tNorm = (this.elapsed % period) / period;
        const cRate = sampleRamp(this.rateRamp, tNorm, 0);
        this.creatorAccum = this.emitFromSource(
          cRate,
          dt,
          this.creatorAccum,
          this.initialPosVg,
          this.initialVelVg,
        );
      }
    }

    // Update geometry attribute buffers + draw range. We pack the live
    // particles to the front; saves GPU vertex count vs. always drawing
    // `capacity` slots. ``ageGpu`` is always packed (unlike ``age[]``,
    // which stays slot-indexed as the CPU truth source — see field
    // comment) so the fragment shader's grid math reads the right value.
    let writeIdx = 0;
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      if (writeIdx !== i) {
        this.pos[writeIdx * 3 + 0] = this.pos[i * 3 + 0];
        this.pos[writeIdx * 3 + 1] = this.pos[i * 3 + 1];
        this.pos[writeIdx * 3 + 2] = this.pos[i * 3 + 2];
        this.colorRGBA[writeIdx * 4 + 0] = this.colorRGBA[i * 4 + 0];
        this.colorRGBA[writeIdx * 4 + 1] = this.colorRGBA[i * 4 + 1];
        this.colorRGBA[writeIdx * 4 + 2] = this.colorRGBA[i * 4 + 2];
        this.colorRGBA[writeIdx * 4 + 3] = this.colorRGBA[i * 4 + 3];
        this.sizeArr[writeIdx] = this.sizeArr[i];
      }
      this.ageGpu[writeIdx] = this.age[i];
      writeIdx++;
    }
    this.points.geometry.setDrawRange(0, writeIdx);
    this.posAttr.needsUpdate = true;
    this.colorAttr.needsUpdate = true;
    this.sizeAttr.needsUpdate = true;
    this.ageAttr.needsUpdate = true;
  }

  /** Spawn whole particles from one emission source at ``rate`` Hz, carrying
   *  the fractional remainder in ``accum`` across frames. Returns the updated
   *  accumulator. Honors the shared capacity cap (so multiple sources can't
   *  overflow the ring buffer). */
  private emitFromSource(
    rate: number,
    dt: number,
    accum: number,
    posVg: ParticleVariantVg | undefined,
    velVg: ParticleVariantVg | undefined,
  ): number {
    if (rate <= 0) return accum;
    rate = Math.min(rate, HARD_MAX_EMIT_RATE_HZ);
    accum += rate * dt;
    while (accum >= 1 && this.alive < this.capacity) {
      accum -= 1;
      this.spawnParticle(posVg, velVg);
    }
    // Don't let the accumulator run away while at capacity (avoids a burst
    // when slots free up).
    if (this.alive >= this.capacity) accum = Math.min(accum, 1);
    return accum;
  }

  private spawnParticle(
    posVg: ParticleVariantVg | undefined,
    velVg: ParticleVariantVg | undefined,
  ): void {
    // Find an empty slot.
    let slot = -1;
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) {
        slot = i;
        break;
      }
    }
    if (slot < 0) return;
    samplePosFromVariantVg(posVg, SystemRenderer.TMP_POS);
    samplePosFromVariantVg(velVg, SystemRenderer.TMP_VEL);
    this.pos[slot * 3 + 0] = SystemRenderer.TMP_POS.x;
    this.pos[slot * 3 + 1] = SystemRenderer.TMP_POS.y;
    this.pos[slot * 3 + 2] = SystemRenderer.TMP_POS.z;
    this.vel[slot * 3 + 0] = SystemRenderer.TMP_VEL.x;
    this.vel[slot * 3 + 1] = SystemRenderer.TMP_VEL.y;
    this.vel[slot * 3 + 2] = SystemRenderer.TMP_VEL.z;
    this.age[slot] = 0;
    this.lifetime[slot] = this.maxAge;
    sampleColor(this.tintColor, 0, SystemRenderer.TMP_COL);
    const alphaT0 = this.alphaSetterIsSystemAge ? this.elapsed : 0;
    const alpha = sampleRamp(this.alphaRamp, alphaT0, 1) * SystemRenderer.TMP_COL[3];
    this.colorRGBA[slot * 4 + 0] = SystemRenderer.TMP_COL[0];
    this.colorRGBA[slot * 4 + 1] = SystemRenderer.TMP_COL[1];
    this.colorRGBA[slot * 4 + 2] = SystemRenderer.TMP_COL[2];
    this.colorRGBA[slot * 4 + 3] = alpha;
    // Per-particle u8 spawn index (the particleIndex ramp axis) + the cached
    // size base = emitter.sizeGenerator (METRES) × ageScale, sampled ONCE here
    // (both are typically linear→random). Scaler multipliers are per-frame.
    this.pidx[slot] = this.spawnCounter;
    this.spawnCounter = (this.spawnCounter + 1) & 0xff;
    const sc = SystemRenderer.TMP_CLOCKS;
    sc.particleAge = 0;
    sc.systemAge = this.elapsed;
    sc.systemActiveTime =
      this.emitterActivePeriod > 0 ? this.elapsed % this.emitterActivePeriod : this.elapsed;
    sc.particleSpeed = 0;
    sc.systemSpeed = 0;
    sc.particleIndex = this.pidx[slot];
    const base = sampleGenAxis(this.emitterSizeGen, sc, DEFAULT_SIZE_M);
    const ageScale = this.ageScaleGen ? sampleGenAxis(this.ageScaleGen, sc, 1) : 1;
    this.psize[slot] = base * ageScale;
    let sz0 = this.psize[slot];
    for (let s = 0; s < this.scalerGens.length; s++) {
      sz0 *= sampleGenAxis(this.scalerGens[s], sc, 1);
    }
    this.sizeArr[slot] = Math.max(0, sz0);
    this.alive++;
  }

  dispose(): void {
    this.points.geometry.dispose();
    this.material.dispose();
    // Texture lifetime is managed by the ParticleScene's texture cache
    // (shared across systems that point at the same DDS) — don't dispose
    // it here.
  }
}

// ---------------------------------------------------------------------------
// Per-system point-sprite material
// ---------------------------------------------------------------------------

interface ParticleMaterialOptions {
  /** PS_RBT label (10 values). Drives the THREE.* blend equation. */
  blendType?: string;
  /** Sprite-sheet grid (animation block). Defaults to 1x1 (no animation). */
  framesPerX?: number;
  framesPerY?: number;
  /** Active frame range [begin, end); ``end - begin`` is the total frame
   *  count animated through. Defaults to 0..0 (no animation). */
  framesRangeBegin?: number;
  framesRangeEnd?: number;
  /** Animation cycle length in seconds. 0 disables animation. */
  animationPeriod?: number;
  /** PS_PAT animation type (renderer/animation.animationType). The flipbook
   *  grid is applied ONLY for ``framesPlayback`` / ``motionVectors``;
   *  ``noAnimation`` (the engine default) leaves framesPerX/Y/range/period as
   *  vestigial authoring data and must sample the FULL texture — gridding a
   *  single-frame sprite (e.g. a logo) crops it to mostly-transparent cells and
   *  it renders as nothing. Accepts the named or raw ``type_N`` schema form. */
  animationType?: string;
  /** Manifest-resolved atlas UV rect ``[u0, v0, u1, v1]`` (sidecar's
   *  ``textureAtlas0``). When set, the fragment shader maps the (already
   *  grid-sampled) UV through this rect — composes with the grid rather
   *  than replacing it, because ~20% of atlas-mapped systems also carry
   *  a non-trivial framesPerX*Y grid (the rect bounds the whole grid in
   *  the parent atlas page). */
  atlasRect?: [number, number, number, number];
  /** PS_RBT modes that read textureName0 as a grayscale luminance map
   *  and remap it via a color LUT in textureName1 (GRADIENT_MAP,
   *  UNDERWATER_GRADIENT_MAP). The fragment shader switches to
   *  ``texture2D(lut, vec2(base.r, 0.5)).rgb`` when ``useLut=1``. */
  useLut?: boolean;
  /** PS_RLT lighting mode (renderer.lightingType). For the lightmapping
   *  modes (``lightmapping4Way`` / ``lightmappingHL2``) the bound texture
   *  is a DIRECTIONAL LIGHTMAP, not albedo: its RGB are 3 grayscale
   *  renders of the same sprite baked-lit from 3 fixed HL2-basis
   *  directions, and A is the opacity mask (RE-verified 2026-05-29 — see
   *  memory `project-particle-lm-lightmap`). The fragment shader then
   *  reconstructs one lit luminance from a sun direction instead of
   *  showing the RGB directly (which reads as a wrong rainbow). */
  lightingType?: string;
  /** Motion-vector flipbook blending (animation.motionVectorsDistortion).
   *  The per-pixel optical-flow warp magnitude; scales the UV displacement
   *  decoded from the `_MVEA` texture's (G,B) channels. 0 → pure cross-fade
   *  (no spatial warp), still smoother than a hard frame step. */
  motionVectorsDistortion?: number;
  /** animation.useEmissionAlphaFromMV — when set, the `_MVEA` texture's R
   *  channel drives emission and its A channel drives opacity. */
  useEmissionAlphaFromMV?: boolean;
}

/**
 * Map PS_RBT enum label -> THREE.js blending parameters. Six modes have
 * direct equivalents; GRADIENT_MAP / UNDERWATER_GRADIENT_MAP additionally
 * trigger LUT remap (driven by ``useLut`` in the material options, where
 * the renderer's ``textureName1`` is bound as the color ramp). SHIMMER
 * and DEFORM_WATER_SURFACE still lack bespoke shaders; they use
 * AdditiveBlending as a visually-louder placeholder than the previous
 * NormalBlending one (most shimmer/foam authoring is bright-on-water,
 * so additive at least makes them visible).
 */
function blendConfigForPsRbt(label: string | undefined): {
  blending: THREE.Blending;
  blendSrc?: THREE.BlendingSrcFactor;
  blendDst?: THREE.BlendingDstFactor;
} {
  switch (label) {
    case 'ADDITIVE':
    case 'ADDITIVE_WATER_SURFACE':
      return { blending: THREE.AdditiveBlending };
    case 'BLENDED':
    case 'BLENDED_UNDERWATER':
    case 'BLENDED_WATER_SURFACE':
      return { blending: THREE.NormalBlending };
    case 'BLENDED_GLOW':
      // Premultiplied-additive: dst stays, src adds scaled by its alpha.
      return {
        blending: THREE.CustomBlending,
        blendSrc: THREE.SrcAlphaFactor,
        blendDst: THREE.OneFactor,
      };
    case 'GRADIENT_MAP':
    case 'UNDERWATER_GRADIENT_MAP':
      // RE-corrected 2026-05-29 (Ghidra + DXBC, two independent agents): the
      // engine renders GRADIENT_MAP particles PREMULTIPLIED alpha-over
      // (Src=ONE, Dst=INV_SRC_ALPHA), depth-sorted back-to-front,
      // depth-write off — NOT additive. Ghidra: blendType=7 sits in the
      // depth-sort bitmask 0x2e8 (order-dependent ⇒ alpha, not additive).
      // DXBC: the PS emits premultiplied RGB and only zeroes alpha for the
      // additive blend bit (which GRADIENT_MAP does not set). The warm
      // gradient glow rides inside the alpha so it adds light where opacity
      // is partial, while the smoke body OCCLUDES. The prior additive
      // assumption made smoke puffs glow instead of darken.
      return {
        blending: THREE.CustomBlending,
        blendSrc: THREE.OneFactor,
        blendDst: THREE.OneMinusSrcAlphaFactor,
      };
    case 'SHIMMER':
    case 'DEFORM_WATER_SURFACE':
      // Need bespoke shader paths; AdditiveBlending placeholder until
      // they land (water-deform / refraction effects are usually bright
      // on dark water — additive at least keeps them visible).
      return { blending: THREE.AdditiveBlending };
    default:
      // Unknown / missing label — keep the historical additive default
      // so behaviour matches the pre-blendType-RE'd renderer.
      return { blending: THREE.AdditiveBlending };
  }
}

/** PS_RBT labels that should sample textureName1 as a 1D color LUT and
 *  remap textureName0's red channel through it. */
const PS_RBT_LUT_MODES = new Set(['GRADIENT_MAP', 'UNDERWATER_GRADIENT_MAP']);

/** PS_RLT labels whose textureName0 is a directional lightmap (RGB = 3
 *  baked light-direction renders, A = opacity), NOT albedo. The fragment
 *  shader reconstructs a single lit luminance against the sun direction
 *  instead of sampling RGB as colour. See memory
 *  `project-particle-lm-lightmap`. */
const PS_RLT_LIGHTMAP_MODES = new Set(['lightmapping4Way', 'lightmappingHL2']);

/**
 * Build a per-system point-sprite material. Each SystemRenderer owns
 * its own copy so per-system uniforms (atlas rect, frame grid, texture
 * binding) don't clobber siblings.
 *
 * Fragment shader paths:
 *   useMap=0                 -> procedural soft-disc falloff (no texture)
 *   useMap=1, useAtlasRect=1 -> sample texture at lerp(rect.xy, rect.zw,
 *                               gl_PointCoord)  (manifest atlas region)
 *   useMap=1, grid>1, grid frames>0, period>0 -> animate frame index from
 *                               per-particle vAge, sample cell within
 *                               framesPerX x framesPerY grid
 *   useMap=1 otherwise       -> sample full texture at gl_PointCoord
 */
function buildParticleMaterial(opts: ParticleMaterialOptions = {}): THREE.ShaderMaterial {
  const blend = blendConfigForPsRbt(opts.blendType);
  const rect = opts.atlasRect;
  // PS_PAT gate: only framesPlayback / motionVectors actually flip through the
  // framesPerX*Y grid. noAnimation (PS_PAT type_0) keeps the grid fields as
  // vestigial authoring data — applying the grid then crops a single-frame
  // sprite into 1/N mostly-transparent cells (a logo renders as nothing). Accept
  // both the named and raw `type_N` forms; default-on when absent so real
  // flipbooks never regress.
  const at = opts.animationType;
  const gridEnabled = at !== 'noAnimation' && at !== 'type_0';
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      map: { value: null as THREE.Texture | null },
      useMap: { value: 0 },
      // LUT (textureName1) for GRADIENT_MAP / UNDERWATER_GRADIENT_MAP.
      // useLut=1 routes the fragment shader through the LUT remap.
      // Initialised to 0 — flipped to 1 by bindLutTexture only after
      // the LUT DDS loads successfully (BC6H HDR isn't supported by
      // the worker yet; failing-but-still-set useLut would render
      // black against the null sampler).
      lut: { value: null as THREE.Texture | null },
      useLut: { value: 0 },
      // Manifest atlas rect (u0, v0, u1, v1). useAtlasRect=1 lerps the
      // (already grid-sampled) UV through the rect — composes with grid.
      atlasRect: {
        value: new THREE.Vector4(rect?.[0] ?? 0, rect?.[1] ?? 0, rect?.[2] ?? 1, rect?.[3] ?? 1),
      },
      useAtlasRect: { value: rect ? 1 : 0 },
      // Animation grid (framesPerX, framesPerY) + range (begin, end) +
      // period. The shader skips the grid sample unless useFrameGrid (PS_PAT
      // != noAnimation) AND framesPerX*Y > 1 AND (end - begin) > 0 AND period > 0.
      framesPerXY: {
        value: new THREE.Vector2(opts.framesPerX ?? 1, opts.framesPerY ?? 1),
      },
      frameRange: {
        value: new THREE.Vector2(opts.framesRangeBegin ?? 0, opts.framesRangeEnd ?? 0),
      },
      animationPeriod: { value: opts.animationPeriod ?? 0 },
      // PS_PAT gate (gridEnabled): suppress the flipbook grid for noAnimation
      // so a single-frame sprite (logo) shows whole instead of a cropped cell.
      useFrameGrid: { value: gridEnabled ? 1 : 0 },
      // Directional-lightmap (PS_RLT) reconstruction. uLightingMode=1 when
      // the texture is an `_LM` lightmap (lightmapping4Way/HL2); the
      // fragment shader then treats `map` RGB as a 3-direction HL2-basis
      // lightmap reconstructed against uSunDirWorld, not albedo. The sun
      // dir defaults to the scene's key DirectionalLight (scene.ts:121-122,
      // positioned at (50,80,50)) so particle lighting matches the hull.
      uLightingMode: {
        value: PS_RLT_LIGHTMAP_MODES.has(opts.lightingType ?? '') ? 1 : 0,
      },
      uSunDirWorld: { value: new THREE.Vector3(50, 80, 50).normalize() },
      // Motion-vector flipbook blending (`_MVEA`). useMv is flipped to 1 by
      // bindMvTexture once the MV DDS loads; the shader then samples two
      // adjacent frames, warps each along the MV (G,B) optical-flow field,
      // and cross-fades them — replacing the hard age-driven frame step.
      // Math RE'd instruction-for-instruction from particles.win.dx11.fxo.
      mvMap: { value: null as THREE.Texture | null },
      useMv: { value: 0 },
      mvDistortion: { value: opts.motionVectorsDistortion ?? 0 },
      useEmissionAlphaFromMV: { value: opts.useEmissionAlphaFromMV ? 1 : 0 },
      // Relit-smoke brightness scale (RE 2026-05-29). The engine multiplies
      // the relit lightmap luminance by sunColor/(luma(sunColor)+1) (a Reinhard
      // normalization ≈ 0.5 for a white sun) × g_particleLightingFactor before
      // the per-particle tint. The webview's scene sun is white and we don't
      // carry the lighting factor, so 0.5 approximates that attenuation — it's
      // what keeps the relit smoke DARK (occluding) rather than ~2× too bright
      // once the authored HDR tint (e.g. (2,2,2)) is applied. Lightmapping
      // path only.
      uSmokeLightScale: { value: 0.5 },
      // Premultiplied-alpha output (RE 2026-05-29). GRADIENT_MAP /
      // UNDERWATER_GRADIENT_MAP blend premultiplied alpha-over in the engine
      // (Src=ONE, Dst=INV_SRC_ALPHA), so the fragment shader must premultiply
      // its RGB by the output alpha. Every other blend mode outputs straight
      // (non-premultiplied) colour as before. Keyed off blendType — the two
      // gradient modes are exactly PS_RBT_LUT_MODES.
      uPremultiply: {
        value: PS_RBT_LUT_MODES.has(opts.blendType ?? '') ? 1 : 0,
      },
      // Warm "detonation glow" strength for the lightmapping + GRADIENT_MAP
      // path. RE (DXBC) shows the engine pins the ramp lookup to U=0 (the
      // ramp's brightest texel) in lightmapping mode and adds it as an
      // emissive term scaled by a per-particle intensity (v10.x) the parser
      // doesn't carry — approximated by this constant. 0.15 keeps the puff a
      // grey occluding smoke body with a warm core (vs a uniformly tan blob at
      // higher values), matching the flak-burst look.
      uGlowStrength: { value: 0.15 },
    },
    vertexShader: /* glsl */ `
      attribute vec4 color;
      attribute float size;
      attribute float age;
      varying vec4 vColor;
      varying float vAge;

      void main() {
        vColor = color;
        vAge = age;
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mvPosition;
        // size is metres (world space); divide by distance for perspective.
        gl_PointSize = max(1.0, size * 200.0 / -mvPosition.z);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform sampler2D map;
      uniform float useMap;
      uniform sampler2D lut;
      uniform float useLut;
      uniform vec4 atlasRect;
      uniform float useAtlasRect;
      uniform vec2 framesPerXY;
      uniform vec2 frameRange;
      uniform float animationPeriod;
      uniform float useFrameGrid;
      uniform float uLightingMode;
      uniform vec3 uSunDirWorld;
      uniform sampler2D mvMap;
      uniform float useMv;
      uniform float mvDistortion;
      uniform float useEmissionAlphaFromMV;
      uniform float uPremultiply;
      uniform float uGlowStrength;
      uniform float uSmokeLightScale;
      varying vec4 vColor;
      varying float vAge;

      void main() {
        vec4 base;
        vec3 glow = vec3(0.0);   // additive warm glow (gradient+lightmapping)
        if (useMap > 0.5) {
          vec2 local = gl_PointCoord;
          float fx = framesPerXY.x;
          float fy = framesPerXY.y;
          float total = frameRange.y - frameRange.x;
          bool animated = (useFrameGrid > 0.5 && fx * fy > 1.0 && total > 0.0 && animationPeriod > 0.0);
          float mvEmissive = 0.0;   // additive emission from _MVEA.R (if on)

          if (animated && useMv > 0.5) {
            // Motion-vector flipbook blend (WG _MVEA): sample the two
            // adjacent frames, warp each along the per-pixel optical-flow
            // field stored in the MV texture's (G,B) channels, and cross-
            // fade by the inter-frame fraction. Decode is (G,B)*2-1;
            // mvDistortion scales the warp. RE'd instruction-for-instruction
            // from particles.win.dx11.fxo (20 motion-vector PS permutations,
            // identical math). Replaces the hard age-driven frame step.
            float fps = total / animationPeriod;
            float idxF = mod(vAge * fps, total);
            float f = fract(idxF);
            float n0 = floor(idxF) + frameRange.x;
            float n1 = mod(floor(idxF) + 1.0, total) + frameRange.x;
            vec2 grid = vec2(fx, fy);
            vec2 cell0 = (vec2(mod(n0, fx), floor(n0 / fx)) + local) / grid;
            vec2 cell1 = (vec2(mod(n1, fx), floor(n1 / fx)) + local) / grid;
            vec2 mv0 = texture2D(mvMap, cell0).gb * 2.0 - 1.0;
            vec2 mv1 = texture2D(mvMap, cell1).gb * 2.0 - 1.0;
            vec2 uv0 = cell0 - mv0 * f * mvDistortion;
            vec2 uv1 = cell1 + mv1 * (1.0 - f) * mvDistortion;
            base = mix(texture2D(map, uv0), texture2D(map, uv1), f);
            if (useEmissionAlphaFromMV > 0.5) {
              // _MVEA.R = emission, .A = opacity — sampled at the warped
              // UVs, lerped by f. Opacity overrides the base alpha; emission
              // is ADDED to the final colour below (the engine replaces in
              // its non-lit permutation; adding lets it compose with the
              // lightmap relight without erasing it).
              vec4 e0 = texture2D(mvMap, uv0);
              vec4 e1 = texture2D(mvMap, uv1);
              base.a = mix(e0.a, e1.a, f);
              mvEmissive = mix(e0.r, e1.r, f);
            }
          } else {
            // Hard age-driven frame step (framesPlayback / no MV texture),
            // composed with the manifest atlas-rect mapping when present.
            vec2 puv = local;
            if (animated) {
              float fps = total / animationPeriod;
              float idx = mod(floor(vAge * fps), total) + frameRange.x;
              puv = (vec2(mod(idx, fx), floor(idx / fx)) + puv) / vec2(fx, fy);
            }
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
          }
          if (uLightingMode > 0.5) {
            // _LM directional lightmap (PS_RLT lightmapping4Way/HL2):
            // base.rgb are 3 grayscale renders of the same sprite baked-lit
            // from 3 fixed HL2-basis directions; base.a is the opacity mask.
            // Showing rgb directly reads as a wrong rainbow — instead blend
            // the 3 directional renders by how strongly each basis direction
            // faces the sun, recovering "the sprite lit from the sun".
            //
            // Point sprites are screen-aligned, so the sprite tangent frame
            // is the view-space axes (tangent +X, bitangent +Y, normal +Z
            // toward camera). viewMatrix is auto-injected by three.js and
            // updates every frame, so transforming the world sun dir into
            // view space gives a live relight with no per-frame uniform
            // plumbing.
            vec3 sunV = normalize((viewMatrix * vec4(uSunDirWorld, 0.0)).xyz);
            // Half-Life-2 radiosity-normal-map basis (tangent space).
            const vec3 B0 = vec3(-0.40824829, -0.70710678, 0.57735027);
            const vec3 B1 = vec3(-0.40824829,  0.70710678, 0.57735027);
            const vec3 B2 = vec3( 0.81649658,  0.0,        0.57735027);
            vec3 w = vec3(max(0.0, dot(B0, sunV)),
                          max(0.0, dot(B1, sunV)),
                          max(0.0, dot(B2, sunV)));
            w *= w;                       // HL2 weighting is dot^2
            float wsum = w.x + w.y + w.z;
            float flatLum = (base.r + base.g + base.b) / 3.0;
            // Energy-normalised directional blend; fall back to the flat
            // average when the sun is edge-on/behind (wsum~0) so smoke never
            // goes fully black.
            float lit = wsum > 1e-4 ? dot(w, base.rgb) / wsum : flatLum;
            // Ambient floor (engine carries a lightingAmbient term) — keeps
            // back-lit sprites visible rather than crushing them to zero.
            lit = mix(lit, flatLum, 0.30);
            // Sun-color Reinhard normalization × lighting factor (RE): keeps
            // the relit smoke DARK so it occludes, rather than ~2x too bright
            // once the authored HDR tint multiplies in.
            lit *= uSmokeLightScale;
            base = vec4(vec3(lit), base.a);
          }
          if (useLut > 0.5) {
            if (uLightingMode > 0.5) {
              // GRADIENT_MAP + lightmapping (RE 2026-05-29, DXBC): the engine
              // PINS the ramp lookup to U=0 (the ramp's brightest texel) in
              // lightmapping mode — the gradient does NOT recolor the sprite by
              // luminance here (that's the lambert path). The brightest ramp
              // colour is instead added as a warm emissive "detonation" glow on
              // top of the relit smoke body, OUTSIDE the per-particle tint
              // (engine: rgb = base*lit + emis*v10.x). Shaped by the output
              // alpha via the premultiply below; the dim relit body stays the
              // occluding smoke.
              vec4 g = texture2D(lut, vec2(0.0, 0.5));
              glow = g.rgb * g.a * uGlowStrength;
            } else {
              // Lambert GRADIENT_MAP: luminance-keyed recolor (engine lambert
              // path) — sweep the ramp by the sprite luminance (base.r).
              base = vec4(texture2D(lut, vec2(base.r, 0.5)).rgb, base.a);
            }
          }
          // _MVEA emission (when useEmissionAlphaFromMV): additive glow on
          // top of the lit/remapped colour. Zero when not enabled.
          base.rgb += vec3(mvEmissive);
        } else {
          vec2 c = gl_PointCoord - vec2(0.5);
          float r = length(c) * 2.0;
          if (r > 1.0) discard;
          // Soft circular falloff (squared).
          float a = (1.0 - r * r);
          base = vec4(1.0, 1.0, 1.0, a);
        }
        float outA = vColor.a * base.a;
        vec3 outRgb = vColor.rgb * base.rgb + glow;
        if (uPremultiply > 0.5) {
          // Premultiplied alpha-over (matches the engine's premultiplied PS
          // output + One/INV_SRC_ALPHA blend). The glow (outRgb may exceed 1)
          // adds light where outA < 1, while the body occludes.
          gl_FragColor = vec4(outRgb * outA, outA);
        } else {
          // Straight output for additive / normal blends — the blend equation
          // applies the alpha weighting itself.
          gl_FragColor = vec4(outRgb, outA);
        }
      }
    `,
    blending: blend.blending,
    transparent: true,
    depthWrite: false,
  });
  if (blend.blendSrc !== undefined) mat.blendSrc = blend.blendSrc;
  if (blend.blendDst !== undefined) mat.blendDst = blend.blendDst;
  return mat;
}

// ---------------------------------------------------------------------------
// Public API — one ParticleScene per ShipViewer instance
// ---------------------------------------------------------------------------

export interface ParticleAttachmentHandle {
  attachment: ParticleAttachment;
  group: THREE.Group;
  /** Per-system simulators inside the attachment. */
  systems: SystemRenderer[];
  /** The parsed source record this attachment renders. Carries the
   *  authoring data the UI inspector needs (renderer.textureName0,
   *  general.capacity, components[].action, …) without poking through
   *  SystemRenderer internals. */
  record: ParticleRecord;
  active: boolean;
}

/**
 * Manages the scene-level particle layer for one ship: a root group +
 * one sub-group per attachment. Created when the sidecar is loaded;
 * disposed when the ship is unloaded.
 *
 * Texture lifetime: DDS maps referenced by the particle systems are
 * loaded on demand and cached in `textureCache` keyed by absolute URL.
 * Two systems pointing at the same `Fire01.dds` share the THREE.Texture
 * instance and the cache disposes them all on `dispose()`.
 */
export class ParticleScene {
  readonly root: THREE.Group;
  private attachments = new Map<string, ParticleAttachmentHandle>();
  private lastTickMs = -1;
  /** WebGL renderer used to issue DDS compressed-texture uploads.
   *  Provided once via `setRenderer`. Until set, particle systems load
   *  with the procedural-disc fallback. */
  private renderer: THREE.WebGLRenderer | null = null;
  /** Cache: absolute URL → in-flight or resolved THREE.Texture. Shared
   *  across emitters so duplicate `Fire01.dds` refs upload once. */
  private textureCache = new Map<string, Promise<THREE.Texture | null>>();

  constructor(renderer?: THREE.WebGLRenderer) {
    this.root = new THREE.Group();
    this.root.name = 'ParticleEffects';
    if (renderer) this.renderer = renderer;
  }

  /** Provide the WebGL renderer used to upload DDS textures. Safe to
   *  call after construction (idempotent — subsequent calls update the
   *  reference but already-cached textures stay valid since DDS uploads
   *  bind to the GL context not a specific renderer instance). */
  setRenderer(renderer: THREE.WebGLRenderer): void {
    this.renderer = renderer;
  }

  /** Build the scene from a sidecar's `effects` block. Returns the
   *  flat list of attachment handles for UI binding. */
  build(
    attachments: ParticleAttachment[],
    particles: Record<string, ParticleRecord>,
    resolveNodePosition: (attachment: ParticleAttachment) => THREE.Vector3 | null,
  ): ParticleAttachmentHandle[] {
    this.clear();
    const handles: ParticleAttachmentHandle[] = [];
    for (let i = 0; i < attachments.length; i++) {
      const a = attachments[i];
      const rec = particles[a.particle_path];
      if (!rec) continue;
      const grp = new THREE.Group();
      grp.name = `effect:${a.group}:${a.node}`;
      const anchor = resolveNodePosition(a);
      if (anchor) {
        grp.position.copy(anchor);
      } else {
        // No bone match — stage the un-resolvable effect on a raised
        // platform above the ship so the authoring data is clearly
        // visible. We arrange the unresolved effects in a grid:
        //
        //   Y = 60m   (well above the highest mast at ~46m on Montana)
        //   X spread = -30..+30m  (covers the typical hull beam)
        //   Z spread = -120..+120m (covers the hull length)
        //
        // Indexed deterministically so the same effect always lands at
        // the same point — easier to reason about while inspecting.
        const colCount = 6;
        const col = i % colCount;
        const row = Math.floor(i / colCount);
        const x = (col - (colCount - 1) / 2) * 8;
        const z = (row - 5) * 8;
        grp.position.set(x, 60, z);
      }
      this.root.add(grp);
      const systems: SystemRenderer[] = [];
      for (const sys of rec.systems) {
        const r = sys.renderer;
        const anim = sys.animation;
        // Texture source: prefer the direct DDS URL when present (the
        // texture was extracted as its own file); otherwise route
        // through the manifest atlas mapping (the .tga ref resolves to
        // a named region inside a shared atlas page — the rect uniform
        // narrows gl_PointCoord into that sub-region). Both paths
        // compose with the animation grid if framesPerX*Y > 1.
        const useAtlas = !r?.textureUrl0 && !!r?.textureAtlas0;
        const useLut = !!r?.blendType && PS_RBT_LUT_MODES.has(r.blendType) && !!r?.textureUrl1;
        // Motion-vector flipbook blend only when the engine authored it
        // (animationType === 'motionVectors') AND the `_MVEA` DDS resolved.
        const useMv = anim?.animationType === 'motionVectors' && !!anim?.motionVectorsTextureUrl;
        const mat = buildParticleMaterial({
          blendType: r?.blendType,
          lightingType: r?.lightingType,
          framesPerX: anim?.framesPerX,
          framesPerY: anim?.framesPerY,
          framesRangeBegin: anim?.framesRangeBegin,
          framesRangeEnd: anim?.framesRangeEnd,
          animationPeriod: anim?.animationPeriod,
          animationType: anim?.animationType,
          atlasRect: useAtlas ? r!.textureAtlas0!.rect : undefined,
          useLut,
          motionVectorsDistortion: useMv ? anim?.motionVectorsDistortion : undefined,
          useEmissionAlphaFromMV: useMv ? anim?.useEmissionAlphaFromMV : undefined,
        });
        const renderer = new SystemRenderer(sys, mat, rec.maxEmittingDuration);
        renderer.setActive(false); // start inactive — UI toggles on
        grp.add(renderer.points);
        systems.push(renderer);
        // Texture binding: direct URL takes precedence; otherwise load
        // the manifest atlas page (the rect is already in the material's
        // uniforms via buildParticleMaterial).
        const texPath = r?.textureUrl0 ?? (useAtlas ? r!.textureAtlas0!.page : undefined);
        if (texPath) {
          void this.bindTexture(mat, texPath);
        }
        if (useLut && r?.textureUrl1) {
          void this.bindLutTexture(mat, r.textureUrl1);
        }
        if (useMv && anim?.motionVectorsTextureUrl) {
          void this.bindMvTexture(mat, anim.motionVectorsTextureUrl);
        }
      }
      const handle: ParticleAttachmentHandle = {
        attachment: a,
        group: grp,
        systems,
        record: rec,
        active: false,
      };
      const key = `${a.group}:${a.node}:${i}`;
      this.attachments.set(key, handle);
      handles.push(handle);
    }
    return handles;
  }

  /** Resolve a workspace-relative DDS path through the texture cache
   *  and bind it onto `material.uniforms.map`. Idempotent per URL. */
  private async bindTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsMipChain([url], false, r).catch((err) => {
        console.warn('[particles] DDS load failed', workspaceRelPath, err);
        return null;
      });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.map.value = tex;
    material.uniforms.useMap.value = 1;
    material.needsUpdate = true;
  }

  /** Bind ``workspaceRelPath`` as the LUT sampler used by GRADIENT_MAP /
   *  UNDERWATER_GRADIENT_MAP. Same cache as ``bindTexture`` — many fire
   *  systems share the same ``fire_yellow_*.dds`` ramp. */
  private async bindLutTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsMipChain([url], false, r).catch((err) => {
        console.warn('[particles] LUT DDS load failed', workspaceRelPath, err);
        return null;
      });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.lut.value = tex;
    material.uniforms.useLut.value = 1;
    material.needsUpdate = true;
  }

  /** Bind ``workspaceRelPath`` as the motion-vector sampler (`_MVEA`) for
   *  the motionVectors animation path. Loaded LINEAR (sRGB=false) — its
   *  (G,B) channels are signed optical-flow data, not colour, so an sRGB
   *  curve would corrupt the (G,B)*2-1 decode. Same cache as bindTexture. */
  private async bindMvTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsMipChain([url], false, r).catch((err) => {
        console.warn('[particles] MV DDS load failed', workspaceRelPath, err);
        return null;
      });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.mvMap.value = tex;
    material.uniforms.useMv.value = 1;
    material.needsUpdate = true;
  }

  /** Step every emitter forward by `dt`. Call this from the render loop. */
  tick(nowMs?: number): void {
    const now = nowMs ?? performance.now();
    if (this.lastTickMs < 0) {
      this.lastTickMs = now;
      return;
    }
    const dt = Math.min(0.1, (now - this.lastTickMs) * 0.001); // clamp big gaps
    this.lastTickMs = now;
    if (dt <= 0) return;
    for (const handle of this.attachments.values()) {
      if (!handle.active) continue;
      for (const s of handle.systems) s.tick(dt);
    }
  }

  /** Toggle one attachment on or off. */
  setAttachmentActive(handle: ParticleAttachmentHandle, active: boolean): void {
    handle.active = active;
    for (const s of handle.systems) s.setActive(active);
    handle.group.visible = active;
  }

  /** Toggle every attachment on/off. */
  setAllActive(active: boolean): void {
    for (const h of this.attachments.values()) this.setAttachmentActive(h, active);
  }

  clear(): void {
    for (const handle of this.attachments.values()) {
      for (const s of handle.systems) s.dispose();
      this.root.remove(handle.group);
    }
    this.attachments.clear();
    this.lastTickMs = -1;
  }

  dispose(): void {
    this.clear();
    // Drop cached textures. Each entry may still be resolving — settle
    // first, then dispose. Failures during settle are already swallowed
    // by `bindTexture`, so we don't need to re-handle them here.
    for (const pending of this.textureCache.values()) {
      void pending.then((tex) => tex?.dispose());
    }
    this.textureCache.clear();
    this.renderer = null;
  }
}
