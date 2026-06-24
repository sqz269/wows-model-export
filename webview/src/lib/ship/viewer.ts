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

import {
  createSceneEnvironment,
  DEFAULT_TONEMAP_PARAMS,
  type BloomParams,
  type SceneEnvironment,
} from '$lib/three/scene';
import { ParticleScene, type ParticleAttachmentHandle } from '$lib/three/particles';
import { observeResize } from '$lib/three/resize';
import { startRenderLoop } from '$lib/three/render_loop';
import { disposeTree } from '$lib/three/dispose';
import { loadWgEnvironment, type WetnessParams, type WgEnvironment } from '$lib/three/env_ibl';
import { fetchParticleRecords, repoUrl } from '$lib/api';
import { SHIP_SECTIONS } from '$lib/types';
import type {
  ExteriorRecord,
  LibraryIndex,
  ParticleAttachment,
  ParticleRecord,
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
  materialIdAtIntersection,
  mountArmorThicknessOf,
  type ArmorMeshEntry,
  type ArmorThicknessFn,
} from './armor_view';
import {
  applyHitboxView,
  disposeHitboxView,
  prepareHitboxMeshes,
  type HitboxMeshEntry,
} from './hitbox_view';
import { NodeOverlay, type NodeCategory, type NodeEntry } from './node_overlay';

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

/**
 * Result of hovering an armor surface in the X-ray view. Resolved from the
 * per-vertex `_MATERIAL_ID` at the raycast hit → the sidecar thickness table.
 */
export interface ArmorPickResult {
  /** Effective plate thickness at the hit point (summed layers), in mm. */
  thicknessMm: number;
  /** The raw `_MATERIAL_ID` the thickness was resolved from. */
  materialId: number;
  /** Hull belt/deck plating vs. a turret/secondary mount's own armor. */
  source: 'hull' | 'mount';
  /** Short zone label derived from the armor mesh name (`Armor_Citadel`
   *  → `Citadel`), or null when the mesh is unnamed. */
  zoneLabel: string | null;
  /** Outer→inner layer thicknesses (hull armor only; null for mounts). */
  layers: number[] | null;
  /** Armor zones this material spans (hull armor only). */
  zones: string[] | null;
  /** Owning hardpoint for mount armor (e.g. `HP_AGM_1`), else null. */
  hp: string | null;
  /** Owning mount's library asset_id (mount armor only). */
  owner: string | null;
  /** Typed section the mount sits in (mount armor only). */
  section: string | null;
  /** World-space hit point. */
  point: THREE.Vector3;
}

interface ParticleAnchorMotion {
  lastPosition: THREE.Vector3;
  velocity: THREE.Vector3;
  lastTimeMs: number;
}

export interface WgEnvironmentInfo {
  space: string;
  weather: string;
  wetness: WetnessParams;
  avgLum: number;
}

const pickRaycaster = new THREE.Raycaster();
const pickPointer = new THREE.Vector2();
const particleWorldPos = new THREE.Vector3();
const particleWorldQuat = new THREE.Quaternion();

// Lighting when a WG IBL cube is active — the cube supplies ambient + specular,
// so the hemisphere fill is killed and the key directional is driven from the
// per-weather WG sun (yaw/pitch/color) at a fixed intensity multiplier (the
// color carries the per-weather brightness/tint). PROCEDURAL_* match scene.ts's
// creation defaults, restored on clear.
const WG_FILL_HEMI = 0.0;
const WG_SUN_INTENSITY = 3.0;
const PROCEDURAL_FILL_HEMI = 0.85;
const PROCEDURAL_FILL_DIR = 0.85;
const PROCEDURAL_SUN_DIR = new THREE.Vector3(50, 80, 50).normalize();

/** WG sun azimuth (yaw) + elevation (pitch) in degrees -> a unit vector
 *  pointing TOWARD the sun. Azimuth from +Z toward +X; elevation above the
 *  horizon (+Y up). Sign/reference tuned empirically against the render. */
function sunDirection(yaw: number, pitch: number): THREE.Vector3 {
  const y = THREE.MathUtils.degToRad(yaw);
  const p = THREE.MathUtils.degToRad(pitch);
  const cp = Math.cos(p);
  return new THREE.Vector3(cp * Math.sin(y), Math.sin(p), cp * Math.cos(y));
}

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

/** Friendly zone label from an armor mesh name: strips the `Armor_` prefix
 *  and any trailing ` [HP_…]` instance tag (`Armor_Citadel` → `Citadel`,
 *  `Armor_Turret [HP_AGM_1]` → `Turret`). Null for an empty / bare name. */
function armorZoneLabel(name: string): string | null {
  if (!name) return null;
  let s = name.replace(/^Armor[_-]?/i, '');
  const br = s.indexOf('[');
  if (br >= 0) s = s.slice(0, br);
  s = s.trim();
  return s || null;
}

function muzzleIndex(name: string): number {
  const m = /^HP_gunFire(\d+)$/i.exec(name);
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER;
}

const SHIP_AMBIENT_PARTICLE_GROUPS = new Set(['idleport', 'smoke']);
const SHIP_PARTICLE_SOURCE_ORDER = new Map<string, number>([
  ['hull', 0],
  ['artillery', 1],
  ['atba', 2],
  ['airDefense', 3],
  ['aa_aura', 4],
  ['munition', 5],
  ['map', 6],
]);

function isWakeParticleGroup(group: string): boolean {
  const g = group.toLowerCase();
  return g.startsWith('waketrace') || g.startsWith('propeller');
}

// Screen-space distortion blend modes (heat-haze / water-deform): they refract
// the scene-color snapshot rather than painting a sprite, so with no ocean
// behind them they read as flat squares. The ship view drops the faux water
// surface when a ship's particles include one of these (see SHIMMER funnel
// haze, DEFORM_WATER_SURFACE wakes/splashes). Mirrors Particles.svelte's
// recordHasDeformWater, broadened to SHIMMER.
const PARTICLE_DISTORTION_BLEND_MODES = new Set(['SHIMMER', 'DEFORM_WATER_SURFACE']);

function recordsHaveWaterDistortion(records: Record<string, ParticleRecord>): boolean {
  for (const rec of Object.values(records)) {
    for (const s of rec.systems ?? []) {
      if (PARTICLE_DISTORTION_BLEND_MODES.has(s.renderer?.blendType ?? '')) return true;
    }
  }
  return false;
}

