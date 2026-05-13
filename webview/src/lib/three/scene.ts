// Reusable Three.js scene scaffolding. Shared by the ship viewer and
// (eventually) the library viewer; lift any duplicated setup here.
//
// `createSceneEnvironment` builds an empty scene with renderer, camera,
// orbit controls, IBL, lights, grid, and axes — everything needed to
// render *something*, but no domain-specific geometry. Callers append
// their own groups to `scene` and call `dispose()` when done.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export interface SceneEnvironment {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  renderer: THREE.WebGLRenderer;
  controls: OrbitControls;
  grid: THREE.GridHelper;
  axes: THREE.AxesHelper;
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

  let disposed = false;
  return {
    scene,
    camera,
    renderer,
    controls,
    grid,
    axes,
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
      renderer.dispose();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
