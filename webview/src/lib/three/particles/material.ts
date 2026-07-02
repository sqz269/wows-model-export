// Per-system ShaderMaterial builder (blend modes + GLSL).
// Extracted verbatim from the former monolithic particles.ts.

import * as THREE from 'three';
import type { ParticleRamp } from '$lib/types/sidecar';
import {
  DEFAULT_PARTICLE_SUN_COLOR_NORM,
  DEFAULT_PARTICLE_SUN_DIR,
  NATIVE_TO_METRES,
} from './constants';
import { finiteNumber, particleByteStepCount, vectorHasLength } from './sampling';

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
  /** DEFORM_WATER_SURFACE per-texel strength encoding: true when the base
   *  texture is a `particles/deform16f/` float field (signed height around
   *  R=0.5); false ⇒ RGB foam mask (DXT1 alpha-less). Drives uDeformSigned. */
  deformSigned?: boolean;
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
  /** Renderer.billboard — with a nonzero eo selects the AXIAL billboard
   *  (card whose long axis = eo, yawing about it to face the camera); see
   *  uUseAxialBillboard. */
  billboard?: boolean;
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
export const PS_RBT_LUT_MODES = new Set(['GRADIENT_MAP', 'UNDERWATER_GRADIENT_MAP']);

/** PS_RLT labels whose textureName0 is a directional lightmap (RGB = 3
 *  baked light-direction renders, A = opacity), NOT albedo. The fragment
 *  shader reconstructs a single lit luminance against the sun direction
 *  instead of sampling RGB as colour. See memory
 *  `project-particle-lm-lightmap`. */
const PS_RLT_LIGHTMAP_MODES = new Set(['lightmapping4Way', 'lightmappingHL2']);

function rotationPivotForCenter(
  _label: string | undefined,
  _customOffset: [number, number] | undefined,
): THREE.Vector2 {
  // The SPIN pivot is always the quad center (RE 2026-06-23, live build hook:
  // fx_Sprite_buildQuad's sincos rotates the U/V basis with no pivot input).
  // rotationCenter/customCenterOffset do NOT feed the spin — but they are NOT
  // decode-only either: see anchorShiftForRotationCenter below (2026-07-02).
  return new THREE.Vector2(0.5, 0.5);
}

