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
  ParticleSystemIntensityConfig,
  ParticleSystemIntensityChannel,
  ParticleValueGenerator,
  ParticleVariantVg,
  ParticleVgtPrototype,
} from '$lib/types/sidecar';
import { fetchParticleRecord, repoUrl } from '$lib/api';
import { loadDdsMipChain, loadDdsSoftwareRgbaTexture } from '$lib/dds';

const DEFAULT_PARTICLE_LIFETIME = 4.0; // seconds, when WG didn't author one
const ABSOLUTE_MAX_CAPACITY = 512; // hard cap per system
// The toolkit exports ship geometry/bones/rig pivots scaled by this factor
// (compose/turret_autorig.py:254 NATIVE_TO_METRES) so the GLB is in metres
// (Baltimore hull = 205.8 m, ~15× its native BigWorld-unit length). Particle
// records, however, carry RAW native BigWorld-unit lengths (the faithful
// engine decode). The native engine has no ×15 — it renders everything in one
// BW-unit space (RE: FUN_1406d29c0 builds the billboard corner as
// worldPos + billboardAxis·(size·tiling), so SIZE is a world-space length in
// the SAME unit as position). Because the consumer's world is ×15 metres,
// every length-dimensioned particle quantity — size, spawn offset, velocity,
// and all velocity-derived displacement — must be ×15 to sit correctly on the
// ship. The sim runs in raw record units and this factor is applied to its
// OUTPUT (and the few world-frame INPUTS are divided back). NOT applied to
// times, dimensionless multipliers (dampfer/ageScale), sprite-space offsets
// (customCenterOffset, tiling), or colour.
const NATIVE_TO_METRES = 15;
// Native per-particle update substep ceiling, seconds (FUN_140718f00 clamps
// every integration substep to DAT_142556548 = 0.25; RE 2026-06-09).
const NATIVE_SUBSTEP_MAX_S = 0.25;
const DEFAULT_SIZE = 0.02; // native BW units (≈0.3 m after NATIVE_TO_METRES) —
// sane baseline if the particle didn't author a size generator
const HARD_MAX_EMIT_RATE_HZ = 200; // safety clamp on the per-frame
// particles-emitted count
const PARTICLE_POINT_LIGHT_BUDGET = 24;
const CHILD_EFFECT_DEPTH_LIMIT = 3;
const CHILD_EFFECT_BUDGET = 256;
const CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK = 8;
const SEA_LEVEL_Y = 0;
const DEFAULT_PARTICLE_SUN_DIR = new THREE.Vector3(50, 80, 50).normalize();
const DEFAULT_PARTICLE_SUN_COLOR_NORM = new THREE.Color(0.5, 0.5, 0.5);

const PS_IC_PARTICLE_TILING_U = 0;
const PS_IC_LIGHT_TINT_R = 1;
const PS_IC_PARTICLE_STREAMER_X = 2;
const PS_IC_PARTICLE_SCALE_X = 3;
const PS_IC_PARTICLE_VEL_Z = 4;
const PS_IC_LIGHT_RADIUS = 5;
const PS_IC_LIGHT_TINT_B = 6;
const PS_IC_PARTICLE_COLOR_R = 7;
const PS_IC_AGE_SCALE = 8;
const PS_IC_PARTICLE_COLOR_B = 9;
const PS_IC_PARTICLE_VEL_Y = 10;
const PS_IC_PARTICLE_TILING_V = 11;
const PS_IC_PARTICLE_COLOR_A = 12;
const PS_IC_PARTICLE_TINT_G = 13;
const PS_IC_AGE_AUX_SCALE = 14;
const PS_IC_PARTICLE_TINT_B = 15;
const PS_IC_PARTICLE_SCALE_Y = 16;
const PS_IC_PARTICLE_STREAMER_Y = 17;
const PS_IC_PARTICLE_TINT_R = 18;
const PS_IC_EMITTER_RATE = 19;
const PS_IC_PARTICLE_VEL_X = 20;
const PS_IC_PARTICLE_STREAMER_Z = 21;
const PS_IC_PARTICLE_COLOR_G = 22;
const PS_IC_PARTICLE_SIZE = 23;
const PS_IC_PARTICLE_TINT_A = 24;
const PS_IC_LIGHT_TINT_G = 25;
const PS_RBT_DEPTH_SORT_MODES = new Set([
  'BLENDED_UNDERWATER',
  'UNDERWATER_GRADIENT_MAP',
  'BLENDED_GLOW',
  'GRADIENT_MAP',
  'BLENDED',
]);

function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function particleByteStepCount(value: unknown): number {
  const raw = Math.trunc(finiteNumber(value, 0));
  return raw < 2 ? 0 : Math.min(raw - 1, 255);
}

/** Detect a VESTIGIAL sprite-sheet grid — the only reliable flipbook-vs-single
 *  sprite discriminator. RE (2026-06-21, wf_9fc303df-cab) proved the engine has
 *  NO metadata gate: `FUN_14071cec0` copies framesPerX/Y verbatim and always
 *  samples cell 0 for noAnimation, so a single centered logo mis-authored with a
 *  framesPerX/Y grid (e.g. BA_Logo.dds, 7x7 + noAnimation) samples a transparent
 *  corner and is (near-)invisible in STOCK WoWS too — a WG authoring bug. Since
 *  framesPerX=7+noAnimation is bit-identical to a real 7x7 sheet, the grid can
 *  only be judged vestigial from CONTENT: cell (0,0) over [0,1/fx]x[0,1/fy] is
 *  ~fully transparent while the texture has real opaque coverage elsewhere.
 *  Returns false when decoded pixels are unavailable (GPU-compressed fallback,
 *  no `.image.data`) → the engine-faithful cell-0 crop is kept. */
function spriteSheetCell0Empty(tex: THREE.Texture, fx: number, fy: number): boolean {
  const img = (tex as { image?: { data?: ArrayLike<number>; width?: number; height?: number } }).image;
  const data = img?.data;
  const W = img?.width ?? 0;
  const H = img?.height ?? 0;
  if (!(data instanceof Uint8Array || data instanceof Uint8ClampedArray) || W < fx || H < fy) {
    return false;
  }
  const cw = Math.max(1, Math.floor(W / fx));
  const ch = Math.max(1, Math.floor(H / fy));
  const A = 16; // alpha threshold (0..255) for "opaque enough to be content"
  let cell0 = 0;
  for (let y = 0; y < ch; y++) {
    for (let x = 0; x < cw; x++) if (data[(y * W + x) * 4 + 3] > A) cell0++;
  }
  if (cell0 / (cw * ch) >= 0.01) return false; // cell 0 has content → a real frame → crop
  // cell 0 is ~empty; confirm the texture isn't just blank (subsampled scan).
  const step = Math.max(1, Math.floor(Math.min(W, H) / 128));
  let full = 0;
  let tot = 0;
  for (let y = 0; y < H; y += step) {
    for (let x = 0; x < W; x += step) {
      tot++;
      if (data[(y * W + x) * 4 + 3] > A) full++;
    }
  }
  return full / tot > 0.03; // empty cell 0 + real content elsewhere = vestigial grid
}

function hasNonZeroNumber(value: unknown, eps = 1e-6): boolean {
  return Math.abs(finiteNumber(value, 0)) > eps;
}

function vectorHasLength(value: unknown, eps = 1e-6): value is [number, number, number] {
  if (!Array.isArray(value) || value.length !== 3) return false;
  const x = finiteNumber(value[0], 0);
  const y = finiteNumber(value[1], 0);
  const z = finiteNumber(value[2], 0);
  return x * x + y * y + z * z > eps * eps;
}

