// AccessoryViewer: Three.js host for a single accessory GLB. Lighter than
// ShipViewer — no placement composer, no damage cascade, no camo shader.
// One GLB at a time, auto-fitted camera + grid + axes.
//
// Lifecycle:
//   const viewer = new AccessoryViewer(container);
//   const result = await viewer.loadGlb(url);
//   ...user interaction (setSide, setWireframe, setLodFilter, …)
//   viewer.dispose();
//
// Re-loading replaces the previous root (disposes geometry + materials).

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

import { createSceneEnvironment, type SceneEnvironment } from '$lib/three/scene';
import { observeResize } from '$lib/three/resize';
import { startRenderLoop } from '$lib/three/render_loop';

export type SideMode = 'front' | 'back' | 'double';

export interface MeshInfo {
  /** glTF node name. Two LOD-suffix flavours observed: `<base>_lod<N>Shape`
   *  (standard) and `<base>_lodShape<N>` (anomalous, ~30% of library). */
  name: string;
  /** Guessed LOD level from the name suffix — 0 for base/high-res. */
  lod: number;
  triangles: number;
  visible: boolean;
}

export interface LoadResult {
  bounds: THREE.Box3;
  meshes: MeshInfo[];
}

interface TrackedMesh {
  mesh: THREE.Mesh;
  info: MeshInfo;
}

export class AccessoryViewer {
  private env: SceneEnvironment;
  private stopLoop: () => void;
  private stopResize: () => void;
  private loader = new GLTFLoader();

  private root: THREE.Object3D | null = null;
  private tracked: TrackedMesh[] = [];
  private bounds = new THREE.Box3();

  // Material state applied to every loaded mesh.
  private state = {
    side: 'double' as SideMode,
    wireframe: false,
    lodFilter: null as number | null,
  };

  private helpersVisible = true;

  constructor(container: HTMLElement) {
    // Default container bg is a touch lighter than ShipViewer so single
    // accessories pop against the page panel.
    this.env = createSceneEnvironment(container, {
      background: 0x12151b,
      cameraPosition: [4, 3, 4],
      gridSize: 8,
      gridDivisions: 16,
      axesSize: 1,
    });

    this.stopResize = observeResize({
      container,
      renderer: this.env.renderer,
      camera: this.env.camera,
    });

    this.stopLoop = startRenderLoop(() => {
      this.env.controls.update();
      this.env.renderer.render(this.env.scene, this.env.camera);
    });
  }

  // ── Public API ────────────────────────────────────────────────────────

  async loadGlb(url: string): Promise<LoadResult> {
    this.disposeRoot();
    const gltf = await this.loader.loadAsync(url);
    const r = gltf.scene;
    this.env.scene.add(r);
    this.root = r;

    r.traverse((obj) => {
      const m = obj as THREE.Mesh;
      if (!m.isMesh) return;
      const name = m.name || '(unnamed)';
      this.tracked.push({
        mesh: m,
        info: {
          name,
          lod: guessLod(name),
          triangles: meshTriangleCount(m),
          visible: true,
        },
      });
    });

    this.applyMaterialState();
    this.applyLodFilter();
    this.bounds = new THREE.Box3().setFromObject(r);
    this.frame();

    return {
      bounds: this.bounds.clone(),
      meshes: this.tracked.map((t) => t.info),
    };
  }

  /** Re-fit the camera to the current model's bounds. Idempotent. */
  frame(): void {
    if (!this.root) return;
    const size = new THREE.Vector3();
    this.bounds.getSize(size);
    const center = new THREE.Vector3();
    this.bounds.getCenter(center);

    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dist = maxDim * 1.8;
    this.env.camera.position.set(center.x + dist, center.y + dist * 0.7, center.z + dist);
    this.env.camera.near = maxDim * 0.01;
    this.env.camera.far = maxDim * 50;
    this.env.camera.updateProjectionMatrix();
    this.env.controls.target.copy(center);
    this.env.controls.update();

    // Scale axes + center grid under model. Grid stays at default size — the
    // scene env's grid lives at world origin; we move axes onto the bottom
    // of the bounds box for visual anchoring.
    this.env.axes.scale.setScalar(maxDim * 0.5);
    this.env.axes.position.copy(center).setY(this.bounds.min.y);
  }

