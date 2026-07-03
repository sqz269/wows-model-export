// Per-system CPU simulation + instanced billboard-quad renderer.
// Extracted verbatim from the former monolithic particles.ts.

import * as THREE from 'three';
import type {
  ParticleColor,
  ParticleRamp,
  ParticleSystem,
  ParticleSystemIntensityConfig,
  ParticleSystemIntensityChannel,
  ParticleValueGenerator,
  ParticleVariantVg,
} from '$lib/types/sidecar';
import {
  ABSOLUTE_MAX_CAPACITY,
  BLEND_BUCKET_RENDER_ORDER,
  CHILD_EFFECT_SPAWNS_PER_SYSTEM_TICK,
  DEFAULT_PARTICLE_LIFETIME,
  DEFAULT_SIZE,
  HARD_MAX_EMIT_RATE_HZ,
  NATIVE_SUBSTEP_MAX_S,
  NATIVE_TO_METRES,
  PS_IC_AGE_AUX_SCALE,
  PS_IC_AGE_SCALE,
  PS_IC_EMITTER_RATE,
  PS_IC_PARTICLE_COLOR_A,
  PS_IC_PARTICLE_COLOR_B,
  PS_IC_PARTICLE_COLOR_G,
  PS_IC_PARTICLE_COLOR_R,
  PS_IC_PARTICLE_SCALE_X,
  PS_IC_PARTICLE_SCALE_Y,
  PS_IC_PARTICLE_SIZE,
  PS_IC_PARTICLE_STREAMER_X,
  PS_IC_PARTICLE_STREAMER_Y,
  PS_IC_PARTICLE_STREAMER_Z,
  PS_IC_PARTICLE_TILING_U,
  PS_IC_PARTICLE_TILING_V,
  PS_IC_PARTICLE_TINT_A,
  PS_IC_PARTICLE_TINT_B,
  PS_IC_PARTICLE_TINT_G,
  PS_IC_PARTICLE_TINT_R,
  PS_IC_PARTICLE_VEL_X,
  PS_IC_PARTICLE_VEL_Y,
  PS_IC_PARTICLE_VEL_Z,
  PS_RBT_DEPTH_SORT_MODES,
  SEA_LEVEL_Y,
} from './constants';
import {
  clamp01,
  coordinateStyleUsesDetachedFrame,
  finiteNumber,
  rampHasNonZeroValue,
  sampleColor,
  sampleGenAxis,
  samplePosFromVariantVg,
  sampleRamp,
  sampleScalarVg,
} from './sampling';
import type { ParticleClocks } from './sampling';
import {
  BARRIER_REACTION_ALPHA,
  BARRIER_REACTION_BOUNCE,
  BARRIER_REACTION_DAMP,
  BARRIER_REACTION_FORCE,
  BARRIER_REACTION_REMOVE,
  BARRIER_REACTION_SCALE,
  BARRIER_REACTION_SPAWN,
  BARRIER_REACTION_WRAP,
} from './actions';
import type {
  BarrierAction,
  JitterAction,
  MagnetAction,
  OrbitorAction,
  SpawnerAction,
  StreamAction,
  VelocityFieldAction,
} from './actions';
import { fetchVelocityField } from './velocity-field';
import type { VelocityFieldData } from './velocity-field';

export interface ParticleEffectSpawnRequest {
  effectName: string;
  position: [number, number, number];
}

export type ParticleEffectSpawnCallback = (request: ParticleEffectSpawnRequest) => void;

/** One entry of the scene-global main-bucket draw list — the webview twin of
 *  a native 0x50-stride DrawRec in pass-context list 0. `key` mirrors the
 *  rec+0x00 sort key: camera-view depth in NATIVE units, positive = farther
 *  (fx_ParticleSystem_cookDrawRecords), −1000 sentinel for camera-attached
 *  coordinateStyle 1 systems (DAT_1425575c0 — sorts nearest, drawn last). */
export interface BucketDrawRecord {
  sys: SystemRenderer;
  slot: number;
  key: number;
}

/** Native camera-attached sort key (DAT_1425575c0 = −1000.0f). */
const CAMERA_ATTACHED_SORT_KEY = -1000;
/** Native minimum folded alpha for a particle to be cooked into the draw
 *  list at all (DAT_142556328 ≈ 0.15/255; cookDrawRecords culls below it). */
const MIN_DRAW_ALPHA = 0.000588;

/** Packed draw-attribute target — either the system's own mesh arrays or one
 *  pooled run mesh's arrays (composed main bucket). */
interface RunDrawArrays {
  pos: Float32Array;
  vel: Float32Array;
  col: Float32Array;
  size: Float32Array;
  glow: Float32Array;
  scaleX: Float32Array;
  age: Float32Array;
  frameSeed: Float32Array;
  framePhase: Float32Array;
  rotationPhase: Float32Array;
}

interface RunMeshEntry {
  mesh: THREE.Mesh;
  geom: THREE.InstancedBufferGeometry;
  arrays: RunDrawArrays;
  attrs: THREE.InstancedBufferAttribute[];
}

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

// ---------------------------------------------------------------------------
// Per-system simulation
// ---------------------------------------------------------------------------

/**
 * One Three.Points emitter wrapping a single System inside a particle's
 * Effect record. Updates a ring buffer of particles each frame.
 */
