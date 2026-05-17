// Subset of `<Ship>.meta.json` the webview consumes. The full schema
// lives in `docs/contracts/sidecar-schema.md` (TODO); only the fields
// actively read by ship-page features live here.

import type { BallisticsSection } from './ballistics';
import type { Skin } from './skin';

export interface SidecarSlot {
  dds_mips?: string[];
}

export interface SidecarTextureScheme {
  baseColor?: SidecarSlot;
  metallicRoughness?: SidecarSlot;
  normal?: SidecarSlot;
  occlusion?: SidecarSlot;
  emissive?: SidecarSlot;
  /**
   * BC4 single-channel mask carrying the categorical no-camo region
   * marker that used to live in the normal map's B channel.
   */
  camoMask?: SidecarSlot;
}

export interface SidecarMaterial {
  material_id?: string;
  shader_intent?: string;
  texture_sets?: Record<string, SidecarTextureScheme>;
}

/**
 * Per-mount subset. The real sidecar carries more (display_name,
 * attach_to, transform, parent_section, …) — we only need what joins
 * to `ballistics.shells` and what the attached-accessories composer
 * needs.
 *
 * `misc_filter` is the per-HP WHITELIST the WG runtime uses to select
 * which of the asset's bundled miscs render on this hardpoint (verified
 * 2026-05-08 from `MiscsController._getMiscsForLoading`). Three-state
 * semantics: null = render all; `[]` = drop all non-isStyle; non-empty
 * = whitelist.
 */
export interface SidecarMount {
  instance_id?: string;
  hp_name?: string;
  asset_id?: string;
  ammo_ids?: string[];
  misc_filter?: string[];
  /**
   * Diagnostic flag stamped by `apply_variant_asset_swaps` when the host
   * placement matrix was Ry(180°)-corrected to absorb a bone mismatch.
   * No longer acted on by the renderer — schema_v6 attached_accessories
   * bakes the convention-B basis conjugation into the child matrices.
   */
  attached_y_flip?: boolean;
}

export interface SidecarShip {
  /**
   * Asset_ids that were rewritten by the variant peculiarityModels /
   * nodesConfig swap. Consumers gate the variant mat-overlay fold
   * per-asset against this list so bespoke variant albedos win over
   * the flat `mat_camo/<variant>.dds` tile.
   */
  variant_swapped_asset_ids?: string[];
  /**
   * Asset_ids whose library material set carries the engine's `_9`
   * suffix marker (e.g. `TL2_SHIPMAT_PBS_Misc9`,
   * `TL2_SHIPMAT_PBS_Gun9_skinned`). At runtime WG's
   * material-name → part_index lookup table (exe `0x140071a20`) lacks
   * any `*9` entry, so meshes carrying those materials never enter
   * camo dispatch. Mirror that here: opt these asset_ids out of camo /
   * mat_camo binding just like the swap-target set. Themed / skin-
   * exclusive decorative geometry — Hoshino bow whale, Azur Lane
   * secondaries, Ayane gun barrels, etc.
   */
  camo_skip_asset_ids?: string[];
}

// ─── Particle effects ──────────────────────────────────────────────────────
// Surface a subset of the parsed Effect record. Full schema lives in
// `reference/topics/particle/particle_format_spec.md`.
//
// The renderer needs: emission rate (Ramp), particle lifetime, initial
// position cloud (PS_VGT volume), tint Color curve, alpha Ramp,
// size/scale Ramps, and force Vec3 generators. Everything else we read
// for inspection only.

export interface ParticleRampPoint {
  value: number;
  time: number;
}
export interface ParticleColorPoint {
  r: number;
  g: number;
  b: number;
  a: number;
  time: number;
}
export interface ParticleRamp {
  count: number;
  points?: ParticleRampPoint[];
}
export interface ParticleColor {
  count: number;
  points?: ParticleColorPoint[];
}

/**
 * Scalar ``ValueGenerator`` — one of:
 *   - ``{type: "none"}``
 *   - ``{type: "constant", value: <f32>}``
 *   - ``{type: "linear",   from: <f32>, to: <f32>}``
 *   - ``{type: "ramp",     ramp: <Ramp>, parameterType, samplingType}``
 *
 * Used for: rate, force XYZ, size, alpha, scaler, jitter, orbitor angular
 * velocity, etc.
 */
export interface ParticleValueGenerator {
  type: 'none' | 'constant' | 'linear' | 'ramp';
  value?: number;
  from?: number;
  to?: number;
  ramp?: ParticleRamp;
  parameterType?: string | number;
  samplingType?: string | number;
}