  setSide(mode: SideMode): void {
    this.state.side = mode;
    this.applyMaterialState();
  }

  setWireframe(on: boolean): void {
    this.state.wireframe = on;
    this.applyMaterialState();
  }

  setHelpers(show: boolean): void {
    this.helpersVisible = show;
    this.env.grid.visible = show;
    this.env.axes.visible = show;
  }

  /** Show only meshes whose `MeshInfo.lod === lod`; or all if null. */
  setLodFilter(lod: number | null): void {
    this.state.lodFilter = lod;
    this.applyLodFilter();
  }

  setMeshVisibleByIndex(index: number, visible: boolean): void {
    const t = this.tracked[index];
    if (!t) return;
    t.info.visible = visible;
    t.mesh.visible = visible && this.passesLod(t.info.lod);
  }

  getHelpersVisible(): boolean {
    return this.helpersVisible;
  }

  getSide(): SideMode {
    return this.state.side;
  }

  getWireframe(): boolean {
    return this.state.wireframe;
  }

  getLodFilter(): number | null {
    return this.state.lodFilter;
  }

  dispose(): void {
    this.stopLoop();
    this.stopResize();
    this.disposeRoot();
    this.env.dispose();
  }

  // ── Internals ─────────────────────────────────────────────────────────

  private disposeRoot(): void {
    if (!this.root) return;
    this.env.scene.remove(this.root);
    this.root.traverse((obj) => {
      const m = obj as THREE.Mesh;
      if (!m.isMesh) return;
      m.geometry?.dispose();
      const mat = m.material;
      if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
      else if (mat) (mat as THREE.Material).dispose();
    });
    this.root = null;
    this.tracked = [];
  }

  private applyMaterialState(): void {
    for (const t of this.tracked) {
      t.mesh.material = cloneWithOverrides(originalMaterialOf(t.mesh), this.state);
    }
  }

  private applyLodFilter(): void {
    for (const t of this.tracked) {
      t.mesh.visible = t.info.visible && this.passesLod(t.info.lod);
    }
  }

  private passesLod(lod: number): boolean {
    return this.state.lodFilter === null || this.state.lodFilter === lod;
  }
}

// ── Free helpers ────────────────────────────────────────────────────────

function meshTriangleCount(m: THREE.Mesh): number {
  const g = m.geometry;
  if (!g) return 0;
  if (g.index) return Math.floor(g.index.count / 3);
  const pos = g.getAttribute('position');
  return pos ? Math.floor(pos.count / 3) : 0;
}

function guessLod(name: string): number {
  // Two WG naming conventions:
  //   `<base>_lod<N>Shape`  (standard)
  //   `<base>_lodShape<N>`  (anomalous — Maya shape-node rename swapped
  //                          Shape/<N>; ~30% of the accessory library).
  const lower = name.toLowerCase();
  const m = lower.match(/_lod(?:shape)?(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}

function sideValue(m: SideMode): THREE.Side {
  if (m === 'front') return THREE.FrontSide;
  if (m === 'back') return THREE.BackSide;
  return THREE.DoubleSide;
}

/**
 * Snapshot the GLTF-loaded material on first call so re-clones go back
 * to the canonical source rather than compounding clones.
 */
function originalMaterialOf(mesh: THREE.Mesh): THREE.Material | THREE.Material[] {
  const any = mesh as unknown as { __origMat?: THREE.Material | THREE.Material[] };
  if (!any.__origMat) any.__origMat = mesh.material;
  return any.__origMat;
}

function cloneWithOverrides(
  original: THREE.Material | THREE.Material[],
  state: { side: SideMode; wireframe: boolean },
): THREE.Material | THREE.Material[] {
  const apply = (m: THREE.Material): THREE.Material => {
    const c = m.clone();
    c.side = sideValue(state.side);
    if ('wireframe' in c) (c as unknown as { wireframe: boolean }).wireframe = state.wireframe;
    return c;
  };
  if (Array.isArray(original)) return original.map(apply);
  return apply(original);
}
