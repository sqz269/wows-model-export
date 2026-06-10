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

/**
 * One entry from the ``particles/textures/particles.atlas`` manifest.
 * Resolves an authoring-side ``.tga`` filename (the basename of
 * ``textureName0/1`` / ``motionVectorsTexture``) to a named region
 * inside one of the 6 shipped atlas DDS pages.
 *
 * `page` is a workspace-relative URL the webview hands to ``repoUrl()``;
 * `rect` is a normalised UV rect ``[u0, v0, u1, v1]`` within the page.
 */
export interface ParticleAtlasRect {
  page: string;
  rect: [number, number, number, number];
}

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
  reaction?: number;
  strength?: number;
  stopAge?: number;
  position?: [number, number, number];
  radius?: number;
  corner?: [number, number, number];
  opposite?: [number, number, number];
  planeEquation?: [number, number, number, number];
  useWorldSpace?: boolean;
  topLeftFront?: [number, number, number];
  bottomRightBack?: [number, number, number];
  velocityScale?: number;
  influence?: number;
  // stream
  vector?: [number, number, number];
  halfLife?: number;
  switchCoordinateStyle?: boolean;
  // component kind=light
  colorAnimationPeriod?: number;
  colorAnimation?: ParticleColor;
  radiusAnimationPeriod?: number;
  radiusAnimation?: ParticleRamp;
  color?: [number, number, number, number];
  localPosition?: [number, number, number];
  minQuality?: number;
  animatedColor?: boolean;
  animatedRadius?: boolean;
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

export interface ParticleSystemDistance {
  maxDistance: number;
  configsCount: number;
  configs: ParticleSystemIntensityConfig[];
}

/**
 * Renderer block surfaced from the Effect blob. Field offsets confirmed
 * against the WoWS binary (build 12506899, FUN_1406f0c30), superseding
 * the 2026-05-23 statistical probe: texture refs
 * (`textureName0`/`textureName1`) + `yawRateRamp` at +0x00/+0x10/+0x20,
 * `explicitOrientation` at +0x30, `customCenterOffset` at +0x3c,
 * spin/orientation/lighting/material scalars through +0x7c, the tail
 * enums/floats (`rotationCenter`/`lightingType`/`blendType`/`sortType`/
 * `tilingU`/`tilingV`) at +0x80..+0x94, and bool flags at +0x98..+0x9d
 * within the 0xa0-byte struct.
 *
 * `textureUrl0` / `textureUrl1` are stamped by the library builder
 * (`compose/library_particles.py`) and carry a workspace-relative
 * path that the webview hands to `repoUrl()` to load the DDS via the
 * standard texture machinery.
 */
export interface ParticleRenderer {
  textureName0?: string;
  textureName1?: string;
  textureUrl0?: string;
  textureUrl1?: string;
  /** Atlas-mapped fallback for textureName0 when it's a ``.tga`` ref
   *  whose name appears in the atlas manifest. Mutually exclusive with
   *  ``textureUrl0`` — direct extract takes precedence. */
  textureAtlas0?: ParticleAtlasRect;
  textureAtlas1?: ParticleAtlasRect;
  yawRateRamp?: ParticleRamp;
  /** Renderer +0x30 Vec3. Native explicit-orientation vector. */
  explicitOrientation?: [number, number, number];
  /** Renderer +0x3c Vec2. Used when `rotationCenter` is `custom`. */
  customCenterOffset?: [number, number];
  /** Renderer +0x44/+0x48 random spin-rate range/base, radians/sec in webview. */
  spinRateRange?: number;
  spinRateBase?: number;
  /** Renderer +0x4c lighting scalar. WG field spelling is `Shineness`. */
  lightingShineness?: number;
  /** Renderer +0x50/+0x58 random initial sprite orientation range/base. */
  initialOrientationRange?: number;
  initialOrientationBase?: number;
  /** Renderer lighting scalars at +0x54, +0x64..+0x70. */
  lightingAmbient?: number;
  lightingDiffuse?: number;
  lightingTransmission?: number;
  lightWrapAmount?: number;
  shadowsStrength?: number;
  /** Renderer +0x5c/+0x78/+0x7c view/depth fade scalars. */
  hideStartCos?: number;
  hideSpeed?: number;
  softParticleDepthScale?: number;
  /** Renderer +0x60 width multiplier for sprite aspect. */
  scaleX?: number;
  /** Renderer +0x74 alpha multiplier. */
  opacityMultiplier?: number;
  /** PS_RRC label (4 values: bottom / corner / center / custom).
   *  Recovered from the binary enum table @ 0x1420bf0d0. */
  rotationCenter?: string;
  /** PS_RLT label (3 values: lambert / lightmapping4Way / lightmappingHL2),
   *  Renderer +0x84. Recovered from the binary enum table @ 0x1420bf490.
   *  This is the slot the old probe mislabeled as "blendFlag84". */
  lightingType?: string;
  /** PS_RBT label (10 values) — drives the per-system blending mode.
   *  Maps to THREE.* blending: ADDITIVE -> AdditiveBlending, BLENDED ->
   *  NormalBlending, others need custom shader paths (see particle
   *  render roadmap). */
  blendType?: string;
  /** Raw i32 sort-type (enum fx::RendererSortType; labels not yet
   *  recovered). */
  sortType?: number;
  /** Per-system UV tiling factors; default 1.0/1.0. */
  tilingU?: number;
  tilingV?: number;
  /** Renderer +0x98..+0x9b orientation/background flags. */
  explicitOrientationLocal?: boolean;
  billboard?: boolean;
  velocityOriented?: boolean;
  background?: boolean;
  /** Renderer +0x9c/+0x9d UV flip flags. */
  flipTexcoordU?: boolean;
  flipTexcoordV?: boolean;
}

