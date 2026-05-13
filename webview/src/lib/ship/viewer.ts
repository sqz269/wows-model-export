// ShipViewer: Three.js host that loads a ship's hull GLB and clones every
// accessory placement at its declared transform. Owns the texture
// pipeline + camo shader subsystem via the TextureManager.
//
// Lifecycle:
//   const viewer = new ShipViewer(container);
//   await viewer.loadShip(ship, library, onProgress);
//   ...user interaction (setShowTextures, setActiveSkin, …)
//   await viewer.dispose();
//
// `loadShip` is idempotent — calling it a second time disposes the
// previous ship and loads the new one. Accessory templates + decoded
// DDS textures survive across ship swaps; only `dispose()` releases them.

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

import { createSceneEnvironment, type SceneEnvironment } from '$lib/three/scene';
import { observeResize } from '$lib/three/resize';
import { startRenderLoop } from '$lib/three/render_loop';
import { disposeTree } from '$lib/three/dispose';
import { repoUrl } from '$lib/api';
import { SHIP_SECTIONS } from '$lib/types';
import type {
  LibraryIndex,
  SeamKey,
  SeamState,
  ShipPlacement,
  ShipPlacementsDoc,
  ShipSectionKey,
  ShipSummary,
  SidecarDoc,
  Skin,
} from '$lib/types';
import {
  type ColorMaterials,
  type ColorMode,
  type PlacementColorEntry,
  applyColorMode,
  createColorMaterials,
  disposeColorMaterials,
} from './color_mode';
import { applyAllStates } from './damage_cascade';
import { classifyHullScene, type ClassifiedHull } from './classify_hull';
import { defaultSeamStates } from './visibility';
import {
  applyAttachedMatrix,
  applyPlacementMatrix,
  tagAndIndexInstance,
  type LodPolicy,
} from './placement';
import { AccessoryCache } from './accessory_loader';
import { AttachedDocCache } from './attached_loader';
import { TextureManager } from './textures';
import { LOD_RE } from './visibility';

export interface ShipLoadStats {
  ship: ShipSummary;
  hullMeshCount: number;
  placementsRequested: number;
  placementsRendered: number;
  /** Attached children (WG-runtime-composed bundled miscs) rendered. */
  attachmentsRendered: number;
  /** Attached entries dropped by the per-HP miscFilter whitelist. */
  attachmentsFilteredByMisc: number;
  loadMs: number;
  unresolvedAssets: Map<string, number>;
  skinCount: number;
}

export class ShipViewer {
  private env: SceneEnvironment;
  private stopLoop: () => void;
  private stopResize: () => void;

  // Scene graph: shipRoot → { Hull, sections{turrets, ...} }
  private shipRoot: THREE.Group;
  private hullRoot: THREE.Object3D | null = null;
  private sectionGroups: Record<ShipSectionKey, THREE.Group>;

  // GLB loaders + accessory caches
  private hullLoader = new GLTFLoader();
  private accessoryCache = new AccessoryCache();
  private attachedDocCache = new AttachedDocCache();

  // Hull classification (rebuilt per ship)
  private classified: ClassifiedHull = {
    groups: [],
    renderersByMesh: new Map(),
    damageMeshes: [],
    lowLodMeshes: [],
  };

  // Placement tracking
  private placementsByMesh = new Map<string, THREE.Object3D[]>();
  private placementColorEntries: PlacementColorEntry[] = [];
  private placementLowLodMeshes: THREE.Object3D[] = [];

  // Color
  private colorMaterials: ColorMaterials;
  private colorMode: ColorMode = 'off';

  // Texture pipeline
  private textures: TextureManager;

  // Visibility state
  private seamStates: Record<SeamKey, SeamState> = defaultSeamStates();
  private lodPolicy: LodPolicy = 'lod0';
  private damageVariantsVisible = false;
  private helpersVisible = true;

