// Reusable Three.js scene scaffolding. Shared by the ship viewer and
// (eventually) the library viewer; lift any duplicated setup here.
//
// `createSceneEnvironment` builds an empty scene with renderer, camera,
// orbit controls, IBL, lights, grid, and axes — everything needed to
// render *something*, but no domain-specific geometry. Callers append
// their own groups to `scene` and call `dispose()` when done.
//
// Optional post-FX (currently UnrealBloomPass + OutputPass) are built
// lazily on first `setBloomEnabled(true)` — viewers that never enable
// bloom don't pay the render-target cost.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

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
  /** Forwarded by the resize observer so the composer stays in sync. */
  setSize(width: number, height: number): void;
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
  /** Tone mapping exposure (default: 1.1). */
  exposure?: number;
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

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);

  const camera = new THREE.PerspectiveCamera(fov, 1, 0.1, far);
  camera.position.set(...cameraPosition);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = exposure;
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

  // ── Post-FX (lazy) ────────────────────────────────────────────────
  // Composer + UnrealBloomPass + OutputPass are built on first
  // setBloomEnabled(true). Viewers that never use bloom skip the cost
  // entirely. Current params + enabled flag are tracked here so the
  // viewer can call setBloomParams *before* enabling without losing
  // the values.
  let bloomEnabled = false;
  const bloomParams: BloomParams = { ...DEFAULT_BLOOM_PARAMS };
  let composer: EffectComposer | null = null;
  let bloomPass: UnrealBloomPass | null = null;
  let lastSize = { w: 1, h: 1 };

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
    composer.addPass(bloomPass);
    // OutputPass applies tone mapping + sRGB conversion in the composer
    // path; without it the composer output is linear and looks washed-out
    // since RenderPass renders into a linear half-float target.
    composer.addPass(new OutputPass());
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
      if (bloomEnabled && composer) {
        composer.render();
      } else {
        renderer.render(scene, camera);
      }
    },
    setBloomEnabled(on: boolean) {
      if (on && !composer) buildComposer();
      bloomEnabled = on;
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
    setSize(w: number, h: number) {
      lastSize = { w, h };
      composer?.setSize(w, h);
      bloomPass?.setSize(w, h);
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
      renderer.dispose();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
