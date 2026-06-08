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
// Coordinates: most systems (coordinateStyle=2) simulate in the attachment
// group's local frame. WG's detached coordinate styles (0/1/3) sample their
// spawn data in the source attachment frame, then run in the particle-root
// frame so older particles do not keep inheriting future parent motion.

import * as THREE from 'three';
import type {
  ParticleAttachment,
  ParticleColor,
  ParticleComponentBody,
  ParticleRamp,
  ParticleRecord,
  ParticleSystem,
  ParticleValueGenerator,
  ParticleVariantVg,
  ParticleVgtPrototype,
} from '$lib/types/sidecar';
import { fetchParticleRecord, repoUrl } from '$lib/api';
import { loadDdsMipChain } from '$lib/dds';

const DEFAULT_PARTICLE_LIFETIME = 4.0; // seconds, when WG didn't author one
const ABSOLUTE_MAX_CAPACITY = 512; // hard cap per system
const DEFAULT_SIZE_M = 0.3; // metres — sane baseline if the
// particle didn't author a size
// generator
const HARD_MAX_EMIT_RATE_HZ = 200; // safety clamp on the per-frame
// particles-emitted count
const PARTICLE_POINT_LIGHT_BUDGET = 24;
const CHILD_EFFECT_DEPTH_LIMIT = 3;
const CHILD_EFFECT_BUDGET = 256;
const CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK = 8;
const SEA_LEVEL_Y = 0;

function systemUsesDetachedCoordinateFrame(system: ParticleSystem): boolean {
  const coord = system.general?.coordinateStyle ?? 2;
  return coordinateStyleUsesDetachedFrame(coord);
}

function coordinateStyleUsesDetachedFrame(coord: number): boolean {
  return coord < 2 || coord === 3;
}

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

interface StreamAction {
  vector: THREE.Vector3;
  halfLife: number;
  delay: number;
  switchCoordinateStyle: boolean;
}

interface JitterAction {
  positionGenerator: ParticleVariantVg | undefined;
  velocityGenerator: ParticleVariantVg | undefined;
  delay: number;
  affectPosition: boolean;
  affectVelocity: boolean;
}

interface OrbitorAction {
  angularVelocityGenerator: ParticleValueGenerator | undefined;
  point: THREE.Vector3;
  axis: THREE.Vector3;
  delay: number;
  affectPosition: boolean;
  affectVelocity: boolean;
}

interface MagnetAction {
  attractorPoint: THREE.Vector3;
  delay: number;
  minimalDistance: number;
  strength: number;
}

type BarrierShape = 'sphere' | 'cylinder' | 'box' | 'plane';

const BARRIER_REACTION_SCALE = 0;
const BARRIER_REACTION_BOUNCE = 1;
const BARRIER_REACTION_REMOVE = 2;
const BARRIER_REACTION_SPAWN = 3;
const BARRIER_REACTION_WRAP = 4;
const BARRIER_REACTION_ALPHA = 5;
const BARRIER_REACTION_DAMP = 6;
const BARRIER_REACTION_FORCE = 7;

interface BarrierAction {
  shape: BarrierShape;
  reaction: number;
  strength: number;
  stopAge: number;
  delay: number;
  position: THREE.Vector3;
  radius: number;
  corner: THREE.Vector3;
  opposite: THREE.Vector3;
  planeNormal: THREE.Vector3;
  planeConstant: number;
  useWorldSpace: boolean;
  effectName: string;
}

interface SpawnerAction {
  spawnRamp?: ParticleRamp;
  effectName: string;
  accum: number;
}

interface ParticleEffectSpawnRequest {
  effectName: string;
  position: [number, number, number];
}

type ParticleEffectSpawnCallback = (request: ParticleEffectSpawnRequest) => void;

interface SystemRendererOptions {
  spawnEffect?: ParticleEffectSpawnCallback;
  loopOneShot?: boolean;
  /** Attachment/group frame WG uses while sampling spawn authoring data. */
  sourceGroup?: THREE.Object3D;
  /** Scene-level particle root used as the alternate stream vector frame. */
  rootGroup?: THREE.Object3D;
}

function rampHasNonZeroValue(ramp: ParticleRamp | undefined): boolean {
  return !!ramp?.points?.some((p) => Math.abs(p.value) > 1e-6);
}

interface VelocityFieldData {
  sizeX: number;
  sizeY: number;
  sizeZ: number;
  vectors: Float32Array;
}

interface VelocityFieldAction {
  topLeftFront: THREE.Vector3;
  bottomRightBack: THREE.Vector3;
  stopAge: number;
  delay: number;
  velocityScale: number;
  influence: number;
  fieldSourceName: string;
  field: VelocityFieldData | null;
}

const velocityFieldCache = new Map<string, Promise<VelocityFieldData | null>>();

function fetchVelocityField(path: string): Promise<VelocityFieldData | null> {
  let pending = velocityFieldCache.get(path);
  if (!pending) {
    pending = fetch(repoUrl(path))
      .then(async (res) => {
        if (!res.ok) return null;
        const field = decodeVelocityField(await res.arrayBuffer());
        if (!field) console.warn('[particles] velocity field decode failed', path);
        return field;
      })
      .catch((err) => {
        console.warn('[particles] velocity field load failed', path, err);
        return null;
      });
    velocityFieldCache.set(path, pending);
  }
  return pending;
}

function decodeVelocityField(buffer: ArrayBuffer): VelocityFieldData | null {
  if (buffer.byteLength < 24) return null;
  const view = new DataView(buffer);
  const production = decodeProductionVelocityField(view, buffer.byteLength);
  if (production) return production;
  return decodeLegacyVelocityField(view, buffer.byteLength);
}

function decodeProductionVelocityField(view: DataView, byteLength: number): VelocityFieldData | null {
  const sizeX = view.getUint32(0, true);
  const sizeY = view.getUint32(4, true);
  const sizeZ = view.getUint32(8, true);
  const scalarCount = view.getUint32(12, true);
  const dataOffset = view.getUint32(16, true) + view.getUint32(20, true) * 0x100000000;
  const expectedCount = sizeX * sizeY * sizeZ * 3;
  if (
    sizeX <= 0 ||
    sizeY <= 0 ||
    sizeZ <= 0 ||
    sizeX > 256 ||
    sizeY > 256 ||
    sizeZ > 256 ||
    scalarCount !== expectedCount ||
    dataOffset < 24 ||
    dataOffset + scalarCount * 2 > byteLength
  ) {
    return null;
  }
  const vectors = new Float32Array(expectedCount);
  for (let i = 0; i < expectedCount; i++) {
    vectors[i] = halfToFloat(view.getUint16(dataOffset + i * 2, true));
  }
  return { sizeX, sizeY, sizeZ, vectors };
}

function decodeLegacyVelocityField(view: DataView, byteLength: number): VelocityFieldData | null {
  if (byteLength < 8 || view.getUint32(0, true) !== 0x444c4656) return null; // "VFLD"
  if (view.getUint8(4) !== 1) return null;
  const sizeX = view.getUint8(5);
  const sizeY = view.getUint8(6);
  const sizeZ = view.getUint8(7);
  const count = sizeX * sizeY * sizeZ * 3;
  if (sizeX <= 0 || sizeY <= 0 || sizeZ <= 0 || byteLength < 8 + count * 2) return null;
  const vectors = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    vectors[i] = Math.max(-1, view.getInt16(8 + i * 2, true) / 32767);
  }
  return { sizeX, sizeY, sizeZ, vectors };
}

