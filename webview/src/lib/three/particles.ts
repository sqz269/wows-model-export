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
const ABSOLUTE_MAX_CAPACITY = 512;     // hard cap per system
const DEFAULT_SIZE_M = 0.3;            // metres — sane baseline if the
                                        // particle didn't author a size
                                        // generator
const HARD_MAX_EMIT_RATE_HZ = 200;     // safety clamp on the per-frame
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
    out[0] = 1; out[1] = 1; out[2] = 1; out[3] = 1;
    return;
  }
  const pts = color.points;
  if (t <= pts[0].time) {
    out[0] = pts[0].r; out[1] = pts[0].g; out[2] = pts[0].b; out[3] = pts[0].a;
    return;
  }
  if (t >= pts[pts.length - 1].time) {
    const p = pts[pts.length - 1];
    out[0] = p.r; out[1] = p.g; out[2] = p.b; out[3] = p.a;
    return;
  }
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].time) {
      const a = pts[i - 1];
      const b = pts[i];
      const span = b.time - a.time;
      if (span <= 0) {
        out[0] = a.r; out[1] = a.g; out[2] = a.b; out[3] = a.a;
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
    case 'constant': return vg.value ?? fallback;
    case 'linear': {
      // Random pick in [from, to]. Caller can re-sample for randomness.
      const f = vg.from ?? 0;
      const tt = vg.to ?? f;
      return f + Math.random() * (tt - f);
    }
    case 'ramp': return sampleRamp(vg.ramp, t, fallback);
    default: return fallback;
  }
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
  if (!body) { out.set(0, 0, 0); return; }
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
  private rateRamp: ParticleRamp | undefined;
  private initialPosVg: ParticleVariantVg | undefined;
  private initialVelVg: ParticleVariantVg | undefined;
  // Per-action driver fields.
  private tintColor: ParticleColor | undefined;
  private alphaRamp: ParticleRamp | undefined;
  private sizeRampVg: ParticleValueGenerator | undefined;
  private forceX: ParticleValueGenerator | undefined;
  private forceY: ParticleValueGenerator | undefined;
  private forceZ: ParticleValueGenerator | undefined;

  // Particle attribute arrays.
  private pos: Float32Array;
  private vel: Float32Array;
  private age: Float32Array;   // age in seconds; -1 = empty slot
  private lifetime: Float32Array;
  private colorRGBA: Float32Array;
  private sizeArr: Float32Array;
  private alive = 0;            // count of currently-alive particles

  // Reusable scratch buffers for the geometry attributes (we update
  // each frame in-place).
  private posAttr: THREE.BufferAttribute;
  private colorAttr: THREE.BufferAttribute;
  private sizeAttr: THREE.BufferAttribute;

  // Accumulator for the emission rate (particles per second).
  private emissionAccumulator = 0;
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

  constructor(system: ParticleSystem, material: THREE.ShaderMaterial) {
    this.material = material;
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
        if (body.initialPositionGenerator) this.initialPosVg = body.initialPositionGenerator as ParticleVariantVg;
        if (body.initialVelocityGenerator) this.initialVelVg = body.initialVelocityGenerator as ParticleVariantVg;
      } else if (c.action === 'tint') {
        if (body.tint) this.tintColor = body.tint as ParticleColor;
      } else if (c.action === 'alphaSetter') {
        if (body.ramp) this.alphaRamp = body.ramp as ParticleRamp;
      } else if (c.action === 'scaler' || c.action === 'resizer') {
        if (body.sizeGenerator) this.sizeRampVg = body.sizeGenerator as ParticleValueGenerator;
      } else if (c.action === 'force') {
        if (body.forceXGenerator) this.forceX = body.forceXGenerator as ParticleValueGenerator;
        if (body.forceYGenerator) this.forceY = body.forceYGenerator as ParticleValueGenerator;
        if (body.forceZGenerator) this.forceZ = body.forceZGenerator as ParticleValueGenerator;
      }
    }
    // Fall back to the emitter's sizeGenerator if the components didn't
    // ship a scaler.
    if (!this.sizeRampVg && system.emitter?.sizeGenerator) {
      this.sizeRampVg = system.emitter.sizeGenerator;
    }

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
    geom.setAttribute('position', this.posAttr);
    geom.setAttribute('color', this.colorAttr);
    geom.setAttribute('size', this.sizeAttr);
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
      const u = age / this.lifetime[i];
      // Force integration. Forces don't typically use ramps in the
      // corpus (mostly constants), so the normalised arg is fine.
      const fx = sampleScalarVg(this.forceX, u, 0);
      const fy = sampleScalarVg(this.forceY, u, 0);
      const fz = sampleScalarVg(this.forceZ, u, 0);
      this.vel[i * 3 + 0] += fx * dt;
      this.vel[i * 3 + 1] += fy * dt;
      this.vel[i * 3 + 2] += fz * dt;
      this.pos[i * 3 + 0] += this.vel[i * 3 + 0] * dt;
      this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt;
      this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt;
      // Tint + alpha curves drive the color attribute. Tint is keyed
      // by particle age in seconds. alphaSetter may be particle-age or
      // system-age — see `alphaSetterIsSystemAge` for the heuristic.
      sampleColor(this.tintColor, age, SystemRenderer.TMP_COL);
      const alphaT = this.alphaSetterIsSystemAge ? this.elapsed : age;
      const alpha = sampleRamp(this.alphaRamp, alphaT, 1) * SystemRenderer.TMP_COL[3];
      this.colorRGBA[i * 4 + 0] = SystemRenderer.TMP_COL[0];
      this.colorRGBA[i * 4 + 1] = SystemRenderer.TMP_COL[1];
      this.colorRGBA[i * 4 + 2] = SystemRenderer.TMP_COL[2];
      this.colorRGBA[i * 4 + 3] = alpha;
      // Size curve. Most size generators are scaler.sizeGenerator
      // ramp-by-particleIndex (the data uses a [0,1] axis to spread
      // sizes across the population). Fall back to the emitter's
      // generator otherwise — most of those are linear ranges where
      // the args don't matter.
      this.sizeArr[i] = Math.max(
        0.01,
        sampleScalarVg(this.sizeRampVg, u, DEFAULT_SIZE_M),
      );
    }

    // Emit new particles.
    if (this.rateRamp) {
      // Drive the rate ramp by elapsed-system-time ÷ activePeriod.
      // For systems without an activePeriod we wrap at maxAge.
      const period = Math.max(0.01, this.maxAge);
      const tNorm = (this.elapsed % period) / period;
      let rate = sampleRamp(this.rateRamp, tNorm, 0);
      rate = Math.min(Math.max(0, rate), HARD_MAX_EMIT_RATE_HZ);
      this.emissionAccumulator += rate * dt;
      while (this.emissionAccumulator >= 1 && this.alive < this.capacity) {
        this.emissionAccumulator -= 1;
        this.spawnParticle();
      }
      // Don't let the accumulator overflow if the system is at capacity
      // (avoids a burst of particles when an idle system frees slots).
      if (this.alive >= this.capacity) {
        this.emissionAccumulator = Math.min(this.emissionAccumulator, 1);
      }
    }

    // Update geometry attribute buffers + draw range. We pack the live
    // particles to the front; saves GPU vertex count vs. always drawing
    // `capacity` slots.
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
      writeIdx++;
    }
    this.points.geometry.setDrawRange(0, writeIdx);
    this.posAttr.needsUpdate = true;
    this.colorAttr.needsUpdate = true;
    this.sizeAttr.needsUpdate = true;
  }

  private spawnParticle(): void {
    // Find an empty slot.
    let slot = -1;
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) { slot = i; break; }
    }
    if (slot < 0) return;
    samplePosFromVariantVg(this.initialPosVg, SystemRenderer.TMP_POS);
    samplePosFromVariantVg(this.initialVelVg, SystemRenderer.TMP_VEL);
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
    this.sizeArr[slot] = Math.max(
      0.01,
      sampleScalarVg(this.sizeRampVg, Math.random(), DEFAULT_SIZE_M),
    );
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

