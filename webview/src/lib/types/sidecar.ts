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
   * BC4 single-channel mask carrying the Path B deny-list source
   * (originally the WG normal map's B channel, repacked as a BC4
   * sibling `_nbmask.dd?`). Drives the 4-threshold paint factor in
   * `ship_camo_mgn_material.fx`.
   */
  camoMask?: SidecarSlot;
  /**
   * BC4 single-channel mask carrying the Path A binary paint mask
   * (originally the WG metallic-gloss map's B channel, repacked as a
   * BC4 sibling `_camomask.dd?`). The engine reads this as the
   * per-pixel exclusion gate in `ship_camo_material.fx` — see
   * `reference/topics/camo/wg_camo_shader_reference.md` §"Path A".
   * Absent on assets extracted with a pre-2026-05-16 toolkit; the
   * consumer falls back to the `camoMask`-derived nbPaint factor in
   * that case (visually similar; engine-different).
   */
  camoExclusionMask?: SidecarSlot;
}

export interface SidecarMaterial {
  material_id?: string;
  shader_intent?: string;
  texture_sets?: Record<string, SidecarTextureScheme>;
  /**
   * Per-material detail-normal blend weights (extracted from the MFM's
   * `g_detail*` scalars). Present only when at least one of
   * normal/albedo/gloss influence is non-zero — absence means "detail
   * disabled" for this material. When present, ALL six keys are
   * populated (the sidecar emits them as a unit; the consumer does not
   * need `??` fallbacks). Pairs with the shared
   * `ship_atlas_detail.dds` atlas bound under the `detail` slot of
   * `texture_sets["main"]`.
   *
   * Engine recipe (PBS_ship_metallic.win.dx11 / Path A `ship_camo_material.fx`):
   * sample detail at `vMapUv × (scale_u, scale_v)`, decode RG as signed
   * tangent XY, then add to the base tangent normal weighted by
   * `normal_influence × distance_fade(fade_distance)`. Albedo and gloss
   * variants apply the detail's other channels with their own influences.
   */
  detail_params?: {
    normal_influence: number;
    albedo_influence: number;
    gloss_influence: number;
    fade_distance: number;
    scale_u: number;
    scale_v: number;
  };
}

/**
 * Raw GameParams dispersion scalars surfaced for downstream combat consumers.
 * Field names intentionally mirror GameParams until gameplay code normalizes
 * the exact radius / ellipse model.
 */
export interface SidecarMountDispersion {
  maxDist?: number;
  sigmaCount?: number;
  taperDist?: number;
  normalDistribution?: boolean;
  idealRadius?: number;
  idealDistance?: number;
  minRadius?: number;
  delim?: number;
  radiusOnZero?: number;
  radiusOnDelim?: number;
  radiusOnMax?: number;
  ellipseRangeMin?: number;
  ellipseRangeMax?: number;
  minEllipseRanging?: number;
  medEllipseRanging?: number;
  maxEllipseRanging?: number;
  aiMGminEllipseRanging?: number;
  aiMGmedEllipseRanging?: number;
  aiMGmaxEllipseRanging?: number;
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
  dispersion?: SidecarMountDispersion;
  misc_filter?: string[];
  /**
   * Yaw traverse limits `[min, max]` in degrees, in the mount's rest-relative
   * frame (0° = bind/rest heading — the rig's `Rotate_Y` bind). The arc the
   * mount can physically rotate through. Absent on AA (omnidirectional) and
   * fixed mounts. Sourced from GameParams `horizSector`.
   */
  yaw_range_deg?: [number, number];
  /**
   * Elevation limits `[depression, elevation]` in degrees. `[0]` ≤ 0 (below
   * horizontal), `[1]` > 0 (above). Positive = up. From GameParams `vertSector`.
   */
  elev_range_deg?: [number, number];
  /**
   * No-fire wedges `[[start, end], …]` in degrees, same frame as
   * `yaw_range_deg`. The mount can rotate INTO these (they sit inside the
   * traverse arc) but will not FIRE — it's pointing at the ship's own
   * superstructure. From GameParams `deadZone`. Absent when empty.
   */
  yaw_dead_zones_deg?: [number, number][];
  /**
   * Diagnostic flag stamped by `apply_variant_asset_swaps` when the host
   * placement matrix was Ry(180°)-corrected to absorb a bone mismatch.
   * No longer acted on by the renderer — schema_v6 attached_accessories
   * bakes the convention-B basis conjugation into the child matrices.
   */
  attached_y_flip?: boolean;
}