/**
 * Variant ``ValueGenerator`` (PS_VGT) — array of typed volume prototypes
 * driving creator.initialPositionGenerator + creator.initialVelocityGenerator.
 */
export interface ParticleVgtPrototype {
  vgt_type: 'empty' | 'box' | 'point' | 'cylinder' | 'sphere' | 'line';
  body?: {
    // box / line: corner + opposite (line uses 'difference')
    corner?: [number, number, number];
    opposite?: [number, number, number];
    difference?: [number, number, number];
    // point
    position?: [number, number, number];
    // sphere
    center?: [number, number, number];
    minRadius?: number;
    maxRadius?: number;
    // cylinder
    origin?: [number, number, number];
    basisU?: [number, number, number];
    basisV?: [number, number, number];
    scale?: number[];
  };
}
export interface ParticleVariantVg {
  outer_type?: number;
  count: number;
  prototypes: ParticleVgtPrototype[];
}

export interface ParticleComponentBody {
  // creator + jitter fields (variant PS_VGT volume generators)
  rateRamp?: ParticleRamp;
  initialPositionGenerator?: ParticleVariantVg;
  initialVelocityGenerator?: ParticleVariantVg;
  velocityGenerator?: ParticleValueGenerator | ParticleVariantVg;
  positionGenerator?: ParticleVariantVg;
  systemAgeLimitMin?: number;
  systemAgeLimitMax?: number;
  velocityInheritanceFactor?: number;
  minRandomRateBound?: number;
  repeated?: boolean;
  useSmoothRate?: boolean;
  useWorldCoordinates?: boolean;
  // tint
  tint?: ParticleColor;
  period?: number;
  repeat?: boolean;
  useVelocity?: boolean;
  // alphaSetter
  ramp?: ParticleRamp;
  // scaler
  sizeGenerator?: ParticleValueGenerator;
  scaleXGenerator?: ParticleValueGenerator;
  // force
  forceXGenerator?: ParticleValueGenerator;
  forceYGenerator?: ParticleValueGenerator;
  forceZGenerator?: ParticleValueGenerator;
  // generic
  delay?: number;
  // stream
  vector?: [number, number, number];
  halfLife?: number;
  switchCoordinateStyle?: boolean;
  // resource refs (sphere/cylinder/box/spawner/plane/velocityField/etc.)
  effectName?: string;
  fieldSourceName?: string;
  // misc fields are surfaced as needed
  [key: string]: unknown;
}

export interface ParticleComponent {
  kind: 'empty' | 'PCAT' | 'light' | 'decal' | 'PSAT' | string;
  action?: string;
  body?: ParticleComponentBody;
}

export interface ParticleEmitter {
  rateGenerator?: ParticleValueGenerator;
  initialPositionGenerator?: ParticleVariantVg;
  initialVelocityGenerator?: ParticleVariantVg;
  sizeGenerator?: ParticleValueGenerator;
  ageScaleGenerator?: ParticleValueGenerator;
  ageScaleAuxGenerator?: ParticleValueGenerator;
  delayGenerator?: ParticleValueGenerator;
  sleepPeriodGenerator?: ParticleValueGenerator;
  activePeriod?: number;
  inheritVelocityFactor?: number;
  particleDistributionStrength?: number;
  snapToSeaLevel?: boolean;
}
export interface ParticleGeneralSection {
  capacity: number;
  maxInstancesCount: number;
  maxParticleAge: number;
  cameraAttachOffset: number;
  coordinateStyle: number;
  reflectionVisible: boolean;
  prewarm: boolean;
}

/**
 * Renderer block surfaced from the Effect blob. Only the byte-mapped
 * trio is populated today: `textureName0` / `textureName1` (VFS paths,
 * pool-form ResourceRefs) and `yawRateRamp`. Tail fields (`blendType`,
 * `tilingU`/`V`, `billboard`, lighting params) live at unmapped offsets
 * and aren't surfaced yet — see particle_render_roadmap P3.
 *
 * `textureUrl0` / `textureUrl1` are stamped by the pipeline-side
 * texture-extract pass (`compose/effects_textures.py`) and carry a
 * workspace-relative path that the webview hands to `repoUrl()` to load
 * the DDS via the standard texture machinery.
 */
export interface ParticleRenderer {
  textureName0?: string;
  textureName1?: string;
  textureUrl0?: string;
  textureUrl1?: string;
  yawRateRamp?: ParticleRamp;
}

