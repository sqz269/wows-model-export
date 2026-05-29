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

import { createSceneEnvironment, type BloomParams, type SceneEnvironment } from '$lib/three/scene';
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
import { TextureManager, type CamoDiagnostics } from './textures';
import { lodLevelOfName } from './visibility';
import {
  cloneAccessoryInstance,
  extractTurretRig,
  TurretRigManager,
  type MountArcLimits,
  type TurretRig,
} from './turret_rig';
import {
  applyArmorView,
  buildArmorEntries,
  createArmorXrayMaterial,
  disposeArmorView,
  mountArmorThicknessOf,
  type ArmorMeshEntry,
} from './armor_view';
import {
  applyHitboxView,
  disposeHitboxView,
  prepareHitboxMeshes,
  type HitboxMeshEntry,
} from './hitbox_view';

/**
 * Info resolved from `userData` on the clicked accessory instance. The
 * pipeline stamps these in `tagAndIndexInstance` (live placements) and
 * the attached-children loop in `loadShip` (bundled child meshes).
 */
export interface PickedAssetInfo {
  /** The accessory root (the node carrying `asset_id` in userData). */
  root: THREE.Object3D;
  /** Library asset_id (joins to `LibraryIndex.assets`). */
  asset_id: string;
  /** Typed section the placement belongs to. */
  section: string | null;
  /** Sidecar instance_id (unique within a ship). May be missing on
   *  attached children — those carry `attached_placement_id` instead. */
  instance_id: string | null;
  /** Hull section the placement is anchored to. */
  parent_section: string | null;
  /** Parent hull mesh name (drives the damage-state cascade). */
  parent_mesh: string | null;
  /** For attached children: the host's instance_id. */
  attached_to_instance_id: string | null;
  /** For attached children: WG-runtime placement_id within the bundle. */
  attached_placement_id: string | null;
}

export interface PickResult {
  object: THREE.Object3D;
  point: THREE.Vector3;
  distance: number;
  info: PickedAssetInfo;
}

const pickRaycaster = new THREE.Raycaster();
const pickPointer = new THREE.Vector2();

function isVisibleChain(o: THREE.Object3D): boolean {
  let n: THREE.Object3D | null = o;
  while (n) {
    if (!n.visible) return false;
    n = n.parent;
  }
  return true;
}

/**
 * Walk up the parent chain from the raycaster's hit until we find a
 * node stamped with `userData.asset_id`. Returns null if we walk off
 * the top without finding one (hull mesh hits, helper hits, etc.).
 */
function resolveAssetUserData(start: THREE.Object3D): PickedAssetInfo | null {
  let n: THREE.Object3D | null = start;
  while (n) {
    const ud = n.userData;
    // Accessory clones carry `asset_id`. Attached children carry both
    // `attached_asset_id` (set by the loadShip attached loop) and inherit
    // none of the placement's other ids; we prefer the more-specific
    // attached_asset_id if present so the user sees the rangefinder /
    // ammo box rather than the turret host.
    if (typeof ud.attached_asset_id === 'string') {
      return {
        root: n,
        asset_id: ud.attached_asset_id,
        section: typeof ud.section === 'string' ? ud.section : null,
        instance_id: null,
        parent_section: null,
        parent_mesh: null,
        attached_to_instance_id:
          typeof ud.attached_to_instance_id === 'string' ? ud.attached_to_instance_id : null,
        attached_placement_id:
          typeof ud.attached_placement_id === 'string' ? ud.attached_placement_id : null,
      };
    }
    if (typeof ud.asset_id === 'string') {
      return {
        root: n,
        asset_id: ud.asset_id,
        section: typeof ud.section === 'string' ? ud.section : null,
        instance_id: typeof ud.instance_id === 'string' ? ud.instance_id : null,
        parent_section: typeof ud.parent_section === 'string' ? ud.parent_section : null,
        parent_mesh: typeof ud.parent_mesh === 'string' ? ud.parent_mesh : null,
        attached_to_instance_id: null,
        attached_placement_id: null,
      };
    }
    n = n.parent;
  }
  return null;
}

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