// ─── Armor (§6) ──────────────────────────────────────────────────────────
// Per-vertex `_MATERIAL_ID` on the hull GLB's `Armor` group joins to
// `materials_table[id]` for thickness. `zones` is the human-readable
// per-zone summary. See reference/schemas/METADATA_SPEC.md §6.

export interface SidecarArmorZone {
  default_thickness_mm: number;
  max_thickness_mm: number;
  plate_count: number;
}

export interface SidecarArmorMaterial {
  /** Effective total thickness (sum of `layers`). */
  thickness_mm: number;
  /** Ordered outer→inner layer thicknesses, e.g. [19, 19] = 2×19mm spaced. */
  layers: number[];
  /** Armor zones that use this material (a material may span several). */
  zones: string[];
  /** Toolkit `zone_hidden` flag — internal decks / bulkheads. */
  hidden?: boolean;
}

export interface SidecarArmor {
  source_glb?: string;
  class_canonical?: boolean;
  plate_count?: number;
  triangles?: number;
  zones?: Record<string, SidecarArmorZone>;
  /** Keyed by integer-as-string `material_id` (matches GLB `_MATERIAL_ID`). */
  materials_table?: Record<string, SidecarArmorMaterial>;
  /** Per-mount armor `{HP_AGM_1: {material_id_str: thickness_mm}}` (v3+). */
  mount_armor?: Record<string, Record<string, number>>;
  /** Per-turret barbette material-id lists (v3+). */
  barbettes?: Record<string, string[]>;
  hidden_zones?: string[];
}

// ─── Hitbox (§7) ─────────────────────────────────────────────────────────
// `Hitboxes` group on the hull GLB carries `CM_SB_*` cube meshes; each box
// name keys into `boxes`. `hit_locations` carries the per-section damage
// pool + regen numbers. See METADATA_SPEC.md §7.

export interface SidecarHitboxRegion {
  box_count: number;
  /** Present when the on-disk token differed from the canonical zone. */
  raw_name?: string;
  raw_names?: string[];
}

export interface SidecarHitboxBox {
  /** Damage section the cube belongs to (e.g. "Cit", "engine", "artillery"). */
  section: string;
  /** Verbatim GameParams hlType (e.g. "citadel_hitlocation"). */
  hl_type: string;
  /** Damage-cascade target (e.g. "Hull"). */
  parent_hl?: string;
  /** Owning hardpoint for turret-barbette boxes (`CM_SB_gk_*`). */
  owner_hp?: string;
}

export interface SidecarHitLocation {
  hl_type: string;
  parent_hl?: string;
  max_hp?: number;
  regen_part?: number;
  enhanced_regen_part?: number;
  auto_repair_s?: number;
  broken_repair_s?: number;
}

export interface SidecarHitbox {
  source_glb?: string;
  region_count?: number;
  regions?: Record<string, SidecarHitboxRegion>;
  /** Keyed by raw `CM_SB_*` name (matches the GLB node names). */
  boxes?: Record<string, SidecarHitboxBox>;
  hit_locations?: Record<string, SidecarHitLocation>;
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
export type ParticleSource = 'hull' | 'artillery' | 'atba' | 'airDefense' | 'aa_aura' | 'munition';

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
  armor?: SidecarArmor;
  hitbox?: SidecarHitbox;
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
