// AccessoryViewer: Three.js host for a single accessory GLB. Lighter than
// ShipViewer — no placement composer, no damage cascade — but shares the
// same TextureManager so library renders look like in-context ship renders.
//
// Lifecycle:
//   const viewer = new AccessoryViewer(container);
//   const result = await viewer.loadGlb(url, libContext?);
//   ...user interaction (setSide, setWireframe, setLodFilter, …)
//   viewer.dispose();
//
// Re-loading replaces the previous root (disposes geometry + materials).

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

import { createSceneEnvironment, type SceneEnvironment } from '$lib/three/scene';
import { observeResize } from '$lib/three/resize';
import { startRenderLoop } from '$lib/three/render_loop';
import { TextureManager } from '$lib/ship/textures';
import type { LibraryAsset, ShipPlacement } from '$lib/types';

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

/**
 * Library-asset context for a textured render. When passed, the viewer
 * wires the asset's texture sets into the TextureManager and triggers a
 * texture-on render. Omit for untextured-only previews.
 *
 * `variant` picks the scheme key inside `texture_sets` — `'main'` for the
 * intact GLB, `'dead'` when the destroyed-variant GLB is loaded. Missing
 * slots fall back to `main` inside the manager, so a partial dead scheme
 * (e.g. only baseColor overridden) still renders correctly.
 */
export interface LibraryContext {
  assetId: string;
  asset: LibraryAsset;
  variant?: 'main' | 'dead';
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
  private textures: TextureManager;

  private root: THREE.Object3D | null = null;
  private tracked: TrackedMesh[] = [];
  private bounds = new THREE.Box3();

  // First-touch clone marker — accessory GLBs occasionally share a
  // material across meshes; cloning on first mutation prevents a
  // side/wireframe toggle from leaking into a sibling.
  private clonedMaterials = new WeakSet<THREE.Material>();

  // Material state applied to every loaded mesh.
  private state = {
    side: 'double' as SideMode,
    wireframe: false,
    lodFilter: null as number | null,
  };

  private texturesOn = true;
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

    // Reuse the ship-side texture pipeline: same DDS decoder, same camo
    // shader chunks, same per-material binding. The library viewer has
    // no skin table (no per-camo scheme overrides) — the manager
    // defaults to the `main` scheme which is what every accessory ships
    // with anyway.
    this.textures = new TextureManager({
      renderer: this.env.renderer,
      onAccessoryMaterialSwap: (_mesh, mat) => {
        // TextureManager replaces the material with a textured clone.
        // Re-apply side/wireframe so the user's toggles survive the swap.
        for (const m of asArray(mat)) {
          m.side = sideValue(this.state.side);
          if ('wireframe' in m) (m as { wireframe: boolean }).wireframe = this.state.wireframe;
        }
      },
      onAfterTextureApply: () => this.applyLodFilter(),
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

  /**
   * Load a GLB, optionally with a library-asset context that drives the
   * DDS texture pipeline. Omit `lib` to preview an untextured GLB
   * (kept for the dead-variant toggle when no `<asset_id>_dead.*.dds`
   * sidecar files are present).
   */
  async loadGlb(url: string, lib?: LibraryContext | null): Promise<LoadResult> {
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

    // Initial material setup: clone-on-first-mutate, then apply side /
    // wireframe in place. The TextureManager (if `lib` is provided) will
    // clone again for textured variants; the cloned-once marker prevents
    // the GLB's source material from being mutated.
    this.applyMaterialState();
    this.applyLodFilter();
    this.bounds = new THREE.Box3().setFromObject(r);
    this.frame();

    if (lib) {
      // Register + bind on every load regardless of `texturesOn` — the
      // `disposeRoot` above clears the manager's entries, and a later
      // `setShowTextures(true)` would otherwise find nothing to texture.
      // The final flip-on is gated inside `applyLibraryTextures`.
      await this.applyLibraryTextures(lib);
    }

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

  /** Toggle the DDS texture pipeline. Off shows the GLB's bundled
   *  materials only (flat-shaded WG defaults). */
  async setShowTextures(on: boolean): Promise<void> {
    this.texturesOn = on;
    await this.textures.setShowTextures(on);
  }

  isShowingTextures(): boolean {
    return this.texturesOn;
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
    this.textures.dispose();
    this.env.dispose();
  }

  // ── Internals ─────────────────────────────────────────────────────────

  private async applyLibraryTextures(lib: LibraryContext): Promise<void> {
    // Synthesise the minimum ShipPlacement shape registerAccessoryMesh
    // reads: asset_id (binding key), category + subcategory (camo-class).
    // The other fields aren't touched by the library code path.
    const synth = {
      asset_id: lib.assetId,
      category: lib.asset.category,
      subcategory: lib.asset.subcategory,
    } as unknown as ShipPlacement;
    for (const t of this.tracked) {
      this.textures.registerAccessoryMesh(t.mesh, synth);
    }
    // No sidecar → no skin overrides → asset-level `main` scheme wins.
    // Empty `hullBaseUrl` is unused on this path (texture paths inside
    // libEntry resolve against the libEntry.glb location).
    this.textures.bindLibraryAsset(lib.assetId, lib.asset, null, '');
    this.textures.setActiveSchemeKey(lib.variant ?? 'main');
    // Flip textures on only when the user wants them — registration
    // above survives across toggles so a later setShowTextures(true)
    // finds populated entries.
    if (this.texturesOn) await this.textures.setShowTextures(true);
  }

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
    // Drop the per-load TextureManager state too — schemes / decoded
    // textures the previous asset bound are no longer relevant. Resets
    // the bind index without disposing the manager itself.
    this.textures.clearShip();
    this.clonedMaterials = new WeakSet();
  }

  private applyMaterialState(): void {
    for (const t of this.tracked) {
      this.ensureCloned(t.mesh);
      for (const mat of asArray(t.mesh.material)) {
        mat.side = sideValue(this.state.side);
        if ('wireframe' in mat) (mat as { wireframe: boolean }).wireframe = this.state.wireframe;
      }
    }
  }

  /**
   * Clone the GLB's source material on first mutation so side/wireframe
   * tweaks don't leak between meshes that share a material. Idempotent
   * — second call sees the cloned-once marker and short-circuits.
   */
  private ensureCloned(mesh: THREE.Mesh): void {
    if (Array.isArray(mesh.material)) {
      mesh.material = mesh.material.map((m) => this.cloneOnce(m));
    } else if (mesh.material) {
      mesh.material = this.cloneOnce(mesh.material);
    }
  }

  private cloneOnce(m: THREE.Material): THREE.Material {
    if (this.clonedMaterials.has(m)) return m;
    const c = m.clone();
    this.clonedMaterials.add(c);
    return c;
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

function asArray<T>(x: T | T[]): T[] {
  return Array.isArray(x) ? x : [x];
}

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