/**
 * Animation block — sprite-atlas grid + motion vectors. Field offsets
 * confirmed against the WoWS binary (build 12267945, FUN_1406f37a0),
 * superseding the 2026-05-23 probe (which had the +0x3c/+0x3d bools
 * swapped).
 *
 * For systems with `framesPerX > 1 || framesPerY > 1`, the renderer
 * samples the texture at cell `(currentFrame % framesPerX, currentFrame
 * / framesPerX)`. Native playback advances `currentFrame` by integrating
 * `frameRateRamp` over particle age and then applying
 * `framesRangeBegin/End`; `animationPeriod` is carried for inspection only.
 * `animationType` (PS_PAT) selects the animation mode (noAnimation /
 * framesPlayback / motionVectors) — NOT a loop/once/pingPong wrap mode
 * (that is the separate ramp-sampling enum).
 */
export interface ParticleAnimation {
  frameRateRamp?: ParticleRamp;
  motionVectorsTexture?: string;
  motionVectorsTextureUrl?: string;
  /** Atlas-mapped fallback for motionVectorsTexture when it's a ``.tga``
   *  ref whose name appears in the atlas manifest. Mutually exclusive
   *  with ``motionVectorsTextureUrl``. */
  motionVectorsTextureAtlas?: ParticleAtlasRect;
  /** Sprite-sheet column count. 1 means "no atlas". */
  framesPerX?: number;
  /** Sprite-sheet row count. 1 means "no atlas". */
  framesPerY?: number;
  /** First active frame index; usually 0. */
  framesRangeBegin?: number;
  /** Last active frame index (exclusive); usually == framesPerX*framesPerY. */
  framesRangeEnd?: number;
  /** Total animation cycle length in seconds. */
  animationPeriod?: number;
  /** MV distortion factor (small, typically 0..0.017). */
  motionVectorsDistortion?: number;
  /** PS_PAT label (3 values: noAnimation / framesPlayback / motionVectors).
   *  Recovered from the binary enum table @ 0x1420bf430. */
  animationType?: string;
  /** Read emission alpha from the motion-vector texture's alpha. */
  useEmissionAlphaFromMV?: boolean;
  /** Pick one random frame per particle instead of animating. */
  randomFrameOnly?: boolean;
}

export interface ParticleSystem {
  renderer?: ParticleRenderer;
  animation?: ParticleAnimation;
  emitter?: ParticleEmitter;
  general?: ParticleGeneralSection;
  distance?: ParticleSystemDistance;
  intensities?: ParticleSystemIntensities;
  components: ParticleComponent[];
}

export interface ParticleSystemIntensityConfig {
  ramp?: ParticleRamp;
  flagsCount?: number;
  flags?: number[];
  flagNames?: string[];
}

export interface ParticleSystemIntensityChannel {
  configsCount: number;
  configs: ParticleSystemIntensityConfig[];
}

export interface ParticleSystemIntensities {
  channelCount: number;
  channels: ParticleSystemIntensityChannel[];
}

export interface ParticleIntensityChannel {
  index: number;
  name: string;
  nameLength?: number;
  minIntensity?: number;
  maxIntensity?: number;
  defaultIntensity?: number;
  channelKind?: number;
}

