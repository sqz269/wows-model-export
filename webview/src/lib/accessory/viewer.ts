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
import { maybeApplyBoneFrameFix } from '$lib/ship/bone_frame_fix';
import { TextureManager } from '$lib/ship/textures';
import type {
  LibraryAsset,
  PieceInfo,
  RigCategory,
  RigPivots,
  ShipPlacement,
} from '$lib/types';

import { DebugSceneController, type DebugSceneLoadResult } from './debug_scene';
import { RigPivotOverlay } from './rig_pivots';
import { flipWindingIndex, resetWindingIndex } from './winding';

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

export interface BoneInfo {
  /** glTF node name (e.g. `Rotate_X`, `Rotate_X_BlendBone`, `Roll_Back1`,
   *  `HP_gunFire1`). */
  name: string;
  /** Bind-pose world translation captured at load time. Does not move
   *  when the user rotates other bones — this is the rest position. */
  worldPosition: { x: number; y: number; z: number };
  /** True for `THREE.Bone` instances (members of a `Skin.joints` palette).
   *  False for plain `Object3D` pivot empties like `Rotate_X` itself,
   *  which sit above the joints. */
  isSkinJoint: boolean;
  /** True if the node has any children — distinguishes leaf hardpoints
   *  (typically `HP_*` markers) from internal pivot bones. */
  hasChildren: boolean;
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
  /**
   * Sub-directory under `<workspace>/libraries/` where this asset's
   * GLB + DDS textures live. Defaults to `'accessories'` so the
   * existing Library + Ships pages keep working unchanged. Set to
   * `'projectiles'` from the Projectiles route.
   */
  libraryRoot?: string;
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
    flipWinding: false,
  };

  private texturesOn = true;
  private helpersVisible = true;

  // Rig pivot overlay. Lives in the scene from construction; visibility
  // and contents are mutated via setRigPivots / setRigPivotsVisible.
  // Living in the scene (rather than per-load) means the flip-180°
  // toggle keeps its rotation across asset swaps.
  private rigOverlay: RigPivotOverlay;

  // Debug-scene picker. Lazily attaches DOM listeners on first
  // setPickerMode call so we don't pay the cost on assets that never
  // open the rig editor.
  private debugScene: DebugSceneController | null = null;

  /** True while a `<asset>.rig.debug.glb` is loaded (vs a regular GLB).
   *  Used so `setFlipWinding` etc. don't fight the picker's material
   *  state. */
  private debugSceneActive = false;

  // ── Bone inspector state ──────────────────────────────────────────────
  // Populated on every `loadGlb` so the Bones tab can list nodes + their
  // bind-pose world positions, and apply per-bone rotation sliders. Cleared
  // in `disposeRoot`. The rest-quat map is lazy: a bone enters it only on
  // the first slider move so we don't pay the per-frame quaternion-clone
  // cost on assets where the user doesn't touch any sliders.
  private boneNodes = new Map<string, THREE.Object3D>();
  private boneInfo: BoneInfo[] = [];
  private boneRestQuats = new Map<string, THREE.Quaternion>();
  private boneMarker: THREE.Mesh | null = null;

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

    this.rigOverlay = new RigPivotOverlay();
    this.env.scene.add(this.rigOverlay.group);

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

    // Bone-frame-mismatch fix (see lib/ship/bone_frame_fix.ts). Runs
    // before the bone snapshot so the BoneInspector shows world positions
    // in the corrected frame — pinning Rotate_X lands on the trunnion
    // instead of the top-back of the housing for affected assets like
    // AGS145. No-op for the 99% of assets whose bones already align with
    // their verts. Same call is wired into the ship-view rig extractor
    // (lib/ship/turret_rig.ts) so both viewers see consistent positions.
    maybeApplyBoneFrameFix(r);

    // Snapshot the rig bone tree: every named node + its bind-pose world
    // position. World transforms aren't current until updateMatrixWorld
    // propagates, so force a pass before capturing. The Bones tab in the
    // bottom panel reads `boneInfo` directly — sorted lexicographically
    // here so the UI doesn't need to re-sort on every render.
    r.updateMatrixWorld(true);
    const seen = new Set<string>();
    const collected: BoneInfo[] = [];
    r.traverse((obj) => {
      if (!obj.name || seen.has(obj.name)) return;
      seen.add(obj.name);
      this.boneNodes.set(obj.name, obj);
      const p = new THREE.Vector3();
      obj.getWorldPosition(p);
      collected.push({
        name: obj.name,
        worldPosition: { x: p.x, y: p.y, z: p.z },
        isSkinJoint: (obj as THREE.Bone).isBone === true,
        hasChildren: obj.children.length > 0,
      });
    });
    collected.sort((a, b) => a.name.localeCompare(b.name));
    this.boneInfo = collected;

    // Initial material setup: clone-on-first-mutate, then apply side /
    // wireframe in place. The TextureManager (if `lib` is provided) will
    // clone again for textured variants; the cloned-once marker prevents
    // the GLB's source material from being mutated.
    this.applyMaterialState();
    this.applyLodFilter();
    // Re-apply the persistent flip-winding toggle to freshly-loaded
    // meshes. The setting outlives individual loads, so a user who
    // flipped winding on asset A and clicks asset B expects B to come
    // up already flipped.
    if (this.state.flipWinding) {
      for (const t of this.tracked) flipWindingIndex(t.mesh);
    }
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

  /** Reverse triangle winding live — preview the persisted-flip effect
   *  without touching the GLB on disk. Toggle off restores the original
   *  index buffer. Persisting is a separate API call. */
  setFlipWinding(on: boolean): void {
    this.state.flipWinding = on;
    for (const t of this.tracked) {
      if (on) flipWindingIndex(t.mesh);
      else resetWindingIndex(t.mesh);
    }
  }

  getFlipWinding(): boolean {
    return this.state.flipWinding;
  }

  // ── Rig pivots overlay ───────────────────────────────────────────────

  /** Replace the rig-pivot markers from a freshly-fetched
   *  `<asset>.rig_pivots.json`. Pass `null` to clear. Marker sizes
   *  scale to the current load's bbox. */
  setRigPivots(pivots: RigPivots | null): void {
    this.rigOverlay.setPivots(pivots, this.bounds);
  }

  setRigPivotsVisible(show: boolean): void {
    this.rigOverlay.setVisible(show);
  }

  /** Rotate the overlay 180° around the yaw axis. Local-only — the
   *  pivot JSON on disk is unchanged. */
  setRigFlip180(on: boolean): void {
    this.rigOverlay.setFlip180(on);
  }

  // ── Bone inspector ────────────────────────────────────────────────────

  /** Snapshot of every named node in the loaded GLB with its bind-pose
   *  world position. Sorted alphabetically. Empty until `loadGlb`
   *  completes. World positions are captured at load time and do NOT
   *  update when the user rotates bones — they represent the rest
   *  geometry. */
  getBones(): BoneInfo[] {
    return this.boneInfo;
  }

  /** Apply an Euler rotation (radians, XYZ order) to the named bone
   *  relative to its rest pose. The first call for a given bone snaps
   *  the rest quaternion; subsequent calls re-compose `rest × R(x,y,z)`
   *  so dragging a slider doesn't accumulate. No-op if the name isn't
   *  in the current bone map. */
  setBoneEuler(name: string, x: number, y: number, z: number): void {
    const node = this.boneNodes.get(name);
    if (!node) return;
    let rest = this.boneRestQuats.get(name);
    if (!rest) {
      rest = node.quaternion.clone();
      this.boneRestQuats.set(name, rest);
    }
    const e = new THREE.Euler(x, y, z, 'XYZ');
    const q = new THREE.Quaternion().setFromEuler(e);
    node.quaternion.multiplyQuaternions(rest, q);
  }

  /** Restore a single bone to its rest pose. Idempotent. */
  resetBone(name: string): void {
    const node = this.boneNodes.get(name);
    const rest = this.boneRestQuats.get(name);
    if (!node || !rest) return;
    node.quaternion.copy(rest);
    this.boneRestQuats.delete(name);
  }

  /** Restore every bone the user has rotated. Cheap — only iterates
   *  bones that were actually touched. */
  resetAllBones(): void {
    for (const [name, rest] of this.boneRestQuats) {
      const node = this.boneNodes.get(name);
      if (node) node.quaternion.copy(rest);
    }
    this.boneRestQuats.clear();
  }

  /** Pin a small marker sphere at the bind-pose world position of the
   *  named bone — useful for visually verifying where a pivot sits
   *  relative to the geometry. Pass `null` to hide. The marker reads
   *  from the snapshot in `boneInfo`, so it tracks rest, not runtime
   *  motion. Marker radius scales to the model bounds. */
  highlightBone(name: string | null): void {
    if (!name) {
      if (this.boneMarker) this.boneMarker.visible = false;
      return;
    }
    const info = this.boneInfo.find((b) => b.name === name);
    if (!info) return;
    if (!this.boneMarker) {
      const size = new THREE.Vector3();
      this.bounds.getSize(size);
      const radius = Math.max(size.x, size.y, size.z, 1) * 0.015;
      const geom = new THREE.SphereGeometry(radius, 16, 12);
      const mat = new THREE.MeshBasicMaterial({
        color: 0xff3366,
        depthTest: false,
        transparent: true,
        opacity: 0.85,
      });
      this.boneMarker = new THREE.Mesh(geom, mat);
      this.boneMarker.renderOrder = 999;
      this.env.scene.add(this.boneMarker);
    }
    this.boneMarker.position.set(info.worldPosition.x, info.worldPosition.y, info.worldPosition.z);
    this.boneMarker.visible = true;
  }

  // ── Debug-scene picker (rig editor) ──────────────────────────────────

  /** Load a `<asset>.rig.debug.glb` produced by the rigger's
   *  --debug-scene path. Replaces the current root, sets up picker
   *  tracking, and returns the per-piece info the editor renders. */
  async loadDebugSceneGlb(url: string): Promise<DebugSceneLoadResult> {
    this.disposeRoot();
    this.rigOverlay.setPivots(null, this.bounds);
    const gltf = await this.loader.loadAsync(url);
    const r = gltf.scene;
    this.env.scene.add(r);
    this.root = r;

    const ctrl = this.ensureDebugSceneController();
    const pieces = ctrl.ingestRoot(r);
    this.debugSceneActive = true;

    this.bounds = new THREE.Box3().setFromObject(r);
    this.frame();
    return { bounds: this.bounds.clone(), pieces };
  }

  setPickerMode(enabled: boolean): void {
    const ctrl = this.ensureDebugSceneController();
    ctrl.setPickerMode(enabled);
  }

  onPiecePicked(handler: ((piece: PieceInfo) => void) | null): void {
    const ctrl = this.ensureDebugSceneController();
    ctrl.onPiecePicked(handler);
  }

  setPieceCategoryColor(idx: number, category: RigCategory | 'face' | 'auto'): void {
    this.debugScene?.setPieceCategoryColor(idx, category);
  }

  setSelectedPiece(idx: number | null): void {
    this.debugScene?.setSelectedPiece(idx);
  }

  /** True while a debug scene is loaded (vs a regular asset GLB).
   *  Useful for the rig editor's "exit" path — caller can ask before
   *  reloading the regular GLB. */
  isDebugSceneActive(): boolean {
    return this.debugSceneActive;
  }

  private ensureDebugSceneController(): DebugSceneController {
    if (this.debugScene) return this.debugScene;
    const canvas = this.env.renderer.domElement;
    this.debugScene = new DebugSceneController(canvas, this.env.camera);
    this.debugScene.attach();
    return this.debugScene;
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
    this.rigOverlay.dispose();
    this.debugScene?.dispose();
    this.debugScene = null;
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
    this.textures.bindLibraryAsset(
      lib.assetId, lib.asset, null, '', lib.libraryRoot ?? 'accessories',
    );
    this.textures.setActiveSchemeKey(lib.variant ?? 'main');
    // Flip textures on only when the user wants them — registration
    // above survives across toggles so a later setShowTextures(true)
    // finds populated entries.
    if (this.texturesOn) await this.textures.setShowTextures(true);
  }

  private disposeRoot(): void {
    if (this.debugSceneActive) {
      // The debug-scene controller owns the materials + geometries for
      // its pieces; let it clean them up rather than the generic walk
      // below (which would double-dispose).
      this.debugScene?.disposePieces();
      if (this.root) {
        this.env.scene.remove(this.root);
      }
      this.root = null;
      this.tracked = [];
      this.debugSceneActive = false;
      this.clonedMaterials = new WeakSet();
      return;
    }
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
    this.boneNodes.clear();
    this.boneInfo = [];
    this.boneRestQuats.clear();
    if (this.boneMarker) {
      this.env.scene.remove(this.boneMarker);
      this.boneMarker.geometry.dispose();
      (this.boneMarker.material as THREE.Material).dispose();
      this.boneMarker = null;
    }
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