/**
 * Build a per-system point-sprite material. Each SystemRenderer owns
 * its own copy so per-system texture binding doesn't clobber siblings.
 *
 * The fragment shader branches on a `useMap` uniform: when a DDS map is
 * bound it samples the texture at `gl_PointCoord` and tints by the
 * per-vertex color attribute. Otherwise it falls back to the procedural
 * soft-circular falloff used pre-texturing — useful for particles whose
 * Renderer fields weren't surfaced or whose DDS extraction missed.
 *
 * Blending: additive by default (fire / sparks / glow). PS_RBT_* dispatch
 * — to switch to alpha / screen blending per system — is queued on the
 * roadmap; needs the byte offset of Renderer.blendType RE'd first.
 */
function buildParticleMaterial(): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    uniforms: {
      map: { value: null as THREE.Texture | null },
      useMap: { value: 0 },
    },
    vertexShader: /* glsl */ `
      attribute vec4 color;
      attribute float size;
      varying vec4 vColor;

      void main() {
        vColor = color;
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mvPosition;
        // size is metres (world space); divide by distance for perspective.
        gl_PointSize = max(1.0, size * 200.0 / -mvPosition.z);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform sampler2D map;
      uniform float useMap;
      varying vec4 vColor;
      void main() {
        vec4 base;
        if (useMap > 0.5) {
          base = texture2D(map, gl_PointCoord);
        } else {
          vec2 c = gl_PointCoord - vec2(0.5);
          float r = length(c) * 2.0;
          if (r > 1.0) discard;
          // Soft circular falloff (squared).
          float a = (1.0 - r * r);
          base = vec4(1.0, 1.0, 1.0, a);
        }
        gl_FragColor = vec4(vColor.rgb * base.rgb, vColor.a * base.a);
      }
    `,
    blending: THREE.AdditiveBlending,
    transparent: true,
    depthWrite: false,
  });
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
        const mat = buildParticleMaterial();
        const renderer = new SystemRenderer(sys, mat);
        renderer.setActive(false);   // start inactive — UI toggles on
        grp.add(renderer.points);
        systems.push(renderer);
        // Kick off DDS texture load if the parser surfaced a refs.
        // Errors degrade silently to the procedural-disc fallback.
        const texPath = sys.renderer?.textureUrl0;
        if (texPath) {
          void this.bindTexture(mat, texPath);
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
    material: THREE.ShaderMaterial, workspaceRelPath: string,
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