export interface ParticleRecord {
  name?: string;
  record_index: number;
  maxEmittingDuration: number;
  intensityChannelCount?: number;
  intensityChannels?: ParticleIntensityChannel[];
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
 *  - `map`         : map-authored `space.bin.particles[]` emitter anchor.
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
  | 'munition'
  | 'map';

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
   *    Consumers that render muzzle flashes expand `shotEffect` rows to
   *    the mounted asset's `HP_gunFire<N>` child markers.
   *  - aa_aura / munition: empty — the effect spawns at altitude or hit
   *    location, with no fixed bone on the ship.
   */
  node: string;
  /** VFS path of the particle XML (key into `particles`). */
  particle_path: string;
  /** Source category — added in the 2026-05-16 multi-scope absorb. */
  source?: ParticleSource;
  /**
   * Hull effect-point world position `[x, y, z]` in hull-GLB metric space
   * (same frame as the hull mesh vertices — drop a marker here directly).
   * Present only for `source: "hull"` `EP_*` nodes, resolved by the
   * pipeline from the model's `<segment>_ep.skel_ext` records (keyed by
   * `Murmur3(EP_node_name)`). Absent on gun / AA / munition rows (those
   * resolve against the accessory rig's own nodes in the live scene),
   * and on `EP_*` rows the pipeline couldn't resolve (no `_ep` data).
   */
  position?: [number, number, number];
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
  // particles field removed in schema v4 — Effect records now live in
  // the shared `library/particles/records.json` artefact. Consumers
  // join by `attachment.particle_path`.
}

// ─── Exteriors (ship-exterior unification Step 0) ────────────────────────
// One record per WG Exterior (mesh-swap permoflage) on the BASE ship's
// sidecar — sibling to `skins[]`, DISTINCT from hull-module `variants`.
// `camo_scheme_key` cross-links into this ship's `skins[].scheme_key` so one
// selector flips geometry + paint together. Additive at schema_version 3;
// absent on pre-Step-0 sidecars. See the producer's resolve/exterior_unify.py.

/** Per-HP resolved mount swap. `transform` is the schema_v6 Ry(180°)-baked
 *  VARIANT placement matrix and `misc_filter` the nodesConfig override —
 *  neither is reconstructable from the base placement; consume VERBATIM. */
export interface ExteriorMountSwap {
  hp_name: string;
  /** The base ship's asset at this HP (the mount this swap replaces). */
  base_asset_id?: string | null;
  asset_id?: string | null;
  dead_asset_id?: string | null;
  transform?: { matrix: number[] } | null;
  /** 3-state whitelist override: null/absent = all, [] = none, [list]. */
  misc_filter?: string[] | null;
  /** Diagnostic only — consumers do NOT re-apply Ry(180°). */
  attached_y_flip?: boolean;
}

export interface ExteriorSwapTable {
  by_asset_id?: Record<string, string>;
  by_hp_name?: Record<string, string>;
  dead_by_hp_name?: Record<string, string>;
  misc_filter_by_hp?: Record<string, string[]>;
}

export interface ExteriorRecord {
  /** WG Exterior param-name (`PAES488_Azur_Baltimore`) — the index key.
   *  Index 0 is always the synthesised `default` (vanilla composition). */
  exterior_id: string;
  display_name?: string | null;
  /** Lowercased variant hull model_dir (`asc080_baltimore_1944_azur`) when
   *  the Exterior carries a genuine hull swap; null when the hull is shared.
   *  The variant hull GLB is NOT extracted into the unified folder yet
   *  (HullDelta is a later producer step) — treat non-null as "hull differs
   *  in game" provenance, not as a loadable asset. */
  wg_asset_id?: string | null;
  /** GameParams typeinfo.species (`Skin` / `MSkin` / `default`). */
  species?: string | null;
  /** Grouping key for pickers (`azurlane` / `arpeggio` / `default` / …). */
  peculiarity?: string | null;
  /** Exactly one record per ship is native; consumers auto-select it on
   *  load (WG renders nativePermoflage by default — ARP-style ships never
   *  show their bare hull in game). */
  is_native?: boolean;
  /** Cross-link into `skins[].scheme_key`; null when the matching skin was
   *  never ingested (keep the current skin and surface a console warning). */
  camo_scheme_key?: string | null;
  /** HullDelta — always null until the producer cutover step lands. */
  hull?: unknown | null;
  swap_table?: ExteriorSwapTable;
  mounts?: ExteriorMountSwap[];
  /** Camo opt-out set for THIS exterior (swap targets + bespoke attached
   *  children); replaces ship.variant_swapped_asset_ids while active. */
  variant_swapped_asset_ids?: string[];
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
  /** Ship-exterior unification: indexable mesh-swap permoflage selector.
   *  Absent on pre-Step-0 sidecars (treat as `[default]`). */
  exteriors?: ExteriorRecord[];
}

/** Per-material scheme inventory surfaced on the Camos tab. */
export interface MaterialSchemeEntry {
  material_id: string;
  /** Includes "main" and every camo_NN / dead key seen. */
  schemes: string[];
}