function normalizedParticleSunColor(color: THREE.Color): THREE.Color {
  // Native particle lightmapping applies colored Reinhard normalization:
  // sunColor / (luma(sunColor) + 1). This keeps white-sun smoke at the old
  // 0.5 attenuation while preserving weather sun hue.
  const luma = color.r * 0.2126 + color.g * 0.7152 + color.b * 0.0722;
  return color.clone().multiplyScalar(1 / (luma + 1));
}

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
  /** Effect-level one-shot loop period (maxEmittingDuration + the longest
   *  sibling maxAge) so every system of an attachment re-bursts on the SAME
   *  clock. 0/absent ⇒ per-system window+maxAge boundary. */
  loopResetPeriod?: number;
  intensityDefaults?: readonly number[];
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
  readonly points: THREE.Mesh;
  private instGeom: THREE.InstancedBufferGeometry;
  private capacity: number;
  private maxAge: number;
  // Record-level emission window (seconds). >0 ⇒ one-shot burst that
  // re-bursts after window+maxAge; <=0 ⇒ continuous emitter. See tick().
  private maxEmittingDuration: number;
  // False until the first active tick has pre-filled the ring buffer (H1
  // prewarm). Reset on each one-shot re-burst so the next burst re-warms.
  private prewarmed = false;
  // Authored `general.prewarm`. Native prewarm is OPT-IN per system: the
  // activation warm in FUN_1406ce8a0 is gated on a per-system flag (+0x34,
  // seeded from the authored bool, set-then-cleared = one-time). Only ~102 of
  // 13,737 corpus systems author it true (continuous ambient loops that must
  // look steady-state when they pop into view). Event one-shots (muzzle/
  // explosion, e.g. all 12 GK_Shot systems) author false and start EMPTY —
  // the spool-up ramp IS the flash. Warming them pre-ages the pool, which
  // skips the orange ignition window of the tint ramp and pops the effect in
  // at full density on frame 1.
  private authoredPrewarm = false;
  // Effect-level one-shot loop period (seconds), shared by every system of
  // the attachment (maxEmittingDuration + the LONGEST sibling maxAge). The
  // engine restarts/kills a one-shot effect as a UNIT; resetting each system
  // at its own window+maxAge desyncs siblings after the first cycle (GK_Shot
  // periods range 1.84-7.5s). 0 ⇒ fall back to the per-system boundary.
  private loopResetPeriod = 0;
  // SIZE model (RE 2026-06-04, build 12506899; memory
  // project-particle-runtime-eval-size-model). Engine:
  //   size = emitter.sizeGenerator (BASE, in METRES, per-particle)
  //        × Π scaler/resizer.sizeGenerator (per-frame multipliers, own axis)
  // ageScale (Emitter+0x40) / aux (+0x50) are NOT size factors (Ghidra
  // FUN_14071a990 — pass@10 + pass@5, 2026-06-21): they drive the per-particle
  // AGE CLOCK instead (see ageScaleAuxGen below). The old `× ageScale` size
  // multiply was removed once the byte-trace proved +0x40/+0x50 route to the
  // age records 0x08/0x0c, never the size record 0x20.
  // NO ×15 on size. `psize[i]` caches the per-particle base (emitter
  // sizeGenerator, fixed at spawn); the scaler ramps are evaluated per-frame on
  // their parameterType axis. The prior code had this INVERTED (scaler-as-base,
  // sampled at a normalized [0,1] age).
  private emitterSizeGen: ParticleValueGenerator | undefined;
  private ageScaleGen: ParticleValueGenerator | undefined;
  // ageScaleAux (Emitter+0x50). With ageScale (+0x40) these are the per-particle
  // AGE-CLOCK coefficients, NOT size factors (pass@10 + pass@5 Ghidra,
  // FUN_14071a990 spawn / FUN_14071b7f0 tick). ageScale scales the age-advance
  // RATE — so age-keyed ramps reach their tail sooner AND the particle dies
  // sooner; aux extends the death threshold only (lifetime × aux), without
  // re-timing the ramp axis. Both default to 1.0 (neutral) → ~98.5% of systems.
  private ageScaleAuxGen: ParticleValueGenerator | undefined;
  // Per-particle age-advance rate = sampled ageScale (1.0 neutral). this.age[]
  // advances by dt × ageRate, so this.age IS the effective/scaled age that every
  // age-keyed sample (ramps, dampfer, cull, GPU flipbook) reads.
  private ageRate!: Float32Array;
  private scalerGens: ParticleValueGenerator[] = [];
  private scalerGlowGens: ParticleValueGenerator[] = [];
  private scalerScaleXGens: ParticleValueGenerator[] = [];
  // Per-action `delay` (seconds): force/scaler/dampfer apply is gated until
  // particleAge >= delay, mirroring stream/jitter/orbitor/magnet/barrier/
  // velocityField which already honor it. Default 0 = active at spawn, so the
  // ~99% of systems with no authored delay stay byte-identical. The scaler
  // delay arrays are index-aligned with the matching gen arrays. (tint/
  // alphaSetter also decode `delay`, but 0 corpus systems author a nonzero
  // one, so they are intentionally left ungated — render-neutral.)
  private scalerDelays: number[] = [];
  private scalerGlowDelays: number[] = [];
  private scalerScaleXDelays: number[] = [];
  // dampfer.velocityGenerator — a per-frame drag MULTIPLIER on the velocity's
  // contribution to position (1.0 → ~0). Undefined = no damping.
  private dampGen: ParticleValueGenerator | undefined;
  private dampDelay = 0;
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
  private emitterDelay = 0;
  private emitterSleepPeriod = Number.NaN;
  private inheritVelocityFactor = 0;
  private snapToSeaLevel = false;
  // Per-action driver fields.
  private tintColor: ParticleColor | undefined;
  // tint period/repeat (PCAT): the curve .time keys are ABSOLUTE SECONDS
  // spanning [0, period] (corpus: max key time == period for ~99.98% of
  // curves), so the curve is sampled at raw age with NO /period normalization.
  // `repeat` (~33% of tint actions) loops the curve every `period` seconds
  // instead of holding the final key; period==0 disables the wrap. (`delay` is
  // 0 across the whole tint corpus, and `useVelocity` — 32 systems — is left
  // unwired pending a ×15 speed-unit check.)
  private tintPeriod = 0;
  private tintRepeat = false;
  private alphaRamp: ParticleRamp | undefined;
  private forceX: ParticleValueGenerator | undefined;
  private forceY: ParticleValueGenerator | undefined;
  private forceZ: ParticleValueGenerator | undefined;
  private forceDelay = 0;
  private streamActions: StreamAction[] = [];
  private jitterActions: JitterAction[] = [];
  private orbitorActions: OrbitorAction[] = [];
  private magnetActions: MagnetAction[] = [];
  private barrierActions: BarrierAction[] = [];
  private spawnerActions: SpawnerAction[] = [];
  private velocityFieldActions: VelocityFieldAction[] = [];
  private frameRateRamp: ParticleRamp | undefined;
  private yawRateRamp: ParticleRamp | undefined;
  private spinRateBase = 0;
  private spinRateRange = 0;
  private initialOrientationBase = 0;
  private initialOrientationRange = 0;
  private readonly depthSortParticles: boolean;
  private sortCamera: THREE.Camera | null = null;
  private distanceConfigs: ParticleSystemIntensityConfig[] = [];
  private intensityChannels: ParticleSystemIntensityChannel[] = [];
  private intensityDefaults: number[] = [];
  private intensityValues: number[] = [];
  private intensityRateMultiplier = 1;
  private intensitySizeMultiplier = 1;
  private intensityScaleXMultiplier = 1;
  private intensityScaleYMultiplier = 1;
  private intensityAgeScaleMultiplier = 1;
  private intensityAgeAuxScaleMultiplier = 1;
  private intensityColorRMultiplier = 1;
  private intensityColorGMultiplier = 1;
  private intensityColorBMultiplier = 1;
  private intensityColorAlphaMultiplier = 1;
  private intensityTintAlphaMultiplier = 1;
  private distanceColorRMultiplier = 1;
  private distanceColorGMultiplier = 1;
  private distanceColorBMultiplier = 1;
  private distanceColorAlphaMultiplier = 1;
  private distanceTintAlphaMultiplier = 1;
  private intensityTilingUMultiplier = 1;
  private intensityTilingVMultiplier = 1;
  private intensityVelXMultiplier = 1;
  private intensityVelYMultiplier = 1;
  private intensityVelZMultiplier = 1;
  private intensityStreamerXMultiplier = 1;
  private intensityStreamerYMultiplier = 1;
  private intensityStreamerZMultiplier = 1;
  private distanceRateMultiplier = 1;
  private distanceSizeMultiplier = 1;
  private distanceScaleXMultiplier = 1;
  private distanceScaleYMultiplier = 1;
  private distanceAgeScaleMultiplier = 1;
  private distanceAgeAuxScaleMultiplier = 1;
  private distanceTilingUMultiplier = 1;
  private distanceTilingVMultiplier = 1;
  private distanceVelXMultiplier = 1;
  private distanceVelYMultiplier = 1;
  private distanceVelZMultiplier = 1;
  private distanceStreamerXMultiplier = 1;
  private distanceStreamerYMultiplier = 1;
  private distanceStreamerZMultiplier = 1;
  private baseSpriteAspectX = 1;
  private baseTilingU = 1;
  private baseTilingV = 1;

  // Particle attribute arrays.
  private pos: Float32Array;
  private vel: Float32Array;
  private velGpu: Float32Array;
  private age: Float32Array; // age in seconds; -1 = empty slot
  private lifetime: Float32Array;
  private colorRGBA: Float32Array;
  private sizeArr: Float32Array;
  private glowStrengthArr: Float32Array;
  private spriteScaleXArr: Float32Array;
  private drawPos: Float32Array;
  private drawColorRGBA: Float32Array;
  private drawSizeArr: Float32Array;
  private drawGlowStrength: Float32Array;
  private drawSpriteScaleX: Float32Array;
  private drawFrameSeed: Float32Array;
  private drawFramePhase: Float32Array;
  private drawRotationPhase: Float32Array;
  // Per-slot (CPU-only) size base (emitter × ageScale, metres) + the u8
  // particleIndex counter, both assigned at spawn. Consumed to produce
  // sizeArr each frame; not packed for the GPU.
  private psize: Float32Array;
  private pidx: Float32Array;
  private alive = 0; // count of currently-alive particles

  // Reusable scratch buffers for the geometry attributes (we update
  // each frame in-place).
  private posAttr: THREE.BufferAttribute;
  private velocityAttr: THREE.BufferAttribute;
  private colorAttr: THREE.BufferAttribute;
  private sizeAttr: THREE.BufferAttribute;
  private glowStrengthAttr: THREE.BufferAttribute;
  private spriteScaleXAttr: THREE.BufferAttribute;
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
   *  rather than degrees/s. Renderer spinRateBase/Range contributes a per-
   *  particle constant angular rate on top of the yaw ramp. */
  private rotationPhase: Float32Array;
  private rotationPhaseAttr: THREE.BufferAttribute;
  /** Per-particle authored spin rate, sampled at spawn from
   *  spinRateBase + random[0,1) * spinRateRange. */
  private spinRate: Float32Array;
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
  private static readonly TMP_SCALE = new THREE.Vector3();
  private static readonly TMP_VIEW_SORT = new THREE.Matrix4();
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
    this.loopResetPeriod = Math.max(0, options.loopResetPeriod ?? 0);
    const gen = system.general;
    this.authoredPrewarm = !!gen?.prewarm;
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
        if (body.tint) {
          this.tintColor = body.tint as ParticleColor;
          this.tintPeriod = typeof body.period === 'number' ? body.period : 0;
          this.tintRepeat = body.repeat === true;
        }
      } else if (c.action === 'alphaSetter') {
        if (body.ramp) this.alphaRamp = body.ramp as ParticleRamp;
      } else if (c.action === 'scaler') {
        const scalerDelay = typeof body.delay === 'number' ? body.delay : 0;
        if (body.sizeGenerator) {
          this.scalerGens.push(body.sizeGenerator as ParticleValueGenerator);
          this.scalerDelays.push(scalerDelay);
          this.scalerGlowGens.push(body.sizeGenerator as ParticleValueGenerator);
          this.scalerGlowDelays.push(scalerDelay);
        }
        if (body.scaleXGenerator) {
          this.scalerScaleXGens.push(body.scaleXGenerator as ParticleValueGenerator);
          this.scalerScaleXDelays.push(scalerDelay);
        }
      } else if (c.action === 'resizer') {
        // resizer = interpolate sprite size sizeFrom -> sizeTo over particle
        // life. Producer now emits body.sizeFrom / body.sizeTo (raw BW units).
        // INTENTIONALLY NOT WIRED: scalerGens is a multiplicative product and
        // these endpoints are absolute sizes (not ~1.0 multipliers), so feeding
        // them there would inflate sprite size up to ~1000x. Native folds
        // resizer into the size path (finding-62) but overwrite-vs-multiply, the
        // lerp axis, and any normalization are UNRESOLVED statically. Wire only
        // after a Frida hook on the resizer per-particle apply callback (sibling
        // of scaler's FUN_140742280) confirms the integrator.
      } else if (c.action === 'dampfer') {
        if (body.velocityGenerator) {
          this.dampGen = body.velocityGenerator as ParticleValueGenerator;
          this.dampDelay = typeof body.delay === 'number' ? body.delay : 0;
        }
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
        this.forceDelay = typeof body.delay === 'number' ? body.delay : 0;
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
    // Emitter duty cycle (BigWorld source_psa.cpp:398-434; live-verified vs
    // build 12506899 — see emitterActive()): the active/sleep cycle applies
    // ONLY when sleepPeriod>0 (emit `activePeriod`, sleep `sleepPeriod`,
    // repeat). sleepPeriod<=0 (e.g. -1) = active the WHOLE emission window —
    // continuous to maxEmittingDuration; activePeriod does NOT bound it (it
    // is the rate ramp's wrap period). Absent (NaN) or activePeriod<=0 ⇒ no
    // gate. The per-emitter delay staggers ignition regardless of duty mode.
    // GK_Shot.xml systems[1] (0-based): rate ramp 200/s over [0,0.275s],
    // sleepPeriod=-1, maxAge=2.25 (verified against assets.bin).
    this.emitterDelay = Math.max(0, sampleScalarVg(system.emitter?.delayGenerator, 0, 0));
    this.emitterSleepPeriod = sampleScalarVg(system.emitter?.sleepPeriodGenerator, 0, Number.NaN);
    this.inheritVelocityFactor = system.emitter?.inheritVelocityFactor ?? 0;
    this.snapToSeaLevel = !!system.emitter?.snapToSeaLevel;
    // SIZE base (RE 2026-06-04): the emitter's sizeGenerator is the per-particle
    // BASE size in METRES; ageScaleGenerator is a per-particle life multiplier.
    // Both are typically linear (random) → sampled once at spawn into psize[].
    // The scaler/resizer ramps (scalerGens, captured above) are the per-frame
    // multipliers, evaluated on their own parameterType axes in tick(). NO ×15.
    this.emitterSizeGen = system.emitter?.sizeGenerator;
    this.ageScaleGen = system.emitter?.ageScaleGenerator;
    this.ageScaleAuxGen = system.emitter?.ageScaleAuxGenerator;
    // H5 random-cell cap: the count of frames a randomFrameOnly particle can
    // land on. Engine seeds the frame byte in [0, framesRangeEnd); fall back
    // to the full grid when the range wasn't authored.
    const anim = system.animation;
    const renderer = system.renderer;
    this.depthSortParticles =
      PS_RBT_DEPTH_SORT_MODES.has(renderer?.blendType ?? '') &&
      finiteNumber(renderer?.sortType, 2) < 2;
    this.frameRateRamp = anim?.frameRateRamp;
    this.yawRateRamp = rampHasNonZeroValue(renderer?.yawRateRamp) ? renderer?.yawRateRamp : undefined;
    this.spinRateBase = finiteNumber(renderer?.spinRateBase, 0);
    this.spinRateRange = finiteNumber(renderer?.spinRateRange, 0);
    this.initialOrientationBase = finiteNumber(renderer?.initialOrientationBase, 0);
    this.initialOrientationRange = finiteNumber(renderer?.initialOrientationRange, 0);
    const fx = anim?.framesPerX ?? 1;
    const fy = anim?.framesPerY ?? 1;
    this.framesRangeEnd = Math.max(0, anim?.framesRangeEnd ?? fx * fy);
    this.distanceConfigs = system.distance?.configs ?? [];

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
    this.velGpu = new Float32Array(this.capacity * 3);
    this.age = new Float32Array(this.capacity);
    this.lifetime = new Float32Array(this.capacity);
    this.ageRate = new Float32Array(this.capacity);
    this.colorRGBA = new Float32Array(this.capacity * 4);
    this.sizeArr = new Float32Array(this.capacity);
    this.glowStrengthArr = new Float32Array(this.capacity);
    this.spriteScaleXArr = new Float32Array(this.capacity);
    this.drawPos = new Float32Array(this.capacity * 3);
    this.drawColorRGBA = new Float32Array(this.capacity * 4);
    this.drawSizeArr = new Float32Array(this.capacity);
    this.drawGlowStrength = new Float32Array(this.capacity);
    this.drawSpriteScaleX = new Float32Array(this.capacity);
    this.drawFrameSeed = new Float32Array(this.capacity);
    this.drawFramePhase = new Float32Array(this.capacity);
    this.drawRotationPhase = new Float32Array(this.capacity);
    this.ageGpu = new Float32Array(this.capacity);
    this.frameSeed = new Float32Array(this.capacity);
    this.framePhase = new Float32Array(this.capacity);
    this.rotationPhase = new Float32Array(this.capacity);
    this.spinRate = new Float32Array(this.capacity);
    this.psize = new Float32Array(this.capacity);
    this.pidx = new Float32Array(this.capacity);
    for (let i = 0; i < this.capacity; i++) this.age[i] = -1;

    // Geometry: INSTANCED camera-facing billboard quads (one instance per
    // particle). Replaces the old THREE.Points path, whose `gl_PointSize` is
    // hardware-capped (ALIASED_POINT_SIZE_RANGE, commonly 1024px) — large/near
    // particles clamped to that cap, which both under-sized them and (via the
    // fragment's center-crop of a fixed square) produced the "blocky square"
    // look. The native engine draws unbounded world-space quads; this matches it.
    const geom = new THREE.InstancedBufferGeometry();
    // Base quad: 4 corners in [0,1]^2 (xy = cornerUV, the old gl_PointCoord),
    // two triangles. All particle data below is PER-INSTANCE.
    geom.setAttribute(
      'position',
      new THREE.Float32BufferAttribute([0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0], 3),
    );
    geom.setIndex([0, 1, 2, 2, 1, 3]);
    this.posAttr = new THREE.InstancedBufferAttribute(this.drawPos, 3);
    this.posAttr.setUsage(THREE.DynamicDrawUsage);
    this.velocityAttr = new THREE.InstancedBufferAttribute(this.velGpu, 3);
    this.velocityAttr.setUsage(THREE.DynamicDrawUsage);
    this.colorAttr = new THREE.InstancedBufferAttribute(this.drawColorRGBA, 4);
    this.colorAttr.setUsage(THREE.DynamicDrawUsage);
    this.sizeAttr = new THREE.InstancedBufferAttribute(this.drawSizeArr, 1);
    this.sizeAttr.setUsage(THREE.DynamicDrawUsage);
    this.glowStrengthAttr = new THREE.InstancedBufferAttribute(this.drawGlowStrength, 1);
    this.glowStrengthAttr.setUsage(THREE.DynamicDrawUsage);
    this.spriteScaleXAttr = new THREE.InstancedBufferAttribute(this.drawSpriteScaleX, 1);
    this.spriteScaleXAttr.setUsage(THREE.DynamicDrawUsage);
    this.ageAttr = new THREE.InstancedBufferAttribute(this.ageGpu, 1);
    this.ageAttr.setUsage(THREE.DynamicDrawUsage);
    this.frameSeedAttr = new THREE.InstancedBufferAttribute(this.drawFrameSeed, 1);
    this.frameSeedAttr.setUsage(THREE.DynamicDrawUsage);
    this.framePhaseAttr = new THREE.InstancedBufferAttribute(this.drawFramePhase, 1);
    this.framePhaseAttr.setUsage(THREE.DynamicDrawUsage);
    this.rotationPhaseAttr = new THREE.InstancedBufferAttribute(this.drawRotationPhase, 1);
    this.rotationPhaseAttr.setUsage(THREE.DynamicDrawUsage);
    geom.setAttribute('iPosition', this.posAttr);
    geom.setAttribute('velocity', this.velocityAttr);
    geom.setAttribute('color', this.colorAttr);
    geom.setAttribute('size', this.sizeAttr);
    geom.setAttribute('glowStrength', this.glowStrengthAttr);
    geom.setAttribute('spriteScaleX', this.spriteScaleXAttr);
    geom.setAttribute('age', this.ageAttr);
    geom.setAttribute('frameSeed', this.frameSeedAttr);
    geom.setAttribute('framePhase', this.framePhaseAttr);
    geom.setAttribute('rotationPhase', this.rotationPhaseAttr);
    geom.instanceCount = 0;
    this.instGeom = geom;
    this.points = new THREE.Mesh(geom, material);
    this.points.frustumCulled = false;
    this.intensityChannels = system.intensities?.channels ?? [];
    this.intensityDefaults = Array.from(options.intensityDefaults ?? []);
    this.baseSpriteAspectX = finiteNumber(this.material.uniforms.uSpriteAspectX?.value, 1);
    const tiling = this.material.uniforms.uUvTiling?.value;
    if (tiling instanceof THREE.Vector2) {
      this.baseTilingU = tiling.x;
      this.baseTilingV = tiling.y;
    }
    this.setIntensityValues(this.intensityDefaults);
  }

  setActive(active: boolean): void {
    this.active = active;
    this.points.visible = active;
    if (active && this.loopOneShot) this.finished = false;
  }

  restart(): void {
    this.active = true;
    this.finished = false;
    this.points.visible = true;
    this.elapsed = 0;
    this.prewarmed = false;
    this.alive = 0;
    this.emitAccum = 0;
    this.creatorAccum = 0;
    for (const action of this.spawnerActions) action.accum = 0;
    for (let i = 0; i < this.capacity; i++) this.age[i] = -1;
    this.instGeom.instanceCount = 0;
  }

  setSortCamera(camera: THREE.Camera | null): void {
    this.sortCamera = camera;
  }

  setIntensityValues(values: readonly number[] | undefined): void {
    const count = Math.max(this.intensityChannels.length, this.intensityDefaults.length);
    this.intensityValues = [];
    for (let i = 0; i < count; i++) {
      const authored = values?.[i];
      const fallback = this.intensityDefaults[i] ?? 1;
      this.intensityValues[i] = Number.isFinite(authored) ? Number(authored) : fallback;
    }
    this.applyIntensityState();
  }

  private applyIntensityState(): void {
    this.intensityRateMultiplier = 1;
    this.intensitySizeMultiplier = 1;
    this.intensityScaleXMultiplier = 1;
    this.intensityScaleYMultiplier = 1;
    this.intensityAgeScaleMultiplier = 1;
    this.intensityAgeAuxScaleMultiplier = 1;
    this.intensityColorRMultiplier = 1;
    this.intensityColorGMultiplier = 1;
    this.intensityColorBMultiplier = 1;
    this.intensityColorAlphaMultiplier = 1;
    this.intensityTintAlphaMultiplier = 1;
    this.intensityTilingUMultiplier = 1;
    this.intensityTilingVMultiplier = 1;
    this.intensityVelXMultiplier = 1;
    this.intensityVelYMultiplier = 1;
    this.intensityVelZMultiplier = 1;
    this.intensityStreamerXMultiplier = 1;
    this.intensityStreamerYMultiplier = 1;
    this.intensityStreamerZMultiplier = 1;

    for (let channelIndex = 0; channelIndex < this.intensityChannels.length; channelIndex++) {
      const channel = this.intensityChannels[channelIndex];
      const value = this.intensityValues[channelIndex] ?? this.intensityDefaults[channelIndex] ?? 1;
      for (const config of channel.configs ?? []) {
        const factor = sampleRamp(config.ramp, value, 1);
        if (!Number.isFinite(factor)) continue;
        for (const flag of config.flags ?? []) {
          this.applyIntensityTarget(flag, factor);
        }
      }
    }
    this.updateIntensityMaterialUniforms();
  }

  private applyIntensityTarget(flag: number, factor: number): void {
    switch (flag) {
      case PS_IC_EMITTER_RATE:
        this.intensityRateMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SIZE:
        this.intensitySizeMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SCALE_X:
        this.intensityScaleXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SCALE_Y:
        this.intensityScaleYMultiplier *= factor;
        break;
      case PS_IC_AGE_SCALE:
        this.intensityAgeScaleMultiplier *= factor;
        break;
      case PS_IC_AGE_AUX_SCALE:
        this.intensityAgeAuxScaleMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_R:
      case PS_IC_PARTICLE_TINT_R:
        this.intensityColorRMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_G:
      case PS_IC_PARTICLE_TINT_G:
        this.intensityColorGMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_B:
      case PS_IC_PARTICLE_TINT_B:
        this.intensityColorBMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_A:
        this.intensityColorAlphaMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TINT_A:
        this.intensityTintAlphaMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TILING_U:
        this.intensityTilingUMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TILING_V:
        this.intensityTilingVMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_X:
        this.intensityVelXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_Y:
        this.intensityVelYMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_Z:
        this.intensityVelZMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_X:
        this.intensityStreamerXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_Y:
        this.intensityStreamerYMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_Z:
        this.intensityStreamerZMultiplier *= factor;
        break;
    }
  }

  private updateIntensityMaterialUniforms(): void {
    const scaleX = this.intensityScaleXMultiplier * this.distanceScaleXMultiplier;
    const scaleY = Math.max(
      0.001,
      Math.abs(this.intensityScaleYMultiplier * this.distanceScaleYMultiplier),
    );
    const aspect = Math.max(
      0.001,
      Math.abs((this.baseSpriteAspectX * scaleX) / scaleY),
    );
    const aspectUniform = this.material.uniforms.uSpriteAspectX?.value;
    if (typeof aspectUniform === 'number') this.material.uniforms.uSpriteAspectX.value = aspect;
    const pointExtent = this.material.uniforms.uPointExtent?.value;
    if (typeof pointExtent === 'number') {
      this.material.uniforms.uPointExtent.value = this.material.uniforms.uUseSpriteRotation?.value
        ? Math.sqrt(aspect * aspect + 1)
        : Math.max(aspect, 1);
    }
    const tiling = this.material.uniforms.uUvTiling?.value;
    if (tiling instanceof THREE.Vector2) {
      tiling.set(
        this.baseTilingU * this.intensityTilingUMultiplier * this.distanceTilingUMultiplier,
        this.baseTilingV * this.intensityTilingVMultiplier * this.distanceTilingVMultiplier,
      );
    }
  }

  /** Parent velocity is sampled in world space by ShipViewer and converted
   *  into this system's local frame. It is applied only to newly spawned
   *  particles through emitter.inheritVelocityFactor. */
  setParentVelocityWorld(velocity: THREE.Vector3): void {
    if (this.inheritVelocityFactor === 0) {
      this.parentVelocityLocal.set(0, 0, 0);
      return;
    }
    // World velocity is metres/s (×15); divide back into the record-unit sim
    // frame so it composes with the raw spawn velocity (re-scaled ×15 at the
    // draw boundary). See NATIVE_TO_METRES.
    this.parentVelocityLocal.copy(velocity).multiplyScalar(1 / NATIVE_TO_METRES);
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
    // Prewarm on the first active frame (and after each one-shot re-burst),
    // ONLY for systems that author `general.prewarm` — the engine's activation
    // warm (FUN_1406ce8a0, 10 substeps of maxAge*0.1) is gated on a per-system
    // flag (+0x34) seeded from that bool. ~102/13737 corpus systems opt in
    // (steady-state ambient loops). Everything else (incl. every GK_Shot
    // muzzle system) starts from an EMPTY pool and spools up naturally; doc-63
    // H1's "one-shot never catches up" applied only to prewarm-authored
    // systems — warming the rest pre-aged the pool past the tint ramp's
    // ignition window (grey instead of orange) and popped in at full density.
    if (!this.prewarmed) {
      if (this.authoredPrewarm) this.runPrewarm();
      this.prewarmed = true;
    }
    this.updateDistanceState();
    this.advanceBy(dt, true);
    this.writeBuffers();
  }

  /** Advance the sim by `dt`, subdivided into ≤0.25 s substeps like the
   *  native integrator (FUN_140718f00 clamps every substep to
   *  DAT_142556548 = 0.25 s; emission, actions and integration all run per
   *  substep). The render-loop dt is already clamped to 0.1 s by
   *  ParticleScene.tick, so this mainly matters for prewarm (steps of
   *  maxAge×0.1 can exceed 0.25 s) and any future coarse-dt callers. */
  private advanceBy(dt: number, write: boolean): void {
    let remaining = dt;
    do {
      const step = Math.min(remaining, NATIVE_SUBSTEP_MAX_S);
      this.advance(step, write);
      remaining -= step;
    } while (remaining > 0);
  }

  private updateDistanceState(): void {
    this.distanceRateMultiplier = 1;
    this.distanceSizeMultiplier = 1;
    this.distanceScaleXMultiplier = 1;
    this.distanceScaleYMultiplier = 1;
    this.distanceAgeScaleMultiplier = 1;
    this.distanceAgeAuxScaleMultiplier = 1;
    this.distanceColorRMultiplier = 1;
    this.distanceColorGMultiplier = 1;
    this.distanceColorBMultiplier = 1;
    this.distanceColorAlphaMultiplier = 1;
    this.distanceTintAlphaMultiplier = 1;
    this.distanceTilingUMultiplier = 1;
    this.distanceTilingVMultiplier = 1;
    this.distanceVelXMultiplier = 1;
    this.distanceVelYMultiplier = 1;
    this.distanceVelZMultiplier = 1;
    this.distanceStreamerXMultiplier = 1;
    this.distanceStreamerYMultiplier = 1;
    this.distanceStreamerZMultiplier = 1;
    if (this.distanceConfigs.length === 0 || !this.sortCamera) {
      this.updateIntensityMaterialUniforms();
      return;
    }
    this.sortCamera.updateMatrixWorld(true);
    this.points.updateWorldMatrix(true, false);
    this.sortCamera.getWorldPosition(SystemRenderer.TMP_POS2);
    this.points.getWorldPosition(SystemRenderer.TMP_WORLD);
    const distance = SystemRenderer.TMP_WORLD.distanceTo(SystemRenderer.TMP_POS2);
    for (const config of this.distanceConfigs) {
      const factor = sampleRamp(config.ramp, distance, 1);
      if (!Number.isFinite(factor)) continue;
      for (const flag of config.flags ?? []) {
        this.applyDistanceTarget(flag, factor);
      }
    }
    this.updateIntensityMaterialUniforms();
  }

  private applyDistanceTarget(flag: number, factor: number): void {
    switch (flag) {
      case PS_IC_EMITTER_RATE:
        this.distanceRateMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SIZE:
        this.distanceSizeMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SCALE_X:
        this.distanceScaleXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_SCALE_Y:
        this.distanceScaleYMultiplier *= factor;
        break;
      case PS_IC_AGE_SCALE:
        this.distanceAgeScaleMultiplier *= factor;
        break;
      case PS_IC_AGE_AUX_SCALE:
        this.distanceAgeAuxScaleMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_R:
      case PS_IC_PARTICLE_TINT_R:
        this.distanceColorRMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_G:
      case PS_IC_PARTICLE_TINT_G:
        this.distanceColorGMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_B:
      case PS_IC_PARTICLE_TINT_B:
        this.distanceColorBMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_COLOR_A:
        this.distanceColorAlphaMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TINT_A:
        this.distanceTintAlphaMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TILING_U:
        this.distanceTilingUMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_TILING_V:
        this.distanceTilingVMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_X:
        this.distanceVelXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_Y:
        this.distanceVelYMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_VEL_Z:
        this.distanceVelZMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_X:
        this.distanceStreamerXMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_Y:
        this.distanceStreamerYMultiplier *= factor;
        break;
      case PS_IC_PARTICLE_STREAMER_Z:
        this.distanceStreamerZMultiplier *= factor;
        break;
    }
  }

  /** Advance the CPU simulation by `dt` seconds (emission + per-particle
   *  update). Does NOT touch the GPU buffers — see writeBuffers(). */
  /** Emitter duty-cycle gate. Per BigWorld `source_psa.cpp:398-434`, the active/
   *  sleep cycle applies ONLY when `sleepPeriod > 0` (emit for `activePeriod`,
   *  sleep for `sleepPeriod`, repeat). `sleepPeriod <= 0` (e.g. -1) means "active
   *  all the time" (continuous); NaN/`activePeriod<=0` means no gate. Verified
   *  live (Frida, build 12506899): GK_Shot (`sleepPeriod=-1`) emits a SUSTAINED
   *  ~0.5s+ plateau bounded by `maxEmittingDuration`, NOT a 0.275s one-shot —
   *  `activePeriod` does NOT bound `sleepPeriod<=0` emission. */
  private emitterActive(elapsed: number): boolean {
    // The per-emitter start delay applies regardless of duty mode — it was
    // previously short-circuited by the sleepPeriod<=0 "continuous" return,
    // which collapsed authored ignition staggers (6/12 GK_Shot systems delay
    // their smoke shells by 0.025-0.105s after the core flash) into one
    // simultaneous t=0 spawn.
    const phase = elapsed - this.emitterDelay;
    if (phase < 0) return false;
    const ap = this.emitterActivePeriod;
    const sp = this.emitterSleepPeriod;
    if (ap <= 0 || Number.isNaN(sp) || sp <= 0) return true; // continuous
    const cycle = ap + sp;
    return cycle <= 0 ? true : phase % cycle <= ap; // active sub-window of each cycle
  }

  /** System time (seconds) at which emission stops, for the finish/loop. All
   *  `sleepPeriod<=0` systems (the majority — incl. every GK_Shot muzzle system)
   *  emit continuously until `maxEmittingDuration`; the `sleepPeriod>0` duty
   *  cycle is gated in `emitterActive()`, not here. */
  private oneShotEmitEnd(): number {
    return this.maxEmittingDuration;
  }

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
    // Loop/finish boundary. When looping for inspection, every system of the
    // attachment resets on the SHARED effect clock (loopResetPeriod = window +
    // longest sibling maxAge) so the 12 systems of e.g. GK_Shot re-burst
    // together instead of drifting apart on 6 different periods. A non-looping
    // system just finishes at its own window+maxAge (nothing re-bursts, and a
    // shared boundary would only delay the cleanup).
    const resetAt =
      this.loopOneShot && this.loopResetPeriod > 0
        ? this.loopResetPeriod
        : this.oneShotEmitEnd() + this.maxAge;
    if (oneShot && this.elapsed >= resetAt) {
      for (let i = 0; i < this.capacity; i++) this.age[i] = -1;
      this.alive = 0;
      this.emitAccum = 0;
      this.creatorAccum = 0;
      for (const action of this.spawnerActions) action.accum = 0;
      this.instGeom.instanceCount = 0;
      if (!this.loopOneShot) {
        this.finished = true;
        this.active = false;
        return;
      }
      this.elapsed = 0;
      // Re-warm the next burst (prewarm-authored systems only; engine
      // re-warms on re-activation).
      this.prewarmed = false;
    }
    const emitting = !oneShot || this.elapsed <= this.oneShotEmitEnd();
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
      // this.age is the EFFECTIVE (ageScale-folded) age: advances at dt × ageRate
      // so age-keyed ramps + the cull see the scaled clock (native rec[0x00] +=
      // dt × ageScaleRate, FUN_14071b7f0). lifetime already carries × aux below.
      this.age[i] += dt * this.ageRate[i];
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
      if (this.spinRate[i] !== 0) {
        this.rotationPhase[i] += this.spinRate[i] * dt;
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
      // Gated by the force action's delay — inactive until age >= forceDelay.
      if (age >= this.forceDelay) {
        this.vel[i * 3 + 0] += sampleGenAxis(this.forceX, clocks, 0) * dt;
        this.vel[i * 3 + 1] += sampleGenAxis(this.forceY, clocks, 0) * dt;
        this.vel[i * 3 + 2] += sampleGenAxis(this.forceZ, clocks, 0) * dt;
      }
      this.applyMagnetActions(i, age, dt);
      this.applyStreamActions(i, age, dt);
      this.applyJitterActions(i, age, dt);
      this.applyVelocityFieldActions(i, age);
      // dampfer: a per-frame drag multiplier on the velocity's displacement.
      // Sample the drag at the start of the integration step. Short impact
      // effects often author damp ramps that fall to zero by 0.1s; sampling
      // only after the coarse activation prewarm step pins freshly spawned
      // particles at the emitter and stacks their quads into square flashes.
      const dampParticleAge = clocks.particleAge;
      clocks.particleAge = prevAge;
      const damp =
        this.dampGen && age >= this.dampDelay ? sampleGenAxis(this.dampGen, clocks, 1) : 1;
      clocks.particleAge = dampParticleAge;
      if (this.applyBarrierActions(i, age, dt * damp, allowChildSpawns)) continue;
      this.pos[i * 3 + 0] += this.vel[i * 3 + 0] * dt * damp;
      this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt * damp;
      this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt * damp;
      this.applyOrbitorActions(i, clocks, age, dt);
      // Final opacity = clamped PS_IC PARTICLE_COLOR_A base alpha
      // × tint.alpha(age) × alphaSetter(t) × PS_IC PARTICLE_TINT_A, then
      // clamped again. Native seeds renderRec[0x44..0x50] from the COLOR
      // targets in FUN_14071a990 (alpha clamped at 0x50), copies that into
      // working color, applies tint/alphaSetter actions, then multiplies the
      // TINT targets in FUN_14071b7f0 and clamps working alpha at 0x40.
      // repeat=true loops the tint curve every `period` seconds (== the curve's
      // last key time); repeat=false holds the final key (sampleColor
      // end-clamps). No /period normalization, no delay subtraction (delay is 0
      // across the whole tint corpus).
      const tintT = this.tintRepeat && this.tintPeriod > 0 ? age % this.tintPeriod : age;
      sampleColor(this.tintColor, tintT, SystemRenderer.TMP_COL);
      const alphaT = this.alphaSetterIsSystemAge ? this.elapsed : age;
      const baseAlpha = clamp01(
        this.intensityColorAlphaMultiplier * this.distanceColorAlphaMultiplier,
      );
      const alpha = clamp01(
        baseAlpha *
          sampleRamp(this.alphaRamp, alphaT, 1) *
          SystemRenderer.TMP_COL[3] *
          this.intensityTintAlphaMultiplier *
          this.distanceTintAlphaMultiplier *
          this.barrierAlphaMultiplier,
      );
      this.colorRGBA[i * 4 + 0] =
        SystemRenderer.TMP_COL[0] * this.intensityColorRMultiplier * this.distanceColorRMultiplier;
      this.colorRGBA[i * 4 + 1] =
        SystemRenderer.TMP_COL[1] * this.intensityColorGMultiplier * this.distanceColorGMultiplier;
      this.colorRGBA[i * 4 + 2] =
        SystemRenderer.TMP_COL[2] * this.intensityColorBMultiplier * this.distanceColorBMultiplier;
      this.colorRGBA[i * 4 + 3] = alpha;
      // SIZE (RE 2026-06-04): per-particle base (emitter × ageScale, cached in
      // psize at spawn) × Π scaler multipliers, each on its own axis. Metres.
      // Native scaler callback FUN_140742280 also writes this first multiplier
      // into the per-particle payload consumed by the GRADIENT_MAP glow path.
      let sizeScale = 1;
      for (let s = 0; s < this.scalerGens.length; s++) {
        if (age < this.scalerDelays[s]) continue;
        sizeScale *= sampleGenAxis(this.scalerGens[s], clocks, 1);
      }
      let glowScale = 1;
      for (let s = 0; s < this.scalerGlowGens.length; s++) {
        if (age < this.scalerGlowDelays[s]) continue;
        glowScale *= sampleGenAxis(this.scalerGlowGens[s], clocks, 1);
      }
      let scalerScaleX = 1;
      for (let s = 0; s < this.scalerScaleXGens.length; s++) {
        if (age < this.scalerScaleXDelays[s]) continue;
        scalerScaleX *= sampleGenAxis(this.scalerScaleXGens[s], clocks, 1);
      }
      let sz = this.psize[i];
      sz *= sizeScale;
      sz *=
        this.intensitySizeMultiplier *
        this.distanceSizeMultiplier *
        this.intensityScaleYMultiplier *
        this.distanceScaleYMultiplier *
        this.intensityAgeScaleMultiplier *
        this.distanceAgeScaleMultiplier *
        this.intensityAgeAuxScaleMultiplier *
        this.distanceAgeAuxScaleMultiplier *
        this.barrierScaleMultiplier;
      this.sizeArr[i] = Math.max(0, sz);
      this.glowStrengthArr[i] = Number.isFinite(glowScale) ? glowScale : 1;
      this.spriteScaleXArr[i] =
        Number.isFinite(scalerScaleX) ? Math.max(0.001, Math.abs(scalerScaleX)) : 1;
    }

    // Emit from BOTH sources (RE-aligned, 2026-05-29): the always-on emitter
    // is the primary spawn source; the PSAT creator is an additive secondary
    // burst. Each carries its own fractional accumulator and its own
    // position/velocity volume generators; they share the capacity cap so the
    // system can't exceed `capacity`. A source whose rate is 0 spawns nothing.
    if (emitting) {
      if (this.emitterRateVg && this.emitterActive(this.elapsed)) {
        // Emitter ramp keyed in SECONDS against systemAge, sampled at
        // `elapsed mod activePeriod` (constant/linear VGs ignore t; ramp VGs
        // hold their last value past the tail; activePeriod==0 ⇒ raw elapsed).
        // The clock starts at the emitter delay (emitterActive gates spawn
        // until then), so the rate ramp begins at ignition, not at t=0.
        const tBase = Math.max(0, this.elapsed - this.emitterDelay);
        const t = this.emitterActivePeriod > 0 ? tBase % this.emitterActivePeriod : tBase;
        const eRate =
          sampleScalarVg(this.emitterRateVg, t, 0) *
          this.intensityRateMultiplier *
          this.distanceRateMultiplier;
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
        const cRate =
          sampleRamp(this.rateRamp, this.elapsed, 0) *
          this.intensityRateMultiplier *
          this.distanceRateMultiplier;
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
    // Update geometry attribute buffers + draw range. The simulation arrays
    // remain slot-indexed; the draw arrays are packed/sorted copies so
    // transparent order-dependent modes can render back-to-front without
    // corrupting live particle state.
    const order: number[] = [];
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      order.push(i);
    }
    if (this.depthSortParticles && this.sortCamera && order.length > 1) {
      this.sortCamera.updateMatrixWorld(true);
      this.points.updateWorldMatrix(true, false);
      const viewSort = SystemRenderer.TMP_VIEW_SORT.multiplyMatrices(
        this.sortCamera.matrixWorldInverse,
        this.points.matrixWorld,
      );
      const m = viewSort.elements;
      order.sort((a, b) => {
        const az = this.cameraSpaceZ(a, m);
        const bz = this.cameraSpaceZ(b, m);
        return az - bz; // farther particles have more-negative view-space Z
      });
    }
    for (let writeIdx = 0; writeIdx < order.length; writeIdx++) {
      this.writeDrawSlot(order[writeIdx], writeIdx);
    }
    this.instGeom.instanceCount = order.length;
    this.posAttr.needsUpdate = true;
    this.velocityAttr.needsUpdate = true;
    this.colorAttr.needsUpdate = true;
    this.sizeAttr.needsUpdate = true;
    this.glowStrengthAttr.needsUpdate = true;
    this.spriteScaleXAttr.needsUpdate = true;
    this.ageAttr.needsUpdate = true;
    this.frameSeedAttr.needsUpdate = true;
    this.framePhaseAttr.needsUpdate = true;
    this.rotationPhaseAttr.needsUpdate = true;
  }

  private cameraSpaceZ(slot: number, matrixElements: ArrayLike<number>): number {
    const ix = slot * 3;
    const x = this.pos[ix + 0];
    const y = this.pos[ix + 1];
    const z = this.pos[ix + 2];
    return (
      matrixElements[2] * x +
      matrixElements[6] * y +
      matrixElements[10] * z +
      matrixElements[14]
    );
  }

  private writeDrawSlot(sourceSlot: number, drawSlot: number): void {
    const src3 = sourceSlot * 3;
    const dst3 = drawSlot * 3;
    const src4 = sourceSlot * 4;
    const dst4 = drawSlot * 4;
    // Convert the sim's raw native BW-unit local frame to the consumer's ×15
    // metre world (see NATIVE_TO_METRES). Position + velocity + size are all
    // world-space lengths; the sim is linear in them, so scaling the OUTPUT
    // reproduces the correctly-scaled envelope, force/stream displacement, and
    // sprite footprint without touching every per-frame input. (World-frame
    // INPUTS — sea-level snap, parent velocity — are divided back at their
    // source so they enter the record-unit sim consistently.)
    this.drawPos[dst3 + 0] = this.pos[src3 + 0] * NATIVE_TO_METRES;
    this.drawPos[dst3 + 1] = this.pos[src3 + 1] * NATIVE_TO_METRES;
    this.drawPos[dst3 + 2] = this.pos[src3 + 2] * NATIVE_TO_METRES;
    this.velGpu[dst3 + 0] = this.vel[src3 + 0] * NATIVE_TO_METRES;
    this.velGpu[dst3 + 1] = this.vel[src3 + 1] * NATIVE_TO_METRES;
    this.velGpu[dst3 + 2] = this.vel[src3 + 2] * NATIVE_TO_METRES;
    this.drawColorRGBA[dst4 + 0] = this.colorRGBA[src4 + 0];
    this.drawColorRGBA[dst4 + 1] = this.colorRGBA[src4 + 1];
    this.drawColorRGBA[dst4 + 2] = this.colorRGBA[src4 + 2];
    this.drawColorRGBA[dst4 + 3] = this.colorRGBA[src4 + 3];
    this.drawSizeArr[drawSlot] = this.sizeArr[sourceSlot] * NATIVE_TO_METRES;
    this.drawGlowStrength[drawSlot] = this.glowStrengthArr[sourceSlot];
    this.drawSpriteScaleX[drawSlot] = this.spriteScaleXArr[sourceSlot];
    this.ageGpu[drawSlot] = this.age[sourceSlot];
    this.drawFrameSeed[drawSlot] = this.frameSeed[sourceSlot];
    this.drawFramePhase[drawSlot] = this.framePhase[sourceSlot];
    this.drawRotationPhase[drawSlot] = this.rotationPhase[sourceSlot];
  }

  /** Pre-fill the ring buffer to the engine's frame-1 density: run STEPS
   *  internal sub-steps (no GPU writes) before the first visible frame, like the
   *  engine's 10x activation prewarm (FUN_1406ce8a0, scale 0.1). Called ONLY for
   *  systems that author `general.prewarm` (the native warm is flag-gated,
   *  +0x34); see tick(). RE doc 63 (H1). */
  private runPrewarm(): void {
    // Native period = the max emitter particle LIFETIME (maxAge), NOT
    // maxEmittingDuration (Ghidra FUN_1406ce8a0: dt = maxAge*0.1 ×10, clock left
    // ADVANCED ~1 lifetime, not reset). Warming over maxEmittingDuration instead
    // parks `elapsed` exactly at the emission-STOP boundary, so the always-
    // looping inspector renders only the post-emission DECAY tail and never the
    // dense emission phase (the H1 sparseness). Warm one lifetime, capped to the
    // emission window so a true short burst still stops at its peak; a
    // continuous/long-window emitter lands mid-emission at steady state. RE doc 63 H1.
    const emitWindow = this.maxEmittingDuration > 0 ? this.maxEmittingDuration : Infinity;
    const horizon = Math.min(this.maxAge, emitWindow);
    if (!(horizon > 0)) return;
    const STEPS = 10;
    const dt = horizon / STEPS;
    for (let s = 0; s < STEPS; s++) this.advanceBy(dt, false);
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
    SystemRenderer.TMP_VEL.set(
      SystemRenderer.TMP_VEL.x * this.intensityVelXMultiplier * this.distanceVelXMultiplier,
      SystemRenderer.TMP_VEL.y * this.intensityVelYMultiplier * this.distanceVelYMultiplier,
      SystemRenderer.TMP_VEL.z * this.intensityVelZMultiplier * this.distanceVelZMultiplier,
    );
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
    const baseAlpha = clamp01(
      this.intensityColorAlphaMultiplier * this.distanceColorAlphaMultiplier,
    );
    const alpha = clamp01(
      baseAlpha *
        sampleRamp(this.alphaRamp, alphaT0, 1) *
        SystemRenderer.TMP_COL[3] *
        this.intensityTintAlphaMultiplier *
        this.distanceTintAlphaMultiplier,
    );
    this.colorRGBA[slot * 4 + 0] =
      SystemRenderer.TMP_COL[0] * this.intensityColorRMultiplier * this.distanceColorRMultiplier;
    this.colorRGBA[slot * 4 + 1] =
      SystemRenderer.TMP_COL[1] * this.intensityColorGMultiplier * this.distanceColorGMultiplier;
    this.colorRGBA[slot * 4 + 2] =
      SystemRenderer.TMP_COL[2] * this.intensityColorBMultiplier * this.distanceColorBMultiplier;
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
    this.rotationPhase[slot] =
      this.initialOrientationBase + Math.random() * this.initialOrientationRange;
    this.spinRate[slot] = this.spinRateBase + Math.random() * this.spinRateRange;
    const sc = SystemRenderer.TMP_CLOCKS;
    sc.particleAge = 0;
    sc.systemAge = this.elapsed;
    sc.systemActiveTime =
      this.emitterActivePeriod > 0 ? this.elapsed % this.emitterActivePeriod : this.elapsed;
    sc.particleSpeed = 0;
    sc.systemSpeed = 0;
    sc.particleIndex = this.pidx[slot];
    const base = sampleGenAxis(this.emitterSizeGen, sc, DEFAULT_SIZE);
    // SIZE = emitter.sizeGenerator × static ONLY (native record 0x20). ageScale/aux
    // are age-clock coefficients, not size factors: ageScale → per-particle age
    // RATE; aux → death-threshold extension (lifetime × aux). Native dies when
    // scaledAge > maxAge × aux (FUN_14071b7f0); here this.age is already scaled, so
    // the existing `age >= lifetime` cull holds with lifetime = maxAge × aux.
    this.psize[slot] = base;
    const ageScale = this.ageScaleGen ? sampleGenAxis(this.ageScaleGen, sc, 1) : 1;
    const ageAux = this.ageScaleAuxGen ? sampleGenAxis(this.ageScaleAuxGen, sc, 1) : 1;
    this.ageRate[slot] = ageScale > 0 ? ageScale : 1; // guard div-by-zero / negative
    this.lifetime[slot] *= ageAux; // base was this.maxAge (set above); aux>1 → lives longer
    let sizeScale0 = 1;
    for (let s = 0; s < this.scalerGens.length; s++) {
      if (this.scalerDelays[s] > 0) continue; // delayed scalers are inactive at spawn (age 0)
      sizeScale0 *= sampleGenAxis(this.scalerGens[s], sc, 1);
    }
    let glowScale0 = 1;
    for (let s = 0; s < this.scalerGlowGens.length; s++) {
      if (this.scalerGlowDelays[s] > 0) continue;
      glowScale0 *= sampleGenAxis(this.scalerGlowGens[s], sc, 1);
    }
    let scalerScaleX0 = 1;
    for (let s = 0; s < this.scalerScaleXGens.length; s++) {
      if (this.scalerScaleXDelays[s] > 0) continue;
      scalerScaleX0 *= sampleGenAxis(this.scalerScaleXGens[s], sc, 1);
    }
    let sz0 = this.psize[slot] * sizeScale0;
    sz0 *=
      this.intensitySizeMultiplier *
      this.distanceSizeMultiplier *
      this.intensityScaleYMultiplier *
      this.distanceScaleYMultiplier *
      this.intensityAgeScaleMultiplier *
      this.distanceAgeScaleMultiplier *
      this.intensityAgeAuxScaleMultiplier *
      this.distanceAgeAuxScaleMultiplier;
    this.sizeArr[slot] = Math.max(0, sz0);
    this.glowStrengthArr[slot] = Number.isFinite(glowScale0) ? glowScale0 : 1;
    this.spriteScaleXArr[slot] =
      Number.isFinite(scalerScaleX0) ? Math.max(0.001, Math.abs(scalerScaleX0)) : 1;
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
    // The world Y is in metres (×15); the sim runs in raw record units, so the
    // snap delta is divided back by NATIVE_TO_METRES before entering it.
    const dy = (SEA_LEVEL_Y - SystemRenderer.TMP_WORLD.y) / NATIVE_TO_METRES;
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
    out
      .copy(action.vector)
      .multiply(
        SystemRenderer.TMP_SCALE.set(
          this.intensityStreamerXMultiplier * this.distanceStreamerXMultiplier,
          this.intensityStreamerYMultiplier * this.distanceStreamerYMultiplier,
          this.intensityStreamerZMultiplier * this.distanceStreamerZMultiplier,
        ),
      );
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

  /** RE-VERIFIED byte-accurate vs native (Ghidra 2026-06-09, build 12506899):
   *  jitter apply FUN_140741720 does exactly `pos/vel += generate() * dt`
   *  with a FRESH generator sample per tick — no per-particle persistence,
   *  no extra randomness. A `point` generator returns its fixed vector
   *  (FUN_14073e080), so point-jitter is a deterministic drift BY DESIGN;
   *  plume diversity comes from sibling systems' sphere/line generators.
   *  Do not "fix" this into a random-walk or persistent-offset model. */
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
            // The child group is positioned relative to the metre-scaled parent
            // group, so the record-unit sim position is re-scaled ×15. See
            // NATIVE_TO_METRES (the spawner-action path passes [0,0,0], no scale).
            this.spawnEffect({
              effectName: action.effectName,
              position: [
                spawnPos.x * NATIVE_TO_METRES,
                spawnPos.y * NATIVE_TO_METRES,
                spawnPos.z * NATIVE_TO_METRES,
              ],
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

let particleLightSpriteTexture: THREE.DataTexture | null = null;

function getParticleLightSpriteTexture(): THREE.DataTexture {
  if (particleLightSpriteTexture) return particleLightSpriteTexture;
  const size = 64;
  const center = (size - 1) * 0.5;
  const invRadius = 1 / center;
  const pixels = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = (x - center) * invRadius;
      const dy = (y - center) * invRadius;
      const r = Math.sqrt(dx * dx + dy * dy);
      const t = Math.max(0, 1 - r);
      const alpha = Math.pow(t, 2.25) * (3 - 2 * t);
      const off = (y * size + x) * 4;
      pixels[off + 0] = 255;
      pixels[off + 1] = 255;
      pixels[off + 2] = 255;
      pixels[off + 3] = Math.max(0, Math.min(255, Math.round(alpha * 255)));
    }
  }
  const tex = new THREE.DataTexture(pixels, size, size, THREE.RGBAFormat, THREE.UnsignedByteType);
  tex.name = 'particle-light-radial-alpha';
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;
  tex.colorSpace = THREE.NoColorSpace;
  tex.needsUpdate = true;
  particleLightSpriteTexture = tex;
  return tex;
}

class LightRenderer {
  readonly group: THREE.Group;
  readonly sprite: THREE.Sprite;
  pointLight: THREE.PointLight | null = null;
  readonly score: number;
  private elapsed = 0;
  private active = true;
  private readonly material: THREE.SpriteMaterial;
  private intensityValues: number[] = [];
  private lightRadiusMultiplier = 1;
  private lightTintRMultiplier = 1;
  private lightTintGMultiplier = 1;
  private lightTintBMultiplier = 1;

  private static readonly SPRITE_RADIUS_SCALE = 0.25;
  private static readonly SPRITE_MAX_SIZE = 1.25;
  private static readonly SPRITE_OPACITY_SCALE = 0.22;

  constructor(
    private readonly body: ParticleComponentBody,
    private readonly intensityChannels: ParticleSystemIntensityChannel[] = [],
    private readonly intensityDefaults: readonly number[] = [],
  ) {
    this.group = new THREE.Group();
    this.group.name = 'particle-light';
    const pos = body.localPosition;
    if (Array.isArray(pos) && pos.length === 3) {
      // Light offset is a native BW-unit length placed in the ×15 metre frame.
      this.group.position.set(
        pos[0] * NATIVE_TO_METRES,
        pos[1] * NATIVE_TO_METRES,
        pos[2] * NATIVE_TO_METRES,
      );
    }
    this.material = new THREE.SpriteMaterial({
      color: new THREE.Color(1, 1, 1),
      map: getParticleLightSpriteTexture(),
      opacity: 1,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      depthTest: true,
    });
    this.sprite = new THREE.Sprite(this.material);
    this.group.add(this.sprite);
    this.setIntensityValues(intensityDefaults);
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

  restart(): void {
    this.active = true;
    this.group.visible = true;
    this.elapsed = 0;
    this.applySample(0);
  }

  setIntensityValues(values: readonly number[] | undefined): void {
    const count = Math.max(this.intensityChannels.length, this.intensityDefaults.length);
    this.intensityValues = [];
    for (let i = 0; i < count; i++) {
      const authored = values?.[i];
      const fallback = this.intensityDefaults[i] ?? 1;
      this.intensityValues[i] = Number.isFinite(authored) ? Number(authored) : fallback;
    }
    this.applyIntensityState();
    this.applySample(this.elapsed);
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
    let peak = Math.max(
      0,
      fixed[0] * this.lightTintRMultiplier,
      fixed[1] * this.lightTintGMultiplier,
      fixed[2] * this.lightTintBMultiplier,
    );
    for (const p of this.body.colorAnimation?.points ?? []) {
      peak = Math.max(
        peak,
        p.r * this.lightTintRMultiplier,
        p.g * this.lightTintGMultiplier,
        p.b * this.lightTintBMultiplier,
      );
    }
    let radius = Math.max(0, (this.body.radius ?? 0) * this.lightRadiusMultiplier);
    for (const p of this.body.radiusAnimation?.points ?? []) {
      radius = Math.max(radius, p.value * this.lightRadiusMultiplier);
    }
    return peak * Math.max(0.1, radius);
  }

  private applyIntensityState(): void {
    this.lightRadiusMultiplier = 1;
    this.lightTintRMultiplier = 1;
    this.lightTintGMultiplier = 1;
    this.lightTintBMultiplier = 1;
    for (let channelIndex = 0; channelIndex < this.intensityChannels.length; channelIndex++) {
      const channel = this.intensityChannels[channelIndex];
      const value = this.intensityValues[channelIndex] ?? this.intensityDefaults[channelIndex] ?? 1;
      for (const config of channel.configs ?? []) {
        const factor = sampleRamp(config.ramp, value, 1);
        if (!Number.isFinite(factor)) continue;
        for (const flag of config.flags ?? []) {
          switch (flag) {
            case PS_IC_LIGHT_RADIUS:
              this.lightRadiusMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_R:
              this.lightTintRMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_G:
              this.lightTintGMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_B:
              this.lightTintBMultiplier *= factor;
              break;
          }
        }
      }
    }
  }

  private applySample(t: number): void {
    const color = this.sampleColorAt(t);
    // Radius is a native BW-unit influence distance → metres for the ×15 world
    // (drives both the clamped preview flare and the point-light range).
    const radius = Math.max(
      0.01,
      this.sampleRadiusAt(t) * this.lightRadiusMultiplier * NATIVE_TO_METRES,
    );
    const r = Math.max(0, color[0] * this.lightTintRMultiplier);
    const g = Math.max(0, color[1] * this.lightTintGMultiplier);
    const b = Math.max(0, color[2] * this.lightTintBMultiplier);
    const peak = Math.max(r, g, b);
    if (peak > 0) {
      this.material.color.setRGB(r / peak, g / peak, b / peak);
    } else {
      this.material.color.setRGB(0, 0, 0);
    }
    this.material.opacity = Math.max(
      0,
      Math.min(1, color[3] * LightRenderer.SPRITE_OPACITY_SCALE),
    );
    // The decoded radius is a point-light influence distance, not the diameter
    // of a visible billboard. Keep the preview flare compact so light metadata
    // does not mask the authored smoke/fire/debris systems.
    const spriteSize = Math.max(
      0.08,
      Math.min(radius * LightRenderer.SPRITE_RADIUS_SCALE, LightRenderer.SPRITE_MAX_SIZE),
    );
    this.sprite.scale.set(spriteSize, spriteSize, spriteSize);
    if (!this.pointLight) return;
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
    // The decoded period matches the final key time across the current light
    // corpus; there is no repeat flag on the light prototype. Clamp through
    // sampleColor instead of wrapping, otherwise one-shot explosion flashes
    // restart every period and dominate the effect as a recurring orb.
    const axis = t;
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
    const axis = t;
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
  /** Renderer.opacityMultiplier (+0x74): native packs this as a lighting
   *  posterize byte, not a final alpha multiplier. */
  opacityMultiplier?: number;
  /** Renderer.tilingU/V (+0x90/+0x94): repeat local sprite UVs. */
  tilingU?: number;
  tilingV?: number;
  /** Renderer.flipTexcoordU/V (+0x9c/+0x9d): mirror local sprite UVs. */
  flipTexcoordU?: boolean;
  flipTexcoordV?: boolean;
  /** Renderer.velocityOriented (+0x9a): orient toward velocity. NATIVE
   *  FUN_1406d29c0 picks a velocity-aligned BASIS (U,V,N) with the SAME scalar
   *  size on BOTH axes — there is NO velocity-magnitude stretch (elongation is
   *  authored in scaleX; the per-axis scaler is Frida open-question #2). The
   *  current webview only spins the SAMPLED UV footprint inside a fixed
   *  camera-facing square (FS sprite-rotation block), so rectangular
   *  (scaleX!=1) sprites don't yet get a velocity-aligned quad. A faithful
   *  geometry-level basis (screen-2D vs world-3D + axis assignment) is BLOCKED
   *  on a Frida hook of FUN_1406d29c0 — do NOT guess it from parse offsets
   *  (DrawRec +8 trap). */
  velocityOriented?: boolean;
  /** Renderer lighting scalars (+0x54, +0x64..+0x6c). Note: renderer
   *  lightingShineness (+0x4c) is deliberately NOT consumed here — DXBC audit
   *  2026-06-09 showed the native body pow exponent is the PerFrame global
   *  g_gammaCorrection.x (≈1.0), not the per-record field; lightingShineness
   *  only reaches a CPU-side draw descriptor (FUN_140716f00 +0x24). */
  lightingAmbient?: number;
  lightingDiffuse?: number;
  lightingTransmission?: number;
  lightWrapAmount?: number;
  /** Renderer.shadowsStrength (+0x70): native packs int(value)-1 as the
   *  GRADIENT_MAP lightmapping glow posterize step count. */
  shadowsStrength?: number;
  /** Renderer.explicitOrientation (+0x30) and hide-angle fade controls. */
  explicitOrientation?: [number, number, number];
  explicitOrientationLocal?: boolean;
  hideStartCos?: number;
  hideSpeed?: number;
  /** Renderer.softParticleDepthScale (+0x7c): alpha fade against opaque scene depth. */
  softParticleDepthScale?: number;
  /** Live key-light direction, world-space, pointing toward the sun. */
  sunDirection?: THREE.Vector3;
  /** Colored Reinhard-normalized key-light color for particle lightmaps. */
  sunColorNorm?: THREE.Color;
}

/**
 * Map PS_RBT enum label -> THREE.js blending parameters. Six modes have
 * direct equivalents; GRADIENT_MAP / UNDERWATER_GRADIENT_MAP additionally
 * trigger LUT remap (driven by ``useLut`` in the material options, where
 * the renderer's ``textureName1`` is bound as the color ramp). SHIMMER
 * and DEFORM_WATER_SURFACE are rendered alpha-over while their fragment path
 * samples the scene-color snapshot as a screen-space distortion source.
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
      // RE doc 63 L5: BLENDED_GLOW is in the same order-dependent sorted
      // alpha-over bucket as GRADIENT_MAP. The earlier SrcAlpha/One path was
      // too additive and bypassed the premultiplied output convention.
      return {
        blending: THREE.CustomBlending,
        blendSrc: THREE.OneFactor,
        blendDst: THREE.OneMinusSrcAlphaFactor,
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
  const hasExplicitOrientation = vectorHasLength(opts.explicitOrientation);
  const explicitOrientation =
    hasExplicitOrientation && (opts.hideStartCos ?? 1) < 0.999
      ? opts.explicitOrientation
      : undefined;
  const useHideAngle = explicitOrientation ? 1 : 0;
  // Fixed-orientation quad (RE FUN_1406d29c0 explicit-vector branch, build
  // 12506899): native builds the billboard basis from the per-particle
  // orientation vector — seeded from renderer.explicitOrientation via
  // FUN_1406d2790 — so the quad FACES explicitOrientation instead of the camera.
  // The default (zero explicitOrientation) keeps the camera-facing basis
  // (param_2[1] = camera, unanimous in the 5-agent corroboration). ~36% of
  // systems carry a nonzero explicitOrientation (mostly (0,1,0) ground-flat +
  // (1,0,0)/(0,0,1) cards). velocityOriented systems use a velocity basis, so
  // they are excluded here (left camera-facing) rather than guessed at.
  const fixedOrientationVec =
    hasExplicitOrientation && !opts.velocityOriented ? opts.explicitOrientation : undefined;
  const useFixedOrientation = fixedOrientationVec ? 1 : 0;
  const orientationVec = explicitOrientation ?? fixedOrientationVec;
  const hideSpeed =
    opts.hideSpeed !== undefined && Number.isFinite(opts.hideSpeed) && opts.hideSpeed > 0
      ? opts.hideSpeed
      : 1;
  const softParticleDepthScale =
    opts.softParticleDepthScale !== undefined &&
    Number.isFinite(opts.softParticleDepthScale) &&
    opts.softParticleDepthScale > 0
      ? opts.softParticleDepthScale
      : 0;
  const distortionMode =
    opts.blendType === 'DEFORM_WATER_SURFACE' ? 1 : opts.blendType === 'SHIMMER' ? 2 : 0;
  const distortionStrength = distortionMode === 1 ? 0.018 : distortionMode === 2 ? 0.012 : 0;
  const lightingAmbient = Math.max(0, finiteNumber(opts.lightingAmbient, 0.06));
  const lightingDiffuse = Math.max(0, finiteNumber(opts.lightingDiffuse, 1));
  const lightingTransmission = Math.max(0, finiteNumber(opts.lightingTransmission, 0));
  const lightWrapAmount = Math.max(0, finiteNumber(opts.lightWrapAmount, 0));
  const glowPosterizeSteps = particleByteStepCount(opts.shadowsStrength);
  const opacityLightingSteps = particleByteStepCount(opts.opacityMultiplier);
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
      // Native gradient-map permutations take the authored ramp/glow branch.
      // Keep this separate from useLut, which starts at 0 until the ramp
      // texture finishes loading and therefore is not a reliable shader-mode
      // discriminator.
      uGradientMapMode: { value: PS_RBT_LUT_MODES.has(opts.blendType ?? '') ? 1 : 0 },
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
      // Set to 1 by bindTexture when texture content proves the framesPerX/Y grid
      // is vestigial (a single sprite mis-authored with a grid; see
      // spriteSheetCell0Empty) — makes the noAnimation static-cell path sample the
      // FULL texture instead of the transparent cell-0 corner. Default 0 keeps the
      // engine-faithful cell-0 crop for genuine sheets.
      uVestigialGrid: { value: 0 },
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
      uVelocityOriented: { value: opts.velocityOriented ? 1 : 0 },
      uViewportHeight: { value: 600 },
      // Renderer hide-angle fade. Native FUN_1406d31f0 multiplies alpha by a
      // clamped `(abs(dot(viewDir, explicitOrientation)) - start) * speed`
      // term and flips it for one lightmapping path. Gate to non-default
      // hideStartCos so default-authored explicit orientations stay neutral.
      uUseHideAngle: { value: useHideAngle },
      uUseFixedOrientation: { value: useFixedOrientation },
      uExplicitOrientation: {
        value: new THREE.Vector3(
          orientationVec?.[0] ?? 0,
          orientationVec?.[1] ?? 0,
          orientationVec?.[2] ?? 1,
        ).normalize(),
      },
      uExplicitOrientationLocal: { value: opts.explicitOrientationLocal ? 1 : 0 },
      uHideStartCos: { value: opts.hideStartCos ?? 1 },
      uHideSpeed: { value: hideSpeed },
      uHideInvert: { value: opts.lightingType === 'lightmapping4Way' ? 1 : 0 },
      // Native uses an opaque-only depth copy for soft particles/fog. The
      // scene environment binds a WebGL DepthTexture here before each render;
      // when absent, uSoftDepthSize stays 1x1 and the shader skips the fade.
      uSoftParticleDepthScale: { value: softParticleDepthScale },
      uSoftDepthTexture: { value: null as THREE.DepthTexture | null },
      uSoftDepthSize: { value: new THREE.Vector2(1, 1) },
      uSoftCameraNear: { value: 0.1 },
      uSoftCameraFar: { value: 1000 },
      // Directional-lightmap (PS_RLT) reconstruction. uLightingMode=1 when
      // the texture is an `_LM` lightmap (lightmapping4Way/HL2); the
      // fragment shader then treats `map` RGB as a 3-direction HL2-basis
      // lightmap reconstructed against uSunDirWorld, not albedo. The sun
      // dir defaults to the scene's key DirectionalLight (scene.ts:121-122,
      // positioned at (50,80,50)) so particle lighting matches the hull.
      uLightingMode: {
        value: PS_RLT_LIGHTMAP_MODES.has(opts.lightingType ?? '') ? 1 : 0,
      },
      // Live key-light direction/color. RE doc 63 H7/M2: particle lightmaps
      // should track the same world sun as the scene, and the native shader
      // applies colored Reinhard normalization, sunColor/(luma+1), rather than
      // a fixed grayscale scale.
      uSunDirWorld: { value: opts.sunDirection?.clone() ?? DEFAULT_PARTICLE_SUN_DIR.clone() },
      uSunColorNorm: {
        value: opts.sunColorNorm?.clone() ?? DEFAULT_PARTICLE_SUN_COLOR_NORM.clone(),
      },
      // The native log/mul/exp on body RGB uses the PerFrame global
      // g_gammaCorrection.x (cb1[20], default 1.0 — identity), NOT a
      // per-record exponent (DXBC audit 2026-06-09, ps4/6/24/40/46/47/55).
      // No uniform needed: the webview renders in linear space already.
      uLightingAmbient: { value: lightingAmbient },
      uLightingDiffuse: { value: lightingDiffuse },
      uLightingTransmission: { value: lightingTransmission },
      uLightWrapAmount: { value: lightWrapAmount },
      // Motion-vector flipbook blending (`_MVEA`). useMv is flipped to 1 by
      // bindMvTexture once the MV DDS loads; the shader then samples two
      // adjacent frames, warps each along the MV (G,B) optical-flow field,
      // and cross-fades them — replacing the hard age-driven frame step.
      // Math RE'd instruction-for-instruction from particles.win.dx11.fxo.
      mvMap: { value: null as THREE.Texture | null },
      useMv: { value: 0 },
      mvDistortion: { value: opts.motionVectorsDistortion ?? 0 },
      useEmissionAlphaFromMV: { value: opts.useEmissionAlphaFromMV ? 1 : 0 },
      // Premultiplied-alpha output (RE 2026-05-29). GRADIENT_MAP /
      // UNDERWATER_GRADIENT_MAP blend premultiplied alpha-over in the engine
      // (Src=ONE, Dst=INV_SRC_ALPHA), so the fragment shader must premultiply
      // its RGB by the output alpha. Every other blend mode outputs straight
      // (non-premultiplied) colour as before. Keyed off blendType — the two
      // gradient modes are exactly PS_RBT_LUT_MODES.
      uPremultiply: {
        value:
          PS_RBT_LUT_MODES.has(opts.blendType ?? '') || opts.blendType === 'BLENDED_GLOW' ? 1 : 0,
      },
      // DEFORM_WATER_SURFACE / SHIMMER are screen-space distortion passes whose
      // tex0 is a normal/deform map (not albedo). The scene environment binds
      // a pre-particle scene-color snapshot so the fragment shader can warp the
      // background instead of showing the normal map as colour.
      uDistortion: {
        value: distortionMode > 0 ? 1 : 0,
      },
      uDistortionMode: { value: distortionMode },
      uDistortionStrength: { value: distortionStrength },
      uDistortionSceneTexture: { value: null as THREE.Texture | null },
      uDistortionSceneSize: { value: new THREE.Vector2(1, 1) },
      // Warm "detonation glow" strength for the lightmapping + GRADIENT_MAP
      // path. Native multiplies the ramp term by the scaler-driven
      // per-particle payload; the uniform is the 1.0 default/fallback.
      uGlowStrength: { value: 1.0 },
      // Native FUN_140716f00 packs Renderer.shadowsStrength as
      // int(value) < 2 ? 0 : int(value) - 1 into the shader's byte payload.
      // The GRADIENT_MAP + lightmapping pixel path uses it to posterize the
      // glow-ramp coordinate before sampling g_particleGlowTexture.
      uGlowPosterizeSteps: { value: glowPosterizeSteps },
      // Renderer.opacityMultiplier is another packed byte in native
      // FUN_140716f00. Despite the sidecar name, the pixel shader uses it to
      // quantize scalar lighting/body factors, not final alpha.
      uOpacityLightingSteps: { value: opacityLightingSteps },
    },
    vertexShader: /* glsl */ `
      attribute vec4 color;
      attribute vec3 velocity;
      attribute float size;
      attribute float glowStrength;
      attribute float spriteScaleX;
      attribute float age;
      attribute float frameSeed;
      attribute float framePhase;
      attribute float rotationPhase;
      attribute vec3 iPosition;
      uniform float uUseSpriteRotation;
      uniform float uVelocityOriented;
      uniform float uSpriteAspectX;
      uniform float uPointExtent;
      uniform float uViewportHeight;
      uniform float uUseHideAngle;
      uniform vec3 uExplicitOrientation;
      uniform float uExplicitOrientationLocal;
      uniform float uUseFixedOrientation;
      uniform float uHideStartCos;
      uniform float uHideSpeed;
      uniform float uHideInvert;
      varying vec4 vColor;
      varying float vAge;
      varying float vFrameSeed;
      varying float vFramePhase;
      varying float vRotationPhase;
      varying float vVelocityAngle;
      varying float vHideFade;
      varying float vGlowStrength;
      varying float vSpriteAspectX;
      varying float vPointExtent;
      varying vec2 vLocalUV;

      void main() {
        vColor = color;
        vGlowStrength = glowStrength;
        vAge = age;
        vFrameSeed = frameSeed;
        vFramePhase = framePhase;
        vRotationPhase = rotationPhase;
        vec3 viewVel = (modelViewMatrix * vec4(velocity, 0.0)).xyz;
        vVelocityAngle = (uVelocityOriented > 0.5 && length(viewVel.xy) > 0.00001)
          ? atan(viewVel.y, viewVel.x)
          : 0.0;
        vec3 worldPos = (modelMatrix * vec4(iPosition, 1.0)).xyz;
        vHideFade = 1.0;
        if (uUseHideAngle > 0.5) {
          vec3 orientW = uExplicitOrientationLocal > 0.5
            ? normalize(mat3(modelMatrix) * uExplicitOrientation)
            : normalize(uExplicitOrientation);
          vec3 viewDirW = normalize(worldPos - cameraPosition);
          float h = clamp((abs(dot(viewDirW, orientW)) - uHideStartCos) * uHideSpeed, 0.0, 1.0);
          vHideFade = (uHideInvert > 0.5) ? (1.0 - h) : h;
        }
        float spriteAspectX = max(0.001, abs(uSpriteAspectX * spriteScaleX));
        vSpriteAspectX = spriteAspectX;
        vPointExtent = (uUseSpriteRotation > 0.5)
          ? sqrt(spriteAspectX * spriteAspectX + 1.0)
          : max(spriteAspectX, 1.0);
        // INSTANCED camera-facing billboard. position.xy is the quad corner in
        // [0,1] (= the old gl_PointCoord); iPosition is the per-particle world
        // center. The old point's world-space DIAMETER was size*vPointExtent
        // (gl_PointSize = that projected to px); expand the quad by that amount
        // in view space so it always faces the camera. Flip corner.y so vLocalUV
        // matches gl_PointCoord (top-left origin, y down). No gl_PointSize ->
        // no hardware ALIASED_POINT_SIZE_RANGE cap.
        vec2 cornerUV = position.xy;
        vLocalUV = cornerUV;
        float worldDiam = size * vPointExtent;
        if (uUseFixedOrientation > 0.5) {
          // Fixed-orientation quad (RE FUN_1406d29c0 explicit-vector branch): the
          // quad FACES world explicitOrientation (normal = N) instead of the
          // camera; U/V span the perpendicular plane (V up-ish). N=(0,1,0) -> the
          // fallback U yields a flat XZ ground quad; N=(1,0,0) -> a vertical YZ
          // card. Same world-diameter sizing as the camera-facing path.
          vec3 N = uExplicitOrientationLocal > 0.5
            ? normalize(mat3(modelMatrix) * uExplicitOrientation)
            : normalize(uExplicitOrientation);
          vec3 U = cross(N, vec3(0.0, 1.0, 0.0));
          float ulen = length(U);
          U = (ulen > 1e-4) ? U / ulen : vec3(1.0, 0.0, 0.0);
          vec3 V = cross(U, N);
          vec3 centerW = (modelMatrix * vec4(iPosition, 1.0)).xyz;
          vec3 cornerW = centerW
            + U * ((cornerUV.x - 0.5) * worldDiam)
            + V * ((0.5 - cornerUV.y) * worldDiam);
          gl_Position = projectionMatrix * viewMatrix * vec4(cornerW, 1.0);
        } else {
          // INSTANCED camera-facing billboard (default, unchanged): expand the
          // quad in view space so it always faces the camera.
          vec4 mvPosition = modelViewMatrix * vec4(iPosition, 1.0);
          vec2 viewOffset = vec2(cornerUV.x - 0.5, 0.5 - cornerUV.y) * worldDiam;
          gl_Position = projectionMatrix * vec4(mvPosition.xyz + vec3(viewOffset, 0.0), 1.0);
        }
      }
    `,
    fragmentShader: /* glsl */ `
      uniform sampler2D map;
      uniform float useMap;
      uniform sampler2D lut;
      uniform float useLut;
      uniform float uGradientMapMode;
      uniform vec4 atlasRect;
      uniform float useAtlasRect;
      uniform vec2 framesPerXY;
      uniform float uVestigialGrid;
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
      uniform float uVelocityOriented;
      uniform float uSpriteAspectX;
      uniform float uPointExtent;
      uniform vec2 uUvTiling;
      uniform vec2 uUvFlip;
      uniform sampler2D uSoftDepthTexture;
      uniform vec2 uSoftDepthSize;
      uniform float uSoftParticleDepthScale;
      uniform float uSoftCameraNear;
      uniform float uSoftCameraFar;
      uniform float uLightingMode;
      uniform vec3 uSunDirWorld;
      uniform vec3 uSunColorNorm;
      uniform float uLightingAmbient;
      uniform float uLightingDiffuse;
      uniform float uLightingTransmission;
      uniform float uLightWrapAmount;
      uniform sampler2D mvMap;
      uniform float useMv;
      uniform float mvDistortion;
      uniform float useEmissionAlphaFromMV;
      uniform float uPremultiply;
      uniform float uDistortion;
      uniform float uDistortionMode;
      uniform float uDistortionStrength;
      uniform sampler2D uDistortionSceneTexture;
      uniform vec2 uDistortionSceneSize;
      uniform float uGlowStrength;
      uniform float uGlowPosterizeSteps;
      uniform float uOpacityLightingSteps;
      varying vec4 vColor;
      varying float vAge;
      varying float vFrameSeed;
      varying float vFramePhase;
      varying float vRotationPhase;
      varying float vVelocityAngle;
      varying float vHideFade;
      varying float vGlowStrength;
      varying float vSpriteAspectX;
      varying float vPointExtent;
      varying vec2 vLocalUV;

      float perspectiveDepthToViewZ(const in float invClipZ, const in float near, const in float far) {
        return (near * far) / ((far - near) * invClipZ - far);
      }

      float quantizeLightingScalar(float value, float steps) {
        if (steps <= 0.5) return value;
        return floor(value * steps + 0.5) / steps;
      }

      void main() {
        vec4 base;
        vec3 glow = vec3(0.0);   // additive warm glow (gradient+lightmapping)
        if (useMap > 0.5) {
          // Convert the square GL_POINT coordinate to authored sprite UVs.
          // Geometry is measured in sprite-height units: width=scaleX,
          // height=1. Rotation happens in that geometric space so rectangular
          // sprites and custom pivots stay coherent.
          vec2 pointGeom = (vLocalUV - vec2(0.5)) * vPointExtent;
          vec2 pivotGeom = vec2(
            (uRotationPivot.x - 0.5) * vSpriteAspectX,
            uRotationPivot.y - 0.5
          );
          vec2 spriteGeom = pointGeom;
          if (uUseSpriteRotation > 0.5) {
            // GL_POINTS cannot rotate the quad geometry. Enlarge the point in
            // the vertex shader, rotate source geometry by the inverse angle,
            // then sample the unrotated sprite UVs.
            vec2 rel = pointGeom - pivotGeom;
            // velocityOriented: spins the SAMPLED UV footprint inside the fixed
            // square (an isotropic approximation of native's velocity-aligned
            // basis). NOT a geometry rotation and NOT a stretch — see the
            // velocityOriented field doc. A faithful basis is Frida-blocked.
            float spriteAngle = vRotationPhase + (uVelocityOriented > 0.5 ? vVelocityAngle : 0.0);
            float s = sin(spriteAngle);
            float c = cos(spriteAngle);
            spriteGeom = pivotGeom + vec2(c * rel.x + s * rel.y, -s * rel.x + c * rel.y);
          }
          vec2 local = vec2(spriteGeom.x / vSpriteAspectX + 0.5, spriteGeom.y + 0.5);
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
          // _MVEA emission plumbing. The emission channel (.R) is consumed by
          // TWO permutations (ps4.txt:589-628): the non-gradient body
          // substitution (M4) and — for GRADIENT_MAP — the glow-ramp KEY (the
          // engine's r5.x is the t3/g_particleMVTexture sample, NOT the _LM
          // body texel). Sample it wherever the texture is available; -1
          // sentinel = no sample this fragment (texture missing/not loaded).
          bool useMvAlpha = (useEmissionAlphaFromMV > 0.5 && uGradientMapMode <= 0.5);
          bool useMvEmissionBody = useMvAlpha;
          bool wantMvEmission = (useMvAlpha || uGradientMapMode > 0.5);
          float mvEmissionSample = -1.0;

          if (randomCell) {
            // Fixed per-particle random cell (no time advance, no cross-fade).
            // vFrameSeed was assigned floor(rand()*framesRangeEnd) at spawn.
            vec2 gridUv = (vec2(mod(vFrameSeed, fx), floor(vFrameSeed / fx)) + local) / vec2(fx, fy);
            vec2 puv = gridUv;
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
            if (useMv > 0.5 && wantMvEmission) {
              // _MVEA shares the flipbook grid layout (it is its own file —
              // the atlas-rect remap applies to the packed page only).
              vec4 e = texture2D(mvMap, gridUv);
              mvEmissionSample = e.r;
              if (useMvAlpha) base.a = e.a;
            }
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
            // _MVEA warp channels are LIGHTING-GATED (RE ps4.txt:574-576):
            // sample r3.xyz=t3, then mad r3, r3.zyxy,2,-1, then
            // movc r3.xy, lightingType, (B,G), (R,G) -- the engine selects
            // (du,dv)=(B,G) under lightmapping and (R,G) under lambert. This
            // path previously read (G,B) unconditionally, which transposes the
            // warp under lightmapping and uses the wrong U source under lambert.
            // The _MVEA DDS decodes in native RGBA order (dds/index.ts -- no BGRA
            // swap), so .bg / .rg address the literal blue/green / red/green texels.
            vec2 mv0 = (uLightingMode > 0.5 ? texture2D(mvMap, cell0).bg : texture2D(mvMap, cell0).rg) * 2.0 - 1.0;
            vec2 mv1 = (uLightingMode > 0.5 ? texture2D(mvMap, cell1).bg : texture2D(mvMap, cell1).rg) * 2.0 - 1.0;
            vec2 uv0 = cell0 - mv0 * f * mvDistortion;
            vec2 uv1 = cell1 + mv1 * (1.0 - f) * mvDistortion;
            base = mix(texture2D(map, uv0), texture2D(map, uv1), f);
            if (wantMvEmission) {
              // _MVEA.R = emission, .A = opacity — sampled at the warped UVs,
              // lerped by f. Non-gradient permutation: emission substitutes
              // the body and .A is the opacity (M4). GRADIENT_MAP permutation:
              // the emission is the glow-ramp KEY (ps4.txt:597-618) while the
              // _LM body keeps its own alpha.
              vec4 e0 = texture2D(mvMap, uv0);
              vec4 e1 = texture2D(mvMap, uv1);
              mvEmissionSample = mix(e0.r, e1.r, f);
              if (useMvAlpha) base.a = mix(e0.a, e1.a, f);
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
            // Static cell. H4 (RE doc 63): a noAnimation system on a direct
            // multi-cell DDS shows CELL 0 only (engine forces the frame byte
            // to 0, FUN_14071b7f0 @0x14071c5a3) — without this the whole grid
            // crams into one quad and reads as garbage. Manifest atlas rects
            // already select one authored TGA sprite inside the packed atlas,
            // so splitting the rect again samples only its padded corner and
            // turns soft/glow sprites into hard squares.
            vec2 puv = local;
            // uVestigialGrid==1 (set by bindTexture's content test) means the
            // framesPerX/Y grid is vestigial on a single sprite — sample the full
            // texture instead of cropping to the (transparent) cell-0 corner.
            if (fx * fy > 1.0 && useAtlasRect <= 0.5 && uVestigialGrid < 0.5) {
              puv = puv / vec2(fx, fy);
            }
            vec2 gridUv = puv; // pre-atlas-remap cell-0 UV, shared by _MVEA
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
            if (useMv > 0.5 && wantMvEmission) {
              vec4 e = texture2D(mvMap, gridUv);
              mvEmissionSample = e.r;
              if (useMvAlpha) base.a = e.a;
            }
          }
          // M4 (RE doc 63, ps4.txt:595-606): when MVEA.R is selected as
          // the emission source, native substitutes it into the non-gradient
          // body before lighting/tinting. It is not an extra additive glow, and
          // gradient-map permutations take the ramp/glow branch instead.
          // Guarded on an actual sample so a system that authors the flag but
          // has no _MVEA loaded keeps its texture body instead of going black.
          if (useMvEmissionBody && mvEmissionSample >= 0.0) {
            base.rgb = vec3(mvEmissionSample);
          }
          // (RETIRED 2026-06-09) Two GRADIENT_MAP+lightmapping alpha hacks
          // lived here; both are gone and must not return:
          // 1. A radial soft-disc falloff ("de-square" Moray smoke, 831a426).
          //    The squares were a pre-×15 size-era artifact — sprites were
          //    15× too small, barely overlapping, so quad bounds showed
          //    (199265f fixed the scale). Re-tested live: Moray renders soft
          //    billows, and GK_Shot looks BETTER without it (the disc edge
          //    accentuated the bead-string banding). Native has no per-texel
          //    falloff.
          // 2. base.a = min(base.a, luminance) "BC7 coverage clamp". Premise
          //    obsolete (bindTexture software-decodes BC7 → exact alpha) and
          //    native never couples alpha to RGB (DXBC: o0.w = texA × tint.a
          //    × fade). It distorted 14.6% of Smoke_run_7x7_LM texels (mean
          //    −42/255, max −231/255), thinning authored dark-opaque smoke
          //    cores. A/B-verified: removal = denser faithful cores, no
          //    squares.
          // GRADIENT_MAP glow key (ps4.txt:589-628; CORRECTS doc-63 M3): the
          // engine keys the ramp by the _MVEA EMISSION sample (r5.x = the
          // t3/g_particleMVTexture read) — never by the _LM body texel. Keying
          // off the _LM red put the warm band of fire_yellow_1_HDR on the
          // wrong texels (cream wash instead of the saturated orange core).
          // Fall back to the raw _LM red only when no _MVEA is available.
          float gmag = (uGradientMapMode > 0.5 && mvEmissionSample >= 0.0)
            ? mvEmissionSample
            : base.r;
          // Native applies pow(base.rgb, g_gammaCorrection.x) here — a
          // PerFrame GLOBAL defaulting to 1.0 (identity), confirmed by DXBC
          // audit 2026-06-09 (cb1[20] in all 7 permutations; reflection
          // header "g_gammaCorrection // Offset: 320"). An earlier port
          // mis-read the exponent as renderer.lightingShineness — authored
          // up to 100 on GK_Shot smoke, which crushed the lightmapped body
          // to black. lightingShineness never reaches the GPU (it stops in
          // a CPU draw descriptor, FUN_140716f00 +0x24), so no pow here.
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
            float wrap = max(0.0, uLightWrapAmount);
            float wrapDenom = max(0.0001, 1.0 + wrap);
            vec3 basisDot = vec3(dot(B0, sunV), dot(B1, sunV), dot(B2, sunV));
            vec3 w = max(vec3(0.0), (basisDot + vec3(wrap)) / wrapDenom);
            w *= w;                       // HL2 weighting is dot^2
            vec3 tw = max(vec3(0.0), (-basisDot + vec3(wrap)) / wrapDenom);
            tw *= tw;
            float flatLum = (base.r + base.g + base.b) / 3.0;
            // RE doc 63 H6: the engine does NOT energy-normalize (no /wsum) and
            // has no 30% flat mix — lit = saturate(Σ LMi·(axisi·sun)²) via
            // mad_sat (ps4.txt:789-794). The old /wsum cancelled the directional
            // magnitude (constant flat shade) and the 0.30 mix washed it out.
            // Renderer carries authored ambient/diffuse/transmission/wrap
            // scalars in the same 0xa0-byte native struct. Apply them here to
            // avoid the old hardcoded ambient-only approximation while keeping
            // the unknown per-particle glow scalar separate below.
            float ambient = uLightingAmbient * flatLum;
            float direct = uLightingDiffuse * dot(w, base.rgb);
            float transmitted = uLightingTransmission * dot(tw, base.rgb);
            // Colored Reinhard-normalized sun term (RE doc 63 M2). A white sun
            // produces the historical 0.5 attenuation; weather-tinted suns now
            // tint the relit smoke/explosion body instead of staying grayscale.
            base = vec4(
              clamp(vec3(ambient) + (direct + transmitted) * uSunColorNorm, 0.0, 1.0),
              base.a
            );
          }
          if (useLut > 0.5) {
            if (uLightingMode > 0.5) {
              // GRADIENT_MAP + lightmapping (RE doc 63 M3, ps4.txt:614-628;
              // corrects the prior "U pinned to 0"): the engine samples the HDR
              // ramp at U = 1 - glow (glow = the particle texture value at the
              // sprite UV, captured as gmag before the LM relight). The warm
              // ramp colour is added as an emissive "detonation" glow on top of
              // the relit smoke body, OUTSIDE the per-particle tint (engine:
              // rgb = base*lit + emis*v10.x). Native packs renderer
              // shadowsStrength as the byte step count that quantizes glow
              // before U = 1 - glow. vGlowStrength carries the native
              // scaler-driven per-particle payload (default 1.0).
              // Now varies per-texel → a warm GRADIENT across the sprite, not
              // one flat tan colour.
              float glowKey = gmag;
              if (uGlowPosterizeSteps > 0.5) {
                glowKey = floor(glowKey * uGlowPosterizeSteps + 0.5) / uGlowPosterizeSteps;
              }
              vec4 g = texture2D(lut, vec2(1.0 - glowKey, 0.5));
              glow = g.rgb * g.a * uGlowStrength * vGlowStrength;
            } else {
              // Lambert GRADIENT_MAP: luminance-keyed recolor (engine lambert
              // path) — sweep the ramp by the sprite luminance (base.r).
              base = vec4(texture2D(lut, vec2(base.r, 0.5)).rgb, base.a);
            }
          }
          if (uDistortion <= 0.5 && uOpacityLightingSteps > 0.5) {
            // Native extracts Renderer.opacityMultiplier as an 8-bit step
            // count and quantizes lighting factors before composing the body
            // with the additive glow. The webview has a collapsed lighting
            // body, so preserve hue while posterizing its luminance.
            float bodyLum = max(max(base.r, base.g), base.b);
            if (bodyLum > 0.000001) {
              float qLum = quantizeLightingScalar(bodyLum, uOpacityLightingSteps);
              base.rgb *= qLum / bodyLum;
            }
          }
        } else {
          vec2 c = vLocalUV - vec2(0.5);
          float r = length(c) * 2.0;
          if (r > 1.0) discard;
          // Soft circular falloff (squared).
          float a = (1.0 - r * r);
          base = vec4(1.0, 1.0, 1.0, a);
        }
        float outA = vColor.a * base.a * vHideFade;
        vec3 outRgb = vColor.rgb * base.rgb + glow;
        if (uDistortion > 0.5) {
          // SHIMMER systems that author useEmissionAlphaFromMV compose an
          // EMISSIVE body with the refraction (the &8 MVEA control bit
          // substitutes the emission into the body even for distortion
          // techniques, ps4.txt:597-602) — without it the muzzle-flash core
          // (GK_Shot systems[0]) contributes nothing. base.rgb already holds
          // the MVEA emission when one was sampled, else the lit _LM body
          // (an approximation for framesPlayback systems with no _MVEA bound).
          vec3 emissionBody =
            (useEmissionAlphaFromMV > 0.5) ? vColor.rgb * base.rgb : vec3(0.0);
          vec2 screenUv = gl_FragCoord.xy / uDistortionSceneSize;
          vec2 normalOffset = base.rg * 2.0 - 1.0;
          if (dot(normalOffset, normalOffset) < 0.0001) {
            normalOffset = (vLocalUV - vec2(0.5)) * 2.0;
          }
          if (uDistortionSceneSize.x > 1.0 && uDistortionSceneSize.y > 1.0) {
            vec2 warpedUv = clamp(
              screenUv + normalOffset * uDistortionStrength * clamp(outA, 0.0, 1.0),
              vec2(0.001),
              vec2(0.999)
            );
            vec3 refracted = texture2D(uDistortionSceneTexture, warpedUv).rgb;
            float foam = uDistortionMode < 1.5 ? 0.10 * clamp(outA, 0.0, 1.0) : 0.0;
            outRgb = refracted + vec3(foam) + emissionBody;
            outA *= (uDistortionMode < 1.5) ? 0.45 : 0.55;
          } else if (useEmissionAlphaFromMV > 0.5) {
            // No scene-colour RTT (the inspector's normal state): keep the
            // emissive flash core visible instead of the faint placeholder.
            outRgb = emissionBody;
          } else {
            // Fallback when the scene-color copy is unavailable.
            outRgb = vec3(1.0);
            outA *= 0.15;
          }
        }
        if (
          uSoftParticleDepthScale > 0.0 &&
          uSoftDepthSize.x > 1.0 &&
          uSoftDepthSize.y > 1.0
        ) {
          vec2 screenUv = gl_FragCoord.xy / uSoftDepthSize;
          float sceneDepth = texture2D(uSoftDepthTexture, screenUv).x;
          if (sceneDepth < 0.999999) {
            float sceneLinear = -perspectiveDepthToViewZ(sceneDepth, uSoftCameraNear, uSoftCameraFar);
            float particleLinear = -perspectiveDepthToViewZ(gl_FragCoord.z, uSoftCameraNear, uSoftCameraFar);
            outA *= clamp((sceneLinear - particleLinear) * uSoftParticleDepthScale, 0.0, 1.0);
          }
        }
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
    // Instanced camera-facing billboard quads (not GL_POINTS) — draw both faces
    // so the quad is never back-face culled regardless of corner winding.
    side: THREE.DoubleSide,
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
  intensityValues?: number[];
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

function intensityDefaultsForRecord(record: ParticleRecord): number[] {
  return (record.intensityChannels ?? []).map((channel) =>
    finiteNumber(channel.defaultIntensity, 1),
  );
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
  private sunDirection = DEFAULT_PARTICLE_SUN_DIR.clone();
  private sunColorNorm = DEFAULT_PARTICLE_SUN_COLOR_NORM.clone();
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
  private sortCamera: THREE.Camera | null = null;
  private viewportSize = new THREE.Vector2();

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

  /** Camera used for WG's order-dependent particle draw sorting. */
  setSortCamera(camera: THREE.Camera | null): void {
    this.sortCamera = camera;
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) system.setSortCamera(camera);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) system.setSortCamera(camera);
    }
  }

  /** Keep particle lightmap reconstruction synced to the scene's key sun.
   *  `direction` points toward the sun, matching `createSceneEnvironment`.
   *  `color` is normalized with WG's colored Reinhard term. */
  setSunLighting(direction: THREE.Vector3, color: THREE.Color): void {
    if (direction.lengthSq() > 1e-10) {
      this.sunDirection.copy(direction).normalize();
    }
    this.sunColorNorm.copy(normalizedParticleSunColor(color));
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) this.applySunLighting(system.material);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) this.applySunLighting(system.material);
    }
  }

  /** Build the scene from a sidecar's `effects` block. Returns the
   *  flat list of attachment handles for UI binding.
   *
   *  `options.loopOneShot` decides per attachment whether a one-shot effect
   *  loops for inspection (inspector/ambient default) or plays once and
   *  finishes (ship-view event effects — muzzle/explosion — whose lifetime is
   *  governed by the trigger, mirroring the native fire-once-then-kill
   *  EffectManager model). `restartAttachment()` re-fires a finished one. */
  build(
    attachments: ParticleAttachment[],
    particles: Record<string, ParticleRecord>,
    resolveNodePosition: (attachment: ParticleAttachment) => THREE.Vector3 | null,
    options: { loopOneShot?: (attachment: ParticleAttachment) => boolean } = {},
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
        options.loopOneShot ? options.loopOneShot(a) : true,
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
    intensityValues?: readonly number[],
  ): { systems: SystemRenderer[]; lights: LightRenderer[] } {
    const systems: SystemRenderer[] = [];
    const lights: LightRenderer[] = [];
    const intensityDefaults = intensityDefaultsForRecord(rec);
    // Effect-level one-shot loop clock: window + the LONGEST system maxAge,
    // shared by all systems so the looped re-burst stays synchronized (the
    // engine restarts an effect as a unit). Mirrors the constructor's maxAge
    // clamp so the boundary can never undercut a system's own decay window.
    let longestMaxAge = 0;
    for (const sys of rec.systems) {
      longestMaxAge = Math.max(
        longestMaxAge,
        Math.max(0.05, sys.general?.maxParticleAge ?? DEFAULT_PARTICLE_LIFETIME),
      );
    }
    const loopResetPeriod =
      (rec.maxEmittingDuration ?? 0) > 0 ? rec.maxEmittingDuration! + longestMaxAge : 0;
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
      const hasAuthoredRotation =
        rampHasNonZeroValue(r?.yawRateRamp) ||
        hasNonZeroNumber(r?.spinRateBase) ||
        hasNonZeroNumber(r?.spinRateRange) ||
        hasNonZeroNumber(r?.initialOrientationBase) ||
        hasNonZeroNumber(r?.initialOrientationRange) ||
        !!r?.velocityOriented;
      const useSpriteRotation = (!!r?.textureUrl0 || useAtlas) && hasAuthoredRotation;
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
        // Authored flag, NOT gated on the MV texture: it also drives the
        // SHIMMER emission-body composite (which falls back to the lit _LM
        // when no _MVEA is loaded). All mvMap samples are guarded on useMv.
        useEmissionAlphaFromMV: anim?.useEmissionAlphaFromMV,
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
        velocityOriented: r?.velocityOriented,
        lightingAmbient: r?.lightingAmbient,
        lightingDiffuse: r?.lightingDiffuse,
        lightingTransmission: r?.lightingTransmission,
        lightWrapAmount: r?.lightWrapAmount,
        shadowsStrength: r?.shadowsStrength,
        explicitOrientation: r?.explicitOrientation,
        explicitOrientationLocal: r?.explicitOrientationLocal,
        hideStartCos: r?.hideStartCos,
        hideSpeed: r?.hideSpeed,
        softParticleDepthScale: r?.softParticleDepthScale,
        sunDirection: this.sunDirection,
        sunColorNorm: this.sunColorNorm,
      });
      this.applySunLighting(mat);
      const renderer = new SystemRenderer(sys, mat, rec.maxEmittingDuration, {
        spawnEffect,
        loopOneShot,
        loopResetPeriod,
        intensityDefaults,
        sourceGroup: group,
        rootGroup: this.root,
      });
      renderer.setSortCamera(this.sortCamera);
      if (intensityValues) renderer.setIntensityValues(intensityValues);
      renderer.setActive(active);
      systemParent.add(renderer.points);
      systems.push(renderer);
      for (const c of sys.components ?? []) {
        if (c.kind !== 'light' || !c.body) continue;
        const light = new LightRenderer(
          c.body,
          sys.intensities?.channels ?? [],
          intensityDefaults,
        );
        if (intensityValues) light.setIntensityValues(intensityValues);
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

  private applySunLighting(material: THREE.ShaderMaterial): void {
    const dir = material.uniforms.uSunDirWorld?.value;
    if (dir instanceof THREE.Vector3) dir.copy(this.sunDirection);
    const color = material.uniforms.uSunColorNorm?.value;
    if (color instanceof THREE.Color) color.copy(this.sunColorNorm);
  }

  private updateViewportHeightUniforms(): void {
    if (!this.renderer) return;
    this.renderer.getDrawingBufferSize(this.viewportSize);
    const height = Math.max(1, this.viewportSize.y);
    const apply = (system: SystemRenderer) => {
      const uniform = system.material.uniforms.uViewportHeight;
      if (uniform) uniform.value = height;
    };
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) apply(system);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) apply(system);
    }
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
      parent.intensityValues,
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
      pending = loadDdsSoftwareRgbaTexture(url, false, r)
        .catch(() => null)
        .then((tex) => tex ?? loadDdsMipChain([url], false, r))
        .catch((err) => {
          console.warn('[particles] DDS load failed', workspaceRelPath, err);
          return null;
        });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.map.value = tex;
    material.uniforms.useMap.value = 1;
    // WG-authoring-bug workaround: a single sprite mis-authored with a
    // framesPerX/Y grid + noAnimation samples a transparent cell-0 corner
    // (engine-faithful but invisible — see spriteSheetCell0Empty). When the
    // decoded content proves the grid is vestigial, sample the full texture.
    // GUARD: WG names genuine flipbook sheets `<name>_CxR` (e.g. Smoke_red_7x7);
    // a declared sheet keeps its cell-0 crop even if frame 0 is sparse, so never
    // override one. (Corpus: 0/140 noAnim+grid textures carry the suffix, and many
    // reuse one texture under conflicting grids — e.g. glow_w as 2x2..32x1 —
    // confirming the grid is vestigial noise. So in practice the content test
    // drives it; the suffix guard just future-proofs real _CxR sheets.)
    const fxy = material.uniforms.framesPerXY?.value as { x: number; y: number } | undefined;
    const baseName = workspaceRelPath.split('/').pop() ?? '';
    const declaredSheet = /_\d+x\d+(?:[._]|$)/i.test(baseName);
    if (!declaredSheet && fxy && fxy.x * fxy.y > 1 && spriteSheetCell0Empty(tex, fxy.x, fxy.y)) {
      material.uniforms.uVestigialGrid.value = 1;
    }
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
      pending = loadDdsSoftwareRgbaTexture(url, false, r)
        .catch(() => null)
        .then((tex) => tex ?? loadDdsMipChain([url], false, r))
        .catch((err) => {
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
    this.updateViewportHeightUniforms();
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

  restartAttachment(handle: ParticleAttachmentHandle): void {
    for (let i = this.spawnedEffects.length - 1; i >= 0; i--) {
      const effect = this.spawnedEffects[i];
      if (effect.parent !== handle) continue;
      this.disposeSpawnedEffect(effect);
      this.spawnedEffects.splice(i, 1);
    }
    handle.active = true;
    for (const s of handle.systems) s.restart();
    for (const l of handle.lights) l.restart();
    handle.group.visible = true;
  }

  setAttachmentIntensityValues(
    handle: ParticleAttachmentHandle,
    values: readonly number[] | undefined,
  ): void {
    handle.intensityValues = values ? Array.from(values) : undefined;
    for (const s of handle.systems) s.setIntensityValues(values);
    for (const l of handle.lights) l.setIntensityValues(values);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setIntensityValues(values);
      for (const l of effect.lights) l.setIntensityValues(values);
    }
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