  constructor(container: HTMLElement) {
    this.env = createSceneEnvironment(container);

    this.shipRoot = new THREE.Group();
    this.shipRoot.name = 'Ship';
    this.env.scene.add(this.shipRoot);

    this.sectionGroups = Object.fromEntries(
      SHIP_SECTIONS.map((k) => {
        const g = new THREE.Group();
        g.name = `Section.${k}`;
        this.shipRoot.add(g);
        return [k, g];
      }),
    ) as Record<ShipSectionKey, THREE.Group>;

    this.colorMaterials = createColorMaterials();

    this.textures = new TextureManager({
      renderer: this.env.renderer,
      onAccessoryMaterialSwap: (mesh, mat) => this.syncColorEntry(mesh, mat),
      onAfterTextureApply: () => {
        if (this.colorMode !== 'off') {
          applyColorMode(this.placementColorEntries, this.colorMode);
        }
      },
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

  async loadShip(
    ship: ShipSummary,
    library: LibraryIndex,
    onProgress?: (msg: string) => void,
  ): Promise<ShipLoadStats> {
    const t0 = performance.now();
    this.clearShip();
    const report = (msg: string) => onProgress?.(msg);

    report('Loading hull GLB…');
    const hullUrl = repoUrl(ship.hull_glb);
    // Resolve hull DDS paths against the hull GLB's directory (sidecar's
    // `texture_sets[<scheme>][<slot>].dds_mips` carry `textures_dds/...`).
    const hullBaseUrl = new URL(hullUrl, window.location.origin).toString();
    const hullGltf = await this.hullLoader.loadAsync(hullUrl);
    this.hullRoot = hullGltf.scene;
    this.hullRoot.name = 'Hull';
    this.shipRoot.add(this.hullRoot);

    this.classified = classifyHullScene(this.hullRoot, {
      hideLowLod: this.lodPolicy === 'lod0',
      hideDamageVariants: !this.damageVariantsVisible,
    });

    // Register every hull mesh in the texture pipeline. Material name
    // serves as the binding key (sidecar `materials[].material_id`
    // matches the glTF material.name).
    let hullMeshCount = 0;
    this.hullRoot.traverse((o) => {
      const m = o as THREE.Mesh;
      if (!m.isMesh) return;
      hullMeshCount++;
      this.textures.registerHullMesh(m);
    });

    // Fetch sidecar (best-effort). Drives hull material bindings,
    // skin table, variant-swap opt-out list.
    let sidecar: SidecarDoc | null = null;
    if (ship.sidecar_json) {
      try {
        const res = await fetch(repoUrl(ship.sidecar_json));
        if (res.ok) {
          sidecar = (await res.json()) as SidecarDoc;
          this.textures.bindHullMaterials(sidecar, hullBaseUrl);
          this.textures.setSkinTable(sidecar.skins ?? []);
        }
      } catch (err) {
        console.warn('[ship] sidecar fetch failed:', err);
      }
    }

    report('Loading placements…');
    const placementsRes = await fetch(repoUrl(ship.accessories_json));
    if (!placementsRes.ok) {
      throw new Error(`failed to load placements: HTTP ${placementsRes.status}`);
    }
    const placementsDoc = (await placementsRes.json()) as ShipPlacementsDoc;

    // Build the per-HP miscFilter lookup from sidecar mounts. WG runtime
    // treats miscFilter as a WHITELIST (verified 2026-05-08 from
    // MiscsController._getMiscsForLoading) — three states:
    //   - undefined         → render every attached_live (no filter info)
    //   - []                → drop every non-isStyle attachment (gates
    //                          Halloween/Skin bleed-through like JD124
    //                          → MP_XM410_Skin_Director on base ships)
    //   - [<placement_id>…] → render only listed entries
    // Keyed by instance_id since hp_name is only unique within a typed
    // group, not across them.
    const miscFilterByInstanceId = new Map<string, string[]>();
    if (sidecar) {
      const groups: (typeof sidecar.turrets)[] = [
        sidecar.turrets,
        sidecar.secondaries,
        sidecar.antiair,
        sidecar.torpedoes,
        sidecar.accessories,
      ];
      for (const grp of groups) {
        if (!grp) continue;
        for (const m of grp) {
          if (!m.instance_id) continue;
          if (m.misc_filter !== undefined) {
            miscFilterByInstanceId.set(m.instance_id, m.misc_filter);
          }
        }
      }
    }

    // Flatten typed sections into one queue.
    const queue: { section: ShipSectionKey; placement: ShipPlacement }[] = [];
    for (const section of SHIP_SECTIONS) {
      for (const p of placementsDoc[section] ?? []) {
        queue.push({ section, placement: p });
      }
    }

    // Group by asset_id so we fetch + parse each unique GLB once.
    const byAsset = new Map<string, { section: ShipSectionKey; placement: ShipPlacement }[]>();
    for (const e of queue) {
      const list = byAsset.get(e.placement.asset_id) ?? [];
      list.push(e);
      byAsset.set(e.placement.asset_id, list);
    }

    let loadedAssets = 0;
    let renderedPlacements = 0;
    let attachmentsRendered = 0;
    let attachmentsFilteredByMisc = 0;
    const unresolved = new Map<string, number>();
    const tasks = Array.from(byAsset.entries());
    let cursor = 0;

    const worker = async (): Promise<void> => {
      while (cursor < tasks.length) {
        const idx = cursor++;
        const [assetId, places] = tasks[idx];
        const libEntry = library.assets[assetId];

        if (!libEntry) {
          unresolved.set(assetId, places.length);
          loadedAssets++;
          report(
            `Loaded ${loadedAssets}/${tasks.length} types · ${renderedPlacements} placements · ${attachmentsRendered} attached`,
          );
          continue;
        }

        // Load host template + attached_accessories.json in parallel.
        // For hosts without a bundle (~most assets) the doc resolves to
        // null; the inner loop short-circuits.
        const [tpl, attachedDoc] = await Promise.all([
          this.accessoryCache.load(libEntry.glb),
          this.attachedDocCache.load(libEntry),
        ]);
        if (!tpl) {
          unresolved.set(assetId, places.length);
          loadedAssets++;
          report(
            `Loaded ${loadedAssets}/${tasks.length} types · ${renderedPlacements} placements · ${attachmentsRendered} attached`,
          );
          continue;
        }

        // Bind host's texture sets before cloning so registerAccessoryMesh
        // sees populated schemes.
        this.textures.bindLibraryAsset(assetId, libEntry, sidecar, hullBaseUrl);

        // Pre-warm every distinct attached-child template + bind its
        // texture sets. The accessoryCache dedupes across hosts so a
        // child bundled by N main turrets is fetched once. Resolved
        // templates are stashed locally so the per-placement loop below
        // can clone them synchronously.
        const attachedChildTpls = new Map<string, THREE.Object3D | null>();
        if (attachedDoc && attachedDoc.attachments_live.length > 0) {
          const childIds = new Set<string>();
          for (const att of attachedDoc.attachments_live) childIds.add(att.asset_id);
          const childPromises = Array.from(childIds).map(async (cid) => {
            const childLib = library.assets[cid];
            if (!childLib) {
              attachedChildTpls.set(cid, null);
              unresolved.set(cid, (unresolved.get(cid) ?? 0) + 1);
              return;
            }
            this.textures.bindLibraryAsset(cid, childLib, sidecar, hullBaseUrl);
            const tpl = await this.accessoryCache.load(childLib.glb);
            attachedChildTpls.set(cid, tpl);
          });
          await Promise.all(childPromises);
        }

        for (const e of places) {
          const inst = tpl.clone(true);
          applyPlacementMatrix(inst, e.placement.transform.matrix);
          const { colorEntries, lowLodMeshes } = tagAndIndexInstance(
            inst,
            {
              section: e.section,
              placement: e.placement,
              colorMaterials: this.colorMaterials,
              lodPolicy: this.lodPolicy,
            },
            this.placementsByMesh,
          );
          this.placementColorEntries.push(...colorEntries);
          this.placementLowLodMeshes.push(...lowLodMeshes);
          inst.traverse((obj) => {
            const m = obj as THREE.Mesh;
            if (!m.isMesh) return;
            this.textures.registerAccessoryMesh(m, e.placement);
          });
          this.sectionGroups[e.section].add(inst);
          renderedPlacements++;

          // Attached accessories. Resolve HP-side miscFilter (sidecar
          // Phase 6 autofill takes precedence over any value the
          // placements JSON might carry).
          if (attachedDoc && attachedDoc.attachments_live.length > 0) {
            const filterList: string[] | null =
              miscFilterByInstanceId.get(e.placement.instance_id) ??
              e.placement.misc_filter ??
              null;
            const filterSet = filterList && filterList.length > 0 ? new Set(filterList) : null;
            const dropAll = filterList !== null && filterList.length === 0;

            for (const att of attachedDoc.attachments_live) {
              if (dropAll) {
                attachmentsFilteredByMisc++;
                continue;
              }
              if (filterSet !== null && !filterSet.has(att.placement_id)) {
                attachmentsFilteredByMisc++;
                continue;
              }
              const childTpl = attachedChildTpls.get(att.asset_id);
              if (!childTpl) continue;

              const childInst = childTpl.clone(true);
              applyAttachedMatrix(childInst, att.transform.matrix);
              childInst.userData.attached_to_instance_id = e.placement.instance_id;
              childInst.userData.attached_placement_id = att.placement_id;
              childInst.userData.attached_asset_id = att.asset_id;
              childInst.userData.section = e.section;
              inst.add(childInst);

              // Build a per-child placement so camo classification reads
              // the child's own scope/category (catches misc/plane/float
              // routing for catapults, rangefinders, ammo boxes).
              const childLib = library.assets[att.asset_id];
              const childPlacement: ShipPlacement = {
                ...e.placement,
                asset_id: att.asset_id,
                scope: childLib?.scope ?? e.placement.scope,
                category: childLib?.category ?? e.placement.category,
                subcategory: childLib?.subcategory ?? e.placement.subcategory,
              };
              childInst.traverse((obj) => {
                const cm = obj as THREE.Mesh;
                if (!cm.isMesh) return;
                if (LOD_RE.test(cm.name || '')) {
                  this.placementLowLodMeshes.push(cm);
                  if (this.lodPolicy === 'lod0') cm.visible = false;
                }
                this.textures.registerAccessoryMesh(cm, childPlacement);
              });
              attachmentsRendered++;
            }
          }
        }

        loadedAssets++;
        report(
          `Loaded ${loadedAssets}/${tasks.length} types · ${renderedPlacements} placements · ${attachmentsRendered} attached`,
        );
      }
    };

    const PARALLEL = 8;
    await Promise.all(Array.from({ length: PARALLEL }, () => worker()));

    // Apply current color mode to all freshly-loaded placements.
    if (this.colorMode !== 'off') {
      applyColorMode(this.placementColorEntries, this.colorMode);
    }
    // Apply current visibility state across hull + cascaded placements.
    this.applyAllStates();

    const loadMs = performance.now() - t0;
    report(`Loaded in ${(loadMs / 1000).toFixed(1)}s.`);

    return {
      ship,
      hullMeshCount,
      placementsRequested: queue.length,
      placementsRendered: renderedPlacements,
      attachmentsRendered,
      attachmentsFilteredByMisc,
      loadMs,
      unresolvedAssets: unresolved,
      skinCount: this.textures.getSkins().length,
    };
  }

  /**
   * Per-ship cleanup. Drops the hull, empties section groups, clears
   * tracking maps, resets seam state. Accessory templates SURVIVE so
   * the next ship swap can reuse them; only `dispose()` releases them.
   */
  clearShip(): void {
    if (this.hullRoot) {
      this.shipRoot.remove(this.hullRoot);
      disposeTree(this.hullRoot);
      this.hullRoot = null;
    }
    for (const k of SHIP_SECTIONS) {
      const g = this.sectionGroups[k];
      // Don't dispose the cloned tree's geometry — accessory clones share
      // refs with the template (still in `accessoryCache`); disposing
      // would leave subsequent clones with freed buffers.
      while (g.children.length) g.remove(g.children[0]);
    }
    this.classified = {
      groups: [],
      renderersByMesh: new Map(),
      damageMeshes: [],
      lowLodMeshes: [],
    };
    this.placementsByMesh.clear();
    this.placementColorEntries.length = 0;
    this.placementLowLodMeshes.length = 0;
    this.seamStates = defaultSeamStates();
    this.textures.clearShip();
    this.attachedDocCache.clear();
  }

  setSectionVisible(section: ShipSectionKey, visible: boolean): void {
    this.sectionGroups[section].visible = visible;
  }

  setHullGroupVisible(name: string, visible: boolean): void {
    const g = this.classified.groups.find((x) => x.name === name);
    if (g) g.node.visible = visible;
  }

  setLodPolicy(p: LodPolicy): void {
    this.lodPolicy = p;
    if (p === 'all') {
      for (const m of this.classified.lowLodMeshes) m.visible = true;
      for (const m of this.placementLowLodMeshes) m.visible = true;
    } else {
      for (const m of this.classified.lowLodMeshes) m.visible = false;
      for (const m of this.placementLowLodMeshes) m.visible = false;
    }
    this.applyAllStates();
  }

  setDamageVariantsVisible(show: boolean): void {
    this.damageVariantsVisible = show;
    for (const m of this.classified.damageMeshes) m.visible = show;
    this.applyAllStates();
  }

  setColorMode(mode: ColorMode): void {
    this.colorMode = mode;
    applyColorMode(this.placementColorEntries, mode);
  }

  setSeamState(seam: SeamKey, state: SeamState): void {
    this.seamStates[seam] = state;
    this.applyAllStates();
  }

  resetSeamStates(): void {
    this.seamStates = defaultSeamStates();
    this.applyAllStates();
  }

  setHelpers(show: boolean): void {
    this.helpersVisible = show;
    this.env.grid.visible = show;
    this.env.axes.visible = show;
  }

  // ── Texture pipeline (delegated) ──────────────────────────────────────

  async setShowTextures(on: boolean, onProgress?: (msg: string) => void): Promise<void> {
    await this.textures.setShowTextures(on, onProgress);
  }

  async setActiveSkin(skinId: string, onProgress?: (msg: string) => void): Promise<void> {
    await this.textures.setActiveSkin(skinId, onProgress);
  }

  setAoEnabled(on: boolean): void {
    this.textures.setAoEnabled(on);
  }

  setMrMapEnabled(on: boolean): void {
    this.textures.setMrMapEnabled(on);
  }

  setPreserveUnderwaterHull(on: boolean): void {
    this.textures.setPreserveUnderwaterHull(on);
  }

  getSkins(): readonly Skin[] {
    return this.textures.getSkins();
  }

  getActiveSkinId(): string | null {
    return this.textures.getActiveSkinId();
  }

  isShowingTextures(): boolean {
    return this.textures.isShowingTextures();
  }

  getAoEnabled(): boolean {
    return this.textures.getAoEnabled();
  }

  getMrMapEnabled(): boolean {
    return this.textures.getMrMapEnabled();
  }

  getPreserveUnderwater(): boolean {
    return this.textures.getPreserveUnderwater();
  }

  // ── Read-only state ───────────────────────────────────────────────────

  getHullGroups(): readonly string[] {
    return this.classified.groups.map((g) => g.name);
  }

  getSeamStates(): Readonly<Record<SeamKey, SeamState>> {
    return this.seamStates;
  }

  getLodPolicy(): LodPolicy {
    return this.lodPolicy;
  }

  getDamageVariantsVisible(): boolean {
    return this.damageVariantsVisible;
  }

  getHelpersVisible(): boolean {
    return this.helpersVisible;
  }

  getColorMode(): ColorMode {
    return this.colorMode;
  }

  async dispose(): Promise<void> {
    this.stopLoop();
    this.stopResize();
    this.clearShip();
    this.textures.dispose();
    await this.accessoryCache.dispose();
    disposeColorMaterials(this.colorMaterials);
    this.env.dispose();
  }

  // ── Internals ─────────────────────────────────────────────────────────

  private applyAllStates(): void {
    applyAllStates({
      hullRenderersByMesh: this.classified.renderersByMesh,
      placementsByMesh: this.placementsByMesh,
      hullLowLodMeshes: this.classified.lowLodMeshes,
      placementLowLodMeshes: this.placementLowLodMeshes,
      hullDamageMeshes: this.classified.damageMeshes,
      seamStates: this.seamStates,
      lodPolicy: this.lodPolicy,
      damageVariantsVisible: this.damageVariantsVisible,
    });
  }

  /**
   * Mirror a texture material swap onto the placement-color-entry's
   * `originalMaterial` so colorMode='off' picks up the textured variant
   * (otherwise toggling away from category-color mode would snap back
   * to untextured).
   */
  private syncColorEntry(mesh: THREE.Mesh, mat: THREE.Material | THREE.Material[]): void {
    const ce = this.placementColorEntries.find((x) => x.mesh === mesh);
    if (ce) ce.originalMaterial = mat;
  }
}