// Quad ANCHOR offset in half-extent units (CORRECTED 2026-07-02 — supersedes
// the "rotationCenter is decode-only" reading, which only proved the SPIN
// pivot centered; the 39k-sprite live population was rotationCenter=center).
// fx_DrawCfg_cook cooks rotationCenter into a pivot pair (writer +0x68/+0x6c
// = buildQuad's cfg+0x70/+0x74, once mislabeled "scaleX/scaleY"), and
// fx_Sprite_buildQuad offsets the INSTANCE POSITION by it:
//   pos = wpos + right·halfW·kx + up·halfH·ky
// PS_RRC: 0=bottom→(0,1) (bottom edge at the particle — e.g. the
// Laser_Charge_Shot_H2020 beam extends one way from the muzzle),
// 1=corner→(1,1), 2=center→(0,0) (customCenterOffset IGNORED for center),
// 3=custom→customCenterOffset verbatim.
function anchorShiftForRotationCenter(
  label: string | undefined,
  customOffset: [number, number] | undefined,
): THREE.Vector2 {
  switch (label) {
    case 'bottom':
      return new THREE.Vector2(0, 1);
    case 'corner':
      return new THREE.Vector2(1, 1);
    case 'custom':
      return new THREE.Vector2(customOffset?.[0] ?? 0, customOffset?.[1] ?? 0);
    default:
      return new THREE.Vector2(0, 0); // 'center' + unknown
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
export function buildParticleMaterial(opts: ParticleMaterialOptions = {}): THREE.ShaderMaterial {
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
  const anchorShift = anchorShiftForRotationCenter(opts.rotationCenter, opts.customCenterOffset);
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
  // Axial (cylindrical) billboard: billboard=true + ANY nonzero eo — eo is
  // the card's LONG/UP AXIS, not its normal (world frame, or the attachment's
  // local frame per explicitOrientationLocal). The card contains the axis and
  // yaws about it to face the camera. Shipped for eo=(0,±y,0) (ThisIsFine
  // memes — readable from the SIDE in game, where a flat +Y card would be
  // edge-on), generalized 2026-07-02 to arbitrary axes:
  // Laser_Charge_Shot_H2020 RAY authors eo=(0,0,1) LOCAL + billboard +
  // LaserRay.dds on a gun muzzle — the beam must run ALONG the barrel, which
  // the ⊥N fixed card cannot do (it rendered the ray orthogonal to its own
  // sparks). BYTE-PROVEN 2026-07-02 (closes handoff §9): fx_DrawCfg_cook maps
  // billboard(+0x99) → orientation mode 1, and fx_Sprite_buildQuad's mode-1
  // branch (ex-"velocity-stretched") builds exactly this basis — texture-up =
  // the spawn-baked orientVec (node-matrix-rotated when eoLocal; velocity dir
  // when velocityOriented), texture-right = normalize(cross(orientVec,
  // camEye − particlePos)) — per draw with the live eye, so the per-frame
  // tracking below is native-exact. Takes precedence over the fixed-card path.
  const useAxialBillboard =
    opts.billboard === true && !opts.velocityOriented && hasExplicitOrientation ? 1 : 0;
  const fixedOrientationVec =
    hasExplicitOrientation && !opts.velocityOriented && !useAxialBillboard
      ? opts.explicitOrientation
      : undefined;
  const useFixedOrientation = fixedOrientationVec ? 1 : 0;
  const orientationVec =
    explicitOrientation ??
    fixedOrientationVec ??
    (useAxialBillboard ? opts.explicitOrientation : undefined);
  const hideSpeed =
    opts.hideSpeed !== undefined && Number.isFinite(opts.hideSpeed) && opts.hideSpeed > 0
      ? opts.hideSpeed
      : 1;
  // Hide-angle fade law, byte-exact (fx_Sprite_hideAngleFade + fx_DrawCfg_cook,
  // RE 2026-07-02): fade = clamp01((|dot(viewDir, orientVec)| − base) × hideSpeed),
  // INVERTED iff orientation mode 1 (billboard/axial) — NOT iff lightingType==
  // lightmapping4Way (that gate was a +8-shift aliasing artifact: the cooked
  // mode byte sits at the parse-layout lightingType offset). The base is cooked
  // per mode: billboard → hideStartCos raw; flat → (1 − hideStartCos) − 1/hideSpeed.
  // Dominant corpus authoring (start=0, speed=1): flat cards fade ∝ |cos| to the
  // normal (soft edge-on fade), axial cards fade ∝ 1−|cos| to the axis (fade
  // looking down the axis). At the defaults (start=1) both forms are neutral.
  const hideIsAxialMode = opts.billboard === true ? 1 : 0;
  const hideStartCos = opts.hideStartCos ?? 1;
  const hideFadeBase = hideIsAxialMode ? hideStartCos : 1 - hideStartCos - 1 / hideSpeed;
  // Soft-particle fade slope: authored against NATIVE (BW) depth deltas
  // (fade = saturate(Δdepth_bw × k)); the shader computes Δdepth in eye METRES,
  // so divide by NATIVE_TO_METRES. Raw-authored (pre-2026-07-01) made the fade
  // band 15× too narrow — soft particles hardened much closer to surfaces than
  // native.
  const softParticleDepthScale =
    opts.softParticleDepthScale !== undefined &&
    Number.isFinite(opts.softParticleDepthScale) &&
    opts.softParticleDepthScale > 0
      ? opts.softParticleDepthScale / NATIVE_TO_METRES
      : 0;
  const distortionMode =
    opts.blendType === 'DEFORM_WATER_SURFACE' ? 1 : opts.blendType === 'SHIMMER' ? 2 : 0;
  // mode 1 = DEFORM_WATER_SURFACE (steady warp), 2 = SHIMMER (heat haze, a
  // touch stronger so the boiling chromatic refraction reads as a lens).
  const distortionStrength = distortionMode === 1 ? 0.018 : distortionMode === 2 ? 0.02 : 0;
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
      // rotationCenter anchor (half-extent units): pos += right·halfW·x + up·halfH·y.
      uAnchorShift: { value: anchorShift },
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
      // 1 = axial billboard about uExplicitOrientation (billboard + nonzero eo); 0 = off.
      uUseAxialBillboard: { value: useAxialBillboard },
      uExplicitOrientation: {
        value: new THREE.Vector3(
          orientationVec?.[0] ?? 0,
          orientationVec?.[1] ?? 0,
          orientationVec?.[2] ?? 1,
        ).normalize(),
      },
      uExplicitOrientationLocal: { value: opts.explicitOrientationLocal ? 1 : 0 },
      uHideFadeBase: { value: hideFadeBase },
      uHideSpeed: { value: hideSpeed },
      // 1 = orientation mode 1 (billboard): the fade term is inverted.
      uHideIsAxial: { value: hideIsAxialMode },
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
      // DEFORM per-texel strength encoding: 1 = deform16f signed field
      // (R centred 0.5), 0 = RGB foam mask. See the mode-1 shader branch.
      uDeformSigned: { value: opts.deformSigned ? 1 : 0 },
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
      uniform float uUseAxialBillboard;
      uniform float uUseFixedOrientation;
      uniform vec2 uAnchorShift;
      uniform float uHideFadeBase;
      uniform float uHideSpeed;
      uniform float uHideIsAxial;
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
          // Byte-exact native law (fx_Sprite_hideAngleFade, RE 2026-07-02):
          // fade = clamp01((|dot(viewDir, orientVec)| - base) * hideSpeed),
          // inverted for orientation mode 1 (billboard/axial). Axial cards
          // thus fade out looking DOWN the axis (|dot|->1 => 1-fade->0, e.g.
          // ThisIsFine memes seen top-down) and show from the side; flat
          // cards fade out edge-on (|dot|->0). The base is pre-cooked per
          // mode on the CPU (uHideFadeBase).
          float h = clamp((abs(dot(viewDirW, orientW)) - uHideFadeBase) * uHideSpeed, 0.0, 1.0);
          vHideFade = (uHideIsAxial > 0.5) ? (1.0 - h) : h;
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
        // rotationCenter anchor (fx_Sprite_buildQuad: pos += right*halfW*kx +
        // up*halfH*ky; halfW folds the aspect like native drawRec+0x04, halfH
        // = the base half-height). bottom=(0,1) puts the bottom edge at the
        // particle (muzzle-anchored beams); center=(0,0) is a no-op.
        float anchorHalfH = size * 0.5;
        vec2 anchorOff = uAnchorShift * vec2(anchorHalfH * spriteAspectX, anchorHalfH);
        if (uUseAxialBillboard > 0.5) {
          // AXIAL (cylindrical) billboard — billboard=true + nonzero eo: the
          // card's long/up axis = eo (world, or the attachment frame when
          // explicitOrientationLocal), and it yaws about that axis to face
          // the camera; texture-up = the axis, front toward the viewer.
          // eo=(0,±y,0): upright memes readable from the side (ThisIsFine
          // capitano/text_cloud — the shipped case, math identical here).
          // eo=(0,0,1) local: a muzzle beam running along the barrel
          // (Laser_Charge_Shot_H2020 RAY). Native-exact (fx_Sprite_buildQuad
          // mode 1, byte-proven 2026-07-02): up = orientVec, right =
          // normalize(cross(orientVec, camEye - particlePos)), evaluated per
          // draw with the live eye — same as the per-frame tracking here.
          vec3 axisW = uExplicitOrientationLocal > 0.5
            ? normalize(mat3(modelMatrix) * uExplicitOrientation)
            : normalize(uExplicitOrientation);
          vec3 centerW = (modelMatrix * vec4(iPosition, 1.0)).xyz;
          vec3 toCam = cameraPosition - centerW;
          vec3 U = cross(axisW, toCam);
          float uLen = length(U);
          U = (uLen > 1e-6)
            ? U / uLen
            : normalize(cross(axisW, abs(axisW.y) < 0.9 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0)));
          vec3 cornerW = centerW
            + U * ((cornerUV.x - 0.5) * worldDiam + anchorOff.x)
            + axisW * ((0.5 - cornerUV.y) * worldDiam + anchorOff.y);
          gl_Position = projectionMatrix * viewMatrix * vec4(cornerW, 1.0);
        } else if (uUseFixedOrientation > 0.5) {
          // Fixed-orientation quad (fx_Sprite_buildQuad fixed-card branch, RE
          // 2026-07-02): the quad FACES world explicitOrientation (normal = N).
          // The in-plane frame is the NATIVE tangent/bitangent pair from
          // fx_Math_orthonormalBasis(N) — A = (sign(N.y),0,0), B = (0,0,1) for
          // the degenerate N=(0,±1,0); else A = normalize(-N.z, 0, N.x),
          // B = A×N — mapped as texture-right = B, texture-up = A, so the
          // READABLE face points toward +N (front = B×A = +N). Proven by the
          // ThisIsFine meme card: the DDS is stored readable (pixel probe) and
          // WG displays it readable from the +N side; the old
          // U=cross(N,+Y)/V=cross(U,N) mapping fronted −N (text MIRRORED when
          // viewed from above) and was 90° rotated in-plane besides.
          vec3 N = uExplicitOrientationLocal > 0.5
            ? normalize(mat3(modelMatrix) * uExplicitOrientation)
            : normalize(uExplicitOrientation);
          float nxz = N.x * N.x + N.z * N.z;
          vec3 tA = (nxz < 1e-8)
            ? vec3(N.y >= 0.0 ? 1.0 : -1.0, 0.0, 0.0)
            : normalize(vec3(-N.z, 0.0, N.x));
          vec3 tB = (nxz < 1e-8) ? vec3(0.0, 0.0, 1.0) : cross(tA, N);
          // Per-particle in-plane angle rotates the AXES here (native
          // fx_Sprite_buildQuad mode-0 sincos), NOT the sampled UVs — so the
          // rotationCenter anchor lever arm below turns with it, exactly like
          // native's post-rotation pos += rightAxis*halfW*kx + upAxis*halfH*ky.
          // rotationPhase carries initialOrientation + spin drift + the
          // camera-azimuth spawn bake (flat local eoY cards). velocityOriented
          // flat cards stay on the FS per-frame velocity-angle path instead
          // (native spawn-bakes the card-plane velocity angle; approximation
          // documented at the FS gate).
          float thetaFC = (uVelocityOriented > 0.5) ? 0.0 : rotationPhase;
          float sFC = sin(thetaFC);
          float cFC = cos(thetaFC);
          vec3 U = tB * cFC - tA * sFC;
          vec3 V = tB * sFC + tA * cFC;
          vec3 centerW = (modelMatrix * vec4(iPosition, 1.0)).xyz;
          vec3 cornerW = centerW
            + U * ((cornerUV.x - 0.5) * worldDiam + anchorOff.x)
            + V * ((0.5 - cornerUV.y) * worldDiam + anchorOff.y);
          gl_Position = projectionMatrix * viewMatrix * vec4(cornerW, 1.0);
        } else {
          // INSTANCED camera-facing billboard (default, unchanged): expand the
          // quad in view space so it always faces the camera.
          vec4 mvPosition = modelViewMatrix * vec4(iPosition, 1.0);
          vec2 viewOffset = vec2(cornerUV.x - 0.5, 0.5 - cornerUV.y) * worldDiam + anchorOff;
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
      uniform float uUseFixedOrientation;
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
      uniform float uDeformSigned;
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
          // Fixed-orientation cards rotate their AXES in the vertex shader
          // (native buildQuad mode 0), so the UV footprint must NOT rotate
          // again here. velocityOriented fixed cards are the exception: their
          // angle still applies to the sampled UVs below (per-frame view-plane
          // velocity — an approximation of native's spawn-baked card-plane
          // velocity angle; see the VS note).
          bool fsRotates = uUseSpriteRotation > 0.5
            && !(uUseFixedOrientation > 0.5 && uVelocityOriented < 0.5);
          if (fsRotates) {
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
          // The MV flow-warp is NOT gated on the cell-grid count in native
          // (producer-traced 2026-06-22): fx_ParticleSystem_tick (0x14071b7f0) writes
          // frac(integral of frameRateRamp) to the per-particle phase independent of
          // framesPerX*Y, and ps4.txt:574-588 warps + cross-fades on that phase
          // unconditionally. So a single-cell (1x1) motionVectors sprite -- the base +
          // separate _MV texture pattern, e.g. Smoke_big_D_Day_Custom -- still billows
          // in-game. Drive the MV branch on the frame clock + MV path alone (drop the
          // hasGrid / fx*fy>1 requirement), still excluding frozen/random frames. The old
          // animated gate froze EVERY 1x1 motionVectors particle. At 1x1 the cell math
          // below degenerates correctly (cell0 == cell1 == full texture); only the gate
          // was wrong.
          bool mvAnimated = (useMv > 0.5 && uFramesEnd > 0.0
            && (uUseFramePhase > 0.5 || uFrameRate > 0.0) && uRandomFrame <= 0.5);
          // H5 (RE doc 63): a randomFrameOnly system is NOT animated — each
          // particle freezes on its spawn-seeded cell. Takes precedence.
          bool randomCell = (hasGrid && uRandomFrame > 0.5);
          // _MVEA emission plumbing. The emission channel (.R) is consumed by
          // TWO permutations (ps4.txt:589-628): the non-gradient body
          // substitution (M4) and — for GRADIENT_MAP — the glow-ramp KEY (the
          // engine's r5.x is the t3/g_particleMVTexture sample, NOT the _LM
          // body texel). Sample it wherever the texture is available; -1
          // sentinel = no sample this fragment (texture missing/not loaded).
          // ALPHA substitution is NOT gradient-gated (ps4.txt &8 branch: the
          // cross-faded MVEA.A lands in r2.x and the final alpha is
          // r2.x * v7.w in BOTH permutations — the &4 LUT test only routes
          // COLOR). Gating it on non-gradient rendered opaque squares for
          // GRADIENT_MAP systems whose _LM alpha is authored junk-opaque
          // because the flag redirects opacity (Moray First/Second_Explosion,
          // FlagExplosion_LM.dds ~76% alpha=1). Only the BODY substitution
          // stays non-gradient (gradient takes the ramp/KEY branch instead).
          bool useMvAlpha = (useEmissionAlphaFromMV > 0.5);
          bool useMvEmissionBody = (useMvAlpha && uGradientMapMode <= 0.5);
          bool wantMvEmission = (useMvAlpha || uGradientMapMode > 0.5);
          float mvEmissionSample = -1.0;
          float mvAlphaSample = -1.0;

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
              if (useMvAlpha) mvAlphaSample = e.a;
            }
          } else if (mvAnimated) {
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
            // Manifest atlas-rect remap on the BODY samples only. When the
            // base texture resolves via the manifest atlas (e.g. empty.tga on
            // a shared 4096^2 page) instead of a direct URL, the packed cell
            // UVs must be mapped into the atlas rect before sampling map --
            // exactly as the randomCell/animated/static branches do. This was
            // the one flipbook branch missing it, so an atlas-resolved base
            // under motionVectors sampled raw grid cells of the WHOLE atlas
            // page: a big textured square (Daruma_Gold_CustomDeath_UnderWater
            // system #0, empty.tga + Smoke_expl_cloud_7x7_MVEA). The _MVEA
            // warp field (mvMap, below) is its own non-atlased file, so it
            // keeps the RAW grid UVs -- the remap applies to the packed page
            // only.
            vec2 buv0 = uv0;
            vec2 buv1 = uv1;
            if (useAtlasRect > 0.5) {
              buv0 = mix(atlasRect.xy, atlasRect.zw, buv0);
              buv1 = mix(atlasRect.xy, atlasRect.zw, buv1);
            }
            base = mix(texture2D(map, buv0), texture2D(map, buv1), f);
            if (wantMvEmission) {
              // _MVEA.R = emission, .A = opacity — sampled at the warped UVs,
              // lerped by f. Non-gradient permutation: emission substitutes
              // the body (M4). GRADIENT_MAP permutation: the emission is the
              // glow-ramp KEY (ps4.txt:597-618). The .A opacity substitution
              // applies in BOTH when the flag is authored (see useMvAlpha).
              vec4 e0 = texture2D(mvMap, uv0);
              vec4 e1 = texture2D(mvMap, uv1);
              mvEmissionSample = mix(e0.r, e1.r, f);
              if (useMvAlpha) mvAlphaSample = mix(e0.a, e1.a, f);
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
            // Static cell = the noAnimation path. The engine samples the FULL
            // texture here: FUN_14071ffb0 forces framesPerX/Y to 1x1 when
            // animationType==noAnimation (CMP [cfg+0x168],0; JZ skips the grid
            // copy), so the authored framesPerX/Y are VESTIGIAL for noAnimation.
            // The old "crop to cell 0" (RE doc 63 H4) diverged from the engine and
            // hid single sprites mis-authored with a grid (e.g. BA_Logo 7x7). The
            // grid is provably meaningless here: noAnim textures reuse one image
            // under conflicting grids (water_ring_c at 8x8 AND 9x9; glow_w at
            // 2x2..32x1) — a single image cannot be two different sheets. So:
            // sample the full texture (atlas-rect mapping still applies below).
            vec2 puv = local;
            vec2 gridUv = puv; // pre-atlas-remap UV, shared by _MVEA
            if (useAtlasRect > 0.5) {
              puv = mix(atlasRect.xy, atlasRect.zw, puv);
            }
            base = texture2D(map, puv);
            if (useMv > 0.5 && wantMvEmission) {
              vec4 e = texture2D(mvMap, gridUv);
              mvEmissionSample = e.r;
              if (useMvAlpha) mvAlphaSample = e.a;
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
            // _LM = a 4-WAY directional lightmap (PS_RLT lightmapping4Way).
            // base.RGBA are FOUR grayscale renders of the same puff baked-lit
            // from the billboard tangent frame's signed axes — NOT albedo and
            // NOT 3 HL2 lobes. Engine-exact decode (ps4.txt:769-794, cross-
            // validated across all 7 shader permutations + the _LM texture
            // content by 3 independent agents, 2026-06-23):
            //   R = +tangent   G = -tangent   A = +bitangent   B = -bitangent
            // Dot each billboard axis with the (toward-)sun, sign-select that
            // axis' channel, weight by (axis.sun)^2, saturate — NO normalize
            // (RE doc 63 H6). The billboard-normal axis has no baked render: it
            // uses a derived brightness (front) / soft fade (back). The native
            // also reuses base.a as the opacity source (kept below as base.a),
            // so A is BOTH the +bitangent render AND the alpha.
            //
            // Camera-facing billboards: the tangent frame is the view axes
            // (+X tangent, +Y bitangent, +Z normal toward camera), so dotting
            // each with the view-space (toward-)sun reduces to the sun's view
            // components. (Velocity/explicit-oriented sprites still use this
            // camera-frame approximation, as the prior decode did; a fully
            // exact basis needs per-particle world axes as varyings.)
            vec3 sunV = normalize((viewMatrix * vec4(uSunDirWorld, 0.0)).xyz);
            float d1 = sunV.x; // +tangent   . toward-sun
            float d2 = sunV.y; // +bitangent . toward-sun
            float d3 = sunV.z; // +normal    . toward-sun
            float ch1 = d1 > 0.0 ? base.r : base.g;
            float ch2 = d2 > 0.0 ? base.a : base.b;
            // Billboard-normal axis: native picks a gamma-curved average of the
            // 4 renders (front) vs a per-particle soft fade (back). The soft
            // scalars (v6.w / v10.z) are not plumbed; approximate the back as
            // the attenuated front term.
            float avg4 = (base.r + base.g + base.b + base.a) * 0.25;
            float frontTerm = min(pow(max(avg4, 0.0), 0.625), 1.0);
            float ch3 = d3 > 0.0 ? frontTerm : frontTerm * 0.5;
            // Squared-cosine weights, saturate, NO energy-normalize
            // (ps4.txt:789-794 mul/mul/mad/mad_sat).
            float lit = clamp(d1 * d1 * ch1 + d2 * d2 * ch2 + d3 * d3 * ch3, 0.0, 1.0);
            // Ambient floor so fully-shadowed smoke isn't pure black; colored
            // sun term (RE doc 63 M2) tints the relit body by the weather sun.
            float ambient = uLightingAmbient * frontTerm;
            base = vec4(
              clamp(vec3(ambient) + lit * uLightingDiffuse * uSunColorNorm, 0.0, 1.0),
              base.a
            );
          }
          // useEmissionAlphaFromMV opacity substitution — applied AFTER the
          // 4-way relight so the decode's +bitangent lobe / avg4 read the
          // ORIGINAL LM.A (native keeps the LM sample r4 intact for lighting;
          // the cross-faded MVEA.A rides a separate register r2.x and only
          // feeds the FINAL alpha, in BOTH gradient and non-gradient
          // permutations — ps4 &8 branch, final mul r1.w = r2.x * v7.w).
          if (useMvAlpha && mvAlphaSample >= 0.0) {
            base.a = mvAlphaSample;
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
            bool isShimmer = uDistortionMode > 1.5;
            // SHIMMER (heat haze) BOILS: ride the per-particle age + a random
            // per-particle phase (vFrameSeed) so the warp field wobbles and the
            // lens looks alive rather than a frozen glass pane. RE doc 63 (M1):
            // SHIMMER is a background warp with no opaque colour, so we only
            // perturb the refraction here — never add a body. DEFORM (water)
            // keeps its steady authored-normal warp untouched.
            if (isShimmer) {
              float t = vAge * 3.0 + vFrameSeed * 6.28318;
              normalOffset += vec2(
                sin(t + vLocalUV.y * 12.0),
                cos(t * 1.13 + vLocalUV.x * 12.0)
              ) * 0.30;
            }
            vec2 warp = normalOffset * uDistortionStrength * clamp(outA, 0.0, 1.0);
            if (isShimmer) {
              // Chromatic refraction: sample R/G/B across slightly different
              // warp offsets (dispersion through hot air). The per-channel
              // split makes the heat lens read even over a near-flat
              // background, while preserving SHIMMER's no-opaque-colour rule —
              // only the warped scene, no foam / white body.
              vec2 c0 = clamp(screenUv + warp * 1.30, vec2(0.001), vec2(0.999));
              vec2 c1 = clamp(screenUv + warp, vec2(0.001), vec2(0.999));
              vec2 c2 = clamp(screenUv + warp * 0.70, vec2(0.001), vec2(0.999));
              outRgb = vec3(
                texture2D(uDistortionSceneTexture, c0).r,
                texture2D(uDistortionSceneTexture, c1).g,
                texture2D(uDistortionSceneTexture, c2).b
              ) + emissionBody;
              outA *= 0.55;
            } else {
              // DEFORM_WATER_SURFACE quads natively write the water-sim RT
              // (signed height + foam accumulation) — they are never visible
              // billboards, and their fields feather to NEUTRAL at the quad
              // borders (probed: water_spray_wave_UD border deviation ~0.003
              // vs centre ~0.13). The flat 0.45-alpha refraction rendered the
              // whole quad as a glassy square (FX_HELPER foam/deform family) —
              // scale warp/foam/alpha by the per-texel deviation instead.
              // deform16f/* = float RG field, R centred 0.5 (signed height),
              // G≈0; other deform-blend textures are RGB foam MASKS (DXT1,
              // alpha-less, black = nothing).
              float deformMag = uDeformSigned > 0.5
                ? clamp((abs(base.r - 0.5) * 2.0 + abs(base.g)) * 2.0, 0.0, 1.0)
                : clamp(max(base.r, max(base.g, base.b)), 0.0, 1.0);
              vec2 warpedUv = clamp(screenUv + warp * deformMag, vec2(0.001), vec2(0.999));
              vec3 refracted = texture2D(uDistortionSceneTexture, warpedUv).rgb;
              float foam = 0.10 * clamp(outA, 0.0, 1.0) * deformMag;
              outRgb = refracted + vec3(foam) + emissionBody;
              outA *= 0.45 * deformMag;
            }
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