export class SystemRenderer {
  readonly points: THREE.Mesh;
  /** Per-system author name (System+0x198), `''` for pre-`name` records. */
  readonly name: string;
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
  // resizer (PCAT typeId 4): a fixed-RATE approach of the per-particle SIZE BASE
  // toward an absolute target, clamped. RE-confirmed at native apply
  // fx::ActionResizer::apply @0x140742190 (2026-07-01): sizeTo = the target size,
  // sizeFrom = the approach rate (size-units/sec), integrated over the substep dt.
  // NOT a sizeFrom→sizeTo life tween, NOT a scaler multiplier — it writes rec[0x20]
  // (the SIZE BASE, = psize[]) directly and INDEPENDENTLY of the scaler product, so
  // they compose at draw. Multiple resizers apply in sequence, each pulling the base
  // toward its own target. (perComponentStatic ≈ 1.0; folded into psize at spawn.)
  private resizerActions: { sizeFrom: number; sizeTo: number }[] = [];
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
  /** ±sign(eo.y) when the native flat-card camera-azimuth spawn bake applies
   *  (mode-2 + explicitOrientationLocal + !billboard + eo=(0,±y,0), not
   *  velocityOriented); 0 = off. See the constructor note
   *  (fx_Particle_emitUpdate @0x14071b231). */
  private spawnCameraYawSign = 0;
  private readonly depthSortParticles: boolean;
  /** Main-bucket member (BLEND_BUCKET_RENDER_ORDER tier 0): the draw is owned
   *  by the scene-global compositor (bucket-compositor.ts), which mirrors the
   *  native shared list-0 — ONE back-to-front sort across every system of
   *  every effect, split into per-technique runs (fx_ParticleSystem_
   *  sortDrawLists + fx_Sprite_coalesceBatches, RE 2026-07-03). The system's
   *  own `points` mesh stays empty (instanceCount 0); pooled run meshes carry
   *  the instances instead. */
  readonly composedMainBucket: boolean;
  /** blend ∈ 0x2e8 sorted set AND sortType < 2 — native REPLACES this
   *  system's real depth keys with a monotone min→max ramp over its append
   *  order (cookDrawRecords key-relinearise), i.e. the system keeps emission
   *  order internally while still interleaving with other systems across its
   *  depth span. sortType ≥ 2 systems keep REAL per-particle keys (true depth
   *  sort). Doc 63 M5 read this gate INVERTED; corrected from the decompile
   *  2026-07-03. */
  private readonly emissionPinnedOrder: boolean;
  private readonly sortTypeNum: number;
  /** Monotone per-slot spawn sequence — the append order proxy for the
   *  native pool order (slot indices are free-list reused, so slot order is
   *  NOT emission order). sortType 1 iterates newest-first. */
  private spawnSerial: Float64Array;
  private spawnSerialCounter = 0;
  /** Pooled per-run meshes for the composed main bucket (one per contiguous
   *  same-system run of the globally sorted list — the three.js analog of a
   *  native coalesced batch). Grown lazily, hidden past runCursor. */
  private runMeshes: RunMeshEntry[] = [];
  private runCursor = 0;
  private readonly selfDrawArrays: RunDrawArrays;
  private sortCamera: THREE.Camera | null = null;
  private distanceConfigs: ParticleSystemIntensityConfig[] = [];
  /** DEV ONLY: raw authored system, kept for debugConfig() property-parity. */
  private dbgSystem: ParticleSystem | null = null;
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
  // Velocity-axial orientation axis, unit, seeded ONCE at spawn — native
  // fx_Particle_buildSpawnRecord (@0x14071a990) seeds the per-particle
  // orientVec (sim+0x70) from the SPAWN velocity and never tracks the live
  // velocity, so cards keep their spawn axis while forces curve the path
  // (splash crowns under gravity). Zero = degenerate spawn speed -> the VS
  // falls back to the authored eo axis. Only filled for the velocity-axial
  // population (billboard + velocityOriented).
  private spawnAxis: Float32Array;
  private readonly velocityAxial: boolean;
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
  /** Integrated sprite yaw, in radians. Byte-proven native model
   *  (FUN_14071b7f0, RE 2026-06-21):
   *    angle = (∫yawRateRamp·dt)·spinRateBase            // product: base SCALES the ramp
   *          + (spinSeed−0.5)·spinRateRange·age          // standalone random drift
   *          + initialOrientation                        // fixed spawn offset
   *  spinRateBase is a ramp COEFFICIENT (corpus default 1.0 = identity);
   *  spinRateRange is a SEPARATE per-particle constant rate, NOT a ramp
   *  scale. A flat ramp + spinRateRange 0 ⇒ no spin (e.g. BA_Logo). */
  private rotationPhase: Float32Array;
  private rotationPhaseAttr: THREE.BufferAttribute;
  /** Per-particle random in [0,1) sampled at spawn; drives the standalone
   *  spinRateRange drift term `(spinSeed−0.5)·spinRateRange·age` (native
   *  record+0x10 ← RNG; centred by −0.5 / DAT_1425565dc). */
  private spinSeed: Float32Array;
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
  /** alphaSetter is a SPATIAL fade, not a time envelope: native
   *  fx_Action_alphaSpatialRamp (@0x1407423c0, RE 2026-07-01) samples the ramp
   *  at the particle's camera-view-space DEPTH (viewMatrix col-2 · particlePos)
   *  and multiplies the working alpha (rec+0x40). The axis is NATIVE (BW)
   *  units, like the distance-LOD ramp axis (one scale-free native frame —
   *  ÷NATIVE_TO_METRES from our metre world, same as updateDistanceState).
   *  The authored keys are near/far camera fades — e.g. GK_Shot dust
   *  0.75→3→27→29 BW (≈11→45→405→435 m) matches its maxDistance 29 — NOT
   *  fade-in seconds (the old system-age heuristic zeroed the whole
   *  muzzle-flash window). Row vector mapping sim-space pos → +forward BW view
   *  depth, rebuilt each advance(); null when no sort camera is set (ramp then
   *  inert, factor 1). */
  private alphaDepthRow: Float32Array | null = null;
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
  private static readonly TMP_BOUNDS_VEC = new THREE.Vector3();
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