/**
 * Animation block. Only `frameRateRamp` and `motionVectorsTexture` are
 * byte-mapped today; the sprite-atlas grid (`framesPerX`/`framesPerY`,
 * `framesRangeBegin`/`framesRangeEnd`, `animationPeriod`) sits in the
 * tail at unmapped offsets — see particle_render_roadmap P2.
 */
export interface ParticleAnimation {
  frameRateRamp?: ParticleRamp;
  motionVectorsTexture?: string;
  motionVectorsTextureUrl?: string;
}

export interface ParticleSystem {
  renderer?: ParticleRenderer;
  animation?: ParticleAnimation;
  emitter?: ParticleEmitter;
  general?: ParticleGeneralSection;
  components: ParticleComponent[];
}

export interface ParticleRecord {
  name?: string;
  record_index: number;
  maxEmittingDuration: number;
  systemsCount: number;
  systems: ParticleSystem[];
}

/**
 * Source taxonomy for particle attachments. Drives the per-section
 * grouping on the Particles tab.
 *
 *  - `hull`        : hull-anchored EP_* effects (fire / flood / death /
 *                    smoke / wake / horn / acid / rage / propeller / …).
 *  - `artillery`   : main-battery muzzle blast + damage / purge / reload.
 *  - `atba`        : secondary-battery muzzle blast + damage / purge.
 *  - `airDefense`  : AA-mount muzzle blast + damage (NOT flak burst).
 *  - `aa_aura`     : per-aura flak (`barrageEffect` / `detonationEffect`
 *                    / `missDetonationEffect`) shared across all AA
 *                    mounts in the band.
 *  - `munition`    : per-Projectile shell-impact / projectile-destroyed
 *                    / tracer XML refs (one ammo prototype per source_id).
 *
 * Older sidecars (pre-source taxonomy) omit `source`; the consumer
 * defaults to `hull` for back-compat.
 */
export type ParticleSource =
  | 'hull'
  | 'artillery'
  | 'atba'
  | 'airDefense'
  | 'aa_aura'
  | 'munition';

export interface ParticleAttachment {
  /**
   * Effect slot name inside the source.
   *  - hull: EffectsGroupName slot (`fire1`, `flood`, `death`,
   *          `waketracefront`, …)
   *  - gun-type sources: `shotEffect` / `brokenEffect` / `damagedEffect`
   *          / `purgingEffect` / `reloadBoostEffect` / `lensEffect`.
   *  - aa_aura: `barrageEffect` / `detonationEffect` / `missDetonationEffect`.
   *  - munition: per-slot key (`projDestroyedEffectHorizontal`, `water`,
   *          `ground`, `blowUpEffect`, …).
   */
  group: string;
  /**
   * WG-side bone / node name.
   *  - hull: typically `EP_*`.
   *  - gun-type sources: the mount's hardpoint (`HP_AGM_1`, `HP_AGA_10`, …).
   *  - aa_aura / munition: empty — the effect spawns at altitude or hit
   *    location, with no fixed bone on the ship.
   */
  node: string;
  /** VFS path of the particle XML (key into `particles`). */
  particle_path: string;
  /** Source category — added in the 2026-05-16 multi-scope absorb. */
  source?: ParticleSource;
  /**
   * Identifier of the owning entity within the source category.
   *  - hull: omitted (the `group` field is the identifier).
   *  - gun-type sources: hardpoint name (`HP_AGM_1`).
   *  - aa_aura: dotted address (`A_AirDefense.AuraMedium`).
   *  - munition: ammo ID (`PAPA014_Shell_406mm_AP_AP_Mk_8`).
   */
  source_id?: string;
}

export interface SidecarEffects {
  source?: { generated_at?: string };
  attachments: ParticleAttachment[];
  particles: Record<string, ParticleRecord>;
}

export interface SidecarDoc {
  ship?: SidecarShip;
  materials?: SidecarMaterial[];
  turrets?: SidecarMount[];
  secondaries?: SidecarMount[];
  antiair?: SidecarMount[];
  torpedoes?: SidecarMount[];
  accessories?: SidecarMount[];
  ballistics?: BallisticsSection;
  skins?: Skin[];
  effects?: SidecarEffects;
}

/** Per-material scheme inventory surfaced on the Camos tab. */
export interface MaterialSchemeEntry {
  material_id: string;
  /** Includes "main" and every camo_NN / dead key seen. */
  schemes: string[];
}