/** Per-mount armor record — a turret/secondary's own armor meshes (from the
 *  accessory GLB's `Armor` group), kept hidden until the armor view reveals
 *  them. When the mount is rigged, the meshes are attached to the yaw bone so
 *  they rotate with the turret; thickness comes from the ship's per-mount
 *  `mount_armor[hp]`. */
interface MountArmorRecord {
  /** Owning hardpoint (e.g. `HP_AGM_1`) for the `mount_armor[hp]` lookup. */
  hp: string | null;
  /** The accessory's armor meshes (under its `Armor` group). */
  meshes: THREE.Mesh[];
  /** The mount's rig, when present — armor attaches to `rig.yaw`. */
  rig: TurretRig | null;
  /** Built lazily on first armor-view enable (per-instance coloured + cloned). */
  entries: ArmorMeshEntry[];
}

/** Detach an accessory's `Armor` group from its parent so the normal
 *  texture / color / LOD registration skips it. Returns the group + meshes;
 *  the caller re-adds the group (hidden) after registration so the armor
 *  rides the placement transform and can later attach to the yaw bone. */
function detachAccessoryArmor(
  inst: THREE.Object3D,
): { parent: THREE.Object3D; group: THREE.Object3D; meshes: THREE.Mesh[] } | null {
  const groups: THREE.Object3D[] = [];
  inst.traverse((o) => {
    if (o.name === 'Armor') groups.push(o);
  });
  const group = groups[0];
  if (!group || !group.parent) return null;
  const meshes: THREE.Mesh[] = [];
  group.traverse((o) => {
    if ((o as THREE.Mesh).isMesh) meshes.push(o as THREE.Mesh);
  });
  const parent = group.parent;
  parent.remove(group);
  return { parent, group, meshes };
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
    meshesByLodLevel: new Map(),
  };

  // Placement tracking
  private placementsByMesh = new Map<string, THREE.Object3D[]>();
  private placementColorEntries: PlacementColorEntry[] = [];
  /** Per-level placement meshes — merged from each placement's
   *  `TagResult.meshesByLodLevel` so the cascade can filter by level. */
  private placementMeshesByLodLevel = new Map<number, THREE.Object3D[]>();
  /** Per-instance turret rigs (yaw / pitch bones) keyed by instance_id.
   *  Populated as accessory placements are cloned; only assets whose
   *  source `.visual` had a bone tree register a rig here. UI consumes
   *  this through `getTurretRigManager()`. */
  private turretRigs = new TurretRigManager();

  // Color
  private colorMaterials: ColorMaterials;
  private colorMode: ColorMode = 'off';

  // Armor + hitbox overlays. Lazy-prepared on first enable (reads the
  // hull GLB's Armor/Hitboxes groups + sidecar tables), reset per ship.
  private armorEntries: ArmorMeshEntry[] | null = null;
  private armorMaterial: THREE.MeshStandardMaterial | null = null;
  private armorViewEnabled = false;
  /** Per-mount (turret/secondary) armor, collected at load, revealed in the
   *  armor view. The X-ray material is shared with hull armor. */
  private mountArmorRecords: MountArmorRecord[] = [];
  /** instance_id → hardpoint name, from the sidecar mounts. Joins a placement
   *  to its `mount_armor[hp]` thickness table. */
  private hpByInstanceId = new Map<string, string>();
  private hitboxEntries: HitboxMeshEntry[] | null = null;
  private hitboxViewEnabled = false;

  // Texture pipeline
  private textures: TextureManager;

  // Last-loaded sidecar JSON. Stashed for the bottom panel's Textures
  // tab — the texture pipeline already parses it; rather than re-fetch
  // we just hold onto the parsed value here. Null between ship swaps.
  private sidecar: SidecarDoc | null = null;
  /** Base URL the sidecar's relative texture paths resolve against
   *  (i.e. the hull GLB's directory). The DDS preview tab needs this
   *  to build absolute URLs the same way the texture manager does. */
  private hullBaseUrl: string | null = null;

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
      onResize: (w, h) => this.env.setSize(w, h),
    });

    this.stopLoop = startRenderLoop(() => {
      this.env.controls.update();
      this.env.render();
    });

    // Expose for in-browser debugging.
    if (typeof window !== 'undefined') {
      (window as unknown as { __wowsShipViewer__?: unknown }).__wowsShipViewer__ = this;
    }
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
        }
      } catch (err) {
        console.warn('[ship] sidecar fetch failed:', err);
      }
    }
    // Stash for the bottom panel's Textures tab. Done after the texture
    // manager binding so a binding failure doesn't strand a partially-
    // loaded sidecar on the viewer.
    this.sidecar = sidecar;
    this.hullBaseUrl = hullBaseUrl;
    // Always populate a skin table — synthesize the legacy default skin
    // when the sidecar is missing or empty. Keeps the active scheme key
    // pinned to `main` and gives callers iterating `getSkins()` at least
    // one entry (matches the pre-refactor behaviour).
    const skins: Skin[] = sidecar?.skins?.length
      ? sidecar.skins
      : [{ skin_id: 'default', display_name: 'Standard', scheme_key: 'main', overrides: [] }];
    this.textures.setSkinTable(skins);

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
    // Per-mount firing-arc limits (yaw/elev range + no-fire dead zones),
    // keyed by instance_id. Sourced from the sidecar's gameplay autofill —
    // the render-source placements doc (accessories.json) doesn't carry them.
    const arcLimitsByInstanceId = new Map<string, MountArcLimits>();
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
          if (m.hp_name) this.hpByInstanceId.set(m.instance_id, m.hp_name);
          if (m.misc_filter !== undefined) {
            miscFilterByInstanceId.set(m.instance_id, m.misc_filter);
          }
          if (m.yaw_range_deg || m.elev_range_deg || m.yaw_dead_zones_deg) {
            // WG wrap-encodes some stern mounts as [min, max] with min > max
            // (e.g. [210, 150] == [-150, 150]; Baltimore's [202, 0] == [-158, 0]).
            // Re-express the wrapped min as a negative so a plain min/max clamp
            // + the fan see a normal contiguous range.
            let yawRange = m.yaw_range_deg;
            if (yawRange && yawRange[0] > yawRange[1]) {
              yawRange = [yawRange[0] - 360, yawRange[1]];
            }
            arcLimitsByInstanceId.set(m.instance_id, {
              yawRangeDeg: yawRange,
              elevRangeDeg: m.elev_range_deg,
              yawDeadZonesDeg: m.yaw_dead_zones_deg,
            });
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
          // Deep-clone with SkeletonUtils for skinned templates so each
          // placement has its own bones — sharing a Skeleton would tie
          // every turret's yaw to the same `Rotate_Y` instance.
          const inst = cloneAccessoryInstance(tpl);
          applyPlacementMatrix(inst, e.placement.transform.matrix);
          // Pull the accessory's own armor (turret/secondary `Armor` group)
          // out before registration so it isn't textured / colored / LOD-
          // bucketed as normal turret geometry. Re-added hidden below.
          const armorDetached = detachAccessoryArmor(inst);
          const { colorEntries, meshesByLodLevel } = tagAndIndexInstance(
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
          for (const [level, meshes] of meshesByLodLevel) {
            let bucket = this.placementMeshesByLodLevel.get(level);
            if (!bucket) {
              bucket = [];
              this.placementMeshesByLodLevel.set(level, bucket);
            }
            bucket.push(...meshes);
          }
          inst.traverse((obj) => {
            const m = obj as THREE.Mesh;
            if (!m.isMesh) return;
            this.textures.registerAccessoryMesh(m, e.placement);
          });
          // Look for WG rig nodes (`Rotate_Y` / `Rotate_X`). Most
          // gun/main and gun/secondary mounts have them; AA and static
          // miscs return null silently.
          const arcLimits = arcLimitsByInstanceId.get(e.placement.instance_id) ?? null;
          const rig = extractTurretRig(
            inst,
            e.placement.asset_id,
            e.placement.instance_id,
            arcLimits,
          );
          if (rig) {
            this.turretRigs.register(rig);
          } else if (arcLimits?.yawRangeDeg) {
            // Static mount with a traverse arc — torpedo tubes are static
            // meshes (no Rotate_Y bone), so draw their firing arc without
            // animating them.
            this.turretRigs.registerStaticArc(e.placement.instance_id, inst, arcLimits);
          }
          // Re-attach the detached armor (hidden) so it rides the placement
          // transform, and record it for the armor view. Rigged mounts get
          // their armor attached to the yaw bone on first reveal so it rotates
          // with the turret.
          if (armorDetached && armorDetached.meshes.length > 0) {
            armorDetached.parent.add(armorDetached.group);
            // Hide per-mesh (not the group): the armor view toggles mesh
            // visibility, and rigged mounts move their meshes out of the group
            // onto the yaw bone — so group-level visibility wouldn't gate them.
            for (const m of armorDetached.meshes) m.visible = false;
            this.mountArmorRecords.push({
              hp: this.hpByInstanceId.get(e.placement.instance_id) ?? null,
              meshes: armorDetached.meshes,
              rig,
              entries: [],
            });
          }
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

              // Attached children (catapults, rangefinders, etc.) are
              // typically static, but use the skin-aware cloner anyway so
              // the rare rigged child (e.g. some director mounts) gets
              // its own bones.
              const childInst = cloneAccessoryInstance(childTpl);
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
                const level = lodLevelOfName(cm.name || '');
                let bucket = this.placementMeshesByLodLevel.get(level);
                if (!bucket) {
                  bucket = [];
                  this.placementMeshesByLodLevel.set(level, bucket);
                }
                bucket.push(cm);
                if (level > 0 && this.lodPolicy === 'lod0') cm.visible = false;
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
      meshesByLodLevel: new Map(),
    };
    // Armor + hitbox overlays. The hull GLB owns the per-mesh geometry +
    // original materials (released by disposeTree above); we own the shared
    // X-ray material + any per-instance cloned mount-armor geometry. Edge
    // LineSegments rode the hull tree too.
    if (this.armorEntries) disposeArmorView(this.armorEntries, null);
    for (const rec of this.mountArmorRecords) disposeArmorView(rec.entries, null);
    this.armorMaterial?.dispose();
    this.armorEntries = null;
    this.armorMaterial = null;
    this.armorViewEnabled = false;
    this.mountArmorRecords = [];
    this.hpByInstanceId.clear();
    if (this.hitboxEntries) {
      disposeHitboxView(this.hitboxEntries);
    }
    this.hitboxEntries = null;
    this.hitboxViewEnabled = false;
    this.placementsByMesh.clear();
    this.placementColorEntries.length = 0;
    this.placementMeshesByLodLevel.clear();
    this.seamStates = defaultSeamStates();
    this.textures.clearShip();
    this.attachedDocCache.clear();
    this.turretRigs.clear();
    this.sidecar = null;
    this.hullBaseUrl = null;
  }

  /** Per-instance turret rigs (yaw + pitch bones extracted from each
   *  placed accessory). Lets the UI drive aim globally or per-section. */
  getTurretRigManager(): TurretRigManager {
    return this.turretRigs;
  }

  setSectionVisible(section: ShipSectionKey, visible: boolean): void {
    this.sectionGroups[section].visible = visible;
  }

  setHullGroupVisible(name: string, visible: boolean): void {
    const g = this.classified.groups.find((x) => x.name === name);
    if (g) g.node.visible = visible;
  }

  // ── Armor + hitbox overlays ───────────────────────────────────────────

  /** True when the loaded ship has an `Armor` group + a sidecar thickness
   *  table — i.e. the armor heat-map can be rendered. */
  hasArmorData(): boolean {
    return (
      this.classified.groups.some((g) => g.name === 'Armor') &&
      !!this.sidecar?.armor?.materials_table
    );
  }

  /** True when the loaded ship has a `Hitboxes` group. Box tinting degrades
   *  gracefully (flat "Other" colour) when the sidecar `hitbox` is absent. */
  hasHitboxData(): boolean {
    return this.classified.groups.some((g) => g.name === 'Hitboxes');
  }

  /** Toggle the per-vertex armor-thickness heat-map across the hull `Armor`
   *  group AND each turret/secondary mount's own armor. Mount armor is
   *  thickness-coloured from the ship's `mount_armor[hp]` and, when the mount
   *  is rigged, attached to its yaw bone so it rotates with the turret. The
   *  X-ray material is shared by hull + mount armor. */
  setArmorView(on: boolean): void {
    if (on) this.armorMaterial ??= createArmorXrayMaterial();
    const materialsTable = this.sidecar?.armor?.materials_table ?? {};

    // Hull armor.
    const group = this.classified.groups.find((g) => g.name === 'Armor');
    if (group) {
      if (on && !this.armorEntries) {
        const meshes: THREE.Mesh[] = [];
        group.node.traverse((o) => {
          if ((o as THREE.Mesh).isMesh) meshes.push(o as THREE.Mesh);
        });
        // `mountArmorThicknessOf(undefined, table)` is just the materials_table
        // lookup — hull armor has unique geometry, so no clone.
        this.armorEntries = buildArmorEntries(
          meshes,
          mountArmorThicknessOf(undefined, materialsTable),
        );
      }
      if (this.armorEntries && this.armorMaterial) {
        applyArmorView(this.armorEntries, this.armorMaterial, on);
      }
      group.node.visible = on;
    }

    // Per-mount (turret / secondary) armor. Lazy-prepare on first reveal:
    // colour from mount_armor[hp], clone the shared template geometry, and
    // attach rigged mounts' armor to the yaw bone so it rotates with the gun.
    //
    // `attach()` preserves world transform, so the rig must be at REST when we
    // re-parent — otherwise armor authored at rest would bind relative to an
    // already-aimed yaw and drift. Reset to rest around the one-time attach,
    // then restore the user's aim (which now also drives the attached armor).
    const needsPrep = on && this.mountArmorRecords.some((r) => r.entries.length === 0);
    const savedAim = needsPrep ? this.turretRigs.getGlobalAim() : null;
    if (needsPrep) {
      this.turretRigs.reset();
      this.shipRoot.updateMatrixWorld(true);
    }
    for (const rec of this.mountArmorRecords) {
      if (on && rec.entries.length === 0) {
        const thicknessOf = mountArmorThicknessOf(
          this.sidecar?.armor?.mount_armor?.[rec.hp ?? ''],
          materialsTable,
        );
        rec.entries = buildArmorEntries(rec.meshes, thicknessOf, { cloneGeometry: true });
        const yaw = rec.rig?.yaw;
        if (yaw) {
          for (const m of rec.meshes) yaw.attach(m);
        }
      }
      if (rec.entries.length > 0 && this.armorMaterial) {
        applyArmorView(rec.entries, this.armorMaterial, on);
      }
      for (const m of rec.meshes) m.visible = on;
    }
    if (savedAim) this.turretRigs.setGlobalAim(savedAim.yaw, savedAim.pitch);

    this.armorViewEnabled = on;
  }

  /** Toggle the translucent hitbox / damage-module overlay. */
  setHitboxView(on: boolean): void {
    const group = this.classified.groups.find((g) => g.name === 'Hitboxes');
    if (!group) return;
    if (on && !this.hitboxEntries) {
      const boxes = this.sidecar?.hitbox?.boxes ?? {};
      this.hitboxEntries = prepareHitboxMeshes(group.node, boxes);
    }
    this.hitboxViewEnabled = on;
    if (this.hitboxEntries) {
      applyHitboxView(this.hitboxEntries, on);
    }
    group.node.visible = on;
  }

  /**
   * Hide the ship's *visual* meshes (hull group + every placed accessory mesh)
   * so the armor / hitbox overlays read clearly. Hides the visual meshes, NOT
   * the section groups — the per-mount armor now lives inside those groups
   * (attached to the yaw bones), so hiding the group would hide the armor too.
   * The hull `Armor` group + mount armor stay visible. Restoring re-runs the
   * visibility cascade.
   */
  setArmorOnly(hide: boolean): void {
    const hull = this.classified.groups.find((g) => g.name === 'Hull');
    if (hull) hull.node.visible = !hide;
    if (hide) {
      for (const meshes of this.placementMeshesByLodLevel.values()) {
        for (const m of meshes) m.visible = false;
      }
    } else {
      this.applyAllStates();
    }
  }

  getArmorViewEnabled(): boolean {
    return this.armorViewEnabled;
  }

  getHitboxViewEnabled(): boolean {
    return this.hitboxViewEnabled;
  }

  setLodPolicy(p: LodPolicy): void {
    this.lodPolicy = p;
    // applyAllStates owns the visibility cascade now — it walks
    // meshesByLodLevel and forces visible=false on every level that
    // doesn't match the policy. No need to pre-toggle here.
    this.applyAllStates();
  }

  /** Sorted ascending list of LOD levels present on the currently
   *  loaded ship (hull + placements combined). Always contains 0 when
   *  any mesh is loaded. Drives the ShipControls LOD dropdown. */
  getAvailableLodLevels(): number[] {
    const levels = new Set<number>();
    for (const level of this.classified.meshesByLodLevel.keys()) levels.add(level);
    for (const level of this.placementMeshesByLodLevel.keys()) levels.add(level);
    return [...levels].sort((a, b) => a - b);
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

  // ── Post-FX (bloom) ───────────────────────────────────────────────────

  setBloomEnabled(on: boolean): void {
    this.env.setBloomEnabled(on);
  }

  setBloomParams(p: Partial<BloomParams>): void {
    this.env.setBloomParams(p);
  }

  getBloomEnabled(): boolean {
    return this.env.isBloomEnabled();
  }

  getBloomParams(): Readonly<BloomParams> {
    return this.env.getBloomParams();
  }

  // ── Camera helpers ────────────────────────────────────────────────────

  /**
   * Reset the camera to the scene default. Mirrors the initial camera
   * setup in `createSceneEnvironment` so the user can always "get back
   * to a known view" with one keystroke (default keybind: R).
   */
  resetCamera(): void {
    this.env.camera.position.set(80, 50, 80);
    this.env.controls.target.set(0, 0, 0);
    this.env.controls.update();
  }

  /**
   * Frame the camera on a specific Object3D (or the whole ship if null).
   * Computes a bounding sphere, drops the controls target on the centre,
   * and pulls the camera back along its current view direction by ~2× the
   * radius (with a small floor for tiny objects). Doesn't tween — for a
   * dev tool the instant snap reads as "applied" without a wait.
   */
  frameOn(target: THREE.Object3D | null): void {
    const obj = target ?? this.shipRoot;
    const box = new THREE.Box3().setFromObject(obj);
    if (box.isEmpty()) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const radius = Math.max(sphere.radius, 1);
    const dir = new THREE.Vector3()
      .subVectors(this.env.camera.position, this.env.controls.target)
      .normalize();
    if (dir.lengthSq() < 1e-6) dir.set(1, 0.6, 1).normalize();
    const distance = radius * 2.4;
    this.env.controls.target.copy(sphere.center);
    this.env.camera.position.copy(sphere.center).addScaledVector(dir, distance);
    this.env.controls.update();
  }

  /** Read-only handle to the renderer's canvas — needed by callers that
   *  attach raycaster click handlers without coupling to scene internals. */
  getCanvas(): HTMLCanvasElement {
    return this.env.renderer.domElement;
  }

  /** Read-only camera handle (for raycaster setup). */
  getCamera(): THREE.PerspectiveCamera {
    return this.env.camera;
  }

  /** Read-only scene handle (raycaster needs to walk objects). */
  getShipRoot(): THREE.Group {
    return this.shipRoot;
  }

  /**
   * Resolve the accessory instance at a screen-space click. Walks the
   * raycaster's first visible hit up the parent chain to the nearest
   * Object3D stamped with `userData.asset_id` (set by
   * `tagAndIndexInstance` on the placement clone). For attached
   * children — bundled rangefinders, periscopes, ammo boxes — we also
   * peek at `userData.attached_asset_id` so a click on a turret-mounted
   * rangefinder reports the rangefinder, not the turret host.
   *
   * Returns null if the cursor missed everything or hit only hull /
   * helper geometry (those have no `asset_id`).
   *
   * `clientX` / `clientY` are CSS pixels in the document (the standard
   * `event.clientX` / `event.clientY` values).
   */
  pickAt(clientX: number, clientY: number): PickResult | null {
    const canvas = this.env.renderer.domElement;
    const rect = canvas.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * 2 - 1;
    const y = -((clientY - rect.top) / rect.height) * 2 + 1;
    pickPointer.set(x, y);
    pickRaycaster.setFromCamera(pickPointer, this.env.camera);
    // Recursive: walks every descendant of shipRoot. Skips invisible.
    const hits = pickRaycaster.intersectObject(this.shipRoot, true);
    for (const hit of hits) {
      if (!isVisibleChain(hit.object)) continue;
      const info = resolveAssetUserData(hit.object);
      if (info) {
        return {
          object: info.root,
          point: hit.point.clone(),
          distance: hit.distance,
          info,
        };
      }
    }
    return null;
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

  setNormalScale(value: number): void {
    this.textures.setNormalScale(value);
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

  getNormalScale(): number {
    return this.textures.getNormalScale();
  }

  /** Diagnostic surface for the normal-map pipeline. See
   *  `TextureManager.getNormalDiagnostics()`. */
  getNormalDiagnostics(): ReturnType<TextureManager['getNormalDiagnostics']> {
    return this.textures.getNormalDiagnostics();
  }

  /** Snapshot of camo bindings + per-entry state for the inspector
   *  debug panel. See `TextureManager.getCamoDiagnostics()`. */
  getCamoDiagnostics(): CamoDiagnostics {
    return this.textures.getCamoDiagnostics();
  }

  // ── Read-only state ───────────────────────────────────────────────────

  getHullGroups(): readonly string[] {
    return this.classified.groups.map((g) => g.name);
  }

  /** Per-hull-group stats for the bottom-panel Hull tab. Walks each
   *  group node once; cheap enough to call per render (the hull tree
   *  is shallow). `triangles` counts the position-attribute / index
   *  buffer of every Mesh under the group, regardless of visibility. */
  getHullGroupStats(): ReadonlyArray<{ name: string; meshes: number; triangles: number }> {
    return this.classified.groups.map((g) => {
      let meshes = 0;
      let triangles = 0;
      g.node.traverse((obj) => {
        const m = obj as THREE.Mesh;
        if (!m.isMesh) return;
        meshes += 1;
        const geom = m.geometry as THREE.BufferGeometry | undefined;
        if (geom?.index) {
          triangles += geom.index.count / 3;
        } else if (geom?.attributes?.position) {
          triangles += geom.attributes.position.count / 3;
        }
      });
      return { name: g.name, meshes, triangles };
    });
  }

  getSeamStates(): Readonly<Record<SeamKey, SeamState>> {
    return this.seamStates;
  }

  /** Last-loaded `<Ship>.meta.json` parse, or null if the sidecar
   *  wasn't on disk / failed to fetch. Bottom-panel Textures tab
   *  reads `sidecar.materials[*].texture_sets` from this. */
  getSidecar(): SidecarDoc | null {
    return this.sidecar;
  }

  /** Absolute URL of the hull GLB's directory (with the GLB filename
   *  as the last segment so `new URL(rel, base)` strips it the way
   *  the texture manager does). Null between ship swaps. */
  getHullBaseUrl(): string | null {
    return this.hullBaseUrl;
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
      hullMeshesByLodLevel: this.classified.meshesByLodLevel,
      placementMeshesByLodLevel: this.placementMeshesByLodLevel,
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