function halfToFloat(bits: number): number {
  const sign = bits & 0x8000 ? -1 : 1;
  const exponent = (bits >> 10) & 0x1f;
  const mantissa = bits & 0x03ff;
  if (exponent === 0) {
    return mantissa === 0 ? sign * 0 : sign * Math.pow(2, -14) * (mantissa / 1024);
  }
  if (exponent === 0x1f) {
    return mantissa === 0 ? sign * Infinity : NaN;
  }
  return sign * Math.pow(2, exponent - 15) * (1 + mantissa / 1024);
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
  // False until the first active tick has pre-filled the ring buffer (H1
  // prewarm). Reset on each one-shot re-burst so the next burst re-warms.
  private prewarmed = false;
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
  private inheritVelocityFactor = 0;
  private snapToSeaLevel = false;
  // Per-action driver fields.
  private tintColor: ParticleColor | undefined;
  private alphaRamp: ParticleRamp | undefined;
  private forceX: ParticleValueGenerator | undefined;
  private forceY: ParticleValueGenerator | undefined;
  private forceZ: ParticleValueGenerator | undefined;
  private streamActions: StreamAction[] = [];
  private jitterActions: JitterAction[] = [];
  private orbitorActions: OrbitorAction[] = [];
  private magnetActions: MagnetAction[] = [];
  private barrierActions: BarrierAction[] = [];
  private spawnerActions: SpawnerAction[] = [];
  private velocityFieldActions: VelocityFieldAction[] = [];
  private frameRateRamp: ParticleRamp | undefined;
  private yawRateRamp: ParticleRamp | undefined;

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
  /** Per-particle random atlas cell (H5, RE doc 63). Assigned once at spawn
   *  = floor(rand() * framesRangeEnd); the fragment shader reads it via the
   *  `frameSeed` vertex attribute when `uRandomFrame` is set, freezing each
   *  particle on one cell. Packed to the front like ``ageGpu``. */
  private frameSeed: Float32Array;
  private frameSeedAttr: THREE.BufferAttribute;
  /** Integrated frame position, in frames, for native-style frameRateRamp
   *  playback. WG advances flipbooks by integrating FPS over particle age. */
  private framePhase: Float32Array;
  private framePhaseAttr: THREE.BufferAttribute;
  /** Integrated sprite yaw, in radians. Renderer.yawRateRamp values are small
   *  signed angular rates in the corpus (typically +/-0.5), matching radians/s
   *  rather than degrees/s. */
  private rotationPhase: Float32Array;
  private rotationPhaseAttr: THREE.BufferAttribute;
  /** Number of cells a randomFrameOnly particle can land on (framesRangeEnd,
   *  falling back to framesPerX*framesPerY). 0 ⇒ feature inert. */
  private framesRangeEnd = 0;

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
  private finished = false;
  private readonly spawnEffect?: ParticleEffectSpawnCallback;
  private readonly loopOneShot: boolean;
  private readonly sourceGroup?: THREE.Object3D;
  private readonly rootGroup?: THREE.Object3D;
  private readonly coordinateStyle: number;
  private readonly detachedCoordinateFrame: boolean;
  private barrierScaleMultiplier = 1;
  private barrierAlphaMultiplier = 1;
  private barrierInsideNow = false;
  private barrierInsideNext = false;
  private barrierDistanceRatio = 1;
  private parentVelocityLocal = new THREE.Vector3();

  // Tmp scratch — avoids per-frame Vector3 allocations.
  private static readonly TMP_POS = new THREE.Vector3();
  private static readonly TMP_VEL = new THREE.Vector3();
  private static readonly TMP_POS2 = new THREE.Vector3();
  private static readonly TMP_VEL2 = new THREE.Vector3();
  private static readonly TMP_AXIS = new THREE.Vector3();
  private static readonly TMP_REL = new THREE.Vector3();
  private static readonly TMP_REL2 = new THREE.Vector3();
  private static readonly TMP_WORLD = new THREE.Vector3();
  private static readonly TMP_QUAT = new THREE.Quaternion();
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

  get isFinished(): boolean {
    return this.finished;
  }

  constructor(
    system: ParticleSystem,
    material: THREE.ShaderMaterial,
    maxEmittingDuration = 0,
    options: SystemRendererOptions = {},
  ) {
    this.material = material;
    this.maxEmittingDuration = maxEmittingDuration;
    this.spawnEffect = options.spawnEffect;
    this.loopOneShot = options.loopOneShot ?? true;
    const gen = system.general;
    this.sourceGroup = options.sourceGroup;
    this.rootGroup = options.rootGroup;
    this.coordinateStyle = gen?.coordinateStyle ?? 2;
    this.detachedCoordinateFrame = coordinateStyleUsesDetachedFrame(this.coordinateStyle);
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
      } else if (c.action === 'spawner') {
        const effectName = typeof body.effectName === 'string' ? body.effectName : '';
        if (effectName) {
          this.spawnerActions.push({
            spawnRamp: body.spawnRamp as ParticleRamp | undefined,
            effectName,
            accum: 0,
          });
        }
      } else if (c.action === 'tint') {
        if (body.tint) this.tintColor = body.tint as ParticleColor;
      } else if (c.action === 'alphaSetter') {
        if (body.ramp) this.alphaRamp = body.ramp as ParticleRamp;
      } else if (c.action === 'scaler' || c.action === 'resizer') {
        if (body.sizeGenerator) this.scalerGens.push(body.sizeGenerator as ParticleValueGenerator);
      } else if (c.action === 'dampfer') {
        if (body.velocityGenerator)
          this.dampGen = body.velocityGenerator as ParticleValueGenerator;
      } else if (c.action === 'stream') {
        const v = body.vector;
        if (Array.isArray(v) && v.length === 3) {
          this.streamActions.push({
            vector: new THREE.Vector3(v[0], v[1], v[2]),
            halfLife: typeof body.halfLife === 'number' ? body.halfLife : -1,
            delay: typeof body.delay === 'number' ? body.delay : 0,
            // Native switchCoordinateStyle uses the alternate frame from the
            // system coordinateStyle audit; convert before applying velocity.
            switchCoordinateStyle: !!body.switchCoordinateStyle,
          });
        }
      } else if (c.action === 'jitter') {
        this.jitterActions.push({
          positionGenerator: body.positionGenerator as ParticleVariantVg | undefined,
          velocityGenerator: body.velocityGenerator as ParticleVariantVg | undefined,
          delay: typeof body.delay === 'number' ? body.delay : 0,
          affectPosition: !!body.affectPosition,
          affectVelocity: !!body.affectVelocity,
        });
      } else if (c.action === 'orbitor') {
        const p = body.point;
        const axis = body.axis;
        this.orbitorActions.push({
          angularVelocityGenerator: body.angularVelocityGenerator as ParticleValueGenerator | undefined,
          point:
            Array.isArray(p) && p.length === 3
              ? new THREE.Vector3(p[0], p[1], p[2])
              : new THREE.Vector3(),
          axis:
            Array.isArray(axis) && axis.length === 3
              ? new THREE.Vector3(axis[0], axis[1], axis[2])
              : new THREE.Vector3(0, 1, 0),
          delay: typeof body.delay === 'number' ? body.delay : 0,
          affectPosition: !!body.affectPosition,
          affectVelocity: !!body.affectVelocity,
        });
      } else if (c.action === 'magnet') {
        const p = body.attractorPoint;
        if (Array.isArray(p) && p.length === 3) {
          this.magnetActions.push({
            attractorPoint: new THREE.Vector3(p[0], p[1], p[2]),
            delay: typeof body.delay === 'number' ? body.delay : 0,
            minimalDistance:
              typeof body.minimalDistance === 'number' ? Math.max(0, body.minimalDistance) : 0,
            strength: typeof body.strength === 'number' ? body.strength : 0,
          });
        }
      } else if (
        c.action === 'sphere' ||
        c.action === 'cylinder' ||
        c.action === 'box' ||
        c.action === 'plane'
      ) {
        const p = body.position;
        const corner = body.corner;
        const opposite = body.opposite;
        const plane = body.planeEquation;
        const planeNormal =
          Array.isArray(plane) && plane.length >= 3
            ? new THREE.Vector3(plane[0], plane[1], plane[2])
            : new THREE.Vector3(0, 1, 0);
        if (planeNormal.lengthSq() <= 1e-10) planeNormal.set(0, 1, 0);
        planeNormal.normalize();
        this.barrierActions.push({
          shape: c.action,
          reaction: typeof body.reaction === 'number' ? body.reaction : BARRIER_REACTION_BOUNCE,
          strength: typeof body.strength === 'number' ? body.strength : 1,
          stopAge: typeof body.stopAge === 'number' ? body.stopAge : 0,
          delay: typeof body.delay === 'number' ? body.delay : 0,
          position:
            Array.isArray(p) && p.length === 3
              ? new THREE.Vector3(p[0], p[1], p[2])
              : new THREE.Vector3(),
          radius: typeof body.radius === 'number' ? Math.max(0, body.radius) : 0,
          corner:
            Array.isArray(corner) && corner.length === 3
              ? new THREE.Vector3(corner[0], corner[1], corner[2])
              : new THREE.Vector3(),
          opposite:
            Array.isArray(opposite) && opposite.length === 3
              ? new THREE.Vector3(opposite[0], opposite[1], opposite[2])
              : new THREE.Vector3(),
          planeNormal,
          planeConstant: Array.isArray(plane) && plane.length >= 4 ? plane[3] : 0,
          // Parsed but still simulated in attachment-local coordinates. Native
          // supports world-space planes; resolving that exactly needs scene-level
          // transform context instead of this per-system local renderer.
          useWorldSpace: !!body.useWorldSpace,
          effectName: typeof body.effectName === 'string' ? body.effectName : '',
        });
      } else if (c.action === 'velocityField') {
        const top = body.topLeftFront;
        const bottom = body.bottomRightBack;
        const fieldSourceName = typeof body.fieldSourceName === 'string' ? body.fieldSourceName : '';
        const action: VelocityFieldAction = {
          topLeftFront:
            Array.isArray(top) && top.length === 3
              ? new THREE.Vector3(top[0], top[1], top[2])
              : new THREE.Vector3(),
          bottomRightBack:
            Array.isArray(bottom) && bottom.length === 3
              ? new THREE.Vector3(bottom[0], bottom[1], bottom[2])
              : new THREE.Vector3(),
          stopAge: typeof body.stopAge === 'number' ? body.stopAge : 0,
          delay: typeof body.delay === 'number' ? body.delay : 0,
          velocityScale: typeof body.velocityScale === 'number' ? body.velocityScale : 1,
          influence: typeof body.influence === 'number' ? body.influence : 1,
          fieldSourceName,
          field: null,
        };
        this.velocityFieldActions.push(action);
        if (fieldSourceName) {
          void fetchVelocityField(fieldSourceName).then((field) => {
            action.field = field;
          });
        }
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
    this.inheritVelocityFactor = system.emitter?.inheritVelocityFactor ?? 0;
    this.snapToSeaLevel = !!system.emitter?.snapToSeaLevel;
    // SIZE base (RE 2026-06-04): the emitter's sizeGenerator is the per-particle
    // BASE size in METRES; ageScaleGenerator is a per-particle life multiplier.
    // Both are typically linear (random) → sampled once at spawn into psize[].
    // The scaler/resizer ramps (scalerGens, captured above) are the per-frame
    // multipliers, evaluated on their own parameterType axes in tick(). NO ×15.
    this.emitterSizeGen = system.emitter?.sizeGenerator;
    this.ageScaleGen = system.emitter?.ageScaleGenerator;
    // H5 random-cell cap: the count of frames a randomFrameOnly particle can
    // land on. Engine seeds the frame byte in [0, framesRangeEnd); fall back
    // to the full grid when the range wasn't authored.
    const anim = system.animation;
    this.frameRateRamp = anim?.frameRateRamp;
    this.yawRateRamp = rampHasNonZeroValue(system.renderer?.yawRateRamp)
      ? system.renderer?.yawRateRamp
      : undefined;
    const fx = anim?.framesPerX ?? 1;
    const fy = anim?.framesPerY ?? 1;
    this.framesRangeEnd = Math.max(0, anim?.framesRangeEnd ?? fx * fy);

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
    this.frameSeed = new Float32Array(this.capacity);
    this.framePhase = new Float32Array(this.capacity);
    this.rotationPhase = new Float32Array(this.capacity);
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
    this.frameSeedAttr = new THREE.BufferAttribute(this.frameSeed, 1);
    this.frameSeedAttr.setUsage(THREE.DynamicDrawUsage);
    this.framePhaseAttr = new THREE.BufferAttribute(this.framePhase, 1);
    this.framePhaseAttr.setUsage(THREE.DynamicDrawUsage);
    this.rotationPhaseAttr = new THREE.BufferAttribute(this.rotationPhase, 1);
    this.rotationPhaseAttr.setUsage(THREE.DynamicDrawUsage);
    geom.setAttribute('position', this.posAttr);
    geom.setAttribute('color', this.colorAttr);
    geom.setAttribute('size', this.sizeAttr);
    geom.setAttribute('age', this.ageAttr);
    geom.setAttribute('frameSeed', this.frameSeedAttr);
    geom.setAttribute('framePhase', this.framePhaseAttr);
    geom.setAttribute('rotationPhase', this.rotationPhaseAttr);
    geom.setDrawRange(0, 0);
    this.points = new THREE.Points(geom, material);
    this.points.frustumCulled = false;
  }

  setActive(active: boolean): void {
    this.active = active;
    this.points.visible = active;
    if (active && this.loopOneShot) this.finished = false;
  }

  /** Parent velocity is sampled in world space by ShipViewer and converted
   *  into this system's local frame. It is applied only to newly spawned
   *  particles through emitter.inheritVelocityFactor. */
  setParentVelocityWorld(velocity: THREE.Vector3): void {
    if (this.inheritVelocityFactor === 0) {
      this.parentVelocityLocal.set(0, 0, 0);
      return;
    }
    this.parentVelocityLocal.copy(velocity);
    const source = this.sourceFrame();
    if (!source) return;
    source.updateWorldMatrix(true, false);
    source.getWorldQuaternion(SystemRenderer.TMP_QUAT).invert();
    this.parentVelocityLocal.applyQuaternion(SystemRenderer.TMP_QUAT);
  }

  /** Step the simulation by `dt` seconds. Updates the GPU buffers. */
  tick(dt: number): void {
    if (!this.active) {
      // Even when paused, decay existing particles so they don't sit
      // frozen. Optional — for MVP we just fully freeze.
      return;
    }
    // Prewarm on the first active frame (and after each one-shot re-burst): the
    // engine pre-runs ~10 substeps on activation (FUN_1406ce8a0, scale 0.1) so a
    // continuous emitter is at steady-state and a one-shot at peak on frame 1.
    // Without it the buffer fills from empty over ~maxAge — 3-4x too sparse in
    // the visible window, and a one-shot never catches up. See RE doc 63 (H1).
    if (!this.prewarmed) {
      this.runPrewarm();
      this.prewarmed = true;
    }
    this.advance(dt, true);
    this.writeBuffers();
  }

  /** Advance the CPU simulation by `dt` seconds (emission + per-particle
   *  update). Does NOT touch the GPU buffers — see writeBuffers(). */
  private advance(dt: number, allowChildSpawns: boolean): void {
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
      for (const action of this.spawnerActions) action.accum = 0;
      this.points.geometry.setDrawRange(0, 0);
      if (!this.loopOneShot) {
        this.finished = true;
        this.active = false;
        return;
      }
      this.elapsed = 0;
      this.prewarmed = false; // re-warm the next burst (engine re-warms on re-activation)
    }
    const emitting = !oneShot || this.elapsed <= this.maxEmittingDuration;
    if (emitting && allowChildSpawns) this.applySpawnerActions(dt);

    // Per-particle update. WG's authoring convention: ramp + color
    // curves are keyed by particle age in *seconds*, not normalised
    // [0,1]. A 4.2-second fire particle samples its tint curve at
    // age=2.3s directly (not 2.3/4.2). Force generators usually use a
    // ramp parameter type of "particleAge" too; for the MVP we feed the
    // normalised ratio to those scalar generators and accept the
    // approximation.
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      const prevAge = this.age[i];
      this.age[i] += dt;
      if (this.age[i] >= this.lifetime[i]) {
        this.age[i] = -1;
        this.alive--;
        continue;
      }
      const age = this.age[i];
      if (this.frameRateRamp) {
        const fps0 = sampleRamp(this.frameRateRamp, prevAge, 0);
        const fps1 = sampleRamp(this.frameRateRamp, age, 0);
        this.framePhase[i] += Math.max(0, 0.5 * (fps0 + fps1) * dt);
      }
      if (this.yawRateRamp) {
        const yaw0 = sampleRamp(this.yawRateRamp, prevAge, 0);
        const yaw1 = sampleRamp(this.yawRateRamp, age, 0);
        this.rotationPhase[i] += 0.5 * (yaw0 + yaw1) * dt;
      }
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
      this.applyMagnetActions(i, age, dt);
      this.applyStreamActions(i, age, dt);
      this.applyJitterActions(i, age, dt);
      this.applyVelocityFieldActions(i, age);
      // dampfer: a per-frame drag multiplier on the velocity's displacement.
      const damp = this.dampGen ? sampleGenAxis(this.dampGen, clocks, 1) : 1;
      if (this.applyBarrierActions(i, age, dt * damp, allowChildSpawns)) continue;
      this.pos[i * 3 + 0] += this.vel[i * 3 + 0] * dt * damp;
      this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt * damp;
      this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt * damp;
      this.applyOrbitorActions(i, clocks, age, dt);
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
      const alpha =
        sampleRamp(this.alphaRamp, alphaT, 1) *
        SystemRenderer.TMP_COL[3] *
        this.barrierAlphaMultiplier;
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
      sz *= this.barrierScaleMultiplier;
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
        // Creator rate is authored in seconds against system active time. The
        // old normalized-age path under-emitted short bursts and over-looped
        // impact effects.
        const cRate = sampleRamp(this.rateRamp, this.elapsed, 0);
        this.creatorAccum = this.emitFromSource(
          cRate,
          dt,
          this.creatorAccum,
          this.initialPosVg,
          this.initialVelVg,
        );
      }
    }
  }

  /** Pack the live particles to the front of the attribute arrays and push the
   *  GPU buffers. Called once per visible frame (after advance()), never during
   *  prewarm. */
  private writeBuffers(): void {
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
        this.frameSeed[writeIdx] = this.frameSeed[i];
        this.framePhase[writeIdx] = this.framePhase[i];
        this.rotationPhase[writeIdx] = this.rotationPhase[i];
      }
      this.ageGpu[writeIdx] = this.age[i];
      writeIdx++;
    }
    this.points.geometry.setDrawRange(0, writeIdx);
    this.posAttr.needsUpdate = true;
    this.colorAttr.needsUpdate = true;
    this.sizeAttr.needsUpdate = true;
    this.ageAttr.needsUpdate = true;
    this.frameSeedAttr.needsUpdate = true;
    this.framePhaseAttr.needsUpdate = true;
    this.rotationPhaseAttr.needsUpdate = true;
  }

  /** Pre-fill the ring buffer to the engine's frame-1 density: run STEPS
   *  internal sub-steps (no GPU writes) before the first visible frame, like the
   *  engine's 10x activation prewarm (FUN_1406ce8a0, scale 0.1). Continuous
   *  emitters reach steady-state; one-shot bursts reach peak. See RE doc 63 (H1). */
  private runPrewarm(): void {
    const window = this.maxEmittingDuration > 0 ? this.maxEmittingDuration : this.maxAge;
    if (!(window > 0)) return;
    const STEPS = 10;
    const dt = window / STEPS;
    for (let s = 0; s < STEPS; s++) this.advance(dt, false);
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

  private applySpawnerActions(dt: number): void {
    if (!this.spawnEffect || this.spawnerActions.length === 0) return;
    let spawnedThisTick = 0;
    for (const action of this.spawnerActions) {
      const rate = sampleRamp(action.spawnRamp, this.elapsed, 0);
      if (rate <= 0) continue;
      action.accum += Math.min(rate, HARD_MAX_EMIT_RATE_HZ) * dt;
      while (action.accum >= 1 && spawnedThisTick < CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK) {
        action.accum -= 1;
        spawnedThisTick++;
        this.spawnEffect({ effectName: action.effectName, position: [0, 0, 0] });
      }
      if (spawnedThisTick >= CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK) {
        action.accum = Math.min(action.accum, 1);
        break;
      }
    }
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
    this.applySeaLevelBaseOffset(SystemRenderer.TMP_POS);
    samplePosFromVariantVg(velVg, SystemRenderer.TMP_VEL);
    if (this.inheritVelocityFactor !== 0) {
      SystemRenderer.TMP_VEL.addScaledVector(this.parentVelocityLocal, this.inheritVelocityFactor);
    }
    this.convertSpawnToSimulationFrame(SystemRenderer.TMP_POS, SystemRenderer.TMP_VEL);
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
    // H5 (RE doc 63): pick this particle's fixed random atlas cell once, in
    // [0, framesRangeEnd). The fragment shader reads it via `frameSeed` only
    // when uRandomFrame is set; harmless to assign unconditionally.
    this.frameSeed[slot] =
      this.framesRangeEnd > 0 ? Math.floor(Math.random() * this.framesRangeEnd) : 0;
    this.framePhase[slot] = 0;
    this.rotationPhase[slot] = 0;
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

  private applySeaLevelBaseOffset(pos: THREE.Vector3): void {
    if (!this.snapToSeaLevel) return;
    const source = this.sourceFrame();
    if (!source) return;
    // Native `snapToSeaLevel` snaps the emitter base, not every particle's
    // authored local height. Preserve local Y offsets and velocity by applying
    // only the parent-translation delta at spawn.
    source.updateWorldMatrix(true, false);
    source.getWorldPosition(SystemRenderer.TMP_WORLD);
    const dy = SEA_LEVEL_Y - SystemRenderer.TMP_WORLD.y;
    if (Math.abs(dy) <= 1e-6) return;
    const offset = SystemRenderer.TMP_POS2.set(0, dy, 0);
    source.getWorldQuaternion(SystemRenderer.TMP_QUAT).invert();
    offset.applyQuaternion(SystemRenderer.TMP_QUAT);
    pos.add(offset);
  }

  private sourceFrame(): THREE.Object3D | null {
    return this.sourceGroup ?? this.points.parent;
  }

  private convertSpawnToSimulationFrame(pos: THREE.Vector3, vel: THREE.Vector3): void {
    if (!this.detachedCoordinateFrame) return;
    const source = this.sourceFrame();
    const target = this.points.parent;
    if (!source || !target || source === target) return;
    source.updateWorldMatrix(true, false);
    target.updateWorldMatrix(true, false);
    source.localToWorld(pos);
    target.worldToLocal(pos);
    source.getWorldQuaternion(SystemRenderer.TMP_QUAT);
    vel.applyQuaternion(SystemRenderer.TMP_QUAT);
    target.getWorldQuaternion(SystemRenderer.TMP_QUAT).invert();
    vel.applyQuaternion(SystemRenderer.TMP_QUAT);
  }

  private convertSimulationPositionToSourceFrame(pos: THREE.Vector3): void {
    if (!this.detachedCoordinateFrame) return;
    const source = this.sourceFrame();
    const simulation = this.points.parent;
    if (!source || !simulation || source === simulation) return;
    simulation.updateWorldMatrix(true, false);
    source.updateWorldMatrix(true, false);
    simulation.localToWorld(pos);
    source.worldToLocal(pos);
  }

  private streamVectorForSimulationFrame(action: StreamAction, out: THREE.Vector3): THREE.Vector3 {
    out.copy(action.vector);
    if (!action.switchCoordinateStyle) return out;
    const source = this.streamSwitchSourceFrame();
    const target = this.points.parent;
    if (!source || !target || source === target) return out;
    source.updateWorldMatrix(true, false);
    target.updateWorldMatrix(true, false);
    source.getWorldQuaternion(SystemRenderer.TMP_QUAT);
    out.applyQuaternion(SystemRenderer.TMP_QUAT);
    target.getWorldQuaternion(SystemRenderer.TMP_QUAT).invert();
    out.applyQuaternion(SystemRenderer.TMP_QUAT);
    return out;
  }

  private streamSwitchSourceFrame(): THREE.Object3D | null {
    if (this.coordinateStyle === 2) return this.rootGroup ?? this.points.parent;
    return this.sourceFrame();
  }

  private applyStreamActions(slot: number, age: number, dt: number): void {
    if (this.streamActions.length === 0) return;
    const ix = slot * 3;
    let vx = this.vel[ix + 0];
    let vy = this.vel[ix + 1];
    let vz = this.vel[ix + 2];
    for (const action of this.streamActions) {
      if (age < action.delay) continue;
      const vector = this.streamVectorForSimulationFrame(action, SystemRenderer.TMP_VEL2);
      if (action.halfLife < 0) continue;
      if (action.halfLife <= 1e-6) {
        vx = vector.x;
        vy = vector.y;
        vz = vector.z;
        continue;
      }
      // BigWorld StreamPSA: velocity moves halfway toward the stream velocity
      // every halfLife seconds. Equivalent continuous update:
      // v += (target - v) * (1 - 0.5 ** (dt / halfLife)).
      const k = 1 - Math.pow(0.5, dt / action.halfLife);
      vx += (vector.x - vx) * k;
      vy += (vector.y - vy) * k;
      vz += (vector.z - vz) * k;
    }
    this.vel[ix + 0] = vx;
    this.vel[ix + 1] = vy;
    this.vel[ix + 2] = vz;
  }

  private applyJitterActions(slot: number, age: number, dt: number): void {
    if (this.jitterActions.length === 0) return;
    const ix = slot * 3;
    for (const action of this.jitterActions) {
      if (age < action.delay) continue;
      if (action.affectPosition) {
        samplePosFromVariantVg(action.positionGenerator, SystemRenderer.TMP_POS2);
        this.pos[ix + 0] += SystemRenderer.TMP_POS2.x * dt;
        this.pos[ix + 1] += SystemRenderer.TMP_POS2.y * dt;
        this.pos[ix + 2] += SystemRenderer.TMP_POS2.z * dt;
      }
      if (action.affectVelocity) {
        samplePosFromVariantVg(action.velocityGenerator, SystemRenderer.TMP_VEL2);
        this.vel[ix + 0] += SystemRenderer.TMP_VEL2.x * dt;
        this.vel[ix + 1] += SystemRenderer.TMP_VEL2.y * dt;
        this.vel[ix + 2] += SystemRenderer.TMP_VEL2.z * dt;
      }
    }
  }

  private applyOrbitorActions(
    slot: number,
    clocks: ParticleClocks,
    age: number,
    dt: number,
  ): void {
    if (this.orbitorActions.length === 0) return;
    const ix = slot * 3;
    for (const action of this.orbitorActions) {
      if (age < action.delay) continue;
      const axis = SystemRenderer.TMP_AXIS.copy(action.axis);
      if (axis.lengthSq() <= 1e-10) axis.set(0, 1, 0);
      axis.normalize();
      // BigWorld's Particle Editor labels this as degrees/second.
      const angularVelocityDeg = sampleGenAxis(action.angularVelocityGenerator, clocks, 0);
      const angle = THREE.MathUtils.degToRad(angularVelocityDeg) * dt;
      if (Math.abs(angle) <= 1e-8) continue;
      if (action.affectPosition) {
        const rel = SystemRenderer.TMP_REL.set(
          this.pos[ix + 0] - action.point.x,
          this.pos[ix + 1] - action.point.y,
          this.pos[ix + 2] - action.point.z,
        );
        rel.applyAxisAngle(axis, angle);
        this.pos[ix + 0] = action.point.x + rel.x;
        this.pos[ix + 1] = action.point.y + rel.y;
        this.pos[ix + 2] = action.point.z + rel.z;
      }
      if (action.affectVelocity) {
        SystemRenderer.TMP_VEL2.set(this.vel[ix + 0], this.vel[ix + 1], this.vel[ix + 2]);
        SystemRenderer.TMP_VEL2.applyAxisAngle(axis, angle);
        this.vel[ix + 0] = SystemRenderer.TMP_VEL2.x;
        this.vel[ix + 1] = SystemRenderer.TMP_VEL2.y;
        this.vel[ix + 2] = SystemRenderer.TMP_VEL2.z;
      }
    }
  }

  private applyMagnetActions(slot: number, age: number, dt: number): void {
    if (this.magnetActions.length === 0) return;
    const ix = slot * 3;
    for (const action of this.magnetActions) {
      if (age < action.delay || action.strength === 0) continue;
      const dir = SystemRenderer.TMP_REL.set(
        action.attractorPoint.x - this.pos[ix + 0],
        action.attractorPoint.y - this.pos[ix + 1],
        action.attractorPoint.z - this.pos[ix + 2],
      );
      const dist = dir.length();
      if (dist <= action.minimalDistance || dist <= 1e-6) continue;
      dir.multiplyScalar(1 / dist);
      this.vel[ix + 0] += dir.x * action.strength * dt;
      this.vel[ix + 1] += dir.y * action.strength * dt;
      this.vel[ix + 2] += dir.z * action.strength * dt;
    }
  }

  private applyVelocityFieldActions(slot: number, age: number): void {
    if (this.velocityFieldActions.length === 0) return;
    const ix = slot * 3;
    const pos = SystemRenderer.TMP_POS.set(this.pos[ix + 0], this.pos[ix + 1], this.pos[ix + 2]);
    const sample = SystemRenderer.TMP_VEL2;
    for (const action of this.velocityFieldActions) {
      if (age < action.delay) continue;
      if (action.stopAge > 0 && age > action.stopAge) continue;
      if (!action.field) continue;
      const u = this.fieldAxisT(pos.x, action.topLeftFront.x, action.bottomRightBack.x);
      const v = this.fieldAxisT(pos.y, action.topLeftFront.y, action.bottomRightBack.y);
      const w = this.fieldAxisT(pos.z, action.topLeftFront.z, action.bottomRightBack.z);
      if (u < 0 || u > 1 || v < 0 || v > 1 || w < 0 || w > 1) continue;
      this.sampleVelocityField(action.field, u, v, w, sample);
      sample.multiplyScalar(action.velocityScale);
      const blend = THREE.MathUtils.clamp(action.influence, 0, 1);
      this.vel[ix + 0] += (sample.x - this.vel[ix + 0]) * blend;
      this.vel[ix + 1] += (sample.y - this.vel[ix + 1]) * blend;
      this.vel[ix + 2] += (sample.z - this.vel[ix + 2]) * blend;
    }
  }

  private fieldAxisT(value: number, a: number, b: number): number {
    const span = b - a;
    if (Math.abs(span) <= 1e-6) return 0.5;
    return (value - a) / span;
  }

  private sampleVelocityField(
    field: VelocityFieldData,
    u: number,
    v: number,
    w: number,
    out: THREE.Vector3,
  ): void {
    const x = THREE.MathUtils.clamp(u, 0, 1) * (field.sizeX - 1);
    const y = THREE.MathUtils.clamp(v, 0, 1) * (field.sizeY - 1);
    const z = THREE.MathUtils.clamp(w, 0, 1) * (field.sizeZ - 1);
    const x0 = Math.floor(x);
    const y0 = Math.floor(y);
    const z0 = Math.floor(z);
    const x1 = Math.min(field.sizeX - 1, x0 + 1);
    const y1 = Math.min(field.sizeY - 1, y0 + 1);
    const z1 = Math.min(field.sizeZ - 1, z0 + 1);
    const tx = x - x0;
    const ty = y - y0;
    const tz = z - z0;
    out.set(0, 0, 0);
    this.accumulateVelocityFieldCorner(field, x0, y0, z0, (1 - tx) * (1 - ty) * (1 - tz), out);
    this.accumulateVelocityFieldCorner(field, x1, y0, z0, tx * (1 - ty) * (1 - tz), out);
    this.accumulateVelocityFieldCorner(field, x0, y1, z0, (1 - tx) * ty * (1 - tz), out);
    this.accumulateVelocityFieldCorner(field, x1, y1, z0, tx * ty * (1 - tz), out);
    this.accumulateVelocityFieldCorner(field, x0, y0, z1, (1 - tx) * (1 - ty) * tz, out);
    this.accumulateVelocityFieldCorner(field, x1, y0, z1, tx * (1 - ty) * tz, out);
    this.accumulateVelocityFieldCorner(field, x0, y1, z1, (1 - tx) * ty * tz, out);
    this.accumulateVelocityFieldCorner(field, x1, y1, z1, tx * ty * tz, out);
  }

  private accumulateVelocityFieldCorner(
    field: VelocityFieldData,
    x: number,
    y: number,
    z: number,
    weight: number,
    out: THREE.Vector3,
  ): void {
    if (weight <= 0) return;
    const idx = ((z * field.sizeY + y) * field.sizeX + x) * 3;
    out.x += field.vectors[idx + 0] * weight;
    out.y += field.vectors[idx + 1] * weight;
    out.z += field.vectors[idx + 2] * weight;
  }

  private applyBarrierActions(
    slot: number,
    age: number,
    displacementDt: number,
    allowChildSpawns: boolean,
  ): boolean {
    this.barrierScaleMultiplier = 1;
    this.barrierAlphaMultiplier = 1;
    if (this.barrierActions.length === 0) return false;
    const ix = slot * 3;
    for (const action of this.barrierActions) {
      if (age < action.delay) continue;
      if (action.stopAge > 0 && age > action.stopAge) continue;

      const current = SystemRenderer.TMP_POS.set(
        this.pos[ix + 0],
        this.pos[ix + 1],
        this.pos[ix + 2],
      );
      const predicted = SystemRenderer.TMP_POS2.set(
        current.x + this.vel[ix + 0] * displacementDt,
        current.y + this.vel[ix + 1] * displacementDt,
        current.z + this.vel[ix + 2] * displacementDt,
      );
      const normal = SystemRenderer.TMP_AXIS;
      this.sampleBarrierState(action, current, predicted, normal);
      const insideNow = this.barrierInsideNow;
      const insideNext = this.barrierInsideNext;
      const crossed = insideNow !== insideNext;

      switch (action.reaction) {
        case BARRIER_REACTION_SCALE:
          if (insideNow) {
            const ratio = THREE.MathUtils.clamp(this.barrierDistanceRatio, 0, 1);
            const targetScale = Math.max(0, action.strength);
            this.barrierScaleMultiplier *= targetScale + (1 - targetScale) * ratio;
          }
          break;
        case BARRIER_REACTION_BOUNCE:
          if (crossed) {
            if (action.shape === 'plane' || action.shape === 'box') {
              this.reflectVelocity(ix, normal);
            } else {
              this.cancelVelocityAlongNormal(ix, normal);
            }
          }
          break;
        case BARRIER_REACTION_REMOVE:
          if (insideNow || insideNext || crossed) {
            this.killParticle(slot);
            return true;
          }
          break;
        case BARRIER_REACTION_SPAWN:
          if (allowChildSpawns && crossed && action.effectName && this.spawnEffect) {
            const spawnPos = SystemRenderer.TMP_WORLD.copy(current);
            this.convertSimulationPositionToSourceFrame(spawnPos);
            this.spawnEffect({
              effectName: action.effectName,
              position: [spawnPos.x, spawnPos.y, spawnPos.z],
            });
          }
          break;
        case BARRIER_REACTION_WRAP:
          this.applyBarrierWrap(action, ix, predicted);
          break;
        case BARRIER_REACTION_ALPHA:
          if (insideNow) {
            let factor = THREE.MathUtils.clamp(this.barrierDistanceRatio, 0, 1);
            const power = Math.abs(action.strength);
            if (action.strength < 0) factor = 1 - factor;
            this.barrierAlphaMultiplier *= Math.pow(Math.max(0, factor), power);
          }
          break;
        case BARRIER_REACTION_DAMP:
          if (insideNow) {
            const k = action.strength * displacementDt;
            this.vel[ix + 0] -= this.vel[ix + 0] * k;
            this.vel[ix + 1] -= this.vel[ix + 1] * k;
            this.vel[ix + 2] -= this.vel[ix + 2] * k;
          }
          break;
        case BARRIER_REACTION_FORCE:
          if (insideNow) {
            this.vel[ix + 0] += normal.x * action.strength * displacementDt;
            this.vel[ix + 1] += normal.y * action.strength * displacementDt;
            this.vel[ix + 2] += normal.z * action.strength * displacementDt;
          }
          break;
        default:
          break;
      }
    }
    return false;
  }

  private sampleBarrierState(
    action: BarrierAction,
    current: THREE.Vector3,
    predicted: THREE.Vector3,
    normalOut: THREE.Vector3,
  ): void {
    this.barrierInsideNow = false;
    this.barrierInsideNext = false;
    this.barrierDistanceRatio = 1;
    normalOut.set(0, 1, 0);
    switch (action.shape) {
      case 'sphere': {
        const r = Math.max(action.radius, 1e-6);
        const r2 = r * r;
        const currRel = SystemRenderer.TMP_REL.copy(action.position).sub(current);
        const nextRel = SystemRenderer.TMP_REL2.copy(action.position).sub(predicted);
        const currDistSq = currRel.lengthSq();
        this.barrierInsideNow = currDistSq <= r2;
        this.barrierInsideNext = nextRel.lengthSq() <= r2;
        this.barrierDistanceRatio = Math.sqrt(currDistSq) / r;
        if (currRel.lengthSq() > 1e-10) normalOut.copy(currRel).normalize();
        else if (nextRel.lengthSq() > 1e-10) normalOut.copy(nextRel).normalize();
        return;
      }
      case 'cylinder': {
        const r = Math.max(action.radius, 1e-6);
        const r2 = r * r;
        const dx = action.position.x - current.x;
        const dz = action.position.z - current.z;
        const ndx = action.position.x - predicted.x;
        const ndz = action.position.z - predicted.z;
        const currDistSq = dx * dx + dz * dz;
        this.barrierInsideNow = currDistSq <= r2;
        this.barrierInsideNext = ndx * ndx + ndz * ndz <= r2;
        this.barrierDistanceRatio = Math.sqrt(currDistSq) / r;
        normalOut.set(dx, 0, dz);
        if (normalOut.lengthSq() > 1e-10) normalOut.normalize();
        return;
      }
      case 'box': {
        const minX = Math.min(action.corner.x, action.opposite.x);
        const minY = Math.min(action.corner.y, action.opposite.y);
        const minZ = Math.min(action.corner.z, action.opposite.z);
        const maxX = Math.max(action.corner.x, action.opposite.x);
        const maxY = Math.max(action.corner.y, action.opposite.y);
        const maxZ = Math.max(action.corner.z, action.opposite.z);
        this.barrierInsideNow =
          current.x >= minX &&
          current.x <= maxX &&
          current.y >= minY &&
          current.y <= maxY &&
          current.z >= minZ &&
          current.z <= maxZ;
        this.barrierInsideNext =
          predicted.x >= minX &&
          predicted.x <= maxX &&
          predicted.y >= minY &&
          predicted.y <= maxY &&
          predicted.z >= minZ &&
          predicted.z <= maxZ;
        const cx = (minX + maxX) * 0.5;
        const cy = (minY + maxY) * 0.5;
        const cz = (minZ + maxZ) * 0.5;
        const hx = Math.max(1e-6, (maxX - minX) * 0.5);
        const hy = Math.max(1e-6, (maxY - minY) * 0.5);
        const hz = Math.max(1e-6, (maxZ - minZ) * 0.5);
        const rx = Math.abs(current.x - cx) / hx;
        const ry = Math.abs(current.y - cy) / hy;
        const rz = Math.abs(current.z - cz) / hz;
        this.barrierDistanceRatio = Math.max(rx, ry, rz);
        normalOut.set(cx - current.x, cy - current.y, cz - current.z);
        if (normalOut.lengthSq() <= 1e-10) {
          if (rx >= ry && rx >= rz) normalOut.set(current.x < cx ? 1 : -1, 0, 0);
          else if (ry >= rz) normalOut.set(0, current.y < cy ? 1 : -1, 0);
          else normalOut.set(0, 0, current.z < cz ? 1 : -1);
        } else {
          normalOut.normalize();
        }
        return;
      }
      case 'plane': {
        const sideNow = action.planeNormal.dot(current) - action.planeConstant;
        const sideNext = action.planeNormal.dot(predicted) - action.planeConstant;
        this.barrierInsideNow = sideNow < 0;
        this.barrierInsideNext = sideNext < 0;
        this.barrierDistanceRatio = sideNow < 0 ? 0 : 1;
        normalOut.copy(action.planeNormal);
        return;
      }
    }
  }

  private reflectVelocity(ix: number, normal: THREE.Vector3): void {
    if (normal.lengthSq() <= 1e-10) return;
    const dot = this.vel[ix + 0] * normal.x + this.vel[ix + 1] * normal.y + this.vel[ix + 2] * normal.z;
    this.vel[ix + 0] -= 2 * dot * normal.x;
    this.vel[ix + 1] -= 2 * dot * normal.y;
    this.vel[ix + 2] -= 2 * dot * normal.z;
  }

  private cancelVelocityAlongNormal(ix: number, normal: THREE.Vector3): void {
    if (normal.lengthSq() <= 1e-10) return;
    const dot = this.vel[ix + 0] * normal.x + this.vel[ix + 1] * normal.y + this.vel[ix + 2] * normal.z;
    this.vel[ix + 0] -= dot * normal.x;
    this.vel[ix + 1] -= dot * normal.y;
    this.vel[ix + 2] -= dot * normal.z;
  }

  private applyBarrierWrap(action: BarrierAction, ix: number, predicted: THREE.Vector3): void {
    if (action.shape === 'sphere') {
      if (!this.barrierInsideNext && action.radius > 0) {
        const inward = SystemRenderer.TMP_REL.copy(action.position).sub(predicted);
        if (inward.lengthSq() > 1e-10) {
          inward.normalize().multiplyScalar(action.radius * 2);
          this.pos[ix + 0] += inward.x;
          this.pos[ix + 1] += inward.y;
          this.pos[ix + 2] += inward.z;
        }
      }
      return;
    }
    if (action.shape === 'cylinder') {
      if (!this.barrierInsideNext && action.radius > 0) {
        const inward = SystemRenderer.TMP_REL.set(
          action.position.x - predicted.x,
          0,
          action.position.z - predicted.z,
        );
        if (inward.lengthSq() > 1e-10) {
          inward.normalize().multiplyScalar(action.radius * 2);
          this.pos[ix + 0] += inward.x;
          this.pos[ix + 2] += inward.z;
        }
      }
      return;
    }
    if (action.shape === 'box') {
      if (this.barrierInsideNext) return;
      const minX = Math.min(action.corner.x, action.opposite.x);
      const minY = Math.min(action.corner.y, action.opposite.y);
      const minZ = Math.min(action.corner.z, action.opposite.z);
      const maxX = Math.max(action.corner.x, action.opposite.x);
      const maxY = Math.max(action.corner.y, action.opposite.y);
      const maxZ = Math.max(action.corner.z, action.opposite.z);
      this.pos[ix + 0] = this.wrapAxis(predicted.x, minX, maxX, this.pos[ix + 0]);
      this.pos[ix + 1] = this.wrapAxis(predicted.y, minY, maxY, this.pos[ix + 1]);
      this.pos[ix + 2] = this.wrapAxis(predicted.z, minZ, maxZ, this.pos[ix + 2]);
      return;
    }
    if (action.shape === 'plane' && this.barrierInsideNext) {
      const side = action.planeNormal.dot(SystemRenderer.TMP_POS.set(
        this.pos[ix + 0],
        this.pos[ix + 1],
        this.pos[ix + 2],
      )) - action.planeConstant;
      this.pos[ix + 0] -= action.planeNormal.x * side;
      this.pos[ix + 1] -= action.planeNormal.y * side;
      this.pos[ix + 2] -= action.planeNormal.z * side;
    }
  }

  private wrapAxis(predicted: number, min: number, max: number, current: number): number {
    const width = max - min;
    if (!(width > 1e-6)) return current;
    if (predicted < min) {
      const off = (min - predicted) % width;
      return max - off;
    }
    if (predicted > max) {
      const off = (predicted - max) % width;
      return min + off;
    }
    return current;
  }

  private killParticle(slot: number): void {
    if (this.age[slot] < 0) return;
    this.age[slot] = -1;
    this.alive--;
  }

  dispose(): void {
    this.points.parent?.remove(this.points);
    this.points.geometry.dispose();
    this.material.dispose();
    // Texture lifetime is managed by the ParticleScene's texture cache
    // (shared across systems that point at the same DDS) — don't dispose
    // it here.
  }
}

class LightRenderer {
  readonly group: THREE.Group;
  readonly sprite: THREE.Sprite;
  pointLight: THREE.PointLight | null = null;
  readonly score: number;
  private elapsed = 0;
  private active = true;
  private readonly material: THREE.SpriteMaterial;

  constructor(private readonly body: ParticleComponentBody) {
    this.group = new THREE.Group();
    this.group.name = 'particle-light';
    const pos = body.localPosition;
    if (Array.isArray(pos) && pos.length === 3) {
      this.group.position.set(pos[0], pos[1], pos[2]);
    }
    this.material = new THREE.SpriteMaterial({
      color: new THREE.Color(1, 1, 1),
      opacity: 1,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      depthTest: true,
    });
    this.sprite = new THREE.Sprite(this.material);
    this.group.add(this.sprite);
    this.score = this.estimateScore();
    this.applySample(0);
  }

  enablePointLight(): void {
    if (this.pointLight) return;
    this.pointLight = new THREE.PointLight(0xffffff, 1, 1, 2);
    this.group.add(this.pointLight);
    this.applySample(this.elapsed);
  }

  setActive(active: boolean): void {
    this.active = active;
    this.group.visible = active;
  }

  tick(dt: number): void {
    if (!this.active) return;
    this.elapsed += dt;
    this.applySample(this.elapsed);
  }

  dispose(): void {
    this.group.parent?.remove(this.group);
    this.material.dispose();
  }

  private estimateScore(): number {
    const fixed = this.body.color ?? [1, 1, 1, 1];
    let peak = Math.max(0, fixed[0], fixed[1], fixed[2]);
    for (const p of this.body.colorAnimation?.points ?? []) {
      peak = Math.max(peak, p.r, p.g, p.b);
    }
    let radius = Math.max(0, this.body.radius ?? 0);
    for (const p of this.body.radiusAnimation?.points ?? []) {
      radius = Math.max(radius, p.value);
    }
    return peak * Math.max(0.1, radius);
  }

  private applySample(t: number): void {
    const color = this.sampleColorAt(t);
    const radius = Math.max(0.01, this.sampleRadiusAt(t));
    this.material.color.setRGB(color[0], color[1], color[2]);
    this.material.opacity = Math.max(0, Math.min(1, color[3]));
    const spriteSize = Math.max(0.1, radius * 2);
    this.sprite.scale.set(spriteSize, spriteSize, spriteSize);
    if (!this.pointLight) return;
    const r = Math.max(0, color[0]);
    const g = Math.max(0, color[1]);
    const b = Math.max(0, color[2]);
    const peak = Math.max(r, g, b);
    if (peak > 0) {
      this.pointLight.color.setRGB(r / peak, g / peak, b / peak);
      this.pointLight.intensity = peak * Math.max(0, color[3]);
      this.pointLight.distance = radius;
      this.pointLight.visible = this.group.visible;
    } else {
      this.pointLight.intensity = 0;
    }
  }

  private sampleColorAt(t: number): [number, number, number, number] {
    const out = LightRenderer.TMP_COLOR;
    const period = this.body.colorAnimationPeriod ?? 0;
    const axis = this.body.animatedColor && period > 0 ? t % period : t;
    sampleColor(this.body.animatedColor ? this.body.colorAnimation : undefined, axis, out);
    if (!this.body.animatedColor || !this.body.colorAnimation?.points?.length) {
      const fixed = this.body.color ?? [1, 1, 1, 1];
      out[0] = fixed[0];
      out[1] = fixed[1];
      out[2] = fixed[2];
      out[3] = fixed[3];
    }
    return [out[0], out[1], out[2], out[3]];
  }

  private sampleRadiusAt(t: number): number {
    const period = this.body.radiusAnimationPeriod ?? 0;
    const axis = this.body.animatedRadius && period > 0 ? t % period : t;
    return this.body.animatedRadius
      ? sampleRamp(this.body.radiusAnimation, axis, this.body.radius ?? 1)
      : (this.body.radius ?? 1);
  }

  private static readonly TMP_COLOR = new Float32Array(4);
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
  /** animation.randomFrameOnly — when set, each particle shows ONE fixed
   *  random atlas cell for its whole life (no flipbook). Engine
   *  FUN_14071b7f0 @0x14071c5b6 leaves the spawn-seeded random frame byte
   *  and skips the integral/modulus (RE doc 63 H5). The cell is chosen
   *  per-particle at spawn via the `frameSeed` vertex attribute. */
  randomFrameOnly?: boolean;
  /** animation.frameRateRamp — a ramp of frames-per-second over particle
   *  age (RE doc 63 L1). SystemRenderer integrates this per particle and
   *  passes the accumulated frame position through the `framePhase`
   *  attribute. */
  frameRateRamp?: ParticleRamp;
  /** Renderer.yawRateRamp support. When enabled, the fragment shader rotates
   *  textured sprite UVs by the per-particle `rotationPhase` attribute. */
  spriteRotation?: boolean;
  /** PS_RRC pivot label: bottom / corner / center / custom. */
  rotationCenter?: string;
  /** Renderer.customCenterOffset (+0x3c Vec2), used for custom pivots. */
  customCenterOffset?: [number, number];
  /** Renderer.scaleX (+0x60): sprite width multiplier relative to height. */
  scaleX?: number;
  /** Renderer.opacityMultiplier (+0x74): final alpha multiplier. */
  opacityMultiplier?: number;
  /** Renderer.tilingU/V (+0x90/+0x94): repeat local sprite UVs. */
  tilingU?: number;
  tilingV?: number;
  /** Renderer.flipTexcoordU/V (+0x9c/+0x9d): mirror local sprite UVs. */
  flipTexcoordU?: boolean;
  flipTexcoordV?: boolean;
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
      // RE doc 63 H3/M1: these are screen-space DISTORTION passes (water-deform
      // / heat-haze refraction) with their own NON-additive engine techniques —
      // not in the 0x2e8 order-dependent set, not additive. We don't model the
      // refraction; render alpha-over (NormalBlending) so the sprite occludes
      // faintly instead of an additive bloom that washes out the whole burst.
      // (Full fix = a background-RTT UV-warp pass driven by tex0/tex1.)
      return { blending: THREE.NormalBlending };
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

function rotationPivotForCenter(
  label: string | undefined,
  customOffset: [number, number] | undefined,
): THREE.Vector2 {
  switch (label) {
    case 'bottom':
      return new THREE.Vector2(0.5, 0.0);
    case 'corner':
      return new THREE.Vector2(0.0, 0.0);
    case 'custom':
      return new THREE.Vector2(
        0.5 + (customOffset?.[0] ?? 0),
        0.5 + (customOffset?.[1] ?? 0),
      );
    case 'center':
    default:
      return new THREE.Vector2(0.5, 0.5);
  }
}

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
  // L1 (RE doc 63): the engine drives the flipbook from `frameRateRamp`
  // (fps over particle age, trapezoid-integrated), NOT `animationPeriod`.
  // SystemRenderer supplies the integrated frame position through the
  // framePhase attribute. Keep the representative frameRate as a fallback for
  // callers that build a material without runtime-integrated particles.
  let frameRate = 0;
  const rrPts = opts.frameRateRamp?.points;
  if (rrPts && rrPts.length > 0) {
    let acc = 0;
    for (const p of rrPts) acc += p.value;
    frameRate = acc / rrPts.length;
  }
  // L2: the engine wraps the frame by `framesRangeEnd` then adds
  // `framesRangeBegin` (@0x14071c5ee IDIV [framesRangeEnd]; ADD [framesRangeBegin]).
  // Carry both raw bounds to the shader so the cell math matches; the old
  // (end - begin) modulus was wrong.
  const framesBegin = opts.framesRangeBegin ?? 0;
  const framesEnd = opts.framesRangeEnd ?? 0;
  const spriteRotation = opts.spriteRotation ? 1 : 0;
  const rotationPivot = rotationPivotForCenter(opts.rotationCenter, opts.customCenterOffset);
  const spriteAspectX = Math.max(0.001, Math.abs(opts.scaleX ?? 1));
  const pointExtent = spriteRotation
    ? Math.sqrt(spriteAspectX * spriteAspectX + 1)
    : Math.max(spriteAspectX, 1);
  const opacityMultiplier =
    opts.opacityMultiplier !== undefined && opts.opacityMultiplier > 0 ? opts.opacityMultiplier : 1;
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      map: { value: null as THREE.Texture | null },
      useMap: { value: 0 },
      // LUT (textureName1) for GRADIENT_MAP / UNDERWATER_GRADIENT_MAP.
      // useLut=1 routes the fragment shader through the LUT remap.
      // Initialised to 0 and flipped to 1 by bindLutTexture only after
      // the LUT DDS loads successfully. BC6H HDR ramps are software-decoded
      // by the DDS worker; failing-but-still-set useLut would render black
      // against the null sampler.
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
        value: new THREE.Vector2(framesBegin, framesEnd),
      },
      // L1/L2/L4 (RE doc 63): authored frame rate (fps, collapsed from
      // frameRateRamp) + the raw range bounds. The shader uses
      // `cell = mod(floor(framePhase), uFramesEnd) + uFramesBegin`,
      // cross-fading by fract(framePhase). `animationPeriod` is never read by
      // the engine; the uniform is kept (unused) so the option type stays
      // stable.
      uFrameRate: { value: frameRate },
      uUseFramePhase: { value: opts.frameRateRamp?.points?.length ? 1 : 0 },
      uFramesBegin: { value: framesBegin },
      uFramesEnd: { value: framesEnd },
      // L4: now-unused (engine never loads animationPeriod). Retained so the
      // option type and any external callers don't break.
      animationPeriod: { value: opts.animationPeriod ?? 0 },
      // PS_PAT gate (gridEnabled): suppress the flipbook grid for noAnimation
      // so a single-frame sprite (logo) shows whole instead of a cropped cell.
      useFrameGrid: { value: gridEnabled ? 1 : 0 },
      // H5 (RE doc 63): randomFrameOnly → each particle shows one fixed
      // random cell (selected from the per-particle `frameSeed` attribute),
      // no time advance. Takes precedence over the animated flipbook.
      uRandomFrame: { value: opts.randomFrameOnly ? 1 : 0 },
      // Renderer.yawRateRamp: per-particle sprite UV rotation. The point size
      // expands by sqrt(2) in the vertex shader so a rotated square sprite does
      // not clip inside the fixed GL_POINT bounds.
      uUseSpriteRotation: { value: spriteRotation },
      uRotationPivot: { value: rotationPivot },
      uSpriteAspectX: { value: spriteAspectX },
      uPointExtent: { value: pointExtent },
      uUvTiling: { value: new THREE.Vector2(opts.tilingU ?? 1, opts.tilingV ?? 1) },
      uUvFlip: {
        value: new THREE.Vector2(opts.flipTexcoordU ? 1 : 0, opts.flipTexcoordV ? 1 : 0),
      },
      // Corpus default is 0.0 on most systems; native treats that as neutral
      // rather than invisible. Positive values are authored boosts/cuts.
      uOpacityMultiplier: { value: opacityMultiplier },
      // Directional-lightmap (PS_RLT) reconstruction. uLightingMode=1 when
      // the texture is an `_LM` lightmap (lightmapping4Way/HL2); the
      // fragment shader then treats `map` RGB as a 3-direction HL2-basis
      // lightmap reconstructed against uSunDirWorld, not albedo. The sun
      // dir defaults to the scene's key DirectionalLight (scene.ts:121-122,
      // positioned at (50,80,50)) so particle lighting matches the hull.
      uLightingMode: {
        value: PS_RLT_LIGHTMAP_MODES.has(opts.lightingType ?? '') ? 1 : 0,
      },
      // TODO M2/H7 (RE doc 63): replace the hard-coded sun dir/scalar with the
      // live scene DirectionalLight (scene.ts:125-126) world direction + a
      // sunColor/(luma+1) vec3 in place of uSmokeLightScale. No-op today: the
      // scene sun is exactly (50,80,50), white — so this constant IS correct.
      // Wiring a live per-frame uniform across scene.ts isn't worth it yet.
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
      // DEFORM_WATER_SURFACE / SHIMMER are screen-space distortion passes whose
      // tex0 is a normal/deform map (not albedo) — we can't do the refraction,
      // so render a faint white foam/haze hint rather than the raw blue deform
      // texture as colour (RE doc 63 H3/M1; see the fragment shader).
      uDistortion: {
        value:
          opts.blendType === 'DEFORM_WATER_SURFACE' || opts.blendType === 'SHIMMER' ? 1 : 0,
      },
      // Warm "detonation glow" strength for the lightmapping + GRADIENT_MAP
      // path. RE doc 63 corrected the old pinned-U=0 theory: lightmapping
      // samples the ramp at U = 1 - glow, where glow comes from the sprite
      // texture before relight. The engine also scales this emissive term by
      // a per-particle intensity (v10.x) the parser does not carry, so this
      // constant approximates that missing scale. 0.15 keeps the puff a grey
      // occluding smoke body with a warm core.
      uGlowStrength: { value: 0.15 },
    },
    vertexShader: /* glsl */ `
      attribute vec4 color;
      attribute float size;
      attribute float age;
      attribute float frameSeed;
      attribute float framePhase;
      attribute float rotationPhase;
      uniform float uUseSpriteRotation;
      uniform float uPointExtent;
      varying vec4 vColor;
      varying float vAge;
      varying float vFrameSeed;
      varying float vFramePhase;
      varying float vRotationPhase;

      void main() {
        vColor = color;
        vAge = age;
        vFrameSeed = frameSeed;
        vFramePhase = framePhase;
        vRotationPhase = rotationPhase;
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mvPosition;
        // size is metres (world space); divide by distance for perspective.
        // uPointExtent expands the square GL_POINT enough to contain scaleX
        // and the worst-case rotated bounding square when sprite rotation is on.
        gl_PointSize = max(1.0, size * uPointExtent * 200.0 / -mvPosition.z);
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
      uniform float uFrameRate;
      uniform float uUseFramePhase;
      uniform float uFramesBegin;
      uniform float uFramesEnd;
      uniform float animationPeriod;
      uniform float useFrameGrid;
      uniform float uRandomFrame;
      uniform float uUseSpriteRotation;
      uniform vec2 uRotationPivot;
      uniform float uSpriteAspectX;
      uniform float uPointExtent;
      uniform vec2 uUvTiling;
      uniform vec2 uUvFlip;
      uniform float uOpacityMultiplier;
      uniform float uLightingMode;
      uniform vec3 uSunDirWorld;
      uniform sampler2D mvMap;
      uniform float useMv;
      uniform float mvDistortion;
      uniform float useEmissionAlphaFromMV;
      uniform float uPremultiply;
      uniform float uDistortion;
      uniform float uGlowStrength;
      uniform float uSmokeLightScale;
      varying vec4 vColor;
      varying float vAge;
      varying float vFrameSeed;
      varying float vFramePhase;
      varying float vRotationPhase;

      void main() {
        vec4 base;
        vec3 glow = vec3(0.0);   // additive warm glow (gradient+lightmapping)
        if (useMap > 0.5) {
          // Convert the square GL_POINT coordinate to authored sprite UVs.
          // Geometry is measured in sprite-height units: width=scaleX,
          // height=1. Rotation happens in that geometric space so rectangular
          // sprites and custom pivots stay coherent.
          vec2 pointGeom = (gl_PointCoord - vec2(0.5)) * uPointExtent;
          vec2 pivotGeom = vec2(
            (uRotationPivot.x - 0.5) * uSpriteAspectX,
            uRotationPivot.y - 0.5
          );
          vec2 spriteGeom = pointGeom;
          if (uUseSpriteRotation > 0.5) {
            // GL_POINTS cannot rotate the quad geometry. Enlarge the point in
            // the vertex shader, rotate source geometry by the inverse angle,
            // then sample the unrotated sprite UVs.
            vec2 rel = pointGeom - pivotGeom;
            float s = sin(vRotationPhase);
            float c = cos(vRotationPhase);
            spriteGeom = pivotGeom + vec2(c * rel.x + s * rel.y, -s * rel.x + c * rel.y);
          }
          vec2 local = vec2(spriteGeom.x / uSpriteAspectX + 0.5, spriteGeom.y + 0.5);
          if (local.x < 0.0 || local.x > 1.0 || local.y < 0.0 || local.y > 1.0) discard;
          local = mix(local, vec2(1.0) - local, uUvFlip);
          if (abs(uUvTiling.x - 1.0) > 0.0001 || abs(uUvTiling.y - 1.0) > 0.0001) {
            local = fract(local * uUvTiling);
          }
          float fx = framesPerXY.x;
          float fy = framesPerXY.y;
          // L4 (RE doc 63): the engine never reads animationPeriod. Gate the
          // flipbook on a real authored frame rate + range end instead.
          bool hasGrid = (useFrameGrid > 0.5 && fx * fy > 1.0);
          bool animated = (hasGrid && uFramesEnd > 0.0 && (uUseFramePhase > 0.5 || uFrameRate > 0.0));
          // H5 (RE doc 63): a randomFrameOnly system is NOT animated — each
          // particle freezes on its spawn-seeded cell. Takes precedence.
          bool randomCell = (hasGrid && uRandomFrame > 0.5);
          float mvEmissive = 0.0;   // additive emission from _MVEA.R (if on)

          if (randomCell) {
            // Fixed per-particle random cell (no time advance, no cross-fade).
            // vFrameSeed was assigned floor(rand()*framesRangeEnd) at spawn.
            vec2 puv = (vec2(mod(vFrameSeed, fx), floor(vFrameSeed / fx)) + local) / vec2(fx, fy);
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
          } else if (animated && useMv > 0.5) {
            // Motion-vector flipbook blend (WG _MVEA): sample the two
            // adjacent frames, warp each along the per-pixel optical-flow
            // field stored in the MV texture's (G,B) channels, and cross-
            // fade by the inter-frame fraction. Decode is (G,B)*2-1;
            // mvDistortion scales the warp. RE'd instruction-for-instruction
            // from particles.win.dx11.fxo (20 motion-vector PS permutations,
            // identical math). Replaces the hard age-driven frame step.
            // L1/L2: frame law is now frameRate-driven and wrapped by
            // framesRangeEnd + framesRangeBegin (was period-driven, wrapped by
            // end-begin). The MV warp/cross-fade is unchanged.
            float idxF = (uUseFramePhase > 0.5) ? vFramePhase : vAge * uFrameRate;
            float f = fract(idxF);
            float fl = floor(idxF);
            float n0 = mod(fl, uFramesEnd) + uFramesBegin;
            float n1 = mod(fl + 1.0, uFramesEnd) + uFramesBegin;
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
          } else if (animated) {
            // Age-driven flipbook (framesPlayback / no MV texture), composed
            // with the manifest atlas-rect mapping when present. L1/L2: frame
            // = floor(framePhase) mod framesRangeEnd + framesRangeBegin.
            // L3: cross-fade the floored cell into the next by fract(framePhase)
            // (the engine writes blend byte +0x7d = frac*255 for any nonzero
            // animationType; the older non-MV branch hard-popped).
            float idxF = (uUseFramePhase > 0.5) ? vFramePhase : vAge * uFrameRate;
            float f = fract(idxF);
            float fl = floor(idxF);
            float n0 = mod(fl, uFramesEnd) + uFramesBegin;
            float n1 = mod(fl + 1.0, uFramesEnd) + uFramesBegin;
            vec2 grid = vec2(fx, fy);
            vec2 puv0 = (vec2(mod(n0, fx), floor(n0 / fx)) + local) / grid;
            vec2 puv1 = (vec2(mod(n1, fx), floor(n1 / fx)) + local) / grid;
            if (useAtlasRect > 0.5) {
              puv0 = mix(atlasRect.xy, atlasRect.zw, puv0);
              puv1 = mix(atlasRect.xy, atlasRect.zw, puv1);
            }
            base = mix(texture2D(map, puv0), texture2D(map, puv1), f);
          } else {
            // Static cell. H4 (RE doc 63): a noAnimation system on a multi-cell
            // atlas shows CELL 0 only (engine forces the frame byte to 0,
            // FUN_14071b7f0 @0x14071c5a3) — without this the whole grid crams
            // into one quad and reads as garbage. fx*fy==1 → full texture.
            vec2 puv = local;
            if (fx * fy > 1.0) {
              puv = puv / vec2(fx, fy);
            }
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
          }
          // M3 (RE doc 63, ps4.txt:614-628): the GRADIENT_MAP+lightmapping glow
          // samples the ramp at U = 1 - glow, where glow is the particle
          // texture's value at the sprite UV BEFORE the LM relight overwrites
          // base. Capture it here (the relight block below replaces base.rgb).
          float gmag = base.r;
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
            float flatLum = (base.r + base.g + base.b) / 3.0;
            // RE doc 63 H6: the engine does NOT energy-normalize (no /wsum) and
            // has no 30% flat mix — lit = saturate(Σ LMi·(axisi·sun)²) via
            // mad_sat (ps4.txt:789-794). The old /wsum cancelled the directional
            // magnitude (constant flat shade) and the 0.30 mix washed it out.
            // Keep only a small additive ambient floor so fully back-lit sprites
            // are not crushed to black.
            float lit = clamp(dot(w, base.rgb), 0.0, 1.0) + 0.06 * flatLum;
            // Sun-color Reinhard normalization × lighting factor (RE): keeps
            // the relit smoke DARK so it occludes, rather than ~2x too bright
            // once the authored HDR tint multiplies in.
            lit *= uSmokeLightScale;
            base = vec4(vec3(lit), base.a);
          }
          if (useLut > 0.5) {
            if (uLightingMode > 0.5) {
              // GRADIENT_MAP + lightmapping (RE doc 63 M3, ps4.txt:614-628;
              // corrects the prior "U pinned to 0"): the engine samples the HDR
              // ramp at U = 1 - glow (glow = the particle texture value at the
              // sprite UV, captured as gmag before the LM relight). The warm
              // ramp colour is added as an emissive "detonation" glow on top of
              // the relit smoke body, OUTSIDE the per-particle tint (engine:
              // rgb = base*lit + emis*v10.x). The posterize step-count (bits
              // 16..23) isn't carried by the parser, so we use the unposterized
              // U. uGlowStrength stays as the v10.x intensity approximation.
              // Now varies per-texel → a warm GRADIENT across the sprite, not
              // one flat tan colour.
              vec4 g = texture2D(lut, vec2(1.0 - gmag, 0.5));
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
        if (uDistortion > 0.5) {
          // No refraction pass: show a faint white foam/haze hint instead of the
          // raw blue normal/deform texture as opaque colour (RE doc 63 H3/M1).
          outRgb = vec3(1.0);
          outA = vColor.a * base.a * 0.15;
        }
        outA *= uOpacityMultiplier;
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
  /** Decoded kind=light components rendered as glow sprites, with the
   *  strongest subset also promoted to real point lights. */
  lights: LightRenderer[];
  /** The parsed source record this attachment renders. Carries the
   *  authoring data the UI inspector needs (renderer.textureName0,
   *  general.capacity, components[].action, …) without poking through
   *  SystemRenderer internals. */
  record: ParticleRecord;
  active: boolean;
}

interface SpawnedParticleEffect {
  parent: ParticleAttachmentHandle;
  group: THREE.Group;
  systems: SystemRenderer[];
  lights: LightRenderer[];
  depth: number;
}

type ParticleQuality = 'high' | 'low' | 'shared';

interface ParticleEffectRef {
  path: string;
  quality: ParticleQuality;
}

function normalizeParticleEffectPath(path: string): string {
  return parseParticleEffectRef(path).path;
}

function parseParticleEffectRef(path: string): ParticleEffectRef {
  let p = path.replace(/\\/g, '/').trim().replace(/^\/+/, '');
  if (p.startsWith('?')) p = p.slice(1);
  let quality: ParticleQuality = 'high';
  const suffix = p.slice(p.lastIndexOf('/') + 1);
  if (suffix === 'high' || suffix === 'low' || suffix === 'shared') {
    quality = suffix;
    p = p.slice(0, p.lastIndexOf('/'));
  }
  return { path: p, quality };
}

function particleRecordCacheKey(path: string, quality: ParticleQuality): string {
  return `${path}#${quality}`;
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
  private particleRecords = new Map<string, ParticleRecord>();
  private particleRecordFetches = new Map<string, Promise<ParticleRecord | null>>();
  private spawnedEffects: SpawnedParticleEffect[] = [];

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
    this.particleRecords.clear();
    this.particleRecordFetches.clear();
    for (const [path, record] of Object.entries(particles)) {
      const effectRef = parseParticleEffectRef(path);
      this.particleRecords.set(particleRecordCacheKey(effectRef.path, effectRef.quality), record);
      if (effectRef.quality === 'high') this.particleRecords.set(effectRef.path, record);
    }
    const handles: ParticleAttachmentHandle[] = [];
    for (let i = 0; i < attachments.length; i++) {
      const a = attachments[i];
      const particlePath = normalizeParticleEffectPath(a.particle_path);
      const rec = particles[a.particle_path] ?? particles[particlePath];
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
      let handle: ParticleAttachmentHandle;
      const instantiated = this.instantiateRecordSystems(
        rec,
        grp,
        false,
        true,
        (request) => {
          void this.spawnChildEffect(handle, grp, request, 0);
        },
      );
      handle = {
        attachment: a,
        group: grp,
        systems: instantiated.systems,
        lights: instantiated.lights,
        record: rec,
        active: false,
      };
      const key = `${a.group}:${a.node}:${i}`;
      this.attachments.set(key, handle);
      handles.push(handle);
    }
    const pointLights = handles
      .flatMap((h) => h.lights)
      .sort((a, b) => b.score - a.score)
      .slice(0, PARTICLE_POINT_LIGHT_BUDGET);
    for (const l of pointLights) l.enablePointLight();
    return handles;
  }

  private instantiateRecordSystems(
    rec: ParticleRecord,
    group: THREE.Group,
    active: boolean,
    loopOneShot: boolean,
    spawnEffect?: ParticleEffectSpawnCallback,
  ): { systems: SystemRenderer[]; lights: LightRenderer[] } {
    const systems: SystemRenderer[] = [];
    const lights: LightRenderer[] = [];
    for (const sys of rec.systems) {
      const r = sys.renderer;
      const anim = sys.animation;
      const systemParent = systemUsesDetachedCoordinateFrame(sys) ? this.root : group;
      // Texture source: prefer the direct DDS URL when present (the
      // texture was extracted as its own file); otherwise route through the
      // manifest atlas mapping. Both paths compose with the animation grid.
      const useAtlas = !r?.textureUrl0 && !!r?.textureAtlas0;
      const useLut = !!r?.blendType && PS_RBT_LUT_MODES.has(r.blendType) && !!r?.textureUrl1;
      const useMv = anim?.animationType === 'motionVectors' && !!anim?.motionVectorsTextureUrl;
      const useSpriteRotation = (!!r?.textureUrl0 || useAtlas) && rampHasNonZeroValue(r?.yawRateRamp);
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
        randomFrameOnly: anim?.randomFrameOnly,
        frameRateRamp: anim?.frameRateRamp,
        spriteRotation: useSpriteRotation,
        rotationCenter: r?.rotationCenter,
        customCenterOffset: r?.customCenterOffset,
        scaleX: r?.scaleX,
        opacityMultiplier: r?.opacityMultiplier,
        tilingU: r?.tilingU,
        tilingV: r?.tilingV,
        flipTexcoordU: r?.flipTexcoordU,
        flipTexcoordV: r?.flipTexcoordV,
      });
      const renderer = new SystemRenderer(sys, mat, rec.maxEmittingDuration, {
        spawnEffect,
        loopOneShot,
        sourceGroup: group,
        rootGroup: this.root,
      });
      renderer.setActive(active);
      systemParent.add(renderer.points);
      systems.push(renderer);
      for (const c of sys.components ?? []) {
        if (c.kind !== 'light' || !c.body) continue;
        const light = new LightRenderer(c.body);
        light.setActive(active);
        if (systemParent !== group) {
          group.updateWorldMatrix(true, false);
          systemParent.updateWorldMatrix(true, false);
          group.localToWorld(light.group.position);
          systemParent.worldToLocal(light.group.position);
        }
        systemParent.add(light.group);
        lights.push(light);
      }
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
    return { systems, lights };
  }

  private async loadParticleRecord(
    path: string,
    quality: ParticleQuality,
  ): Promise<ParticleRecord | null> {
    const normalized = normalizeParticleEffectPath(path);
    const key = particleRecordCacheKey(normalized, quality);
    const cached =
      this.particleRecords.get(key) ??
      (quality === 'high' ? this.particleRecords.get(normalized) : undefined);
    if (cached) return cached;
    let pending = this.particleRecordFetches.get(key);
    if (!pending) {
      pending = fetchParticleRecord(normalized, quality).catch((err) => {
        console.warn('[particles] child effect record load failed', normalized, quality, err);
        return null;
      });
      this.particleRecordFetches.set(key, pending);
    }
    const record = await pending;
    if (record) {
      this.particleRecords.set(key, record);
      if (quality === 'high') this.particleRecords.set(normalized, record);
    }
    return record;
  }

  private async spawnChildEffect(
    parent: ParticleAttachmentHandle,
    parentGroup: THREE.Group,
    request: ParticleEffectSpawnRequest,
    depth: number,
  ): Promise<void> {
    if (!parent.active || depth >= CHILD_EFFECT_DEPTH_LIMIT) return;
    if (this.spawnedEffects.length >= CHILD_EFFECT_BUDGET) return;
    const effectRef = parseParticleEffectRef(request.effectName);
    const effectPath = effectRef.path;
    if (!effectPath.endsWith('.xml')) return;
    const rec = await this.loadParticleRecord(effectPath, effectRef.quality);
    if (!rec || !parent.active) return;

    const group = new THREE.Group();
    group.name = `spawn:${effectPath}`;
    group.position.set(request.position[0], request.position[1], request.position[2]);
    parentGroup.add(group);
    const spawned: SpawnedParticleEffect = {
      parent,
      group,
      systems: [],
      lights: [],
      depth: depth + 1,
    };
    const instantiated = this.instantiateRecordSystems(
      rec,
      group,
      parent.active,
      false,
      (nextRequest) => {
        void this.spawnChildEffect(parent, group, nextRequest, spawned.depth);
      },
    );
    spawned.systems = instantiated.systems;
    spawned.lights = instantiated.lights;
    this.spawnedEffects.push(spawned);
  }

  private disposeSpawnedEffect(effect: SpawnedParticleEffect): void {
    for (const s of effect.systems) s.dispose();
    for (const l of effect.lights) l.dispose();
    effect.group.parent?.remove(effect.group);
  }

  private pruneFinishedSpawnedEffects(): void {
    for (let i = this.spawnedEffects.length - 1; i >= 0; i--) {
      const effect = this.spawnedEffects[i];
      if (!effect.systems.every((s) => s.isFinished)) continue;
      this.disposeSpawnedEffect(effect);
      this.spawnedEffects.splice(i, 1);
    }
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
      for (const l of handle.lights) l.tick(dt);
    }
    for (const effect of this.spawnedEffects) {
      if (!effect.parent.active) continue;
      for (const s of effect.systems) s.tick(dt);
      for (const l of effect.lights) l.tick(dt);
    }
    this.pruneFinishedSpawnedEffects();
  }

  /** Toggle one attachment on or off. */
  setAttachmentActive(handle: ParticleAttachmentHandle, active: boolean): void {
    handle.active = active;
    for (const s of handle.systems) s.setActive(active);
    for (const l of handle.lights) l.setActive(active);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setActive(active);
      for (const l of effect.lights) l.setActive(active);
      effect.group.visible = active;
    }
    handle.group.visible = active;
  }

  setAttachmentParentVelocity(handle: ParticleAttachmentHandle, velocityWorld: THREE.Vector3): void {
    for (const s of handle.systems) s.setParentVelocityWorld(velocityWorld);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setParentVelocityWorld(velocityWorld);
    }
  }

  /** Toggle every attachment on/off. */
  setAllActive(active: boolean): void {
    for (const h of this.attachments.values()) this.setAttachmentActive(h, active);
  }

  clear(): void {
    for (const effect of this.spawnedEffects) this.disposeSpawnedEffect(effect);
    this.spawnedEffects = [];
    for (const handle of this.attachments.values()) {
      for (const s of handle.systems) s.dispose();
      for (const l of handle.lights) l.dispose();
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