  /** World-space AABB enclosing this system's live particles (their sprite
   *  footprints), written into `target`. Returns false (leaving `target`
   *  untouched) when nothing is currently drawn. Drives the inspector's
   *  per-system bounding-box overlay. Reads the packed draw buffers (already
   *  in the ×15 metre frame), transforms them by the points mesh's world
   *  matrix, then inflates by the largest sprite radius so the box covers the
   *  visible footprint, not just particle centres. */
  computeWorldBounds(target: THREE.Box3): boolean {
    const n = this.instGeom.instanceCount;
    if (n <= 0) return false;
    this.points.updateWorldMatrix(true, false);
    const m = this.points.matrixWorld;
    const v = SystemRenderer.TMP_BOUNDS_VEC;
    target.makeEmpty();
    let maxRadius = 0;
    for (let i = 0; i < n; i++) {
      const i3 = i * 3;
      v.set(this.drawPos[i3], this.drawPos[i3 + 1], this.drawPos[i3 + 2]).applyMatrix4(m);
      target.expandByPoint(v);
      const r = this.drawSizeArr[i] * 0.5;
      if (r > maxRadius) maxRadius = r;
    }
    if (maxRadius > 0) target.expandByScalar(maxRadius);
    return true;
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
    this.name = system.name ?? '';
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
        // resizer = fixed-RATE approach of the SIZE BASE toward an absolute target,
        // clamped (RE-confirmed 2026-07-01, fx::ActionResizer::apply @0x140742190):
        //   step = dt × sizeFrom;  base → clamp(base ± step, target = sizeTo)
        // sizeTo is the TARGET size, sizeFrom the RATE — NOT a sizeFrom→sizeTo tween
        // and NOT the scaler product (that earlier "would inflate 1000×" concern was
        // the wrong model). Applied per substep into psize[] (the size base), so 35→1000
        // etc. converge to and clamp at the target rather than blowing up.
        if (typeof body.sizeFrom === 'number' && typeof body.sizeTo === 'number') {
          this.resizerActions.push({ sizeFrom: body.sizeFrom, sizeTo: body.sizeTo });
        }
      } else if (c.action === 'dampfer') {
        // PCAT only: the native per-particle apply pass is gated to kind==0
        // (fx_ParticleSystem_tick); a PSAT dampfer damps the SYSTEM/emitter
        // velocity (fx_Action_dampfer_system) and never touches particles.
        // Last-writer-wins across PCAT dampfers is native-exact: the apply is
        // a pure store into the damp slot (fx_Action_dampfer_particle writes
        // record+0x30), so each dampfer overwrites the previous in component
        // order — do NOT compose them multiplicatively.
        if (c.kind !== 'PSAT' && body.velocityGenerator) {
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
          angularVelocityGenerator: body.angularVelocityGenerator as
            | ParticleValueGenerator
            | undefined,
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
        // Native tests pos·n − d with the UNNORMALIZED authored normal
        // (fx_Action_barrierPlane_particle; corpus authors (0,0.1,0)), so
        // normalizing n here must rescale d by 1/|n| to keep the same plane.
        let planeLen = planeNormal.length();
        if (planeLen <= 1e-5) {
          planeNormal.set(0, 1, 0);
          planeLen = 1;
        }
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
          planeConstant: (Array.isArray(plane) && plane.length >= 4 ? plane[3] : 0) / planeLen,
          // Frame law (fx_Action_barrierPlane_system @0x140743530, RE
          // 2026-07-02): useWorldSpace=false → the plane lives in the SIM
          // frame verbatim; useWorldSpace=true → the plane is authored in
          // WORLD space (native world y=0 = sea level) and native converts it
          // into the sim frame per frame via the pool inverse-node matrix.
          // sampleBarrierState implements the equivalent world-frame test.
          useWorldSpace: !!body.useWorldSpace,
          effectName: typeof body.effectName === 'string' ? body.effectName : '',
        });
      } else if (c.action === 'velocityField') {
        const top = body.topLeftFront;
        const bottom = body.bottomRightBack;
        const fieldSourceName =
          typeof body.fieldSourceName === 'string' ? body.fieldSourceName : '';
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
    this.sortTypeNum = finiteNumber(renderer?.sortType, 2);
    // Native key rules (fx_ParticleSystem_cookDrawRecords, decompile-corrected
    // 2026-07-03 — doc 63 M5 had this INVERTED): every record gets a REAL
    // per-particle view-depth key; blend ∈ 0x2e8 && sortType < 2 then
    // OVERWRITES them with a monotone min→max ramp over append order
    // (= emission order pinned). So sortType ≥ 2 sorted-set systems ARE
    // depth-sorted, sortType < 2 ones are NOT. Main-bucket systems get the
    // exact rule via the scene compositor; this per-system gate is the
    // self-packed (underwater-tier) approximation of the same law.
    this.depthSortParticles =
      PS_RBT_DEPTH_SORT_MODES.has(renderer?.blendType ?? '') && this.sortTypeNum >= 2;
    this.emissionPinnedOrder =
      PS_RBT_DEPTH_SORT_MODES.has(renderer?.blendType ?? '') && this.sortTypeNum < 2;
    this.composedMainBucket =
      (BLEND_BUCKET_RENDER_ORDER[renderer?.blendType ?? ''] ?? 0) === 0;
    this.frameRateRamp = anim?.frameRateRamp;
    this.yawRateRamp = rampHasNonZeroValue(renderer?.yawRateRamp)
      ? renderer?.yawRateRamp
      : undefined;
    this.spinRateBase = finiteNumber(renderer?.spinRateBase, 0);
    this.spinRateRange = finiteNumber(renderer?.spinRateRange, 0);
    this.initialOrientationBase = finiteNumber(renderer?.initialOrientationBase, 0);
    this.initialOrientationRange = finiteNumber(renderer?.initialOrientationRange, 0);
    // Camera-azimuth spawn bake (fx_Particle_emitUpdate @0x14071b231, flag
    // map corrected 2026-07-02 §9): a mode-2 system with
    // explicitOrientationLocal=true, billboard=FALSE and eo=(0,±y,0) bakes
    // atan2f(−camFwd.x, camFwd.z) (negated for eo.y<0) into each particle's
    // spawn angle — record+0x5c, ADDITIVE with initialOrientation
    // (fx_Particle_initSpinAngle @0x14071a710). Flat LOCAL water cards
    // (shell-splash foam/deform sheets, 661 systems) keep their texture-up +
    // rotationCenter anchor lever arm facing the viewer's azimuth at spawn.
    // billboard=true systems never get it (they are the AXIAL population,
    // handled per-draw in the VS via uUseAxialBillboard). velocityOriented is
    // NOT in the native gate, but initSpinAngle then OVERRIDES the angle with
    // the velocity direction projected into the card plane (unless the
    // projection is degenerate); the webview approximates that override with
    // the per-frame FS velocity angle, so velocityOriented systems skip the
    // bake here rather than double-count the facing.
    {
      const eo = renderer?.explicitOrientation;
      this.spawnCameraYawSign =
        this.coordinateStyle === 2 &&
        renderer?.explicitOrientationLocal === true &&
        renderer?.billboard !== true &&
        renderer?.velocityOriented !== true &&
        Array.isArray(eo) &&
        (eo[0] ?? 0) === 0 &&
        (eo[2] ?? 0) === 0 &&
        (eo[1] ?? 0) !== 0
          ? Math.sign(eo[1] as number)
          : 0;
    }
    const fx = anim?.framesPerX ?? 1;
    const fy = anim?.framesPerY ?? 1;
    this.framesRangeEnd = Math.max(0, anim?.framesRangeEnd ?? fx * fy);
    this.distanceConfigs = system.distance?.configs ?? [];

    // alphaSetter ramp keys are camera-depth NATIVE (BW) units (see
    // alphaDepthRow) — the old "long tail ⇒ system-age seconds" heuristic
    // misread 60/500-key fire/flood curves that are really ~0.9 km / 7.5 km
    // visibility fades.

    this.pos = new Float32Array(this.capacity * 3);
    this.vel = new Float32Array(this.capacity * 3);
    this.velGpu = new Float32Array(this.capacity * 3);
    this.spawnAxis = new Float32Array(this.capacity * 3);
    this.velocityAxial = renderer?.billboard === true && renderer?.velocityOriented === true;
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
    this.spawnSerial = new Float64Array(this.capacity);
    this.selfDrawArrays = {
      pos: this.drawPos,
      vel: this.velGpu,
      col: this.drawColorRGBA,
      size: this.drawSizeArr,
      glow: this.drawGlowStrength,
      scaleX: this.drawSpriteScaleX,
      age: this.ageGpu,
      frameSeed: this.drawFrameSeed,
      framePhase: this.drawFramePhase,
      rotationPhase: this.drawRotationPhase,
    };
    this.spinSeed = new Float32Array(this.capacity);
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
    // Native blend-bucket draw order (fx_ParticleSystem_cookDrawRecords
    // routes each system into one of FIVE per-pass draw lists by blendType;
    // fx_ParticlePass_render drains them: water stage emits deform then
    // water-surface BEFORE the main list, underwater after it, shimmer last —
    // fx_Water_renderSurfaceParticleLists / fx_ParticlePass_emitMainAndUnderwater,
    // RE 2026-07-03). three.js ties on object depth resolve by insertion
    // order, so without an explicit renderOrder a late-indexed water-surface
    // system (e.g. CustomDeath_AzurLane_Explosion_Submarine #20-22 aura
    // rings) lands ON TOP of the whole burst instead of under it. Within one
    // tier insertion order = system index = native emission order.
    this.points.renderOrder = BLEND_BUCKET_RENDER_ORDER[system.renderer?.blendType ?? ''] ?? 0;
    this.intensityChannels = system.intensities?.channels ?? [];
    this.intensityDefaults = Array.from(options.intensityDefaults ?? []);
    this.dbgSystem = system;
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
    if (!active) {
      // The scene compositor only packs ACTIVE systems — hide this system's
      // run meshes so a solo/hide toggle doesn't leave stale runs on screen.
      for (const r of this.runMeshes) {
        r.mesh.visible = false;
        r.geom.instanceCount = 0;
      }
    }
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

  /** DEV ONLY (native↔webview temporal-envelope parity; see
   *  tmp/pfx_re/route2/particle_envelope*). Snapshot every live particle's
   *  cooked per-slot sim state. Reads the SLOT-INDEXED sim arrays (consistent
   *  with age[i]) — NOT the sort-permuted draw* copies. `size` is pre-
   *  NATIVE_TO_METRES, matching the engine's cooked sim+0x04; `rotPhase` is the
   *  raw spin accumulator (radians, un-rewrapped, == engine sim+0x30);
   *  `framePhase` is the cumulative frame count (engine cell = floor(framePhase)
   *  % framesRangeEnd + begin). */
  debugSnapshot(): {
    age: number;
    size: number;
    scaleX: number;
    framePhase: number;
    rotPhase: number;
    alpha: number;
  }[] {
    const out: {
      age: number;
      size: number;
      scaleX: number;
      framePhase: number;
      rotPhase: number;
      alpha: number;
    }[] = [];
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      out.push({
        age: this.age[i],
        size: this.sizeArr[i],
        scaleX: this.spriteScaleXArr[i],
        framePhase: this.framePhase[i],
        rotPhase: this.rotationPhase[i],
        alpha: this.colorRGBA[i * 4 + 3],
      });
    }
    return out;
  }

  /** DEV ONLY: the system's CONSUMED property set, keyed by the producer's
   *  dotted property paths (matching tmp/pfx_re/route2/particle_props.py), with
   *  the webview's EFFECTIVE value. A property present here = the renderer
   *  consumes it; a producer property ABSENT here = a coverage gap (the renderer
   *  ignores it — e.g. `components.resizer`, intentionally not wired). Used by
   *  particle_full_compare.py to diff ALL properties producer↔webview and make
   *  non-consumption explicit instead of silently omitted. */
  debugConfig(): Record<string, unknown> {
    const s = this.dbgSystem;
    if (!s) return {};
    const r = s.renderer ?? {};
    const a = s.animation ?? {};
    const e = s.emitter ?? {};
    const r4 = (n: number) => Math.round(n * 1e4) / 1e4;
    const nv = (v: unknown): unknown => {
      if (typeof v === 'number') return r4(v);
      if (v && typeof v === 'object') {
        const o = v as Record<string, unknown>;
        if (Array.isArray((o as { points?: unknown }).points) && 'count' in o) {
          return {
            _ramp: ((o.points as Array<{ time?: number; value?: number }>) ?? []).map((p) => [
              r4(p.time ?? 0),
              r4(p.value ?? 0),
            ]),
          };
        }
        if (Array.isArray(v)) return v.map(nv);
        const out2: Record<string, unknown> = {};
        for (const k of Object.keys(o)) out2[k] = nv(o[k]);
        return out2;
      }
      return v;
    };
    const out: Record<string, unknown> = {};
    const put = (k: string, v: unknown) => {
      if (v !== undefined) out[k] = nv(v);
    };
    put('name', this.name || undefined); // System+0x198 per-system author name
    const g = s.general ?? {};
    put('general.maxParticleAge', this.maxAge);
    put('general.capacity', this.capacity);
    put('general.prewarm', (g as Record<string, unknown>).prewarm); // -> authoredPrewarm/runPrewarm
    put('general.coordinateStyle', (g as Record<string, unknown>).coordinateStyle); // stream/detached frame
    for (const k of [
      'blendType',
      'lightingType',
      'tilingU',
      'tilingV',
      'flipTexcoordU',
      'flipTexcoordV',
      'velocityOriented',
      'explicitOrientation',
      'explicitOrientationLocal',
      'billboard',
      'scaleX',
      'opacityMultiplier',
      'lightingAmbient',
      'lightingDiffuse',
      'lightingTransmission',
      'lightWrapAmount',
      'shadowsStrength',
      'hideStartCos',
      'hideSpeed',
      'softParticleDepthScale',
      'rotationCenter',
      'customCenterOffset',
      'sortType',
      'textureName0',
      'textureName1',
    ])
      put(`renderer.${k}`, (r as Record<string, unknown>)[k]);
    put('renderer.spinRateBase', this.spinRateBase);
    put('renderer.spinRateRange', this.spinRateRange);
    put('renderer.initialOrientationBase', this.initialOrientationBase);
    put('renderer.initialOrientationRange', this.initialOrientationRange);
    put('renderer.yawRateRamp', this.yawRateRamp ?? null);
    for (const k of [
      'animationType',
      'framesPerX',
      'framesPerY',
      'framesRangeBegin',
      'animationPeriod',
      'motionVectorsDistortion',
      'motionVectorsTexture',
      'randomFrameOnly',
      'useEmissionAlphaFromMV',
    ])
      put(`animation.${k}`, (a as Record<string, unknown>)[k]);
    put('animation.frameRateRamp', this.frameRateRamp ?? null);
    put('animation.framesRangeEnd', this.framesRangeEnd);
    for (const k of [
      'sizeGenerator',
      'ageScaleGenerator',
      'ageScaleAuxGenerator',
      'delayGenerator',
      'sleepPeriodGenerator',
      'rateGenerator',
      'activePeriod',
      'inheritVelocityFactor',
      'snapToSeaLevel',
      'initialPositionGenerator',
      'initialVelocityGenerator',
      'particleDistributionStrength',
    ])
      put(`emitter.${k}`, (e as Record<string, unknown>)[k]);
    put('distance.maxDistance', (s.distance as Record<string, unknown> | undefined)?.maxDistance);
    if (this.distanceConfigs.length) put('distance.configs', this.distanceConfigs);
    s.intensities?.channels?.forEach((ch, ci) => {
      for (const cfg of ch.configs ?? []) {
        for (const fn of cfg.flagNames ?? []) put(`intensities.ch${ci}.${fn}`, cfg.ramp);
      }
    });
    if (s.intensities) put('intensities.channelCount', s.intensities.channelCount);
    const HANDLED = new Set([
      'creator',
      'spawner',
      'tint',
      'alphaSetter',
      'scaler',
      'dampfer',
      'stream',
      'jitter',
      'orbitor',
      'magnet',
      'sphere',
      'cylinder',
      'box',
      'plane',
      'light',
    ]);
    for (const c of s.components ?? []) {
      if (c.action && HANDLED.has(c.action)) put(`components.${c.action}`, c.body);
    }
    return out;
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
    const aspect = Math.max(0.001, Math.abs((this.baseSpriteAspectX * scaleX) / scaleY));
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
    // Main-bucket systems are drawn by the scene-global compositor (native
    // shared list-0); their own mesh stays empty.
    if (!this.composedMainBucket) this.writeBuffers();
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
    // The distance-config ramp axis is authored in NATIVE (BW) units, like every
    // other length in the records (native apply FUN_1406c9c40 measures camera
    // distance in its own world frame, which has no scale nodes). Our world is
    // metres at NATIVE_TO_METRES per BW unit, so convert before sampling.
    // Corpus evidence: Fire_big fades emission over keys 17→60 / 60→500 — as raw
    // metres a deck fire would be emission-culled 60 m from the camera; as BW the
    // fade spans ~0.5-15 km of apparent range, a plausible LOD. Sampling raw
    // metres (pre-2026-07-01) hit the ramps ~15× too early on every effect.
    const distance =
      SystemRenderer.TMP_WORLD.distanceTo(SystemRenderer.TMP_POS2) / NATIVE_TO_METRES;
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
  /** Native systemActiveTime (PS_VALG_RAMP_PARAMETER=4). fx_Generator_eval
   *  case 4 reads emitterState[+0x18] − emitterState[+0x30] (system age minus
   *  the stamp written on the last sleep→active transition), so the clock
   *  NEVER wraps at activePeriod: sleepPeriod<=0 emitters (the one-shot
   *  majority) run a single active cycle and the axis is monotonic; only a
   *  real sleepPeriod>0 duty cycle resets it (once per active+sleep cycle).
   *  The previous `elapsed % activePeriod` wrap re-armed once-clamped
   *  systemActiveTime ramps every activePeriod — e.g. Moray FIRST_EXPL_TOP's
   *  dampfer (1.0→0.10 over 1s, activePeriod 0.8) snapped back to undamped
   *  every 0.8s and ~4.5×-overshot the plume height. (The emitter RATE ramp
   *  keeps its own live-verified wrap in advance() — not this axis.) */
  private systemActiveClock(): number {
    const phase = this.elapsed - this.emitterDelay;
    if (phase < 0) return this.elapsed; // pre-ignition: +0x30 not yet stamped
    const ap = this.emitterActivePeriod;
    const sp = this.emitterSleepPeriod;
    if (ap <= 0 || Number.isNaN(sp) || sp <= 0) return phase; // single cycle
    return phase % (ap + sp); // time since the current cycle's active start
  }

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

    // alphaSetter = camera-depth fade (fx_Action_alphaSpatialRamp): rebuild the
    // sim-space→view-depth row for this step. World pos (metres) =
    // points.matrixWorld × (simPos × NATIVE_TO_METRES); the ramp axis is
    // NATIVE (BW) units, so ÷NATIVE_TO_METRES — the two 15s cancel on the
    // linear part. Three's view looks down −Z, so negate for +forward depth.
    if (this.alphaRamp && this.sortCamera) {
      this.sortCamera.updateMatrixWorld(true);
      this.points.updateWorldMatrix(true, false);
      const e = SystemRenderer.TMP_VIEW_SORT.multiplyMatrices(
        this.sortCamera.matrixWorldInverse,
        this.points.matrixWorld,
      ).elements;
      const row = (this.alphaDepthRow ??= new Float32Array(4));
      row[0] = -e[2];
      row[1] = -e[6];
      row[2] = -e[10];
      row[3] = -e[14] / NATIVE_TO_METRES;
    } else {
      this.alphaDepthRow = null;
    }

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
      // Ramps integrate over the AGE-SCALED clock, not wall time: native
      // fx_ParticleSystem_tick feeds rec+0x00 (age += dt*ageScaleRate) as the
      // fx_Ramp_integrate limit for BOTH frameRateRamp (@0x14071c5d0) and
      // yawRateRamp (@0x14071c575). Raw dt ran the MV-warp + spin ~1/ageScale too
      // fast (jittery) for low-ageScale outliers (e.g. Smoke_big_D_Day_Custom,
      // ageScale=0.005 -> 64fps becomes ~0.32/s). Producer-proven 2026-06-22; raw-dt
      // was the ~98.5% ageScale~=1 approximation.
      const dAge = age - prevAge; // == dt * this.ageRate[i]
      if (this.frameRateRamp) {
        const fps0 = sampleRamp(this.frameRateRamp, prevAge, 0);
        const fps1 = sampleRamp(this.frameRateRamp, age, 0);
        this.framePhase[i] += Math.max(0, 0.5 * (fps0 + fps1) * dAge);
      }
      // Continuous sprite spin — byte-proven native model (FUN_14071b7f0,
      // RE 2026-06-21). TWO independent terms:
      //  1. yawRateRamp integral SCALED by spinRateBase (product). The 1.0
      //     corpus default (92%) is an identity scale, so a flat ramp ⇒ no
      //     ramp spin regardless of spinRateBase. (The old `spinRateBase=1.0
      //     → +1 rad/s standalone` reading spun ~every textured particle.)
      //  2. spinRateRange is a SEPARATE per-particle constant rate
      //     `(spinSeed−0.5)·spinRateRange` (NOT a ramp scale) — 35.7% of the
      //     corpus authors this; it spins even with a flat ramp.
      // initialOrientation (set at spawn) is the fixed start offset. BA_Logo
      // (flat ramp + spinRateRange 0) ⇒ both terms 0 ⇒ static.
      if (this.yawRateRamp && this.spinRateBase !== 0) {
        const yaw0 = sampleRamp(this.yawRateRamp, prevAge, 0);
        const yaw1 = sampleRamp(this.yawRateRamp, age, 0);
        this.rotationPhase[i] += 0.5 * (yaw0 + yaw1) * this.spinRateBase * dAge;
      }
      if (this.spinRateRange !== 0) {
        this.rotationPhase[i] += (this.spinSeed[i] - 0.5) * this.spinRateRange * dAge;
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
      clocks.systemActiveTime = this.systemActiveClock();
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
      const aRow = this.alphaDepthRow;
      const alphaSetterFactor = aRow
        ? sampleRamp(
            this.alphaRamp,
            aRow[0] * this.pos[i * 3 + 0] +
              aRow[1] * this.pos[i * 3 + 1] +
              aRow[2] * this.pos[i * 3 + 2] +
              aRow[3],
            1,
          )
        : 1;
      const baseAlpha = clamp01(
        this.intensityColorAlphaMultiplier * this.distanceColorAlphaMultiplier,
      );
      const alpha = clamp01(
        baseAlpha *
          alphaSetterFactor *
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
      // resizer: integrate the SIZE BASE toward each action's target at its rate,
      // clamped (native fx::ActionResizer::apply, per substep). Mutates psize[i] in
      // place BEFORE the scaler product below reads it, matching the native order
      // (resizer writes rec[0x20], the scaler multiplies rec[0x54/0x58]).
      for (let s = 0; s < this.resizerActions.length; s++) {
        const { sizeFrom, sizeTo } = this.resizerActions[s];
        const cur = this.psize[i];
        const step = dt * sizeFrom;
        this.psize[i] = cur < sizeTo ? Math.min(cur + step, sizeTo) : Math.max(cur - step, sizeTo);
      }
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
      // AGE_SCALE/AGE_AUX (intensity + distance-LOD) channels feed the AGE CLOCK,
      // NOT size — applied at spawn into ageRate/lifetime (see spawnParticle).
      // Byte-proven build 12506899: rec[0x20] size reads ONLY block[23]
      // (PARTICLE_SIZE); the age slots block[8]/block[14] go to rec[0x08]/rec[0x0c].
      sz *=
        this.intensitySizeMultiplier *
        this.distanceSizeMultiplier *
        this.intensityScaleYMultiplier *
        this.distanceScaleYMultiplier *
        this.barrierScaleMultiplier;
      this.sizeArr[i] = Math.max(0, sz);
      this.glowStrengthArr[i] = Number.isFinite(glowScale) ? glowScale : 1;
      this.spriteScaleXArr[i] = Number.isFinite(scalerScaleX)
        ? Math.max(0.001, Math.abs(scalerScaleX))
        : 1;
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
    const order = this.collectLiveSlotsInAppendOrder();
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
      this.writeDrawSlot(order[writeIdx], writeIdx, this.selfDrawArrays);
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
      matrixElements[2] * x + matrixElements[6] * y + matrixElements[10] * z + matrixElements[14]
    );
  }

  private writeDrawSlot(sourceSlot: number, drawSlot: number, t: RunDrawArrays): void {
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
    t.pos[dst3 + 0] = this.pos[src3 + 0] * NATIVE_TO_METRES;
    t.pos[dst3 + 1] = this.pos[src3 + 1] * NATIVE_TO_METRES;
    t.pos[dst3 + 2] = this.pos[src3 + 2] * NATIVE_TO_METRES;
    if (this.velocityAxial) {
      // Spawn-seeded unit axis (native sim+0x70) — the VS only normalizes it,
      // so no NATIVE_TO_METRES scale; a zero axis falls back to eo in the VS.
      t.vel[dst3 + 0] = this.spawnAxis[src3 + 0];
      t.vel[dst3 + 1] = this.spawnAxis[src3 + 1];
      t.vel[dst3 + 2] = this.spawnAxis[src3 + 2];
    } else {
      t.vel[dst3 + 0] = this.vel[src3 + 0] * NATIVE_TO_METRES;
      t.vel[dst3 + 1] = this.vel[src3 + 1] * NATIVE_TO_METRES;
      t.vel[dst3 + 2] = this.vel[src3 + 2] * NATIVE_TO_METRES;
    }
    t.col[dst4 + 0] = this.colorRGBA[src4 + 0];
    t.col[dst4 + 1] = this.colorRGBA[src4 + 1];
    t.col[dst4 + 2] = this.colorRGBA[src4 + 2];
    t.col[dst4 + 3] = this.colorRGBA[src4 + 3];
    t.size[drawSlot] = this.sizeArr[sourceSlot] * NATIVE_TO_METRES;
    t.glow[drawSlot] = this.glowStrengthArr[sourceSlot];
    t.scaleX[drawSlot] = this.spriteScaleXArr[sourceSlot];
    t.age[drawSlot] = this.age[sourceSlot];
    t.frameSeed[drawSlot] = this.frameSeed[sourceSlot];
    t.framePhase[drawSlot] = this.framePhase[sourceSlot];
    t.rotationPhase[drawSlot] = this.rotationPhase[sourceSlot];
  }

  /** Live slots in APPEND order — the native pool iteration order of
   *  fx_ParticleSystem_cookDrawRecords: spawn order (spawnSerial), reversed
   *  to newest-first when sortType == 1. Slots whose folded alpha is below
   *  the native cook cull (MIN_DRAW_ALPHA) are dropped from the draw list
   *  entirely, matching cookDrawRecords. */
  private collectLiveSlotsInAppendOrder(): number[] {
    const order: number[] = [];
    for (let i = 0; i < this.capacity; i++) {
      if (this.age[i] < 0) continue;
      if (this.colorRGBA[i * 4 + 3] < MIN_DRAW_ALPHA) continue;
      order.push(i);
    }
    const serial = this.spawnSerial;
    order.sort(
      this.sortTypeNum === 1 ? (a, b) => serial[b] - serial[a] : (a, b) => serial[a] - serial[b],
    );
    return order;
  }

  // -------------------------------------------------------------------------
  // Composed main bucket (scene-global sorted list — bucket-compositor.ts)
  // -------------------------------------------------------------------------

  /** Whether this system currently takes part in the scene compositor pass. */
  get isActive(): boolean {
    return this.active;
  }

  /** Append this system's live particles to the scene-global main-bucket
   *  draw list with native sort keys (fx_ParticleSystem_cookDrawRecords):
   *  key = camera-view depth in NATIVE units (positive = farther);
   *  coordinateStyle 1 (camera-attached) = the −1000 sentinel; blend ∈ 0x2e8
   *  && sortType < 2 = keys relinearised min→max over append order so the
   *  system keeps emission order internally while spanning its real depth
   *  range in the global interleave. */
  appendDrawRecords(out: BucketDrawRecord[], camera: THREE.Camera | null): void {
    const slots = this.collectLiveSlotsInAppendOrder();
    if (slots.length === 0) return;
    if (this.coordinateStyle === 1 || !camera) {
      for (const s of slots) out.push({ sys: this, slot: s, key: CAMERA_ATTACHED_SORT_KEY });
      return;
    }
    this.points.updateWorldMatrix(true, false);
    const m = SystemRenderer.TMP_VIEW_SORT.multiplyMatrices(
      camera.matrixWorldInverse,
      this.points.matrixWorld,
    ).elements;
    const base = out.length;
    let min = Infinity;
    let max = -Infinity;
    for (const s of slots) {
      const ix = s * 3;
      const x = this.pos[ix + 0] * NATIVE_TO_METRES;
      const y = this.pos[ix + 1] * NATIVE_TO_METRES;
      const z = this.pos[ix + 2] * NATIVE_TO_METRES;
      // three.js view space looks down −Z; negate for the native positive-far
      // key, ÷15 back to native units so the −1000 sentinel keeps its native
      // relationship.
      const key = -(m[2] * x + m[6] * y + m[10] * z + m[14]) / NATIVE_TO_METRES;
      if (key < min) min = key;
      if (key > max) max = key;
      out.push({ sys: this, slot: s, key });
    }
    if (this.emissionPinnedOrder && slots.length > 1) {
      const step = (max - min) / (slots.length - 1);
      for (let i = 0; i < slots.length; i++) out[base + i].key = min + step * i;
    }
  }

  beginRuns(): void {
    this.runCursor = 0;
  }

  /** Pack one contiguous same-system run of the globally sorted list into a
   *  pooled run mesh — the three.js analog of one native coalesced batch
   *  (fx_Sprite_coalesceBatches): its own instance buffers, the system's
   *  material, and a fractional renderOrder inside the main tier so three.js
   *  submits the runs in exactly the global back-to-front order. */
  packRun(
    records: readonly BucketDrawRecord[],
    start: number,
    count: number,
    runOrder: number,
  ): void {
    let entry = this.runMeshes[this.runCursor];
    if (!entry) {
      entry = this.createRunMesh();
      this.runMeshes.push(entry);
    }
    this.runCursor++;
    for (let i = 0; i < count; i++) {
      this.writeDrawSlot(records[start + i].slot, i, entry.arrays);
    }
    for (const a of entry.attrs) a.needsUpdate = true;
    entry.geom.instanceCount = count;
    entry.mesh.renderOrder = Math.min(0.99, runOrder * 1e-4);
    entry.mesh.visible = true;
    if (!entry.mesh.parent && this.points.parent) this.points.parent.add(entry.mesh);
  }

  endRuns(): void {
    for (let k = this.runCursor; k < this.runMeshes.length; k++) {
      this.runMeshes[k].mesh.visible = false;
      this.runMeshes[k].geom.instanceCount = 0;
    }
  }

  private createRunMesh(): RunMeshEntry {
    const geom = new THREE.InstancedBufferGeometry();
    // Own copy of the base quad + index (12 floats / 6 indices). Sharing the
    // BufferAttribute objects with the system's live geometry silently
    // produced empty draws (three.js VAO/attribute bookkeeping) — verified
    // empirically 2026-07-03: a from-scratch clone renders, a shared-attr
    // clone does not.
    geom.setAttribute(
      'position',
      new THREE.Float32BufferAttribute([0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0], 3),
    );
    geom.setIndex([0, 1, 2, 2, 1, 3]);
    const cap = this.capacity;
    const arrays: RunDrawArrays = {
      pos: new Float32Array(cap * 3),
      vel: new Float32Array(cap * 3),
      col: new Float32Array(cap * 4),
      size: new Float32Array(cap),
      glow: new Float32Array(cap),
      scaleX: new Float32Array(cap),
      age: new Float32Array(cap),
      frameSeed: new Float32Array(cap),
      framePhase: new Float32Array(cap),
      rotationPhase: new Float32Array(cap),
    };
    const mk = (arr: Float32Array, itemSize: number): THREE.InstancedBufferAttribute => {
      const attr = new THREE.InstancedBufferAttribute(arr, itemSize);
      attr.setUsage(THREE.DynamicDrawUsage);
      return attr;
    };
    const attrs = [
      mk(arrays.pos, 3),
      mk(arrays.vel, 3),
      mk(arrays.col, 4),
      mk(arrays.size, 1),
      mk(arrays.glow, 1),
      mk(arrays.scaleX, 1),
      mk(arrays.age, 1),
      mk(arrays.frameSeed, 1),
      mk(arrays.framePhase, 1),
      mk(arrays.rotationPhase, 1),
    ];
    geom.setAttribute('iPosition', attrs[0]);
    geom.setAttribute('velocity', attrs[1]);
    geom.setAttribute('color', attrs[2]);
    geom.setAttribute('size', attrs[3]);
    geom.setAttribute('glowStrength', attrs[4]);
    geom.setAttribute('spriteScaleX', attrs[5]);
    geom.setAttribute('age', attrs[6]);
    geom.setAttribute('frameSeed', attrs[7]);
    geom.setAttribute('framePhase', attrs[8]);
    geom.setAttribute('rotationPhase', attrs[9]);
    geom.instanceCount = 0;
    const mesh = new THREE.Mesh(geom, this.material);
    mesh.frustumCulled = false;
    mesh.visible = false;
    return { mesh, geom, arrays, attrs };
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
    // A hidden system must not keep manufacturing VISIBLE child effects — the
    // inspector's per-system toggle only flips points.visible while the sim
    // keeps ticking, so without this gate a hidden spawner floods the scene
    // with orphan children forever (child effects are not in the panel's
    // system list). Same gate as the barrier SPAWN reaction below.
    if (!this.points.visible) return;
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
    if (this.velocityAxial) {
      // Seed the axial axis from the FINAL spawn velocity (post folds, post
      // frame hop — same frame as this.vel). Never re-read live velocity:
      // native seeds sim+0x70 once at spawn (see spawnAxis).
      const svx = SystemRenderer.TMP_VEL.x;
      const svy = SystemRenderer.TMP_VEL.y;
      const svz = SystemRenderer.TMP_VEL.z;
      const sl = svx * svx + svy * svy + svz * svz;
      const inv = sl > 1e-10 ? 1 / Math.sqrt(sl) : 0;
      this.spawnAxis[slot * 3 + 0] = svx * inv;
      this.spawnAxis[slot * 3 + 1] = svy * inv;
      this.spawnAxis[slot * 3 + 2] = svz * inv;
    }
    this.age[slot] = 0;
    this.spawnSerial[slot] = ++this.spawnSerialCounter;
    this.lifetime[slot] = this.maxAge;
    sampleColor(this.tintColor, 0, SystemRenderer.TMP_COL);
    const aRow = this.alphaDepthRow;
    const alphaSetterFactor = aRow
      ? sampleRamp(
          this.alphaRamp,
          aRow[0] * this.pos[slot * 3 + 0] +
            aRow[1] * this.pos[slot * 3 + 1] +
            aRow[2] * this.pos[slot * 3 + 2] +
            aRow[3],
          1,
        )
      : 1;
    const baseAlpha = clamp01(
      this.intensityColorAlphaMultiplier * this.distanceColorAlphaMultiplier,
    );
    const alpha = clamp01(
      baseAlpha *
        alphaSetterFactor *
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
    // Start angle = (rand−0.5)·range + base (native FUN_14071a710 centres
    // the random by −0.5 / DAT_1425565dc). spinSeed feeds the standalone
    // spinRateRange drift term in tick().
    this.rotationPhase[slot] =
      (Math.random() - 0.5) * this.initialOrientationRange + this.initialOrientationBase;
    // Flat-card camera-azimuth spawn bake (fx_Particle_emitUpdate
    // @0x14071b231): add atan2(−fwd.x, fwd.z) of the camera forward at THIS
    // particle's spawn moment, sign per eo.y. Consumed by the VS fixed-card
    // axis rotation (not the FS UV spin). Absolute sign vs native is still
    // unverified (needs a live-game A/B or a corner-stream capture — handoff
    // §7); the term's PRESENCE is byte-proven.
    if (this.spawnCameraYawSign !== 0 && this.sortCamera) {
      this.sortCamera.getWorldDirection(SystemRenderer.TMP_POS2);
      this.rotationPhase[slot] +=
        this.spawnCameraYawSign *
        Math.atan2(-SystemRenderer.TMP_POS2.x, SystemRenderer.TMP_POS2.z);
    }
    this.spinSeed[slot] = Math.random();
    const sc = SystemRenderer.TMP_CLOCKS;
    sc.particleAge = 0;
    sc.systemAge = this.elapsed;
    sc.systemActiveTime = this.systemActiveClock();
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
    // PS_IC AGE_SCALE/AGE_AUX intensity (+ distance-LOD twin) channels feed the AGE
    // CLOCK, not size (byte-proven, build 12506899). Spawn FUN_14071a990:
    //   rec[0x08] age-rate   = ageScaleGen ÷ block[8]        (0x14071ab64 divss)
    //   rec[0x0c] 1/lifetime = 1 ÷ (auxGen × block[14])      (0x14071ab90 divss)
    //   rec[0x20] size       = sizeGen × block[23]           (0x14071ac37 mulss)
    // where block[i] = Π intensity-ramp × Π distance-ramp for PS_IC target i
    // (apply FUN_1406c9d01: accum[idx] *= factor; out[i] = accum[i] × distBlock[i]).
    // So AGE_SCALE DIVIDES the age rate (factor<1 ⇒ ages faster) and AGE_AUX
    // MULTIPLIES lifetime; neither scales size. Mirrors the emitter-generator
    // ageScale fix (2d21bb2) — both factors land on the same age-clock fields.
    const ageScaleBlock = this.intensityAgeScaleMultiplier * this.distanceAgeScaleMultiplier;
    const ageAuxBlock = this.intensityAgeAuxScaleMultiplier * this.distanceAgeAuxScaleMultiplier;
    const ageRate = ageScale / (ageScaleBlock > 0 ? ageScaleBlock : 1);
    this.ageRate[slot] = ageRate > 0 ? ageRate : 1; // guard div-by-zero / negative
    // base was this.maxAge (set above); aux>1 and/or AGE_AUX intensity>1 → lives longer
    this.lifetime[slot] *= ageAux * (ageAuxBlock > 0 ? ageAuxBlock : 1);
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
    // AGE_SCALE/AGE_AUX folded into the age clock above, not size (byte-proven).
    sz0 *=
      this.intensitySizeMultiplier *
      this.distanceSizeMultiplier *
      this.intensityScaleYMultiplier *
      this.distanceScaleYMultiplier;
    this.sizeArr[slot] = Math.max(0, sz0);
    this.glowStrengthArr[slot] = Number.isFinite(glowScale0) ? glowScale0 : 1;
    this.spriteScaleXArr[slot] = Number.isFinite(scalerScaleX0)
      ? Math.max(0.001, Math.abs(scalerScaleX0))
      : 1;
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
    // localToWorld/worldToLocal operate in THREE world units (METRES), but the
    // sim — and `this.pos` — are native BW units (writeDrawSlot scales the
    // OUTPUT ×NATIVE_TO_METRES). The source→target hop injects the source
    // group's world translation, a world-frame INPUT, so it must be divided
    // back into native units just like sea-level snap / parent velocity (see
    // writeDrawSlot). Without this, a detached hull effect (e.g. floodLight,
    // coordinateStyle=3) keeps the group's metre offset as if it were native
    // units, then gets ×15 again at draw → ~15× too far from the ship.
    pos.multiplyScalar(NATIVE_TO_METRES);
    source.localToWorld(pos);
    target.worldToLocal(pos);
    pos.multiplyScalar(1 / NATIVE_TO_METRES);
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
    // Same native↔metre unit bridge as convertSpawnToSimulationFrame: the
    // localToWorld/worldToLocal hop runs in metres, but `pos` is a native-unit
    // sim position and the caller re-scales the result ×NATIVE_TO_METRES.
    pos.multiplyScalar(NATIVE_TO_METRES);
    simulation.localToWorld(pos);
    source.worldToLocal(pos);
    pos.multiplyScalar(1 / NATIVE_TO_METRES);
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

  private applyOrbitorActions(slot: number, clocks: ParticleClocks, age: number, dt: number): void {
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
          // points.visible: a hidden system keeps simulating but must not keep
          // spawning visible children (see applySpawnerActions).
          if (allowChildSpawns && crossed && action.effectName && this.spawnEffect && this.points.visible) {
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
        let cur: THREE.Vector3 = current;
        let next: THREE.Vector3 = predicted;
        normalOut.copy(action.planeNormal);
        const sim = this.points.parent;
        if (action.useWorldSpace && sim) {
          // useWorldSpace (fx_Action_barrierPlane_system @0x140743530, RE
          // 2026-07-02): the authored plane is in WORLD space — native
          // converts it into the sim frame per frame via the pool
          // inverse-node matrix; we equivalently evaluate the particle in the
          // world frame. localToWorld runs in metres while the sim is native
          // BW units, so ×15 in / ÷15 out (same bridge as
          // convertSimulationPositionToSourceFrame); SEA_LEVEL_Y re-bases the
          // scene's sea level onto the native world y=0 that the 78
          // waterline-splash barriers author against. normalOut goes back to
          // the sim frame so bounce/force reactions stay sim-space (unused in
          // corpus — all 92 plane barriers are reaction-3 spawns).
          sim.updateWorldMatrix(true, false);
          cur = SystemRenderer.TMP_REL.copy(current).multiplyScalar(NATIVE_TO_METRES);
          sim.localToWorld(cur);
          cur.y -= SEA_LEVEL_Y;
          cur.multiplyScalar(1 / NATIVE_TO_METRES);
          next = SystemRenderer.TMP_REL2.copy(predicted).multiplyScalar(NATIVE_TO_METRES);
          sim.localToWorld(next);
          next.y -= SEA_LEVEL_Y;
          next.multiplyScalar(1 / NATIVE_TO_METRES);
          sim.getWorldQuaternion(SystemRenderer.TMP_QUAT).invert();
          normalOut.applyQuaternion(SystemRenderer.TMP_QUAT);
        }
        const sideNow = action.planeNormal.dot(cur) - action.planeConstant;
        const sideNext = action.planeNormal.dot(next) - action.planeConstant;
        this.barrierInsideNow = sideNow < 0;
        this.barrierInsideNext = sideNext < 0;
        this.barrierDistanceRatio = sideNow < 0 ? 0 : 1;
        return;
      }
    }
  }

  private reflectVelocity(ix: number, normal: THREE.Vector3): void {
    if (normal.lengthSq() <= 1e-10) return;
    const dot =
      this.vel[ix + 0] * normal.x + this.vel[ix + 1] * normal.y + this.vel[ix + 2] * normal.z;
    this.vel[ix + 0] -= 2 * dot * normal.x;
    this.vel[ix + 1] -= 2 * dot * normal.y;
    this.vel[ix + 2] -= 2 * dot * normal.z;
  }

  private cancelVelocityAlongNormal(ix: number, normal: THREE.Vector3): void {
    if (normal.lengthSq() <= 1e-10) return;
    const dot =
      this.vel[ix + 0] * normal.x + this.vel[ix + 1] * normal.y + this.vel[ix + 2] * normal.z;
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
      const side =
        action.planeNormal.dot(
          SystemRenderer.TMP_POS.set(this.pos[ix + 0], this.pos[ix + 1], this.pos[ix + 2]),
        ) - action.planeConstant;
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
    for (const r of this.runMeshes) {
      r.mesh.parent?.remove(r.mesh);
      r.geom.dispose();
    }
    this.runMeshes = [];
    this.material.dispose();
    // Texture lifetime is managed by the ParticleScene's texture cache
    // (shared across systems that point at the same DDS) — don't dispose
    // it here.
  }
}
