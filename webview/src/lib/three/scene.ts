// Reusable Three.js scene scaffolding. Shared by the ship viewer and
// (eventually) the library viewer; lift any duplicated setup here.
//
// `createSceneEnvironment` builds an empty scene with renderer, camera,
// orbit controls, IBL, lights, grid, and axes — everything needed to
// render *something*, but no domain-specific geometry. Callers append
// their own groups to `scene` and call `dispose()` when done.
//
// The composer is the ALWAYS-ON render path: RenderPass -> UnrealBloomPass
// (toggled) -> GT (Uchimura) tonemap pass. The GT pass replaces Three's
// built-in tonemapper (renderer.toneMapping = NoToneMapping) to match WG's
// Gran-Turismo tonemap curve — see reference/engine/wg_render_hdr_tonemap.md.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';

export interface BloomParams {
  /** Overall bloom contribution (UnrealBloomPass.strength). */
  strength: number;
  /** Spread of the bloom kernel (UnrealBloomPass.radius). */
  radius: number;
  /** Luminance threshold above which fragments contribute (0..1). */
  threshold: number;
}

export const DEFAULT_BLOOM_PARAMS: BloomParams = {
  strength: 0.6,
  radius: 0.35,
  threshold: 0.75,
};

/** GT (Uchimura "Gran Turismo") tonemap curve parameters. Seeded from WG's
 *  `14_Atlantic` / Default `space.ubersettings` (HDR/Tonemapping). `exposure`
 *  is the keyed-exposure multiplier (WG: `middleGray / avgLum * 2^offset`;
 *  with the static `avgLum ≈ middleGray` approximation this reduces to
 *  `2^hdrMapExposureOffset`). Uchimura `P` (max display brightness) and `b`
 *  (pedestal) are fixed at 1 / 0 — WG does not author them. See
 *  reference/engine/wg_render_hdr_tonemap.md. */
export interface GTTonemapParams {
  /** keyed exposure multiplier applied before the curve (linear space). */
  exposure: number;
  /** `a` — gtContrast. */
  contrast: number;
  /** `m` — gtLinearSectionStart. */
  linearStart: number;
  /** `l` — gtLinearSectionLength. */
  linearLength: number;
  /** `c` — gtBlack (toe tightness). */
  black: number;
}

export const DEFAULT_TONEMAP_PARAMS: GTTonemapParams = {
  exposure: 1.1,
  contrast: 0.7,
  linearStart: 0.5,
  linearLength: 0.0,
  black: 1.21,
};

type RenderableDepthObject = THREE.Object3D & {
  material?: THREE.Material | THREE.Material[];
  isMesh?: boolean;
  isInstancedMesh?: boolean;
  isSkinnedMesh?: boolean;
};

function objectMaterials(object: THREE.Object3D): THREE.Material[] {
  const material = (object as RenderableDepthObject).material;
  if (!material) return [];
  return Array.isArray(material) ? material : [material];
}

function materialHasSoftParticleUniform(material: THREE.Material): material is THREE.ShaderMaterial {
  const uniforms = (material as THREE.ShaderMaterial).uniforms;
  return !!uniforms?.uSoftParticleDepthScale;
}

function materialIsDistortionParticle(material: THREE.Material): material is THREE.ShaderMaterial {
  const uniforms = (material as THREE.ShaderMaterial).uniforms;
  return !!uniforms?.uDistortion && Number(uniforms.uDistortion.value ?? 0) > 0.5;
}

function shouldHideForOpaqueDepth(object: THREE.Object3D): boolean {
  if (!object.visible) return false;
  const renderable = object as RenderableDepthObject;
  const materials = objectMaterials(object);
  if (materials.length === 0) return false;
  if (materials.some(materialHasSoftParticleUniform)) return true;
  if (!renderable.isMesh && !renderable.isInstancedMesh && !renderable.isSkinnedMesh) return true;
  return materials.some((mat) => mat.transparent || mat.depthWrite === false);
}

function shouldHideForDistortionSource(object: THREE.Object3D): boolean {
  if (!object.visible) return false;
  return objectMaterials(object).some(materialIsDistortionParticle);
}