function shipParticleEventKey(a: ParticleAttachment): string {
  return `${a.source ?? 'hull'}:${a.group ?? ''}`;
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

export type ShipParticleMode = 'ambient' | 'all';

export interface ShipParticleEventOption {
  key: string;
  source: string;
  group: string;
  label: string;
  handles: number;
  systems: number;
}

export interface ShipParticleStats {
  attachmentRows: number;
  renderableAttachments: number;
  anchorInstances: number;
  activeAttachments: number;
  ambientAttachments: number;
  eventAttachments: number;
  eventOnlyAttachments: number;
  unanchoredAttachments: number;
  unresolvedAnchors: number;
  uniquePaths: number;
  recordsLoaded: number;
  missingRecords: number;
  systems: number;
}

function emptyShipParticleStats(): ShipParticleStats {
  return {
    attachmentRows: 0,
    renderableAttachments: 0,
    anchorInstances: 0,
    activeAttachments: 0,
    ambientAttachments: 0,
    eventAttachments: 0,
    eventOnlyAttachments: 0,
    unanchoredAttachments: 0,
    unresolvedAnchors: 0,
    uniquePaths: 0,
    recordsLoaded: 0,
    missingRecords: 0,
    systems: 0,
  };
}

/** Per-mount armor record — a turret/secondary's own armor meshes (from the
 *  accessory GLB's `Armor` group), kept hidden until the armor view reveals
 *  them. When the mount is rigged, the meshes are attached to the yaw bone so
 *  they rotate with the turret; thickness comes from the ship's per-mount
 *  `mount_armor[hp]`. */
interface MountArmorRecord {
  /** Owning hardpoint (e.g. `HP_AGM_1`) for the `mount_armor[hp]` lookup. */
  hp: string | null;
  /** Library asset_id of the owning mount (turret), for the hover label. */
  assetId: string;
  /** Sidecar instance_id of the owning placement. */
  instanceId: string;
  /** Typed section the mount sits in (`turrets`, `secondaries`, …). */
  section: ShipSectionKey;
  /** The accessory's armor meshes (under its `Armor` group). */
  meshes: THREE.Mesh[];
  /** The mount's rig, when present — armor attaches to `rig.yaw`. */
  rig: TurretRig | null;
  /** Built lazily on first armor-view enable (per-instance coloured + cloned). */
  entries: ArmorMeshEntry[];
  /** material_id → thickness(mm) resolver, cached on first prepare so the
   *  hover pick doesn't re-collapse the `mount_armor[hp]` table per move. */
  thicknessOf: ArmorThicknessFn | null;
}

/** Synthesised vanilla-composition record for sidecars that predate the
 *  exteriors[] emit — mirrors the producer's default_exterior_record(). */
const DEFAULT_EXTERIOR: ExteriorRecord = {
  exterior_id: 'default',
  display_name: 'Standard',
  species: 'default',
  peculiarity: 'default',
  is_native: true,
  camo_scheme_key: 'main',
  hull: null,
  mounts: [],
  variant_swapped_asset_ids: [],
};

/** Context captured at `loadShip` time so `setActiveExterior` can later
 *  re-instantiate individual mounts with the exact machinery (library
 *  index, sidecar-derived per-instance maps, texture base URL) the
 *  original load used. Null before the first load. */
interface ShipLoadContext {
  library: LibraryIndex;
  sidecar: SidecarDoc | null;
  hullBaseUrl: string;
  placementsDoc: ShipPlacementsDoc;
  miscFilterByInstanceId: Map<string, string[]>;
  arcLimitsByInstanceId: Map<string, MountArcLimits>;
}

/** Loaded GLB template + attached-children bundle for one asset_id —
 *  everything `instantiateMount` needs to clone synchronously. */
interface AssetBundle {
  assetId: string;
  tpl: THREE.Object3D;
  attachedDoc: Awaited<ReturnType<AttachedDocCache['load']>>;
  attachedChildTpls: Map<string, THREE.Object3D | null>;
  /** True when loaded as a mount's DESTROYED variant (`glb_dead` +
   *  `attachments_dead`). `instantiateMount` walks the dead attachment list
   *  and applies the dead-orientation (OI-7) correction. */
  dead: boolean;
  /** Producer `dead_orientation` verdict (SAME | Z-MIRRORED | …); only
   *  meaningful when `dead`. Drives the OI-7 placement correction. */
  deadOrientation?: string;
}

/** Per-placement instancing stats, accumulated into `ShipLoadStats`. */
interface MountStats {
  attachmentsRendered: number;
  attachmentsFilteredByMisc: number;
}

/** Per-mount bookkeeping so an exterior switch can tear down + rebuild ONE
 *  placement without a whole-ship reload. Mirrors exactly what
 *  `instantiateMount` contributed to the shared tracking structures. */
interface MountRecord {
  instanceId: string;
  section: ShipSectionKey;
  /** The effective placement used (base, or exterior-overridden). */
  placement: ShipPlacement;
  /** The cloned root added to `sectionGroups[section]`. */
  inst: THREE.Object3D;
  /** Every Object3D in the instance subtree (incl. attached children) —
   *  drives one-pass removal from the shared tracking maps. */
  subtree: Set<THREE.Object3D>;
  armorRecord: MountArmorRecord | null;
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

/** Detach an accessory's `Hitboxes` group (per-mount splash-box AABBs baked
 *  into the GLB by the toolkit) so the normal texture / LOD registration skips
 *  the cube meshes. Mirrors `detachAccessoryArmor`; the caller re-adds the
 *  group hidden so the boxes ride the placement and the hitbox overlay can
 *  reveal them. */
function detachAccessoryHitboxes(
  inst: THREE.Object3D,
): { parent: THREE.Object3D; group: THREE.Object3D; meshes: THREE.Mesh[] } | null {
  const groups: THREE.Object3D[] = [];
  inst.traverse((o) => {
    if (o.name === 'Hitboxes') groups.push(o);
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
  /** Placement clone roots keyed by sidecar/accessories identifiers. Used by
   *  the particle layer to anchor gun/AA effects to live mount roots. */
  private placementRootByInstanceId = new Map<string, THREE.Object3D>();
  private placementRootByHp = new Map<string, THREE.Object3D>();
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
  /** Mounts currently swapped to their destroyed model (module
   *  damage). Source of truth for the control panel's per-mount toggles;
   *  cleared per ship and pruned when a mount is disposed/rebuilt. */
  private destroyedMounts = new Set<string>();
  /** Per-mount accessory hitbox cube meshes (secondary/AA/torpedo splash boxes
   *  baked into the accessory GLB). Detached from texturing, hidden by default,
   *  revealed by the hitbox overlay. Keyed by mount instance_id for teardown. */
  private mountHitboxRecords: { instanceId: string; meshes: THREE.Mesh[] }[] = [];

  // Exterior switching (ship-exterior unification). `mountRecords` mirrors
  // every live placement so `setActiveExterior` can tear down + rebuild the
  // swapped subset; `shipCtx` is the loadShip context the rebuild reuses.
  private mountRecords = new Map<string, MountRecord>();
  private shipCtx: ShipLoadContext | null = null;
  private activeExteriorId = 'default';
  // Hull-swap exteriors (HullDelta): which exterior's VARIANT HULL is
  // currently loaded (null = base hull). When a switch targets a different
  // hull, the whole ship reloads with `exteriorOverrideId` set — v1 is a
  // full reload, which is engine-faithful (WG has no in-place mesh-swap
  // API; see handoff §8.5).
  private activeHullExteriorId: string | null = null;
  private exteriorOverrideId: string | null = null;
  private currentShip: ShipSummary | null = null;

  // WG-authored bones & VFX-points overlay. Lives in the scene from
  // construction (like the accessory viewer's rig overlay); rebuilt per
  // ship from the live scene graph + the sidecar's hull EP_ positions.
  private nodeOverlay = new NodeOverlay();
  private removeNodeHover: (() => void) | null = null;

  // Ship particle rendering. Built lazily when the user enables the layer.
  private particleScene: ParticleScene | null = null;
  private particleHandles: ParticleAttachmentHandle[] = [];
  private particleAnchorObjects = new Map<ParticleAttachmentHandle, THREE.Object3D>();
  private particleAnchorMotion = new Map<ParticleAttachmentHandle, ParticleAnchorMotion>();
  private particleAttachmentAnchors = new WeakMap<ParticleAttachment, THREE.Object3D>();
  private particleLayerEnabled = false;
  // True when the loaded ship particles include a screen-space distortion
  // (SHIMMER / DEFORM_WATER_SURFACE) — the default for whether to drop the
  // ocean surface (so those effects have something to refract).
  private particleHasWaterDistortion = false;
  // User override for the ocean surface (the ShipControls "Water surface"
  // checkbox). null = auto (follow particleHasWaterDistortion); true/false =
  // explicit. Reset per ship so each starts at its sensible default.
  private shipWaterEnabled: boolean | null = null;
  private particleMode: ShipParticleMode = 'ambient';
  private particleEventOptions: ShipParticleEventOption[] = [];
  // Event groups the user has enabled to LOOP (re-burst continuously) via the
  // ShipControls per-event checkboxes. Persistent per ship; cleared on ship
  // change. Replaces the old one-shot "trigger" preview model.
  private particleLoopKeys = new Set<string>();
  private particleBuildPromise: Promise<ShipParticleStats> | null = null;
  private particleBuildToken = 0;
  private particleStats: ShipParticleStats = emptyShipParticleStats();

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
  // WG sky-cube IBL (PMREM), best-effort-loaded after construction; null
  // until the environment library is present + decoded.
  private wgEnv: WgEnvironment | null = null;

  constructor(container: HTMLElement) {
    this.env = createSceneEnvironment(container);

    this.shipRoot = new THREE.Group();
    this.shipRoot.name = 'Ship';
    this.env.scene.add(this.shipRoot);
    this.env.scene.add(this.nodeOverlay.group);

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
      this.textures.tickRipples(performance.now() / 1000); // advance L3.5 rain ripples
      this.updateParticleAttachmentTransforms();
      this.particleScene?.tick();
      this.env.render();
    });

    // Expose for in-browser debugging.
    if (typeof window !== 'undefined') {
      (window as unknown as { __wowsShipViewer__?: unknown }).__wowsShipViewer__ = this;
    }

    // Best-effort: light the ship with WG's sky-cube IBL (PMREM from the
    // environment library) instead of the procedural RoomEnvironment.
    // Silently keeps RoomEnvironment when the library isn't built.
    void this.applyWgEnvironment();

    // Hover labels for the node overlay. Only does work while the overlay
    // is visible (pickAt short-circuits otherwise); a single transient
    // sprite tracks the nearest marker to the cursor.
    const canvas = this.env.renderer.domElement;
    const onMove = (e: PointerEvent) => {
      if (!this.nodeOverlay.isVisible()) return;
      const rect = canvas.getBoundingClientRect();
      const hit = this.nodeOverlay.pickAt(e.clientX, e.clientY, this.env.camera, rect);
      this.nodeOverlay.setHover(hit?.entry ?? null);
    };
    canvas.addEventListener('pointermove', onMove);
    this.removeNodeHover = () => canvas.removeEventListener('pointermove', onMove);
  }

  // ── Public API ────────────────────────────────────────────────────────

  async loadShip(
    ship: ShipSummary,
    library: LibraryIndex,
    onProgress?: (msg: string) => void,
  ): Promise<ShipLoadStats> {
    const t0 = performance.now();
    this.clearShip();
    this.currentShip = ship;
    const report = (msg: string) => onProgress?.(msg);

    // Fetch the sidecar FIRST (best-effort) — a hull-swap exterior override
    // needs its record's `hull.hull_glb` + `hull.materials` BEFORE the hull
    // GLB is chosen. (Registration vs binding order is safe either way:
    // the texture manager's bind index is two-sided.)
    let sidecar: SidecarDoc | null = null;
    if (ship.sidecar_json) {
      try {
        const res = await fetch(repoUrl(ship.sidecar_json));
        if (res.ok) sidecar = (await res.json()) as SidecarDoc;
      } catch (err) {
        console.warn('[ship] sidecar fetch failed:', err);
      }
    }

    // Hull-swap exterior override (set by setActiveExterior's reload path):
    // load the VARIANT hull GLB and bind ITS materials manifest. Texture
    // paths in both manifests resolve against the same base `models/` dir —
    // HullDelta puts variant DDS stems in the shared textures_dds/.
    const overrideId = this.exteriorOverrideId;
    const overrideRec = overrideId
      ? (sidecar?.exteriors ?? []).find((e) => e.exterior_id === overrideId)
      : undefined;
    const overrideHull = overrideRec?.hull ?? null;

    report('Loading hull GLB…');
    // `hull.hull_glb` is ship-folder-relative (`models/exteriors/<id>_hull.glb`).
    const shipRootPath = ship.sidecar_json
      ? ship.sidecar_json.replace(/\/[^/]*$/, '')
      : ship.hull_glb.replace(/\/models\/[^/]*$/, '');
    const hullUrl = repoUrl(
      overrideHull?.hull_glb ? `${shipRootPath}/${overrideHull.hull_glb}` : ship.hull_glb,
    );
    // Resolve hull DDS paths against the BASE hull GLB's directory — the
    // sidecar's (and HullDelta's) `dds_mips` carry `textures_dds/...`
    // relative to `models/`, for the variant hull too.
    const hullBaseUrl = new URL(repoUrl(ship.hull_glb), window.location.origin).toString();
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

    if (sidecar) {
      // Hull material bindings: a variant hull binds its OWN manifest
      // (`hull.materials` — same shape as the top-level `materials[]`).
      const sidecarForHull =
        overrideHull?.materials?.length
          ? { ...sidecar, materials: overrideHull.materials }
          : sidecar;
      try {
        this.textures.bindHullMaterials(sidecarForHull, hullBaseUrl);
      } catch (err) {
        console.warn('[ship] hull material binding failed:', err);
      }
    }
    // Stash for the bottom panel's Textures tab (always the REAL sidecar —
    // the override only redirects the hull bind).
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

    // Variant-hull decoratives REPLACEMENT (hull skel_ext layer): the
    // engine reads hull decoratives (voice tubes, binoculars,
    // searchlights…) from the LOADED hull model's `.skel_ext` files, so a
    // hull swap replaces the whole layer — drop every base
    // `source == "skel_ext_hash"` placement and instantiate the variant
    // doc's instead. Without the doc (producer harvest not run yet) the
    // base decoratives stay — consistent with the never-a-hole fallback.
    if (overrideHull?.decoratives) {
      try {
        const decoRes = await fetch(repoUrl(`${shipRootPath}/${overrideHull.decoratives}`));
        if (decoRes.ok) {
          const decoDoc = (await decoRes.json()) as Partial<ShipPlacementsDoc>;
          let dropped = 0;
          let added = 0;
          for (const section of SHIP_SECTIONS) {
            const before = placementsDoc[section] ?? [];
            const kept = before.filter((p) => p.source !== 'skel_ext_hash');
            dropped += before.length - kept.length;
            const extra = decoDoc[section] ?? [];
            added += extra.length;
            placementsDoc[section] = [...kept, ...extra];
          }
          report(`Variant hull decoratives: ${dropped} base dropped, ${added} variant added.`);
        } else {
          console.warn(`[exterior] decoratives doc HTTP ${decoRes.status}; base decoratives kept`);
        }
      } catch (err) {
        console.warn('[exterior] decoratives doc fetch failed; base decoratives kept:', err);
      }
    }

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

    // Stash the load context — setActiveExterior re-instantiates individual
    // mounts through the same machinery after this load completes.
    const ctx: ShipLoadContext = {
      library,
      sidecar,
      hullBaseUrl,
      placementsDoc,
      miscFilterByInstanceId,
      arcLimitsByInstanceId,
    };
    this.shipCtx = ctx;

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
        const bundle = await this.loadAssetBundle(assetId, ctx, unresolved);

        if (!bundle) {
          unresolved.set(assetId, places.length);
          loadedAssets++;
          report(
            `Loaded ${loadedAssets}/${tasks.length} types · ${renderedPlacements} placements · ${attachmentsRendered} attached`,
          );
          continue;
        }

        for (const e of places) {
          // HP-side miscFilter: sidecar Phase 6 autofill takes precedence
          // over any value the placements JSON might carry.
          const miscFilter =
            miscFilterByInstanceId.get(e.placement.instance_id) ??
            e.placement.misc_filter ??
            null;
          const stats = this.instantiateMount(e.section, e.placement, bundle, miscFilter);
          renderedPlacements++;
          attachmentsRendered += stats.attachmentsRendered;
          attachmentsFilteredByMisc += stats.attachmentsFilteredByMisc;
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

    // Build the WG bones / VFX-points overlay from the freshly-composed
    // scene graph (accessory bones + hardpoints + gun-fire anchors) plus
    // the sidecar's resolved hull EP_ positions. Markers stay hidden until
    // the user enables the overlay.
    this.nodeOverlay.rebuild(this.shipRoot, this.sidecar?.effects?.attachments ?? null);

    // Re-push the active weather's rain wetness onto the freshly-built hull/
    // accessory materials — the WG environment is applied in the constructor,
    // before this ship loaded, so its wetness must be re-applied here.
    this.textures.reapplyWetness();

    if (overrideId) {
      // Hull-swap reload: the override IS the selection. Stamp the loaded
      // hull's owner, then run the per-HP mount path against this fresh
      // base composition (same-hull now → no recursion into the reload).
      this.activeHullExteriorId = overrideHull?.hull_glb ? overrideId : null;
      this.activeExteriorId = 'default';
      if (overrideId !== 'default') {
        await this.setActiveExterior(overrideId, onProgress);
      }
    } else {
      // Auto-select the native exterior (§8 of the unification handoff): WG
      // renders the nativePermoflage by default — an ARP-style ship never
      // shows its bare hull in game. Exactly one record is native; when it's
      // the synthesised default this is a no-op.
      this.activeHullExteriorId = null;
      const native = this.getExteriors().find(
        (e) => e.is_native && e.exterior_id !== 'default',
      );
      if (native) {
        report(`Applying native exterior ${native.exterior_id}…`);
        await this.setActiveExterior(native.exterior_id, onProgress);
      }
    }

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
   * Load one asset's GLB template + attached-children bundle, binding
   * texture sets along the way. Extracted from the loadShip worker so
   * `setActiveExterior` can fetch swap-target assets through the same
   * path (caches dedupe repeat loads). Returns null when the asset is
   * missing from the library or its template fails to load; `unresolved`
   * (when given) collects missing attached-child ids.
   */
  private async loadAssetBundle(
    assetId: string,
    ctx: ShipLoadContext,
    unresolved?: Map<string, number>,
    dead = false,
  ): Promise<AssetBundle | null> {
    const libEntry = ctx.library.assets[assetId];
    if (!libEntry) return null;

    // Dead variant: the destroyed mesh is the live entry's `glb_dead`
    // sibling (turrets/guns carry it inline). Standalone `_dead` assets
    // (gun decks) are loaded as ordinary entries with dead=false by the
    // caller. Bail when this entry has no dead mesh.
    const glbPath = dead ? libEntry.glb_dead : libEntry.glb;
    if (!glbPath) return null;

    // Load host template + attached_accessories.json in parallel.
    // For hosts without a bundle (~most assets) the doc resolves to
    // null; instantiateMount's inner loop short-circuits.
    const [tpl, attachedDoc] = await Promise.all([
      this.accessoryCache.load(glbPath),
      this.attachedDocCache.load(libEntry),
    ]);
    if (!tpl) return null;

    // Bind host's texture sets before cloning so registerAccessoryMesh
    // sees populated schemes.
    this.textures.bindLibraryAsset(assetId, libEntry, ctx.sidecar, ctx.hullBaseUrl);

    // Pre-warm every distinct attached-child template + bind its
    // texture sets. The accessoryCache dedupes across hosts so a
    // child bundled by N main turrets is fetched once. Resolved
    // templates are stashed so instantiateMount can clone them
    // synchronously.
    // Dead bundles draw children from `attachments_dead`, live ones from
    // `attachments_live`. The accessoryCache dedupes across hosts so a
    // child bundled by N mounts is fetched once.
    const attList =
      (dead ? attachedDoc?.attachments_dead : attachedDoc?.attachments_live) ?? [];
    const attachedChildTpls = new Map<string, THREE.Object3D | null>();
    if (attList.length > 0) {
      const childIds = new Set<string>();
      for (const att of attList) childIds.add(att.asset_id);
      const childPromises = Array.from(childIds).map(async (cid) => {
        const childLib = ctx.library.assets[cid];
        if (!childLib) {
          attachedChildTpls.set(cid, null);
          unresolved?.set(cid, (unresolved.get(cid) ?? 0) + 1);
          return;
        }
        this.textures.bindLibraryAsset(cid, childLib, ctx.sidecar, ctx.hullBaseUrl);
        const childTpl = await this.accessoryCache.load(childLib.glb);
        attachedChildTpls.set(cid, childTpl);
      });
      await Promise.all(childPromises);
    }

    return {
      assetId,
      tpl,
      attachedDoc,
      attachedChildTpls,
      dead,
      deadOrientation: libEntry.dead_orientation,
    };
  }

  /**
   * Clone + place + register ONE placement (and its attached children).
   * Extracted verbatim from the loadShip worker loop; also records a
   * `MountRecord` so an exterior switch can later tear this mount down
   * without a whole-ship reload.
   *
   * `miscFilter` is the RESOLVED per-HP whitelist (3-state: null = all,
   * `[]` = drop all, `[list]` = whitelist) — the caller owns precedence
   * (sidecar autofill vs placements JSON vs exterior override).
   */
  private instantiateMount(
    section: ShipSectionKey,
    placement: ShipPlacement,
    bundle: AssetBundle,
    miscFilter: string[] | null,
  ): MountStats {
    const ctx = this.shipCtx;
    const stats: MountStats = { attachmentsRendered: 0, attachmentsFilteredByMisc: 0 };

    // Deep-clone with SkeletonUtils for skinned templates so each
    // placement has its own bones — sharing a Skeleton would tie
    // every turret's yaw to the same `Rotate_Y` instance.
    const inst = cloneAccessoryInstance(bundle.tpl);
    applyPlacementMatrix(inst, placement.transform.matrix);
    // OI-7: a destroyed mount reuses the LIVE placement matrix (alive + dead
    // share one transform), but the producer's dead_variant_audit only
    // DIAGNOSES a Z-mirror — it does not correct it — so a "Z-MIRRORED" dead
    // mesh faces 180° off on the shared placement. Spin it about the mount's
    // local up to face the right way. SAME / X-MIRRORED / AMBIGUOUS need no
    // yaw correction. NOTE: the Z-MIRRORED branch is unverified against a real
    // Z-mirrored asset (Montana's turret is SAME) — validate before relying.
    if (bundle.dead && bundle.deadOrientation === 'Z-MIRRORED') {
      inst.rotateY(Math.PI);
    }
    // Particle/effects anchor maps (gun-effect attachments resolve their
    // host mount by hp_name / instance_id). Re-registered on exterior
    // switch so anchors always point at the LIVE mount root.
    this.placementRootByInstanceId.set(placement.instance_id, inst);
    if (placement.hp_name) this.placementRootByHp.set(placement.hp_name, inst);
    // Pull the accessory's own armor (turret/secondary `Armor` group)
    // out before registration so it isn't textured / colored / LOD-
    // bucketed as normal turret geometry. Re-added hidden below.
    const armorDetached = detachAccessoryArmor(inst);
    // Per-mount splash hitboxes (secondary/AA/torpedo). Detach before texture/
    // LOD registration so the cube meshes aren't treated as render geometry;
    // re-added hidden below so they ride the placement + feed the overlay.
    const hitboxDetached = detachAccessoryHitboxes(inst);
    const { colorEntries, meshesByLodLevel } = tagAndIndexInstance(
      inst,
      {
        section,
        placement,
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
      // A destroyed mount (glb_dead sibling) textures with its own `*_dead_*`
      // set, not the alive scheme. Standalone `_dead` assets (gun decks) and
      // the dead-attachment children below load as ordinary assets whose own
      // `main` scheme already IS the dead look, so they stay deadVariant=false.
      this.textures.registerAccessoryMesh(m, placement, bundle.dead);
    });
    // Look for WG rig nodes (`Rotate_Y` / `Rotate_X`). Most
    // gun/main and gun/secondary mounts have them; AA and static
    // miscs return null silently.
    const arcLimits = ctx?.arcLimitsByInstanceId.get(placement.instance_id) ?? null;
    const rig = extractTurretRig(inst, placement.asset_id, placement.instance_id, arcLimits);
    if (rig) {
      this.turretRigs.register(rig);
    } else if (arcLimits?.yawRangeDeg) {
      // Static mount with a traverse arc — torpedo tubes are static
      // meshes (no Rotate_Y bone), so draw their firing arc without
      // animating them.
      this.turretRigs.registerStaticArc(placement.instance_id, inst, arcLimits);
    }
    // Re-attach the detached armor (hidden) so it rides the placement
    // transform, and record it for the armor view. Rigged mounts get
    // their armor attached to the yaw bone on first reveal so it rotates
    // with the turret.
    let armorRecord: MountArmorRecord | null = null;
    if (armorDetached && armorDetached.meshes.length > 0) {
      armorDetached.parent.add(armorDetached.group);
      // Hide per-mesh (not the group): the armor view toggles mesh
      // visibility, and rigged mounts move their meshes out of the group
      // onto the yaw bone — so group-level visibility wouldn't gate them.
      for (const m of armorDetached.meshes) m.visible = false;
      armorRecord = {
        hp: this.hpByInstanceId.get(placement.instance_id) ?? null,
        assetId: placement.asset_id,
        instanceId: placement.instance_id,
        section,
        meshes: armorDetached.meshes,
        rig,
        entries: [],
        thicknessOf: null,
      };
      this.mountArmorRecords.push(armorRecord);
    }

    // Re-add the detached hitbox group (hidden) so the boxes ride the placement
    // transform; the hitbox overlay toggles them. They keep the toolkit's
    // translucent `hitbox_*` material — no sidecar styling needed.
    if (hitboxDetached && hitboxDetached.meshes.length > 0) {
      hitboxDetached.parent.add(hitboxDetached.group);
      for (const m of hitboxDetached.meshes) m.visible = this.hitboxViewEnabled;
      this.mountHitboxRecords.push({
        instanceId: placement.instance_id,
        meshes: hitboxDetached.meshes,
      });
    }
    this.sectionGroups[section].add(inst);

    // Attached accessories, gated by the resolved miscFilter whitelist.
    const attachedDoc = bundle.attachedDoc;
    const attList =
      (bundle.dead ? attachedDoc?.attachments_dead : attachedDoc?.attachments_live) ?? [];
    if (attList.length > 0) {
      const filterSet = miscFilter && miscFilter.length > 0 ? new Set(miscFilter) : null;
      const dropAll = miscFilter !== null && miscFilter.length === 0;

      for (const att of attList) {
        if (dropAll) {
          stats.attachmentsFilteredByMisc++;
          continue;
        }
        if (filterSet !== null && !filterSet.has(att.placement_id)) {
          stats.attachmentsFilteredByMisc++;
          continue;
        }
        const childTpl = bundle.attachedChildTpls.get(att.asset_id);
        if (!childTpl) continue;

        // Attached children (catapults, rangefinders, etc.) are
        // typically static, but use the skin-aware cloner anyway so
        // the rare rigged child (e.g. some director mounts) gets
        // its own bones.
        const childInst = cloneAccessoryInstance(childTpl);
        applyAttachedMatrix(childInst, att.transform.matrix);
        childInst.userData.attached_to_instance_id = placement.instance_id;
        childInst.userData.attached_placement_id = att.placement_id;
        childInst.userData.attached_asset_id = att.asset_id;
        childInst.userData.section = section;
        inst.add(childInst);

        // Build a per-child placement so camo classification reads
        // the child's own scope/category (catches misc/plane/float
        // routing for catapults, rangefinders, ammo boxes).
        const childLib = ctx?.library.assets[att.asset_id];
        const childPlacement: ShipPlacement = {
          ...placement,
          asset_id: att.asset_id,
          scope: childLib?.scope ?? placement.scope,
          category: childLib?.category ?? placement.category,
          subcategory: childLib?.subcategory ?? placement.subcategory,
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
        stats.attachmentsRendered++;
      }
    }

    // Record the mount for per-HP teardown on exterior switch.
    const subtree = new Set<THREE.Object3D>();
    inst.traverse((o) => subtree.add(o));
    this.mountRecords.set(placement.instance_id, {
      instanceId: placement.instance_id,
      section,
      placement,
      inst,
      subtree,
      armorRecord,
    });

    return stats;
  }

  /**
   * Tear down a set of live mounts — the inverse of `instantiateMount`,
   * scoped to the swapped placements so an exterior switch never touches
   * the rest of the ship. One pass over each shared tracking structure
   * (the union subtree set makes membership checks O(1)).
   *
   * Cloned geometry is NOT disposed — clones share buffers with the
   * templates in `accessoryCache` (same rule as clearShip).
   */
  private disposeMounts(instanceIds: string[]): void {
    const records = instanceIds
      .map((id) => this.mountRecords.get(id))
      .filter((r): r is MountRecord => !!r);
    if (records.length === 0) return;

    const union = new Set<THREE.Object3D>();
    for (const rec of records) {
      for (const o of rec.subtree) union.add(o);
    }

    for (const rec of records) {
      this.sectionGroups[rec.section].remove(rec.inst);
      // Particle anchor maps — drop only when still pointing at THIS root
      // (the replacement mount may already have re-registered).
      if (this.placementRootByInstanceId.get(rec.instanceId) === rec.inst) {
        this.placementRootByInstanceId.delete(rec.instanceId);
      }
      const hp = rec.placement.hp_name;
      if (hp && this.placementRootByHp.get(hp) === rec.inst) {
        this.placementRootByHp.delete(hp);
      }
      // placementsByMesh holds instance ROOTS keyed by parent hull mesh.
      const pm = rec.placement.parent_mesh;
      if (pm) {
        const list = this.placementsByMesh.get(pm);
        if (list) {
          const i = list.indexOf(rec.inst);
          if (i >= 0) list.splice(i, 1);
        }
      }
      this.destroyedMounts.delete(rec.instanceId);
      const hbi = this.mountHitboxRecords.findIndex((r) => r.instanceId === rec.instanceId);
      if (hbi >= 0) this.mountHitboxRecords.splice(hbi, 1);
      this.turretRigs.unregister(rec.instanceId);
      if (rec.armorRecord) {
        disposeArmorView(rec.armorRecord.entries, null);
        const i = this.mountArmorRecords.indexOf(rec.armorRecord);
        if (i >= 0) this.mountArmorRecords.splice(i, 1);
      }
      this.mountRecords.delete(rec.instanceId);
    }

    this.placementColorEntries = this.placementColorEntries.filter(
      (e) => !union.has(e.mesh),
    );
    for (const [level, bucket] of this.placementMeshesByLodLevel) {
      this.placementMeshesByLodLevel.set(
        level,
        bucket.filter((m) => !union.has(m)),
      );
    }
    this.textures.unregisterMeshes(union);
  }

  /**
   * Toggle ONE mount between its live and destroyed (`glb_dead`) model,
   * reusing the placement transform — the per-mount analogue of an exterior
   * swap (dispose → reload bundle → instantiate → re-sync state). The dead
   * mesh resolves as either a standalone `dead_asset_id` library entry
   * (gun decks) or the live entry's `glb_dead` sibling (turrets/guns), with
   * `attachments_dead` swapped in for `attachments_live`. Returns false (and
   * leaves the live mount untouched) when no dead model is available.
   *
   * Reference-consumer scope: the VISUAL swap only —
   * no HP/crit/repair simulation. Downstream consumers port the swap and drive it from their
   * own module-destruction model.
   */
  async setMountDestroyed(instanceId: string, dead: boolean): Promise<boolean> {
    const ctx = this.shipCtx;
    if (!ctx) return false;
    const rec = this.mountRecords.get(instanceId);
    if (!rec) return false;
    // Capture before disposeMounts deletes the record.
    const { section, placement } = rec;
    const miscFilter =
      ctx.miscFilterByInstanceId.get(instanceId) ?? placement.misc_filter ?? null;

    let bundle: AssetBundle | null;
    if (dead) {
      const deadId = placement.dead_asset_id;
      if (deadId && ctx.library.assets[deadId]) {
        // Standalone `_dead` library entry (gun decks) — an ordinary load.
        bundle = await this.loadAssetBundle(deadId, ctx);
      } else if (ctx.library.assets[placement.asset_id]?.glb_dead) {
        // Sibling `glb_dead` on the live entry (turrets/guns).
        bundle = await this.loadAssetBundle(placement.asset_id, ctx, undefined, true);
      } else {
        console.warn(
          `[module] ${instanceId} (${placement.asset_id}): no dead model — keeping live`,
        );
        return false;
      }
    } else {
      bundle = await this.loadAssetBundle(placement.asset_id, ctx);
    }
    if (!bundle) return false;

    this.disposeMounts([instanceId]); // also prunes it from destroyedMounts
    this.instantiateMount(section, placement, bundle, miscFilter);
    if (dead) this.destroyedMounts.add(instanceId);

    // Re-sync the rebuilt mount (mirror setActiveExterior's tail): re-push
    // textures onto the fresh entries, the visibility/LOD cascade, and the
    // armor X-ray if it's on.
    if (this.textures.isShowingTextures()) {
      await this.textures.setShowTextures(true);
      this.textures.reapplyWetness();
    }
    this.applyAllStates();
    if (this.armorViewEnabled) this.setArmorView(true);

    // Cascade to turret RIDERS. A sub-mount whose hp_name nests under this
    // one (composite `<hostHp>_<subHp>`, e.g. an AA gun `HP_AGM_3_HP_AGA_4`
    // bolted to turret `HP_AGM_3`) physically rides this mount. The destroyed
    // model carries no hardpoint for it (the wreck's geometry differs), so the
    // rider would float over the wreck — hide it on death, show it on restore.
    // Hiding the rider ROOT is robust against the LOD/section cascade above:
    // an invisible parent hides its whole subtree regardless of child flags.
    // The trailing '_' in the prefix prevents `HP_AGM_3` matching `HP_AGM_30`.
    const hostHp = placement.hp_name;
    if (hostHp) {
      const prefix = `${hostHp}_`;
      for (const rrec of this.mountRecords.values()) {
        if (rrec.placement?.hp_name?.startsWith(prefix)) {
          rrec.inst.visible = !dead;
        }
      }
    }
    return true;
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
    this.destroyedMounts.clear();
    this.mountHitboxRecords = [];
    this.placementsByMesh.clear();
    this.placementColorEntries.length = 0;
    this.placementMeshesByLodLevel.clear();
    this.seamStates = defaultSeamStates();
    this.textures.clearShip();
    this.attachedDocCache.clear();
    this.turretRigs.clear();
    this.nodeOverlay.clear();
    this.disposeParticleLayer();
    this.placementRootByInstanceId.clear();
    this.placementRootByHp.clear();
    this.sidecar = null;
    this.hullBaseUrl = null;
    this.mountRecords.clear();
    this.shipCtx = null;
    this.activeExteriorId = 'default';
    this.activeHullExteriorId = null;
    // `currentShip` + `exteriorOverrideId` survive deliberately — loadShip
    // re-stamps the former right after this runs, and the reload path owns
    // the latter's lifecycle.
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
    return (
      this.classified.groups.some((g) => g.name === 'Hitboxes') ||
      this.mountHitboxRecords.length > 0
    );
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
        rec.thicknessOf = thicknessOf;
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
    // Hull hitboxes: the hull GLB's "Hitboxes" group, styled per sidecar.
    const group = this.classified.groups.find((g) => g.name === 'Hitboxes');
    if (group) {
      if (on && !this.hitboxEntries) {
        const boxes = this.sidecar?.hitbox?.boxes ?? {};
        this.hitboxEntries = prepareHitboxMeshes(group.node, boxes);
      }
      if (this.hitboxEntries) applyHitboxView(this.hitboxEntries, on);
      group.node.visible = on;
    }
    // Per-mount accessory hitboxes (secondary/AA/torpedo splash boxes baked into
    // the accessory GLB). They keep the toolkit's translucent `hitbox_*`
    // material, so just toggle visibility — no sidecar metadata to style by.
    for (const rec of this.mountHitboxRecords) {
      for (const m of rec.meshes) m.visible = on;
    }
    this.hitboxViewEnabled = on;
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

  /** Loaded mounts that have a destroyed-state model (a standalone
   *  `dead_asset_id` library entry, or the live entry's `glb_dead` sibling),
   *  each tagged with its current live/dead toggle state. Drives the control
   *  panel's per-mount module-damage controls. */
  getDestructibleMounts(): Array<{
    instanceId: string;
    assetId: string;
    hpName: string | null;
    section: ShipSectionKey;
    dead: boolean;
  }> {
    const ctx = this.shipCtx;
    const out: Array<{
      instanceId: string;
      assetId: string;
      hpName: string | null;
      section: ShipSectionKey;
      dead: boolean;
    }> = [];
    if (!ctx) return out;
    this.mountRecords.forEach((rec, id) => {
      const p = rec.placement;
      const deadId = p.dead_asset_id;
      const hasDead =
        (!!deadId && !!ctx.library.assets[deadId]) ||
        !!ctx.library.assets[p.asset_id]?.glb_dead;
      if (!hasDead) return;
      out.push({
        instanceId: id,
        assetId: p.asset_id,
        hpName: p.hp_name ?? null,
        section: rec.section,
        dead: this.destroyedMounts.has(id),
      });
    });
    out.sort((a, b) =>
      (a.section + (a.hpName ?? a.instanceId)).localeCompare(
        b.section + (b.hpName ?? b.instanceId),
      ),
    );
    return out;
  }

  // ── Bones & VFX-points overlay ────────────────────────────────────────

  /** True when the loaded ship has any overlay node (always true once a
   *  ship with accessories is loaded; gates the control panel section). */
  hasNodeData(): boolean {
    return this.nodeOverlay.getNodes().length > 0;
  }

  /** Toggle the whole WG bones / VFX-points overlay. */
  setNodesView(on: boolean): void {
    this.nodeOverlay.setVisible(on);
  }

  getNodesViewEnabled(): boolean {
    return this.nodeOverlay.isVisible();
  }

  setNodeCategoryVisible(cat: NodeCategory, on: boolean): void {
    this.nodeOverlay.setCategoryVisible(cat, on);
  }

  getNodeCategoryVisible(cat: NodeCategory): boolean {
    return this.nodeOverlay.getCategoryVisible(cat);
  }

  /** Per-category marker counts for the legend / control panel. */
  getNodeCounts(): Record<NodeCategory, number> {
    return this.nodeOverlay.getCounts();
  }

  /** Full node inventory for the bottom-panel list. */
  getNodeList(): readonly NodeEntry[] {
    return this.nodeOverlay.getNodes();
  }

  /** Pin a persistent marker + label at the named node (or clear with
   *  null). Enabling the overlay first if needed so the pin is visible. */
  pinNode(name: string | null): void {
    if (name && !this.nodeOverlay.isVisible()) this.nodeOverlay.setVisible(true);
    this.nodeOverlay.pin(name);
  }

  /** Drop the camera onto the named node's position (keeps current view
   *  direction; pulls back a fixed distance so a single point still
   *  frames sensibly). No-op for an unknown name. */
  frameOnNode(name: string): void {
    const entry = this.nodeOverlay.getNodes().find((e) => e.name === name);
    if (!entry) return;
    const center = new THREE.Vector3(entry.position.x, entry.position.y, entry.position.z);
    const dir = new THREE.Vector3()
      .subVectors(this.env.camera.position, this.env.controls.target)
      .normalize();
    if (dir.lengthSq() < 1e-6) dir.set(1, 0.6, 1).normalize();
    this.env.controls.target.copy(center);
    this.env.camera.position.copy(center).addScaledVector(dir, 30);
    this.env.controls.update();
  }

  getPinnedNode(): string | null {
    return this.nodeOverlay.getPinned();
  }

  /** Re-capture accessory marker positions (e.g. after the user aims the
   *  turrets). Hull EP_ points are static. */
  refreshNodes(): void {
    this.nodeOverlay.refresh(this.shipRoot, this.sidecar?.effects?.attachments ?? null);
  }

  // ── Ship particle layer ───────────────────────────────────────────────

  hasShipParticleData(): boolean {
    return (this.sidecar?.effects?.attachments?.length ?? 0) > 0;
  }

  getShipParticlesVisible(): boolean {
    return this.particleLayerEnabled;
  }

  getShipParticleMode(): ShipParticleMode {
    return this.particleMode;
  }

  getShipParticleStats(): Readonly<ShipParticleStats> {
    return this.particleStats;
  }

  getShipParticleEventOptions(): readonly ShipParticleEventOption[] {
    return this.particleEventOptions;
  }

  async setShipParticleMode(mode: ShipParticleMode): Promise<ShipParticleStats> {
    this.particleMode = mode;
    if (!this.particleLayerEnabled) return this.particleStats;
    if (this.particleBuildPromise) return this.particleBuildPromise;
    if (!this.particleScene) return this.setShipParticlesVisible(true);
    this.applyShipParticleActivation();
    return this.particleStats;
  }

  /** Which event groups are currently set to loop (the ShipControls per-event
   *  checkboxes read this). 'all' mode loops every event implicitly. */
  getShipParticleEventLoops(): ReadonlySet<string> {
    return this.particleLoopKeys;
  }

  /** Toggle whether an event group's particles LOOP (re-burst continuously)
   *  instead of staying idle until triggered. Persistent per ship; the layer
   *  must be on for it to take visible effect (the checkbox UI is gated on
   *  that). Enabling restarts the group for a crisp, immediate first burst. */
  async setShipParticleEventLoop(key: string, on: boolean): Promise<ShipParticleStats> {
    if (!key) return this.particleStats;
    if (on) this.particleLoopKeys.add(key);
    else this.particleLoopKeys.delete(key);
    if (this.particleBuildPromise) await this.particleBuildPromise;
    if (!this.particleScene || !this.particleLayerEnabled) return this.particleStats;
    if (on) {
      const handles = this.particleHandles.filter((h) => shipParticleEventKey(h.attachment) === key);
      for (const h of handles) this.particleScene.restartAttachment(h);
      this.updateParticleAttachmentTransforms();
    }
    this.applyShipParticleActivation();
    return this.particleStats;
  }

  /** Show the faux ocean surface while the ship particle layer is on and water
   *  is wanted — either the user's explicit choice (the "Water surface"
   *  checkbox) or, when unset, the auto-default (ships whose particles include
   *  a SHIMMER / DEFORM_WATER distortion that needs an ocean to refract). The
   *  plane (scene.ts) is non-occluding and animates inside env.render(), which
   *  the ship render loop already drives. */
  private updateParticleWaterPlane(): void {
    const want = this.shipWaterEnabled ?? this.particleHasWaterDistortion;
    this.env.setWaterPlaneVisible(this.particleLayerEnabled && want);
  }

  /** Effective ocean-surface state (explicit override, else the auto-default).
   *  Drives the ShipControls "Water surface" checkbox. */
  isShipWaterEnabled(): boolean {
    return this.shipWaterEnabled ?? this.particleHasWaterDistortion;
  }

  /** User toggle for the ocean surface (overrides the auto-default). */
  setShipWaterEnabled(on: boolean): void {
    this.shipWaterEnabled = on;
    this.updateParticleWaterPlane();
  }

  async setShipParticlesVisible(on: boolean): Promise<ShipParticleStats> {
    this.particleLayerEnabled = on;
    if (!on) {
      // Keep the per-event loop choices — toggling the layer off then on
      // resumes the same looping events (they're cleared only on ship change).
      this.particleScene?.setAllActive(false);
      if (this.particleScene) this.particleScene.root.visible = false;
      this.updateParticleWaterPlane();
      this.particleStats = { ...this.particleStats, activeAttachments: 0 };
      return this.particleStats;
    }

    if (!this.sidecar?.effects?.attachments?.length) {
      this.particleStats = emptyShipParticleStats();
      return this.particleStats;
    }
    if (this.particleScene) {
      this.particleScene.root.visible = true;
      this.updateParticleWaterPlane();
      this.applyShipParticleActivation();
      return this.particleStats;
    }
    if (this.particleBuildPromise) return this.particleBuildPromise;

    const token = ++this.particleBuildToken;
    this.particleBuildPromise = this.buildShipParticleLayer(token).finally(() => {
      if (token === this.particleBuildToken) this.particleBuildPromise = null;
    });
    return this.particleBuildPromise;
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

  /**
   * Resolve the armor surface under a screen-space point for the X-ray
   * hover read-out. Raycasts only the armor meshes (hull `Armor` group +
   * each mount's own armor), so a hit reports the nearest armor plate along
   * the ray even when the ship's visual hull is still shown on top. Reads
   * the per-vertex `_MATERIAL_ID` at the hit triangle and joins it to the
   * sidecar thickness table (hull `materials_table` or the mount's
   * `mount_armor[hp]`). Returns null when the armor view is off or the
   * cursor missed every armor surface.
   *
   * `clientX` / `clientY` are document coords (`event.clientX` / `clientY`).
   */
  pickArmorAt(clientX: number, clientY: number): ArmorPickResult | null {
    if (!this.armorViewEnabled) return null;
    const materialsTable = this.sidecar?.armor?.materials_table ?? {};

    // Gather every armor mesh currently in the scene. Hull armor lives under
    // the `Armor` group (toggled visible as a group); mount armor meshes were
    // re-parented onto their yaw bones. We rely on `isVisibleChain` below to
    // skip anything hidden rather than pre-filtering here.
    const meshes: THREE.Mesh[] = [];
    if (this.armorEntries) {
      for (const e of this.armorEntries) meshes.push(e.mesh);
    }
    for (const rec of this.mountArmorRecords) {
      for (const m of rec.meshes) meshes.push(m);
    }
    if (meshes.length === 0) return null;

    const canvas = this.env.renderer.domElement;
    const rect = canvas.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * 2 - 1;
    const y = -((clientY - rect.top) / rect.height) * 2 + 1;
    pickPointer.set(x, y);
    pickRaycaster.setFromCamera(pickPointer, this.env.camera);
    const hits = pickRaycaster.intersectObjects(meshes, false);
    for (const hit of hits) {
      if (!isVisibleChain(hit.object)) continue;
      const matId = materialIdAtIntersection(hit);
      if (matId == null) continue;
      const mesh = hit.object as THREE.Mesh;
      const rec = this.mountArmorRecords.find((r) => r.meshes.includes(mesh));
      if (rec) {
        const thicknessOf =
          rec.thicknessOf ??
          mountArmorThicknessOf(this.sidecar?.armor?.mount_armor?.[rec.hp ?? ''], materialsTable);
        return {
          thicknessMm: thicknessOf(matId),
          materialId: matId,
          source: 'mount',
          zoneLabel: armorZoneLabel(mesh.name),
          layers: null,
          zones: null,
          hp: rec.hp,
          owner: rec.assetId,
          section: rec.section,
          point: hit.point.clone(),
        };
      }
      const m = materialsTable[String(matId)];
      return {
        thicknessMm: m?.thickness_mm ?? 0,
        materialId: matId,
        source: 'hull',
        zoneLabel: armorZoneLabel(mesh.name),
        layers: m?.layers ?? null,
        zones: m?.zones ?? null,
        hp: null,
        owner: null,
        section: null,
        point: hit.point.clone(),
      };
    }
    return null;
  }

  // ── Texture pipeline (delegated) ──────────────────────────────────────

  async setShowTextures(on: boolean, onProgress?: (msg: string) => void): Promise<void> {
    await this.textures.setShowTextures(on, onProgress);
  }

  async setActiveSkin(skinId: string, onProgress?: (msg: string) => void): Promise<void> {
    // In game, camouflages and mesh-swap exteriors are MUTUALLY EXCLUSIVE —
    // mounting a camo unequips the permoflage. Mirror that: picking any
    // skin other than the active exterior's own cross-linked scheme first
    // reverts to the default exterior (reloading the base hull when a
    // variant hull is up), then applies the requested camo.
    if (this.activeExteriorId !== 'default') {
      const active = this.getExteriors().find(
        (e) => e.exterior_id === this.activeExteriorId,
      );
      const ownKey = active?.camo_scheme_key ?? null;
      const target = this.textures.getSkins().find((s) => s.skin_id === skinId);
      const isOwn =
        !!ownKey && !!target && (target.skin_id === ownKey || target.scheme_key === ownKey);
      if (!isOwn) {
        await this.setActiveExterior('default', onProgress);
      }
    }
    await this.textures.setActiveSkin(skinId, onProgress);
  }

  // ── Exteriors (mesh-swap permoflage selector) ─────────────────────────

  /** The sidecar's `exteriors[]`, falling back to a synthesised `default`
   *  for pre-Step-0 sidecars. Index 0 is always the vanilla composition. */
  getExteriors(): readonly ExteriorRecord[] {
    const fromSidecar = this.sidecar?.exteriors;
    return fromSidecar && fromSidecar.length > 0 ? fromSidecar : [DEFAULT_EXTERIOR];
  }

  getActiveExteriorId(): string {
    return this.activeExteriorId;
  }

  /**
   * Full-reload path for hull-swap exteriors: re-run loadShip with
   * `exteriorOverrideId` set, so the variant hull GLB loads + its
   * HullDelta materials bind, then the override's mounts + camo apply on
   * the fresh composition. Restores the textures-on state across the
   * reload (clearShip resets it).
   */
  private async reloadWithExteriorHull(
    exteriorId: string,
    onProgress?: (msg: string) => void,
  ): Promise<void> {
    const ship = this.currentShip;
    const library = this.shipCtx?.library;
    if (!ship || !library) return;
    const wasShowing = this.textures.isShowingTextures();
    this.exteriorOverrideId = exteriorId;
    try {
      await this.loadShip(ship, library, onProgress);
    } finally {
      this.exteriorOverrideId = null;
    }
    if (wasShowing && !this.textures.isShowingTextures()) {
      await this.textures.setShowTextures(true, onProgress);
      this.textures.reapplyWetness();
    }
  }

  /**
   * Switch the active exterior: tear down + rebuild ONLY the swapped
   * mounts with the record's per-HP overrides (asset_id, the Ry180-baked
   * variant transform, the miscFilter whitelist), then re-sync every
   * subsystem that indexed the replaced Object3Ds, and finally flip the
   * paint via the record's `camo_scheme_key` cross-link into `skins[]`.
   *
   * Mirrors how the WG runtime equips a permoflage (geometry resolution +
   * camo bind are one selection). Hull-swap exteriors render their mounts
   * on the BASE hull until the producer's HullDelta step extracts variant
   * hull GLBs — `wg_asset_id != null` marks those records.
   */
  async setActiveExterior(
    exteriorId: string,
    onProgress?: (msg: string) => void,
  ): Promise<void> {
    const ctx = this.shipCtx;
    if (!ctx) return;
    if (exteriorId === this.activeExteriorId) return;
    const exteriors = this.getExteriors();
    const target = exteriors.find((e) => e.exterior_id === exteriorId);
    if (!target) {
      console.warn(`[exterior] unknown exterior ${exteriorId}; keeping current`);
      return;
    }
    // Hull routing (HullDelta): when the target's hull differs from the
    // one currently LOADED, the whole ship reloads with the target as the
    // exterior override — v1 full reload, engine-faithful (§8.5: WG itself
    // has no in-place mesh-swap; entity recreate is the native behavior).
    // Same-hull targets (incl. mount-only exteriors on the base hull)
    // continue into the cheap per-HP path below.
    const targetHullId = (target.hull as { hull_glb?: string | null } | null)?.hull_glb
      ? exteriorId
      : null;
    if (targetHullId !== this.activeHullExteriorId) {
      await this.reloadWithExteriorHull(exteriorId, onProgress);
      return;
    }

    const current = exteriors.find((e) => e.exterior_id === this.activeExteriorId) ?? null;
    const report = (msg: string) => onProgress?.(msg);
    report(`Switching exterior to ${target.display_name ?? exteriorId}…`);

    // Affected mounts = union of HPs swapped by the OLD and NEW records
    // (old-only HPs revert to base; new-only HPs gain the variant).
    const targetByHp = new Map<string, NonNullable<ExteriorRecord['mounts']>[number]>();
    for (const m of target.mounts ?? []) if (m.hp_name) targetByHp.set(m.hp_name, m);
    const affectedHps = new Set<string>(targetByHp.keys());
    for (const m of current?.mounts ?? []) if (m.hp_name) affectedHps.add(m.hp_name);

    // Base (vanilla) placements by instance_id — overrides always compose
    // against the ORIGINAL placements doc, never the previous exterior's.
    const baseByInstanceId = new Map<
      string,
      { section: ShipSectionKey; placement: ShipPlacement }
    >();
    for (const section of SHIP_SECTIONS) {
      for (const p of ctx.placementsDoc[section] ?? []) {
        baseByInstanceId.set(p.instance_id, { section, placement: p });
      }
    }

    const toRebuild: string[] = [];
    for (const [iid, hp] of this.hpByInstanceId) {
      if (affectedHps.has(hp) && baseByInstanceId.has(iid) && this.mountRecords.has(iid)) {
        toRebuild.push(iid);
      }
    }

    this.disposeMounts(toRebuild);

    // Compose the effective placement per mount and group by asset so each
    // swap-target GLB is fetched once (the accessory cache dedupes repeats).
    interface RebuildJob {
      section: ShipSectionKey;
      placement: ShipPlacement;
      miscFilter: string[] | null;
      /** Vanilla fallback when the swap-target GLB isn't in the library
       *  (variant never harvested — a missing variant must degrade to the
       *  base mount, never to a hole in the ship). Null on unswapped jobs. */
      fallback: { placement: ShipPlacement; miscFilter: string[] | null } | null;
    }
    const jobsByAsset = new Map<string, RebuildJob[]>();
    for (const iid of toRebuild) {
      const base = baseByInstanceId.get(iid);
      if (!base) continue;
      const hp = this.hpByInstanceId.get(iid);
      const swap = hp ? (targetByHp.get(hp) ?? null) : null;
      // Base-resolution miscFilter, exactly like loadShip (sidecar autofill
      // over placements JSON).
      const baseMiscFilter: string[] | null =
        ctx.miscFilterByInstanceId.get(iid) ?? base.placement.misc_filter ?? null;
      const placement: ShipPlacement = swap
        ? {
            ...base.placement,
            asset_id: swap.asset_id ?? base.placement.asset_id,
            dead_asset_id: swap.dead_asset_id ?? base.placement.dead_asset_id,
            transform: swap.transform?.matrix
              ? { ...base.placement.transform, matrix: swap.transform.matrix }
              : base.placement.transform,
            misc_filter: swap.misc_filter ?? undefined,
          }
        : base.placement;
      // miscFilter precedence: a swapped mount uses the record's value
      // VERBATIM (the nodesConfig override replaces the vanilla whitelist;
      // null = render all, [] = drop all).
      const miscFilter: string[] | null = swap ? (swap.misc_filter ?? null) : baseMiscFilter;
      const list = jobsByAsset.get(placement.asset_id) ?? [];
      list.push({
        section: base.section,
        placement,
        miscFilter,
        fallback:
          swap && placement.asset_id !== base.placement.asset_id
            ? { placement: base.placement, miscFilter: baseMiscFilter }
            : null,
      });
      jobsByAsset.set(placement.asset_id, list);
    }

    let rebuilt = 0;
    let fellBack = 0;
    for (const [assetId, jobs] of jobsByAsset) {
      const bundle = await this.loadAssetBundle(assetId, ctx);
      if (bundle) {
        for (const j of jobs) {
          this.instantiateMount(j.section, j.placement, bundle, j.miscFilter);
          rebuilt++;
        }
        continue;
      }
      // Swap-target GLB missing from the library (variant never built —
      // see the exteriors[] harvest note in the producer). Degrade to the
      // vanilla mounts so the ship never loses geometry.
      console.warn(
        `[exterior] asset ${assetId} unresolved in library; falling back to base mounts`,
      );
      for (const j of jobs) {
        if (!j.fallback) continue;
        const baseBundle = await this.loadAssetBundle(j.fallback.placement.asset_id, ctx);
        if (!baseBundle) continue;
        this.instantiateMount(j.section, j.fallback.placement, baseBundle, j.fallback.miscFilter);
        fellBack++;
      }
    }
    this.activeExteriorId = exteriorId;
    report(
      `Exterior ${exteriorId}: ${rebuilt} mount(s) rebuilt` +
        (fellBack ? `, ${fellBack} kept vanilla (variant GLB not in library)` : '') +
        '.',
    );

    // ── Re-sync, in dependency order ──────────────────────────────────

    // 1. Camo opt-out follows the active exterior. The default record
    //    falls back to the ship block's list (legacy / native-routed
    //    folders carry it there).
    this.textures.setVariantSwappedAssetIds(
      exteriorId === 'default'
        ? (this.sidecar?.ship?.variant_swapped_asset_ids ?? [])
        : (target.variant_swapped_asset_ids ?? []),
    );

    // 2. Paint: resolve camo_scheme_key against the skin table (skin_id
    //    first, scheme_key fallback — mat_* skins use the same string for
    //    both; tile schemes may be shared by several skins). A null key
    //    (skins entry never ingested, e.g. ARP) keeps the current skin.
    const camoKey = target.camo_scheme_key ?? null;
    if (camoKey) {
      const skins = this.textures.getSkins();
      const skin =
        skins.find((s) => s.skin_id === camoKey) ??
        skins.find((s) => s.scheme_key === camoKey);
      if (skin) {
        await this.textures.setActiveSkin(skin.skin_id, onProgress);
      } else {
        console.warn(`[exterior] camo_scheme_key ${camoKey} has no skins[] entry; keeping current skin`);
      }
    }

    // 3. Apply textures to the freshly-registered entries (setActiveSkin
    //    skips applyTextureState when the scheme didn't change, so the new
    //    mounts would otherwise stay untextured). Also re-evaluates the
    //    per-entry camo opt-out gates against the new set, and re-pushes
    //    weather wetness onto the rebuilt materials.
    if (this.textures.isShowingTextures()) {
      await this.textures.setShowTextures(true, onProgress);
      this.textures.reapplyWetness();
    }

    // 4. Visibility cascade (seam states + LOD policy) for the new mounts.
    this.applyAllStates();

    // 5. Armor X-ray: lazy-prepare + reveal the rebuilt mounts' armor
    //    (setArmorView(true) only touches records with empty entries).
    if (this.armorViewEnabled) this.setArmorView(true);

    // 6. Color mode onto the rebuilt color entries.
    if (this.colorMode !== 'off') {
      applyColorMode(this.placementColorEntries, this.colorMode);
    }

    // 7. Node overlay anchors were captured by world position from the old
    //    Object3Ds — re-derive against the live graph.
    this.nodeOverlay.refresh(this.shipRoot, this.sidecar?.effects?.attachments ?? null);
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

  /**
   * Light the ship with a WG sky-cube IBL (PMREM) for the given space/weather
   * (defaults to a clear-weather representative). Also data-drives the GT
   * tonemap curve from that weather's HDR settings. Returns false (and keeps
   * the procedural RoomEnvironment) when the environment library is absent.
   */
  async applyWgEnvironment(opts: { space?: string; weather?: string } = {}): Promise<boolean> {
    const env = await loadWgEnvironment(this.env.renderer, opts);
    if (!env) return false;
    this.wgEnv?.dispose();
    this.wgEnv = env;
    this.env.setEnvironment(env.texture);

    // WG keyed exposure: scale scene radiance so its log-average maps to
    // middleGray, then apply the per-space EV offset; avgLum is clamped to the
    // space's eye-adaptation window [eyeDarkLimit, eyeLightLimit]. The cube IBL
    // is now the dominant light, so dim the procedural fill to keep this
    // exposure meaningful (a data float CubeTexture would otherwise be lit by
    // both the cube AND the studio fill, over-exposing).
    const hdr = env.hdr;
    const numOf = (k: string, d: number): number =>
      typeof hdr[k] === 'number' ? (hdr[k] as number) : d;
    const middleGray = numOf('middleGray', 0.18);
    const off = numOf('hdrMapExposureOffset', 0);
    const darkLimit = numOf('eyeDarkLimit', 0.01);
    const lightLimit = numOf('eyeLightLimit', 100);
    const avg = Math.min(Math.max(env.avgLum, darkLimit), lightLimit);
    const exposure = (middleGray / avg) * Math.pow(2, off);

    const curve: Partial<{
      exposure: number;
      contrast: number;
      linearStart: number;
      linearLength: number;
      black: number;
    }> = { exposure };
    if (typeof hdr.gtContrast === 'number') curve.contrast = hdr.gtContrast;
    if (typeof hdr.gtLinearSectionStart === 'number') curve.linearStart = hdr.gtLinearSectionStart;
    if (typeof hdr.gtLinearSectionLength === 'number')
      curve.linearLength = hdr.gtLinearSectionLength;
    if (typeof hdr.gtBlack === 'number') curve.black = hdr.gtBlack;
    this.env.setTonemapParams(curve);

    // Drive the key directional from the per-weather WG sun (replacing the flat
    // stand-in). The cube owns ambient + specular, so kill the hemisphere fill.
    this.env.setFillLights(WG_FILL_HEMI);
    const sun = env.sun;
    if (sun && sun.yaw != null && sun.pitch != null) {
      const color = new THREE.Color();
      if (sun.color && sun.color.length >= 3) {
        color.setRGB(sun.color[0], sun.color[1], sun.color[2], THREE.LinearSRGBColorSpace);
      } else {
        color.setRGB(1, 1, 1, THREE.LinearSRGBColorSpace);
      }
      this.env.setSunLight({
        direction: sunDirection(sun.yaw, sun.pitch),
        color,
        intensity: WG_SUN_INTENSITY,
      });
    } else {
      this.env.setSunLight({ intensity: 0.35 });
    }
    this.syncParticleSunLighting();

    // Layer-1 rain wetness: tint albedo toward `wetnessColor` + drop roughness,
    // scaled by the per-weather `overallWetness` (dry in clear weather). The WG
    // `wetnessColor` is authored in linear RGB (same as the SH / sun color).
    const wet = env.wetness;
    let wetColor: THREE.Color | null = null;
    if (wet.wetnessColor && wet.wetnessColor.length >= 3) {
      wetColor = new THREE.Color().setRGB(
        wet.wetnessColor[0],
        wet.wetnessColor[1],
        wet.wetnessColor[2],
        THREE.LinearSRGBColorSpace,
      );
    }
    this.textures.setWetness({
      overallWetness: wet.overallWetness,
      wetnessColor: wetColor,
      puddlesIntensity: wet.puddlesIntensity,
    });
    return true;
  }

  /** Restore the procedural RoomEnvironment IBL + its default exposure/lights. */
  clearWgEnvironment(): void {
    this.wgEnv?.dispose();
    this.wgEnv = null;
    this.env.setEnvironment(null);
    this.env.setTonemapParams({
      exposure: DEFAULT_TONEMAP_PARAMS.exposure,
      contrast: DEFAULT_TONEMAP_PARAMS.contrast,
      linearStart: DEFAULT_TONEMAP_PARAMS.linearStart,
      linearLength: DEFAULT_TONEMAP_PARAMS.linearLength,
      black: DEFAULT_TONEMAP_PARAMS.black,
    });
    this.env.setFillLights(PROCEDURAL_FILL_HEMI, PROCEDURAL_FILL_DIR);
    this.env.setSunLight({ direction: PROCEDURAL_SUN_DIR.clone(), color: 0xffffff });
    this.syncParticleSunLighting();
    // Procedural sky has no weather → dry hull.
    this.textures.setWetness({ overallWetness: 0, wetnessColor: null, puddlesIntensity: 0 });
  }

  /** The active WG environment selection, or null when procedural. */
  getWgEnvironment(): WgEnvironmentInfo | null {
    if (!this.wgEnv) return null;
    const wet = this.wgEnv.wetness;
    return {
      space: this.wgEnv.space,
      weather: this.wgEnv.weather,
      avgLum: this.wgEnv.avgLum,
      wetness: {
        overallWetness: wet.overallWetness,
        wetnessColor: wet.wetnessColor ? [...wet.wetnessColor] : null,
        puddlesIntensity: wet.puddlesIntensity,
        ripplesIntensity: wet.ripplesIntensity,
      },
    };
  }

  async dispose(): Promise<void> {
    this.stopLoop();
    this.stopResize();
    this.removeNodeHover?.();
    this.nodeOverlay.dispose();
    this.clearShip();
    this.textures.dispose();
    await this.accessoryCache.dispose();
    disposeColorMaterials(this.colorMaterials);
    this.wgEnv?.dispose();
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

  private async buildShipParticleLayer(token: number): Promise<ShipParticleStats> {
    const attachments = this.sidecar?.effects?.attachments ?? [];
    const renderable = attachments.filter((a) => this.canAnchorParticleAttachment(a));
    const unanchored = attachments.length - renderable.length;
    const paths = [...new Set(renderable.map((a) => a.particle_path).filter(Boolean))].sort();
    const result = await fetchParticleRecords(paths, { concurrency: 8 });
    if (token !== this.particleBuildToken) return this.particleStats;

    this.disposeParticleLayer({ preserveEnabled: true, preserveToken: true });
    this.particleHasWaterDistortion = recordsHaveWaterDistortion(result.records);
    const expanded = this.expandParticleAttachments(renderable);
    const scene = new ParticleScene(this.env.renderer);
    scene.root.name = 'ShipParticleEffects';
    scene.setSortCamera(this.env.camera);
    this.syncParticleSunLighting(scene);
    this.env.scene.add(scene.root);

    // Every attachment is built to LOOP when active so the per-event loop
    // checkboxes (and 'all' mode) re-burst event effects continuously. Ambient
    // groups are active by default; event groups stay idle until their loop
    // checkbox is ticked (applyShipParticleActivation gates activation).
    const handles = scene.build(expanded, result.records, (a) => this.resolveParticleAnchor(a), {
      loopOneShot: () => true,
    });
    this.particleScene = scene;
    this.particleHandles = handles;
    this.particleAnchorObjects.clear();
    for (const h of handles) {
      const obj = this.resolveParticleAnchorObject(h.attachment);
      if (obj) {
        this.particleAnchorObjects.set(h, obj);
        this.copyParticleAnchorTransform(h, obj);
      }
      // Caliber-scaled muzzle blast: feed the pipeline's per-mount shotEffect
      // intensity as the effect's channel-0 value (drives PARTICLE_SIZE), so a
      // 16" gun's muzzle reads bigger than a 5" secondary's. Absent => the
      // effect's own default intensity (1.0). See ParticleAttachment.intensity.
      const inten = h.attachment.intensity;
      if (typeof inten === 'number' && Number.isFinite(inten)) {
        scene.setAttachmentIntensityValues(h, [inten]);
      }
      scene.setAttachmentActive(h, false);
    }
    scene.root.visible = this.particleLayerEnabled;
    this.updateParticleWaterPlane();

    let systems = 0;
    for (const h of handles) systems += h.systems.length;
    const ambient = handles.filter((h) => this.isAmbientParticleAttachment(h.attachment)).length;
    const events = handles.length - ambient;
    this.particleEventOptions = this.buildShipParticleEventOptions(handles);
    const stats: ShipParticleStats = {
      attachmentRows: attachments.length,
      renderableAttachments: renderable.length,
      anchorInstances: expanded.length,
      activeAttachments: 0,
      ambientAttachments: ambient,
      eventAttachments: events,
      eventOnlyAttachments: events,
      unanchoredAttachments: unanchored,
      unresolvedAnchors: Math.max(0, expanded.length - handles.length),
      uniquePaths: paths.length,
      recordsLoaded: Object.keys(result.records).length,
      missingRecords: result.missing.length + result.errors.length,
      systems,
    };
    this.particleStats = stats;
    this.applyShipParticleActivation();
    if (result.errors.length > 0) {
      console.warn('[ship particles] some particle records failed to load', result.errors);
    }
    return this.particleStats;
  }

  private syncParticleSunLighting(scene = this.particleScene): void {
    if (!scene) return;
    const sun = this.env.getSunLight();
    scene.setSunLighting(sun.direction, sun.color);
  }

  private disposeParticleLayer(
    opts: { preserveEnabled?: boolean; preserveToken?: boolean } = {},
  ): void {
    if (!opts.preserveToken) this.particleBuildToken++;
    if (!opts.preserveEnabled) {
      this.particleLayerEnabled = false;
      this.clearShipParticleEventLoops();
    }
    if (this.particleScene) {
      this.env.scene.remove(this.particleScene.root);
      this.particleScene.dispose();
    }
    this.particleScene = null;
    this.particleHasWaterDistortion = false;
    this.shipWaterEnabled = null;
    this.env.setWaterPlaneVisible(false);
    this.particleHandles = [];
    this.particleAnchorObjects.clear();
    this.particleAnchorMotion.clear();
    this.particleAttachmentAnchors = new WeakMap<ParticleAttachment, THREE.Object3D>();
    this.particleEventOptions = [];
    this.particleBuildPromise = null;
    this.particleStats = emptyShipParticleStats();
  }

  private canAnchorParticleAttachment(a: ParticleAttachment): boolean {
    if (!a.particle_path) return false;
    if (a.position?.length === 3) return true;
    return !!this.resolveParticleAnchorObject(a);
  }

  private isAmbientParticleAttachment(a: ParticleAttachment): boolean {
    const source = a.source ?? 'hull';
    if (source === 'map') return true;
    if (source !== 'hull') return false;
    const group = (a.group ?? '').toLowerCase();
    return SHIP_AMBIENT_PARTICLE_GROUPS.has(group) || isWakeParticleGroup(group);
  }

  private buildShipParticleEventOptions(
    handles: readonly ParticleAttachmentHandle[],
  ): ShipParticleEventOption[] {
    const byKey = new Map<string, ShipParticleEventOption>();
    for (const h of handles) {
      if (this.isAmbientParticleAttachment(h.attachment)) continue;
      const source = h.attachment.source ?? 'hull';
      const group = h.attachment.group ?? '';
      const key = shipParticleEventKey(h.attachment);
      const existing =
        byKey.get(key) ??
        ({
          key,
          source,
          group,
          label: `${source}:${group}`,
          handles: 0,
          systems: 0,
        } satisfies ShipParticleEventOption);
      existing.handles++;
      existing.systems += h.systems.length;
      byKey.set(key, existing);
    }
    return [...byKey.values()].sort((a, b) => {
      const sourceA = SHIP_PARTICLE_SOURCE_ORDER.get(a.source) ?? 999;
      const sourceB = SHIP_PARTICLE_SOURCE_ORDER.get(b.source) ?? 999;
      if (sourceA !== sourceB) return sourceA - sourceB;
      return a.group.localeCompare(b.group);
    });
  }


  private clearShipParticleEventLoops(): void {
    this.particleLoopKeys.clear();
  }

  private applyShipParticleActivation(): number {
    if (!this.particleScene) return 0;
    let active = 0;
    for (const h of this.particleHandles) {
      const key = shipParticleEventKey(h.attachment);
      const on =
        this.particleLayerEnabled &&
        (this.particleMode === 'all' ||
          this.particleLoopKeys.has(key) ||
          this.isAmbientParticleAttachment(h.attachment));
      this.particleScene.setAttachmentActive(h, on);
      if (on) active++;
    }
    this.particleScene.root.visible = this.particleLayerEnabled;
    this.particleStats = { ...this.particleStats, activeAttachments: active };
    return active;
  }

  private expandParticleAttachments(attachments: ParticleAttachment[]): ParticleAttachment[] {
    this.particleAttachmentAnchors = new WeakMap<ParticleAttachment, THREE.Object3D>();
    const out: ParticleAttachment[] = [];
    for (const a of attachments) {
      const muzzleAnchors = this.resolveShotEffectMuzzles(a);
      if (muzzleAnchors.length === 0) {
        out.push(a);
        continue;
      }
      muzzleAnchors.forEach((anchor, i) => {
        const clone: ParticleAttachment = {
          ...a,
          node: `${a.node || a.source_id || 'mount'}/${anchor.name || `muzzle${i + 1}`}`,
        };
        this.particleAttachmentAnchors.set(clone, anchor);
        out.push(clone);
      });
    }
    return out;
  }

  private resolveShotEffectMuzzles(a: ParticleAttachment): THREE.Object3D[] {
    if (a.group !== 'shotEffect') return [];
    if (a.source !== 'artillery' && a.source !== 'atba' && a.source !== 'airDefense') return [];
    const mountRoot = this.resolveParticleAnchorObject(a);
    if (!mountRoot) return [];

    const muzzles: THREE.Object3D[] = [];
    mountRoot.traverse((obj) => {
      if (/^HP_gunFire\d+$/i.test(obj.name)) muzzles.push(obj);
    });
    muzzles.sort((aObj, bObj) => muzzleIndex(aObj.name) - muzzleIndex(bObj.name));
    return muzzles;
  }

  private resolveParticleAnchor(a: ParticleAttachment): THREE.Vector3 | null {
    if (a.position?.length === 3) {
      return new THREE.Vector3(a.position[0], a.position[1], a.position[2]);
    }
    const obj = this.resolveParticleAnchorObject(a);
    return obj ? obj.getWorldPosition(new THREE.Vector3()) : null;
  }

  private resolveParticleAnchorObject(a: ParticleAttachment): THREE.Object3D | null {
    const mapped = this.particleAttachmentAnchors.get(a);
    if (mapped) return mapped;
    const keys = [a.node, a.source_id].filter((v): v is string => !!v);
    for (const key of keys) {
      const byHp = this.placementRootByHp.get(key);
      if (byHp) return byHp;
      const byInstance = this.placementRootByInstanceId.get(key);
      if (byInstance) return byInstance;
      const byName = this.shipRoot.getObjectByName(key);
      if (byName) return byName;
    }
    return null;
  }

  private updateParticleAttachmentTransforms(): void {
    if (!this.particleLayerEnabled || this.particleAnchorObjects.size === 0) return;
    this.shipRoot.updateMatrixWorld(false);
    const nowMs = performance.now();
    for (const [handle, obj] of this.particleAnchorObjects) {
      this.copyParticleAnchorTransform(handle, obj, nowMs);
    }
  }

  private copyParticleAnchorTransform(
    handle: ParticleAttachmentHandle,
    obj: THREE.Object3D,
    nowMs = performance.now(),
  ): void {
    obj.getWorldPosition(particleWorldPos);
    this.updateParticleAnchorMotion(handle, particleWorldPos, nowMs);
    handle.group.position.copy(particleWorldPos);
    handle.group.quaternion.copy(obj.getWorldQuaternion(particleWorldQuat));
  }

  private updateParticleAnchorMotion(
    handle: ParticleAttachmentHandle,
    worldPosition: THREE.Vector3,
    nowMs: number,
  ): void {
    // WG inheritVelocityFactor is a spawn-time velocity term. Sample the
    // anchor's world-space derivative here, then ParticleScene converts it to
    // each system's local frame before the next spawn.
    let motion = this.particleAnchorMotion.get(handle);
    if (!motion) {
      motion = {
        lastPosition: worldPosition.clone(),
        velocity: new THREE.Vector3(),
        lastTimeMs: nowMs,
      };
      this.particleAnchorMotion.set(handle, motion);
      this.particleScene?.setAttachmentParentVelocity(handle, motion.velocity);
      return;
    }
    const dt = (nowMs - motion.lastTimeMs) * 0.001;
    if (dt > 1e-5 && dt < 0.5) {
      motion.velocity.copy(worldPosition).sub(motion.lastPosition).multiplyScalar(1 / dt);
    } else {
      motion.velocity.set(0, 0, 0);
    }
    motion.lastPosition.copy(worldPosition);
    motion.lastTimeMs = nowMs;
    this.particleScene?.setAttachmentParentVelocity(handle, motion.velocity);
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
