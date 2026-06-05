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
  /** Forwarded by the resize observer so the composer stays in sync. */
  setSize(width: number, height: number): void;
  /** Replace the scene background color (e.g. to switch a particle
   *  inspector between a night sky and a daylit sky so occluding,
   *  alpha-blended smoke is actually visible). */
  setBackground(color: number): void;
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

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
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
  scene.add(hemi, dir);

  // Helpers — the ship is huge (~300 m) so the default grid scale
  // (100 m squares × 6 = 600 m) gives one-glance scale reading.
  const grid = new THREE.GridHelper(gridSize, gridDivisions, 0x445065, 0x2a2e38);
  (grid.material as THREE.Material).transparent = true;
  (grid.material as THREE.Material).opacity = 0.4;
  const axes = new THREE.AxesHelper(axesSize);
  scene.add(grid, axes);

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
  let lastSize = { w: 1, h: 1 };

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
      if (!composer) buildComposer();
      composer!.render();
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
      if (opts.direction) dir.position.copy(opts.direction).multiplyScalar(100);
      if (opts.color !== undefined) dir.color.set(opts.color);
      if (opts.intensity !== undefined) dir.intensity = opts.intensity;
    },
    setBackground(color: number) {
      if (scene.background instanceof THREE.Color) {
        scene.background.set(color);
      } else {
        scene.background = new THREE.Color(color);
      }
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      controls.dispose();
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
      axes.geometry.dispose();
      (axes.material as THREE.Material).dispose();
      envRT.dispose();
      pmrem.dispose();
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