// Uchimura GT tonemap (GDC 2017) + sRGB OETF as an EffectComposer ShaderPass.
// Input is linear scene radiance (RenderPass renders into a linear HDR target
// with Three's tonemapper disabled); output is display-encoded sRGB written
// straight to the canvas. Replaces OutputPass.
const GTTonemapShader = {
  name: 'GTTonemapShader',
  uniforms: {
    tDiffuse: { value: null as THREE.Texture | null },
    uExposure: { value: DEFAULT_TONEMAP_PARAMS.exposure },
    uContrast: { value: DEFAULT_TONEMAP_PARAMS.contrast },
    uLinearStart: { value: DEFAULT_TONEMAP_PARAMS.linearStart },
    uLinearLength: { value: DEFAULT_TONEMAP_PARAMS.linearLength },
    uBlack: { value: DEFAULT_TONEMAP_PARAMS.black },
    uMaxBright: { value: 1.0 }, // Uchimura P (fixed)
    uPedestal: { value: 0.0 }, // Uchimura b (fixed)
  },
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    varying vec2 vUv;
    uniform sampler2D tDiffuse;
    uniform float uExposure, uContrast, uLinearStart, uLinearLength, uBlack, uMaxBright, uPedestal;

    // Uchimura "Gran Turismo" curve. x is linear scene-referred; returns [0,P].
    float uchimura(float x) {
      float P = uMaxBright, a = uContrast, m = uLinearStart, l = uLinearLength, c = uBlack, b = uPedestal;
      float l0 = ((P - m) * l) / a;
      float S0 = m + l0;
      float S1 = m + a * l0;
      float C2 = (a * P) / (P - S1);
      float CP = -C2 / P;
      float w0 = 1.0 - smoothstep(0.0, m, x);
      float w2 = step(m + l0, x);
      float w1 = 1.0 - w0 - w2;
      float T = m * pow(x / m, c) + b;             // toe
      float S = P - (P - S1) * exp(CP * (x - S0)); // shoulder
      float L = m + a * (x - m);                   // linear
      return T * w0 + L * w1 + S * w2;
    }

    vec3 linearToSRGB(vec3 c) {
      c = clamp(c, 0.0, 1.0);
      return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
    }

    void main() {
      vec4 texel = texture2D(tDiffuse, vUv);
      vec3 c = max(texel.rgb, 0.0) * uExposure;               // keyed exposure
      c = vec3(uchimura(c.r), uchimura(c.g), uchimura(c.b));   // GT curve (linear)
      gl_FragColor = vec4(linearToSRGB(c), texel.a);          // sRGB OETF
    }
  `,
};

export interface SceneEnvironment {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  renderer: THREE.WebGLRenderer;
  controls: OrbitControls;
  grid: THREE.GridHelper;
  axes: THREE.AxesHelper;
  /** Render the scene through whichever path is active. Use this from
   *  the render loop instead of `renderer.render(scene, camera)` so the
   *  composer (when bloom is enabled) is honored. */
  render(): void;
  /** Adjust the WebGL backing-store pixel ratio. Lower values are useful
   *  for huge map scenes where fill-rate dominates interactivity. */
  setPixelRatio(ratio: number): void;
  /** Toggle the RenderPass -> GT tonemap composer path. When disabled and
   *  bloom is also disabled, render() draws directly to the canvas. */
  setPostprocessEnabled(on: boolean): void;
  isPostprocessEnabled(): boolean;
  /** Toggle bloom. The composer + passes are built lazily on first
   *  `true` and reused thereafter. */
  setBloomEnabled(on: boolean): void;
  /** Patch any subset of bloom params; missing keys keep their current
   *  value. Safe to call before bloom has been enabled. */
  setBloomParams(p: Partial<BloomParams>): void;
  isBloomEnabled(): boolean;
  getBloomParams(): Readonly<BloomParams>;
  /** Patch any subset of the GT tonemap params (curve + keyed exposure);
   *  missing keys keep their current value. */
  setTonemapParams(p: Partial<GTTonemapParams>): void;
  /** Replace `scene.environment` with a WG PMREM (from `env_ibl`), or restore
   *  the default procedural RoomEnvironment when passed `null`. The caller
   *  owns the passed texture's lifecycle. */
  setEnvironment(tex: THREE.Texture | null): void;
  /** Set the procedural fill-light intensities (hemisphere + key directional).
   *  `directional` is optional so the key can be driven separately by
   *  {@link setSunLight}. Both default to 0.85; dim the hemisphere when a WG
   *  IBL is active so the cube radiance dominates (keyed exposure stays sane). */
  setFillLights(hemisphere: number, directional?: number): void;
  /** Aim + tint the key directional ("sun"). `direction` points TOWARD the sun
   *  (the light travels the opposite way). Used to drive the per-weather WG
   *  sun; only the provided fields change. */
  setSunLight(opts: {
    direction?: THREE.Vector3;
    color?: THREE.ColorRepresentation;
    intensity?: number;
  }): void;
  /** Current key-light direction/color. Direction points TOWARD the sun,
   *  matching `setSunLight`. Returned objects are clones. */
  getSunLight(): { direction: THREE.Vector3; color: THREE.Color; intensity: number };
  /** Forwarded by the resize observer so the composer stays in sync. */
  setSize(width: number, height: number): void;
  /** Replace the scene background color (e.g. to switch a particle
   *  inspector between a night sky and a daylit sky so occluding,
   *  alpha-blended smoke is actually visible). */
  setBackground(color: number): void;
  /** Show/hide the faux animated water surface. DEFORM_WATER_SURFACE
   *  particles (ship wakes, splashes) author a water DISTORTION, not a
   *  sprite — with no ocean to refract they read as flat squares. Enabling
   *  this drops a structured, animated water plane at y=0 that the
   *  screen-space distortion pass can warp, so the wake reads as ripples.
   *  Hidden by default; the particle inspector turns it on only for records
   *  that contain a DEFORM_WATER_SURFACE system. */
  setWaterPlaneVisible(on: boolean): void;
  /** Dispose every resource created here. Idempotent. */
  dispose(): void;
}

export interface SceneOptions {
  /** Background color (default: 0x0a0c11). */
  background?: number;
  /** Initial camera position (default: (80, 50, 80)). */
  cameraPosition?: [number, number, number];
  /** Camera FOV (default: 45). */
  fov?: number;
  /** Camera far plane (default: 5000). */
  far?: number;
  /** Grid size in metres (default: 600). */
  gridSize?: number;
  /** Grid divisions — 100 m squares at default 600/12 (default: 12). */
  gridDivisions?: number;
  /** Axes helper size (default: 10). */
  axesSize?: number;
  /** Keyed-exposure multiplier for the GT tonemap pass (default: 1.1). */
  exposure?: number;
  /** Override GT tonemap curve params (default: WG 14_Atlantic / Default). */
  tonemap?: Partial<GTTonemapParams>;
  /** WebGL antialias flag (default: true). */
  antialias?: boolean;
  /** Backing-store pixel ratio cap/override (default: min(devicePixelRatio, 2)). */
  pixelRatio?: number;
  /** Use the postprocess composer by default (default: true). */
  postprocess?: boolean;
}

export function createSceneEnvironment(
  container: HTMLElement,
  opts: SceneOptions = {},
): SceneEnvironment {
  const {
    background = 0x0a0c11,
    cameraPosition = [80, 50, 80],
    fov = 45,
    far = 5000,
    gridSize = 600,
    gridDivisions = 12,
    axesSize = 10,
    exposure = 1.1,
    antialias = true,
    pixelRatio = Math.min(window.devicePixelRatio, 2),
    postprocess = true,
  } = opts;

  // GT tonemap curve + keyed exposure (seeded from WG 14_Atlantic / Default;
  // override via opts.tonemap or env.setTonemapParams()).
  const tonemapParams: GTTonemapParams = {
    ...DEFAULT_TONEMAP_PARAMS,
    exposure,
    ...(opts.tonemap ?? {}),
  };

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);

  const camera = new THREE.PerspectiveCamera(fov, 1, 0.1, far);
  camera.position.set(...cameraPosition);

  const renderer = new THREE.WebGLRenderer({ antialias });
  renderer.setPixelRatio(Math.max(0.25, pixelRatio));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // The GT (Uchimura) tonemap runs in the composer's final pass, so disable
  // Three's built-in tonemapper to avoid double-tonemapping. (Was ACESFilmic
  // — the wrong curve for WG; see wg_render_hdr_tonemap.md.)
  renderer.toneMapping = THREE.NoToneMapping;
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;

  // PBR needs an env map for indirect specular — without it metals render
  // black and roughness has nothing to reflect. RoomEnvironment is a
  // built-in procedural studio IBL; cheap and matches the dark UI bg.
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envRT = pmrem.fromScene(new RoomEnvironment(), 0.04);
  scene.environment = envRT.texture;

  // Hemisphere + directional shape direction/shadow on top of the IBL.
  const hemi = new THREE.HemisphereLight(0xbcd3ff, 0x222833, 0.85);
  const dir = new THREE.DirectionalLight(0xffffff, 0.85);
  dir.position.set(50, 80, 50);
  const sunDirectionState = dir.position.clone().normalize();
  scene.add(hemi, dir);

  // Helpers — the ship is huge (~300 m) so the default grid scale
  // (100 m squares × 6 = 600 m) gives one-glance scale reading.
  const grid = new THREE.GridHelper(gridSize, gridDivisions, 0x445065, 0x2a2e38);
  (grid.material as THREE.Material).transparent = true;
  (grid.material as THREE.Material).opacity = 0.4;
  const axes = new THREE.AxesHelper(axesSize);
  scene.add(grid, axes);

  // ── Faux water surface (opt-in; particle inspector) ───────────────────
  // DEFORM_WATER_SURFACE particles only EXIST in-game as a refraction of the
  // ocean surface. The inspector has no ocean, so the screen-space distortion
  // approximation (particles.ts uDistortion path) has nothing to bend and the
  // wake cards read as faint flat squares. This plane gives the refraction a
  // structured, animated surface to warp. The waves live entirely in the
  // fragment shader (procedural from world XZ) so a single quad suffices.
  //
  // depthTest:false + depthWrite:false + renderOrder -1 make it a pure
  // background layer: it IS captured into the scene-color RTT (the cards
  // refract it) but it neither occludes the cards nor feeds the soft-particle
  // depth snapshot (shouldHideForOpaqueDepth hides depthWrite:false meshes),
  // so the wake sprites do not soft-fade against the surface they sit on.
  const waterUniforms = {
    uTime: { value: 0 },
    uSunDir: { value: sunDirectionState.clone() },
  };
  const waterMaterial = new THREE.ShaderMaterial({
    uniforms: waterUniforms,
    transparent: false,
    depthWrite: false,
    depthTest: false,
    vertexShader: /* glsl */ `
      varying vec3 vWorld;
      void main() {
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vWorld = wp.xyz;
        gl_Position = projectionMatrix * viewMatrix * wp;
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      uniform float uTime;
      uniform vec3 uSunDir;
      varying vec3 vWorld;

      // Summed directional sines -> a cheap, structured wave height field.
      // Mixed wavelengths give both broad swell and fine sparkle ripples;
      // the high-frequency terms also keep the screen-space refraction legible
      // at the small warp magnitude the distortion pass applies.
      float waveH(vec2 p, float t) {
        float h = 0.0;
        // Broad swell (wavelength ~22-33 m).
        h += sin(p.x * 0.28 + t * 1.30) * 0.40;
        h += sin((p.x * 0.19 + p.y * 0.33) + t * 1.00) * 0.34;
        h += sin((p.x * 0.41 - p.y * 0.22) + t * 1.70) * 0.26;
        // Finer chop (~6-10 m).
        h += sin((p.x * 0.75 + p.y * 0.62) + t * 2.40) * 0.16;
        h += sin((p.x * 0.60 - p.y * 1.05) + t * 2.90) * 0.12;
        // Sparkle detail.
        h += sin((p.x * 1.50 + p.y * 1.20) + t * 3.60) * 0.06;
        h += sin((p.x * 2.30 - p.y * 1.90) + t * 4.50) * 0.035;
        return h;
      }

      // Procedural daytime sky used as the reflection probe — gives the surface
      // a real horizon-graded reflection + sun glow WITHOUT a planar reflector
      // (which would not compose with the depth/distortion snapshot passes).
      vec3 skyColor(vec3 dir, vec3 sun) {
        float up = clamp(dir.y, 0.0, 1.0);
        vec3 horizon = vec3(0.62, 0.74, 0.88);
        vec3 zenith = vec3(0.13, 0.33, 0.60);
        vec3 sky = mix(horizon, zenith, pow(up, 0.55));
        float s = clamp(dot(dir, sun), 0.0, 1.0);
        sky += vec3(1.0, 0.92, 0.74) * pow(s, 90.0) * 1.1;   // sun reflection
        sky += vec3(1.0, 0.85, 0.65) * pow(s, 6.0) * 0.06;   // broad glare
        return sky;
      }

      void main() {
        vec2 p = vWorld.xz;
        float t = uTime;
        float e = 0.6;
        float h0 = waveH(p, t);
        float hx = (waveH(p + vec2(e, 0.0), t) - h0) * 3.0;
        float hz = (waveH(p + vec2(0.0, e), t) - h0) * 3.0;
        vec3 n = normalize(vec3(-hx / e, 1.0, -hz / e));

        vec3 viewDir = normalize(cameraPosition - vWorld);
        vec3 sun = normalize(uSunDir);

        // Deep-ocean body: dark teal-blue near the viewer, slightly lifted on
        // the wave faces that catch the sky.
        vec3 deep = vec3(0.004, 0.045, 0.072);
        vec3 shallow = vec3(0.02, 0.13, 0.17);
        vec3 body = mix(deep, shallow, clamp(n.y * 0.5 + 0.5, 0.0, 1.0));

        // Sky reflection off the perturbed surface, blended by Schlick fresnel
        // (mostly body looking straight down, mostly sky toward the horizon).
        vec3 refl = reflect(-viewDir, n);
        refl.y = abs(refl.y);
        vec3 sky = skyColor(refl, sun);
        float fres = 0.02 + 0.98 * pow(1.0 - clamp(dot(viewDir, n), 0.0, 1.0), 5.0);
        vec3 col = mix(body, sky, fres);

        // Sharp sun specular on the crests.
        vec3 halfv = normalize(sun + viewDir);
        float spec = pow(clamp(dot(n, halfv), 0.0, 1.0), 220.0);
        col += vec3(1.0, 0.96, 0.85) * spec * 0.5;

        // A hint of foam on the steepest crests (kept low so it stays under the
        // bloom threshold and the wake disturbance still reads).
        float crest = smoothstep(0.55, 0.95, h0 * 0.5 + 0.5);
        col = mix(col, vec3(0.78, 0.86, 0.92), crest * 0.06);

        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });
  const waterPlane = new THREE.Mesh(new THREE.PlaneGeometry(4000, 4000), waterMaterial);
  waterPlane.rotation.x = -Math.PI / 2;
  waterPlane.renderOrder = -1;
  waterPlane.frustumCulled = false;
  waterPlane.visible = false;
  waterPlane.name = 'FauxWaterSurface';
  scene.add(waterPlane);

  // ── Post-FX (always-on composer) ──────────────────────────────────
  // The composer is the SOLE render path: RenderPass -> UnrealBloomPass
  // (enabled-toggled) -> GTTonemapPass (renderToScreen). The GT pass owns
  // exposure + the Uchimura curve + the sRGB OETF, replacing OutputPass and
  // Three's built-in (disabled) tonemapper. Built lazily on first render()
  // so the canvas size is known; bloom toggles via bloomPass.enabled.
  let bloomEnabled = false;
  const bloomParams: BloomParams = { ...DEFAULT_BLOOM_PARAMS };
  let composer: EffectComposer | null = null;
  let bloomPass: UnrealBloomPass | null = null;
  let gtPass: ShaderPass | null = null;
  let opaqueDepthTarget: THREE.WebGLRenderTarget | null = null;
  let sceneColorTarget: THREE.WebGLRenderTarget | null = null;
  const opaqueDepthMaterial = new THREE.MeshDepthMaterial({
    depthPacking: THREE.BasicDepthPacking,
  });
  opaqueDepthMaterial.blending = THREE.NoBlending;
  const opaqueDepthSize = new THREE.Vector2(1, 1);
  const softDepthUvSize = new THREE.Vector2(1, 1);
  const sceneColorSize = new THREE.Vector2(1, 1);
  const distortionUvSize = new THREE.Vector2(1, 1);
  let lastSize = { w: 1, h: 1 };
  let postprocessEnabled = postprocess;

  const applyTonemapParams = () => {
    if (!gtPass) return;
    const u = gtPass.uniforms;
    u.uExposure.value = tonemapParams.exposure;
    u.uContrast.value = tonemapParams.contrast;
    u.uLinearStart.value = tonemapParams.linearStart;
    u.uLinearLength.value = tonemapParams.linearLength;
    u.uBlack.value = tonemapParams.black;
  };

  const buildComposer = () => {
    if (composer) return;
    // MSAA-capable target so bloom doesn't kill the edge AA that the
    // default framebuffer's antialias:true gives. samples=0 on WebGL1
    // (maxSamples reports 0) → silently falls back to no MSAA.
    const samples = Math.min(4, renderer.capabilities.maxSamples);
    const target = new THREE.WebGLRenderTarget(lastSize.w, lastSize.h, {
      type: THREE.HalfFloatType,
      samples,
    });
    composer = new EffectComposer(renderer, target);
    composer.setSize(lastSize.w, lastSize.h);
    composer.addPass(new RenderPass(scene, camera));
    bloomPass = new UnrealBloomPass(
      new THREE.Vector2(lastSize.w, lastSize.h),
      bloomParams.strength,
      bloomParams.radius,
      bloomParams.threshold,
    );
    bloomPass.enabled = bloomEnabled;
    composer.addPass(bloomPass);
    // GT (Uchimura) tonemap + sRGB OETF — final, screen-bound pass. The
    // scene reaches it as linear HDR (Three's tonemapper is disabled), so
    // this pass alone owns the color transform. Replaces OutputPass.
    gtPass = new ShaderPass(GTTonemapShader);
    gtPass.renderToScreen = true;
    composer.addPass(gtPass);
    applyTonemapParams();
  };

  const collectSoftDepthConsumers = (): THREE.ShaderMaterial[] => {
    const consumers = new Set<THREE.ShaderMaterial>();
    scene.traverse((object) => {
      if (!object.visible) return;
      for (const mat of objectMaterials(object)) {
        if (!materialHasSoftParticleUniform(mat)) continue;
        const scale = Number(mat.uniforms.uSoftParticleDepthScale?.value ?? 0);
        if (Number.isFinite(scale) && scale > 0) consumers.add(mat);
      }
    });
    return [...consumers];
  };

  const collectDistortionConsumers = (): THREE.ShaderMaterial[] => {
    const consumers = new Set<THREE.ShaderMaterial>();
    scene.traverse((object) => {
      if (!object.visible) return;
      for (const mat of objectMaterials(object)) {
        if (materialIsDistortionParticle(mat)) consumers.add(mat);
      }
    });
    return [...consumers];
  };

  const ensureOpaqueDepthTarget = () => {
    renderer.getDrawingBufferSize(opaqueDepthSize);
    const width = Math.max(1, Math.floor(opaqueDepthSize.x));
    const height = Math.max(1, Math.floor(opaqueDepthSize.y));
    if (opaqueDepthTarget && opaqueDepthTarget.width === width && opaqueDepthTarget.height === height) {
      return opaqueDepthTarget;
    }
    opaqueDepthTarget?.dispose();
    opaqueDepthTarget = new THREE.WebGLRenderTarget(width, height, {
      minFilter: THREE.NearestFilter,
      magFilter: THREE.NearestFilter,
      generateMipmaps: false,
      stencilBuffer: false,
      depthBuffer: true,
    });
    opaqueDepthTarget.texture.name = 'WowsOpaqueDepthColorDiscard';
    opaqueDepthTarget.depthTexture = new THREE.DepthTexture(width, height, THREE.UnsignedIntType);
    opaqueDepthTarget.depthTexture.name = 'WowsOpaqueDepthCopy';
    opaqueDepthTarget.depthTexture.format = THREE.DepthFormat;
    opaqueDepthTarget.depthTexture.minFilter = THREE.NearestFilter;
    opaqueDepthTarget.depthTexture.magFilter = THREE.NearestFilter;
    return opaqueDepthTarget;
  };

  const ensureSceneColorTarget = () => {
    renderer.getDrawingBufferSize(sceneColorSize);
    const width = Math.max(1, Math.floor(sceneColorSize.x));
    const height = Math.max(1, Math.floor(sceneColorSize.y));
    if (sceneColorTarget && sceneColorTarget.width === width && sceneColorTarget.height === height) {
      return sceneColorTarget;
    }
    sceneColorTarget?.dispose();
    sceneColorTarget = new THREE.WebGLRenderTarget(width, height, {
      type: THREE.HalfFloatType,
      minFilter: THREE.LinearFilter,
      magFilter: THREE.LinearFilter,
      generateMipmaps: false,
      stencilBuffer: false,
      depthBuffer: true,
    });
    sceneColorTarget.texture.name = 'WowsDistortionSceneColor';
    return sceneColorTarget;
  };

  const renderOpaqueDepthSnapshot = (consumers: THREE.ShaderMaterial[]) => {
    if (consumers.length === 0) return;
    const target = ensureOpaqueDepthTarget();
    const hidden: THREE.Object3D[] = [];
    scene.traverse((object) => {
      if (shouldHideForOpaqueDepth(object)) {
        hidden.push(object);
        object.visible = false;
      }
    });
    const previousTarget = renderer.getRenderTarget();
    const previousOverride = scene.overrideMaterial;
    try {
      scene.overrideMaterial = opaqueDepthMaterial;
      renderer.setRenderTarget(target);
      renderer.clear(true, true, true);
      renderer.render(scene, camera);
    } finally {
      renderer.setRenderTarget(previousTarget);
      scene.overrideMaterial = previousOverride;
      for (const object of hidden) object.visible = true;
    }

    softDepthUvSize.set(target.width, target.height);
    for (const mat of consumers) {
      const uniforms = mat.uniforms;
      if (uniforms.uSoftDepthTexture) uniforms.uSoftDepthTexture.value = target.depthTexture;
      if (uniforms.uSoftDepthSize?.value instanceof THREE.Vector2) {
        uniforms.uSoftDepthSize.value.copy(softDepthUvSize);
      }
      if (uniforms.uSoftCameraNear) uniforms.uSoftCameraNear.value = camera.near;
      if (uniforms.uSoftCameraFar) uniforms.uSoftCameraFar.value = camera.far;
    }
  };

  const renderDistortionSceneColor = (consumers: THREE.ShaderMaterial[]) => {
    if (consumers.length === 0) return;
    const target = ensureSceneColorTarget();
    const hidden: THREE.Object3D[] = [];
    scene.traverse((object) => {
      if (shouldHideForDistortionSource(object)) {
        hidden.push(object);
        object.visible = false;
      }
    });
    const previousTarget = renderer.getRenderTarget();
    try {
      renderer.setRenderTarget(target);
      renderer.clear(true, true, true);
      renderer.render(scene, camera);
    } finally {
      renderer.setRenderTarget(previousTarget);
      for (const object of hidden) object.visible = true;
    }

    distortionUvSize.set(target.width, target.height);
    for (const mat of consumers) {
      const uniforms = mat.uniforms;
      if (uniforms.uDistortionSceneTexture) uniforms.uDistortionSceneTexture.value = target.texture;
      if (uniforms.uDistortionSceneSize?.value instanceof THREE.Vector2) {
        uniforms.uDistortionSceneSize.value.copy(distortionUvSize);
      }
    }
  };

  const applyBloomParams = () => {
    if (!bloomPass) return;
    bloomPass.strength = bloomParams.strength;
    bloomPass.radius = bloomParams.radius;
    bloomPass.threshold = bloomParams.threshold;
  };

  let disposed = false;
  return {
    scene,
    camera,
    renderer,
    controls,
    grid,
    axes,
    render() {
      if (waterPlane.visible) {
        // Ambient surface animation, independent of the sim transport so the
        // ocean keeps moving even when the effect is paused. Keep the sun in
        // sync with the scene key light so the glints match the lighting.
        waterUniforms.uTime.value = performance.now() * 0.001;
        waterUniforms.uSunDir.value.copy(sunDirectionState);
      }
      renderOpaqueDepthSnapshot(collectSoftDepthConsumers());
      renderDistortionSceneColor(collectDistortionConsumers());
      if (!postprocessEnabled && !bloomEnabled) {
        renderer.render(scene, camera);
        return;
      }
      if (!composer) buildComposer();
      composer!.render();
    },
    setPixelRatio(ratio: number) {
      renderer.setPixelRatio(Math.max(0.25, ratio));
      renderer.setSize(lastSize.w, lastSize.h, false);
      composer?.setSize(lastSize.w, lastSize.h);
      bloomPass?.setSize(lastSize.w, lastSize.h);
    },
    setPostprocessEnabled(on: boolean) {
      postprocessEnabled = on;
      if (on && !composer) buildComposer();
    },
    isPostprocessEnabled() {
      return postprocessEnabled;
    },
    setBloomEnabled(on: boolean) {
      bloomEnabled = on;
      if (!composer) buildComposer();
      if (bloomPass) bloomPass.enabled = on;
    },
    setBloomParams(p: Partial<BloomParams>) {
      if (p.strength !== undefined) bloomParams.strength = p.strength;
      if (p.radius !== undefined) bloomParams.radius = p.radius;
      if (p.threshold !== undefined) bloomParams.threshold = p.threshold;
      applyBloomParams();
    },
    isBloomEnabled() {
      return bloomEnabled;
    },
    getBloomParams() {
      return bloomParams;
    },
    setTonemapParams(p: Partial<GTTonemapParams>) {
      if (p.exposure !== undefined) tonemapParams.exposure = p.exposure;
      if (p.contrast !== undefined) tonemapParams.contrast = p.contrast;
      if (p.linearStart !== undefined) tonemapParams.linearStart = p.linearStart;
      if (p.linearLength !== undefined) tonemapParams.linearLength = p.linearLength;
      if (p.black !== undefined) tonemapParams.black = p.black;
      applyTonemapParams();
    },
    setSize(w: number, h: number) {
      lastSize = { w, h };
      composer?.setSize(w, h);
      bloomPass?.setSize(w, h);
    },
    setEnvironment(tex: THREE.Texture | null) {
      scene.environment = tex ?? envRT.texture;
    },
    setFillLights(hemisphere: number, directional?: number) {
      hemi.intensity = hemisphere;
      if (directional !== undefined) dir.intensity = directional;
    },
    setSunLight(opts: {
      direction?: THREE.Vector3;
      color?: THREE.ColorRepresentation;
      intensity?: number;
    }) {
      // DirectionalLight travels from `position` toward its target (origin),
      // so position along the to-sun direction makes the light shine "down"
      // from the sun. Magnitude is irrelevant (directional), so push it out.
      if (opts.direction) {
        sunDirectionState.copy(opts.direction);
        if (sunDirectionState.lengthSq() <= 1e-10) sunDirectionState.set(50, 80, 50);
        sunDirectionState.normalize();
        dir.position.copy(sunDirectionState).multiplyScalar(100);
      }
      if (opts.color !== undefined) dir.color.set(opts.color);
      if (opts.intensity !== undefined) dir.intensity = opts.intensity;
    },
    getSunLight() {
      return {
        direction: sunDirectionState.clone(),
        color: dir.color.clone(),
        intensity: dir.intensity,
      };
    },
    setBackground(color: number) {
      if (scene.background instanceof THREE.Color) {
        scene.background.set(color);
      } else {
        scene.background = new THREE.Color(color);
      }
    },
    setWaterPlaneVisible(on: boolean) {
      waterPlane.visible = on;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      controls.dispose();
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
      axes.geometry.dispose();
      (axes.material as THREE.Material).dispose();
      waterPlane.geometry.dispose();
      waterMaterial.dispose();
      envRT.dispose();
      pmrem.dispose();
      opaqueDepthMaterial.dispose();
      opaqueDepthTarget?.dispose();
      opaqueDepthTarget = null;
      sceneColorTarget?.dispose();
      sceneColorTarget = null;
      composer?.dispose();
      composer = null;
      bloomPass = null;
      gtPass = null;
      renderer.dispose();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
