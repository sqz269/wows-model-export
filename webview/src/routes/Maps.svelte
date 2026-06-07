<script lang="ts">
  // Maps route — Phase 1 of the maps webview.
  //
  // Two-pane layout: left = category-grouped picker (battle / dock / ops /
  // other), right = export action panel OR a three.js GLB viewer once a
  // map has been exported. Synchronous export per /api/maps/{name}/export
  // (3-8s default flags); a future Phase 2 can graduate to async jobs.
  //
  // The renderer uses `createSceneEnvironment` with map-scale defaults
  // (2 km grid, 50 km far plane, camera elevated 800 m) — different
  // enough from ship-scale that we don't share with the ship viewer.
  // Phase 1 scope is "load the GLB and orbit it"; per-instance dye /
  // material overrides land in Phase 2 once the producer-side sidecar
  // ships.

  import { onMount, untrack } from 'svelte';
  import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
  import * as THREE from 'three';

  import { fetchJson } from '$lib/api';
  import {
    listMaps,
    exportMap,
    mapGlbUrl,
    mapCollisionManifestUrl,
    deleteMapCache,
    type MapListEntry,
    type MapCategory,
    type MapExportFlags,
  } from '$lib/api/maps';
  import { Button } from '$lib/components/ui/button';
  import { navState } from '$lib/nav_state.svelte';
  import { navigate } from '$lib/router';
  import { createSceneEnvironment, type SceneEnvironment } from '$lib/three/scene';
  import { observeResize } from '$lib/three/resize';
  import { startRenderLoop } from '$lib/three/render_loop';
  import { disposeTree } from '$lib/three/dispose';
  import { ParticleScene } from '$lib/three/particles';
  import type { ParticleAttachment, ParticleRecord } from '$lib/types/sidecar';

  interface Props {
    spaceName: string | null;
    active: boolean;
  }
  const { spaceName, active: _active }: Props = $props();

  let items = $state<MapListEntry[]>([]);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let exporting = $state(false);
  let exportError = $state<string | null>(null);
  let exportFlags = $state<MapExportFlags>({
    max_texture_size: 512,
    terrain_step: 4,
    vegetation_density: 0,
  });

  const grouped = $derived(groupByCategory(items));
  const selected = $derived(spaceName ? (items.find((i) => i.name === spaceName) ?? null) : null);

  // Keep nav_state in sync so the topnav can route back to the last map.
  $effect(() => {
    if (spaceName) navState.lastSpaceName = spaceName;
  });

  // Reset transient export error when the selection changes.
  $effect(() => {
    void spaceName;
    untrack(() => {
      exportError = null;
    });
  });

  onMount(() => {
    void refresh();
  });

  async function refresh(): Promise<void> {
    loading = true;
    loadError = null;
    try {
      const resp = await listMaps();
      items = resp.items;
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  function groupByCategory(rows: MapListEntry[]): Record<MapCategory, MapListEntry[]> {
    const out: Record<MapCategory, MapListEntry[]> = {
      battle: [],
      dock: [],
      ops: [],
      other: [],
    };
    for (const r of rows) out[r.category].push(r);
    return out;
  }

  function select(name: string): void {
    navigate(`#/maps/${encodeURIComponent(name)}`);
  }

  async function triggerExport(extraFlags: MapExportFlags = {}): Promise<boolean> {
    if (!selected) return false;
    exporting = true;
    exportError = null;
    try {
      await exportMap(selected.name, { ...exportFlags, ...extraFlags });
      await refresh();
      return true;
    } catch (err) {
      exportError = err instanceof Error ? err.message : String(err);
      return false;
    } finally {
      exporting = false;
    }
  }

  async function triggerBuildCollisionManifest(): Promise<void> {
    if (!selected) return;
    const priorFlags = selected.export?.flags ?? exportFlags;
    if (await triggerExport({ ...priorFlags, collision_manifest: true })) {
      collisionError = null;
      showCollision = true;
    }
  }

  async function triggerDelete(): Promise<void> {
    if (!selected) return;
    try {
      await deleteMapCache(selected.name);
      await refresh();
    } catch (err) {
      exportError = err instanceof Error ? err.message : String(err);
    }
  }

  // ── three.js viewer ────────────────────────────────────────────────
  // Mounted whenever `selected.exported` flips true. Single env per
  // selection; teardown on change. Map-scale defaults: 2 km grid, 50 km
  // far plane, camera at (1500, 800, 1500) looking at origin.

  let canvasContainer = $state<HTMLDivElement | null>(null);
  let viewerError = $state<string | null>(null);
  let viewerLoading = $state(false);
  let viewerStats = $state<{
    nodes: number;
    bbox: { min: [number, number, number]; max: [number, number, number] } | null;
    landscapeCount: number;
    fogDensity: number | null;
    lodCullableCount: number;
    lightCount: number;
    qualityTaggedInstances: number;
    qualityHiddenInstances: number;
    qualityTaggedLights: number;
    qualityHiddenLights: number;
    vegetationSpecies: number;
    vegetationInstances: number;
    mapParticleAnchors: number;
    mapParticleResolved: number;
    collisionModels: number;
    collisionObstacles: number;
    collisionTriangles: number;
    dyedInstances: number;
    materialOverrideInstances: number;
  } | null>(null);

  // Whether to render landscape-flagged instances (the LNR* / TILEDLAND
  // backdrop proxies). Default ON — they're real engine content and
  // belong in the scene. Toggle for users who want to inspect just the
  // playable foreground.
  let showLandscape = $state(true);

  // Whether to render water opaquely (engine-faithful: water plane
  // occludes underwater geometry from above-water camera angles).
  // Default ON — the engine doesn't show-through; the GLB's alpha=0.85
  // is a producer artefact. Toggle OFF to inspect submerged geometry.
  let opaqueWater = $state(true);

  // Per-LOD extent culling. Engine semantics: when camera distance to an
  // instance exceeds the instance's outermost LOD extent (last element of
  // `lod_extents`), the engine stops drawing that asset entirely. Hard
  // cut, not soft fade. Toggle OFF to inspect culled content.
  let lodCullEnabled = $state(true);

  // Raw authored model quality gate. Native token map is descending quality:
  // MAX/MAXIMUM=0, VERYHIGH=1, HIGH=2, MEDIUM=3, LOW=4,
  // VERYLOW=5, MIN/MINIMUM=6, OFF=7, USER/CUSTOM=8. Draw only
  // when runtime raw quality is at least as good as the authored
  // minimum, i.e. runtime_raw <= authored_min_raw.
  const QUALITY_RUNTIME_OPTIONS = [
    { value: 0, label: '0 MAXIMUM (all)' },
    { value: 1, label: '1 VERYHIGH' },
    { value: 2, label: '2 HIGH' },
    { value: 3, label: '3 MEDIUM' },
    { value: 4, label: '4 LOW' },
    { value: 5, label: '5 VERYLOW' },
    { value: 6, label: '6 MINIMUM' },
    { value: 7, label: '7 OFF' },
    { value: 8, label: '8 USER/CUSTOM' },
  ] as const;
  const DYNAMIC_LIGHTING_RUNTIME_OPTIONS = [
    { value: 2, label: '2 HIGH' },
    { value: 3, label: '3 MEDIUM' },
    { value: 4, label: '4 LOW' },
    { value: 7, label: '7 OFF/other' },
  ] as const;
  let modelRuntimeQualityRaw = $state(0);
  let dynamicLightingRuntimeRaw = $state(2);

  // Engine point lights from `space.bin` pointLights[] (stride 0xc0).
  // Toggle ON to add the engine's atmospheric/accent lights; OFF to inspect
  // the unlit scene. Lights are small-radius accents (~1-5m) and won't be
  // visible from an overview camera unless the user zooms in.
  let showLights = $state(true);
  let lightObjects: THREE.PointLight[] = [];

  // Vegetation rendering. The toolkit emits one `Tree_<species>_<i>` node
  // per tree instance — for Okinawa that's ~5,269 individual nodes. We
  // collapse them into one `THREE.InstancedMesh` per species at load time,
  // cutting draw calls from thousands to single digits. Toggleable; the
  // whole `Vegetation` group's `.visible` flips in one assignment.
  let showVegetation = $state(true);
  let vegetationGroup: THREE.Group | null = null;

  // Map-authored particle anchors from `space.bin.particles[]`. These are
  // distinct from ship-side Effect attachments: placement comes from the map
  // scene extras, while effect prototypes still come from /api/particles.
  let showMapParticles = $state(false);
  let mapParticleAnchors = $state<MapParticleAnchor[]>([]);
  let mapParticleLoading = $state(false);
  let mapParticleError = $state<string | null>(null);
  let mapParticleStats = $state<{
    anchors: number;
    resolvedAnchors: number;
    uniquePaths: number;
    records: number;
    activeAnchors: number;
    systems: number;
    missingRecords: number;
  } | null>(null);
  let mapParticleScene: ParticleScene | null = null;
  let mapParticleLoadToken = 0;

  // Diagnostic map collision overlay. Backed by the optional
  // `wows.map.collision_manifest.v1` sidecar. The proxy meshes use raw
  // loader-level face loops triangulated as simple fans; this is for
  // inspection, not native solver parity.
  let showCollision = $state(false);
  let collisionLoading = $state(false);
  let collisionError = $state<string | null>(null);
  let collisionStats = $state<{
    models: number;
    obstacles: number;
    triangles: number;
  } | null>(null);
  let collisionGroup: THREE.Group | null = null;
  let collisionLoadToken = 0;
  const MISSING_COLLISION_MANIFEST_ERROR = 'collision manifest not exported';

  // Live handles to the scene root + env so the showLandscape toggle
  // doesn't need to rebuild the whole scene. Set by the load effect;
  // cleared on teardown.
  let activeRoot = $state<THREE.Object3D | null>(null);
  let activeEnv = $state<SceneEnvironment | null>(null);
  let activeFog = $state<THREE.FogExp2 | null>(null);
  let engineFogDensity = $state<number | null>(null);
  let pickedGeometry = $state<MapPickInfo | null>(null);
  let pickHelper: THREE.BoxHelper | null = null;

  // Pre-collected list of instance nodes that carry a `lod_extents` array.
  // Built once per load; the render loop walks this list per-frame instead
  // of traversing the whole scene tree. World position is cached because
  // map placements are static — they never animate.
  interface LodInstance {
    node: THREE.Object3D;
    worldPos: THREE.Vector3;
    maxExtentSq: number;
    isLandscape: boolean;
    minQualityLevel: number | null;
  }
  let lodInstances: LodInstance[] = [];

  // Engine fog density is tuned for a ship-level (~30m altitude) camera
  // where 1-2 km is the practical visibility envelope. Our overview
  // camera typically sits ~1500m above sea level, so the same density
  // fogs out the entire scene from the user's vantage. Divide the
  // engine value by this scale factor for the initial render; expose a
  // slider so users can dial back toward the engine value when they
  // zoom in to ground level.
  const FOG_OVERVIEW_SCALE_DEFAULT = 30;
  let fogScale = $state(FOG_OVERVIEW_SCALE_DEFAULT);

  /** Scene-level extras emitted by the toolkit. See the toolkit's
   *  `build_scene_extras` in gltf_export.rs. */
  interface MapSceneExtras {
    bounds?: { min_x: number; max_x: number; min_z: number; max_z: number };
    fog?: {
      fog_color: [number, number, number, number];
      fog_density: number;
      fog_near_distance: number;
      far_plane: number;
    };
    lights?: MapLight[];
    particles?: MapParticleAnchor[];
  }

  /** A single engine point-light, world-space. RGBA convention: RGB is
   *  linear color, A is the intensity multiplier. */
  interface MapLight {
    type: 'point';
    position: [number, number, number];
    color: [number, number, number, number];
    radius: number;
    min_quality: number;
  }

  interface MapParticleAnchor {
    position?: [number, number, number];
    transform?: number[];
    resource_id?: number;
    resource_id_hex?: string;
    resource_path?: string | null;
    intensity_count?: number;
    intensity_values?: number[];
  }

  interface CollisionManifest {
    schema: 'wows.map.collision_manifest.v1';
    obstacle_count: number;
    collision_model_count: number;
    referenced_collision_model_count: number;
    collision_model_parse_error_count: number;
    obstacles: CollisionObstacle[];
    collision_models: CollisionModelRecord[];
  }

  interface CollisionObstacle {
    transform: number[];
    candidate_collision_model_index: number;
    candidate_collision_model_valid: boolean;
  }

  interface CollisionModelRecord {
    index: number;
    referenced_by_obstacle_count: number;
    parse_error?: string | null;
    stats?: {
      debug_fan_triangle_count: number;
      native_postload_triangle_candidate_count: number;
      face_with_vertex_zero_count: number;
    };
    objects?: CollisionObjectRecord[];
  }

  interface CollisionObjectRecord {
    vertices: [number, number, number][];
    faces: CollisionFaceRecord[];
  }

  interface CollisionFaceRecord {
    debug_fan_triangles: [number, number, number][];
  }

  /** Per-instance extras emitted by the toolkit. See
   *  `build_instance_extras`. */
  interface InstanceExtras {
    is_landscape?: boolean;
    min_quality_level?: number;
    stable_guid?: string;
    lod_extents?: number[];
    /** `[[matter_id, replaces_id], ...]` — opaque u32 pairs identifying
     *  per-instance dye overrides. Themed event maps carry hundreds;
     *  "plain" maps zero. Webview v1 surfaces the count only and doesn't
     *  yet apply the override — the matter_id needs a hash-table lookup
     *  to resolve to an actual texture/color tweak. */
    dyes?: [number, number][];
    /** Count of materialInstances[] overrides on this instance. v1
     *  surfaces the count; decoding the 0x70-byte
     *  MaterialInstancePrototype records is a follow-up. */
    material_instance_count?: number;
    gltf_node_index?: number;
    gltf_node_name?: string;
    gltf_mesh_index?: number;
    gltf_mesh_name?: string;
    gltf_primitive_index?: number;
    vegetation_species_mesh?: number;
    instance_count?: number;
  }

  interface GltfJson {
    nodes?: Array<{ name?: string; mesh?: number }>;
    meshes?: Array<{ name?: string; primitives?: Array<{ material?: number }> }>;
    materials?: Array<{ name?: string }>;
  }

  interface GltfAssociation {
    nodes?: number;
    meshes?: number;
    primitives?: number;
    materials?: number;
  }

  interface GltfWithAssociations {
    parser?: {
      json?: GltfJson;
      associations?: { get(object: object): GltfAssociation | undefined };
    };
  }

  interface PickHitSummary {
    objectName: string;
    meshName: string;
    geometryName: string;
    materialNames: string[];
    distance: number;
    faceIndex: number | null;
    instanceId: number | null;
  }

  interface MapPickInfo extends PickHitSummary {
    parentPath: string[];
    point: [number, number, number];
    objectUuid: string;
    geometryUuid: string | null;
    triangles: number | null;
    stableGuid: string | null;
    gltfNodeIndex: number | null;
    gltfMeshIndex: number | null;
    gltfPrimitiveIndex: number | null;
    hits: PickHitSummary[];
  }

  /** Build a world bbox suitable for camera framing. Prefers scene.extras
   *  bounds (engine-authoritative playable extent) over a Terrain mesh
   *  scan. Falls back to a full content bbox when neither is available.
   *  Returns mesh + per-instance-override counts collected in the same
   *  pass (saving a second traversal). */
  function computeFrameBox(
    root: THREE.Object3D,
    sceneExtras: MapSceneExtras,
  ): {
    box: THREE.Box3;
    meshCount: number;
    landscapeCount: number;
    dyedInstances: number;
    materialOverrideInstances: number;
  } {
    let meshCount = 0;
    let landscapeCount = 0;
    let dyedInstances = 0;
    let materialOverrideInstances = 0;
    root.traverse((o) => {
      if ((o as THREE.Mesh).isMesh) meshCount++;
      const ud = o.userData as InstanceExtras;
      if (!ud) return;
      if (ud.is_landscape) landscapeCount++;
      if (ud.dyes && ud.dyes.length > 0) dyedInstances++;
      if ((ud.material_instance_count ?? 0) > 0) materialOverrideInstances++;
    });

    // Prefer engine bounds. Y comes from terrain min/max (the engine
    // doesn't store vertical extent in space.settings).
    const box = new THREE.Box3();
    if (sceneExtras.bounds) {
      const b = sceneExtras.bounds;
      let yMin = 0;
      let yMax = 60;
      root.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (mesh.isMesh && mesh.name === 'Terrain') {
          const tb = new THREE.Box3().setFromObject(mesh);
          yMin = Math.min(yMin, tb.min.y);
          yMax = Math.max(yMax, tb.max.y);
        }
      });
      box.min.set(b.min_x, yMin, b.min_z);
      box.max.set(b.max_x, yMax, b.max_z);
    } else {
      root.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (mesh.isMesh && mesh.name !== 'Terrain') {
          box.union(new THREE.Box3().setFromObject(mesh));
        }
      });
    }

    return { box, meshCount, landscapeCount, dyedInstances, materialOverrideInstances };
  }

  function annotateGltfSourceNames(root: THREE.Object3D, gltf: GltfWithAssociations): void {
    const parser = gltf.parser;
    const json = parser?.json;
    const associations = parser?.associations;
    if (!json || !associations) return;

    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      const assoc = associations.get(mesh);
      const nodeIndex = assoc?.nodes;
      const meshIndex =
        assoc?.meshes ?? (nodeIndex != null ? json.nodes?.[nodeIndex]?.mesh : undefined);
      const primitiveIndex = assoc?.primitives;
      const materialIndex =
        assoc?.materials ??
        (meshIndex != null && primitiveIndex != null
          ? json.meshes?.[meshIndex]?.primitives?.[primitiveIndex]?.material
          : undefined);
      const nodeName = nodeIndex != null ? json.nodes?.[nodeIndex]?.name : undefined;
      const meshName = meshIndex != null ? json.meshes?.[meshIndex]?.name : undefined;
      const materialName =
        materialIndex != null ? json.materials?.[materialIndex]?.name : undefined;

      const ud = mesh.userData as InstanceExtras;
      if (nodeIndex != null) ud.gltf_node_index = nodeIndex;
      if (nodeName) ud.gltf_node_name = nodeName;
      if (meshIndex != null) ud.gltf_mesh_index = meshIndex;
      if (meshName) {
        ud.gltf_mesh_name = meshName;
        if (!mesh.geometry.name) mesh.geometry.name = meshName;
      }
      if (primitiveIndex != null) ud.gltf_primitive_index = primitiveIndex;
      if (materialName) {
        for (const mat of materialsOf(mesh)) {
          if (!mat.name) mat.name = materialName;
        }
      }
    });
  }

  function materialsOf(mesh: THREE.Mesh): THREE.Material[] {
    const mat = mesh.material;
    if (!mat) return [];
    return Array.isArray(mat) ? mat : [mat];
  }

  function materialNamesOf(mesh: THREE.Mesh): string[] {
    const names = materialsOf(mesh).map((mat) => mat.name || mat.type || '(unnamed material)');
    return names.length > 0 ? names : ['(no material)'];
  }

  function actualVisible(object: THREE.Object3D): boolean {
    for (let n: THREE.Object3D | null = object; n; n = n.parent) {
      if (!n.visible) return false;
    }
    return true;
  }

  function pickableMeshes(root: THREE.Object3D): THREE.Mesh[] {
    const meshes: THREE.Mesh[] = [];
    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh || !actualVisible(mesh)) return;
      meshes.push(mesh);
    });
    return meshes;
  }

  function parentPath(object: THREE.Object3D): string[] {
    const names: string[] = [];
    for (let n: THREE.Object3D | null = object; n && names.length < 12; n = n.parent) {
      if (n.name) names.push(n.name);
      if (n === activeRoot || n === collisionGroup) break;
    }
    return names.reverse();
  }

  function ancestorUserData<T>(object: THREE.Object3D, key: keyof InstanceExtras): T | null {
    for (let n: THREE.Object3D | null = object; n; n = n.parent) {
      const value = (n.userData as InstanceExtras | undefined)?.[key];
      if (value != null) return value as T;
      if (n === activeRoot || n === collisionGroup) break;
    }
    return null;
  }

  function triangleCount(geometry: THREE.BufferGeometry | undefined): number | null {
    if (!geometry) return null;
    if (geometry.index) return Math.floor(geometry.index.count / 3);
    const pos = geometry.getAttribute('position');
    return pos ? Math.floor(pos.count / 3) : null;
  }

  function summarizePickHit(hit: THREE.Intersection): PickHitSummary {
    const mesh = hit.object as THREE.Mesh;
    const ud = mesh.userData as InstanceExtras;
    const meshName = ud.gltf_mesh_name || mesh.geometry?.name || mesh.name || mesh.type;
    return {
      objectName: mesh.name || ud.gltf_node_name || meshName,
      meshName,
      geometryName: mesh.geometry?.name || ud.gltf_mesh_name || '(unnamed geometry)',
      materialNames: materialNamesOf(mesh),
      distance: hit.distance,
      faceIndex: hit.faceIndex ?? null,
      instanceId: hit.instanceId ?? null,
    };
  }

  function buildPickInfo(hit: THREE.Intersection, hits: THREE.Intersection[]): MapPickInfo {
    const mesh = hit.object as THREE.Mesh;
    const ud = mesh.userData as InstanceExtras;
    const point = hit.point;
    return {
      ...summarizePickHit(hit),
      parentPath: parentPath(mesh),
      point: [point.x, point.y, point.z],
      objectUuid: mesh.uuid,
      geometryUuid: mesh.geometry?.uuid ?? null,
      triangles: triangleCount(mesh.geometry),
      stableGuid: ancestorUserData<string>(mesh, 'stable_guid'),
      gltfNodeIndex: ud.gltf_node_index ?? null,
      gltfMeshIndex: ud.gltf_mesh_index ?? null,
      gltfPrimitiveIndex: ud.gltf_primitive_index ?? null,
      hits: hits.slice(0, 8).map(summarizePickHit),
    };
  }

  function disposePickHelper(): void {
    if (!pickHelper) return;
    pickHelper.parent?.remove(pickHelper);
    pickHelper.geometry.dispose();
    const mat = pickHelper.material;
    if (Array.isArray(mat)) {
      for (const m of mat) m.dispose();
    } else {
      mat.dispose();
    }
    pickHelper = null;
  }

  function setPickHelper(env: SceneEnvironment, object: THREE.Object3D): void {
    disposePickHelper();
    pickHelper = new THREE.BoxHelper(object, 0xffd166);
    pickHelper.name = 'MapPickHighlight';
    pickHelper.renderOrder = 9999;
    const mat = pickHelper.material as THREE.Material;
    mat.depthTest = false;
    mat.depthWrite = false;
    pickHelper.update();
    env.scene.add(pickHelper);
  }

  function pickGeometryFromPointer(
    event: PointerEvent,
    env: SceneEnvironment,
    root: THREE.Object3D,
  ): void {
    const rect = env.renderer.domElement.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const pointer = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -(((event.clientY - rect.top) / rect.height) * 2 - 1),
    );
    const targets = pickableMeshes(root);
    if (collisionGroup?.visible) targets.push(...pickableMeshes(collisionGroup));

    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(pointer, env.camera);
    const hits = raycaster.intersectObjects(targets, false);
    if (hits.length === 0) {
      pickedGeometry = null;
      disposePickHelper();
      return;
    }

    pickedGeometry = buildPickInfo(hits[0], hits);
    setPickHelper(env, hits[0].object);
  }

  async function copyPickedGeometry(): Promise<void> {
    if (!pickedGeometry) return;
    await navigator.clipboard?.writeText(JSON.stringify(pickedGeometry, null, 2));
  }

  function formatPickNumber(n: number): string {
    if (!Number.isFinite(n)) return 'nan';
    if (Math.abs(n) >= 1000) return n.toFixed(0);
    if (Math.abs(n) >= 100) return n.toFixed(1);
    return n.toFixed(2);
  }

  function formatPickPoint(point: [number, number, number]): string {
    return point.map(formatPickNumber).join(', ');
  }

  function rawQualityValue(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function passesQualityGate(value: unknown, threshold: number): boolean {
    const q = rawQualityValue(value);
    return q == null || threshold <= q;
  }

  function applyInstanceFilters(
    root: THREE.Object3D,
    showLand: boolean,
    threshold: number,
  ): { tagged: number; hidden: number } {
    let tagged = 0;
    let hidden = 0;
    root.traverse((o) => {
      const ud = o.userData as InstanceExtras | undefined;
      const q = rawQualityValue(ud?.min_quality_level);
      const hasLandscapeFlag = !!ud?.is_landscape;
      if (q == null && !hasLandscapeFlag) return;
      if (q != null) tagged += 1;
      const visible = (!hasLandscapeFlag || showLand) && passesQualityGate(q, threshold);
      if (q != null && threshold > q) hidden += 1;
      if (o.visible !== visible) o.visible = visible;
    });
    return { tagged, hidden };
  }

  function updateViewerQualityStats(
    instanceStats: { tagged: number; hidden: number } | null,
    lightStats: { tagged: number; hidden: number } | null,
  ): void {
    const stats = untrack(() => viewerStats);
    if (!stats) return;
    viewerStats = {
      ...stats,
      qualityTaggedInstances: instanceStats?.tagged ?? stats.qualityTaggedInstances,
      qualityHiddenInstances: instanceStats?.hidden ?? stats.qualityHiddenInstances,
      qualityTaggedLights: lightStats?.tagged ?? stats.qualityTaggedLights,
      qualityHiddenLights: lightStats?.hidden ?? stats.qualityHiddenLights,
    };
  }

  /** Walk the scene once and gather every instance node that carries a
   *  non-empty `lod_extents` array. Caches the world position because
   *  map placements never animate, so re-querying each frame would just
   *  burn CPU on parent-chain matrix walks. Invariant: no caller mutates
   *  ancestor transforms of these nodes after this returns — if that ever
   *  changes (recenter, scale toggle), the cache must be invalidated.
   *
   *  `maxExtent` uses `Math.max` rather than `ext[length-1]` so a
   *  mis-sorted producer array still picks the outermost cap. Engine LODs
   *  are authored finest-first, but defensive in case that ever changes. */
  function collectLodInstances(root: THREE.Object3D): LodInstance[] {
    const out: LodInstance[] = [];
    root.updateMatrixWorld(true);
    root.traverse((o) => {
      const ud = o.userData as InstanceExtras | undefined;
      const ext = ud?.lod_extents;
      if (!ext || ext.length === 0) return;
      let maxExtent = 0;
      for (const e of ext) {
        if (Number.isFinite(e) && e > maxExtent) maxExtent = e;
      }
      if (maxExtent <= 0) return;
      const worldPos = new THREE.Vector3();
      o.getWorldPosition(worldPos);
      out.push({
        node: o,
        worldPos,
        maxExtentSq: maxExtent * maxExtent,
        isLandscape: !!ud?.is_landscape,
        minQualityLevel: rawQualityValue(ud?.min_quality_level),
      });
    });
    return out;
  }

  /** Group vegetation under one layer for stats/toggles. New toolkit GLBs
   *  arrive as `EXT_mesh_gpu_instancing` nodes named `Tree_<species>_instances`;
   *  legacy GLBs arrive as per-tree `Tree_<species>_<i>` mesh nodes and are
   *  collapsed into one `THREE.InstancedMesh` per species at load time.
   *  World matrices of legacy tree nodes are baked into the per-instance
   *  matrices via the root's inverse, so the result is positionally identical
   *  regardless of hierarchy quirks. Trees with a material array are left alone
   *  because `InstancedMesh` doesn't support that case. */
  function collapseVegetation(root: THREE.Object3D): {
    group: THREE.Group | null;
    speciesCount: number;
    instanceCount: number;
  } {
    interface SpeciesBucket {
      meshes: THREE.Mesh[];
      geometry: THREE.BufferGeometry;
      material: THREE.Material;
      minQualityLevel: number | null;
    }
    interface InstancedVegetation {
      mesh: THREE.InstancedMesh;
      bucketKey: string;
      minQualityLevel: number | null;
    }
    const bySpecies = new Map<string, SpeciesBucket>();
    const preInstanced: InstancedVegetation[] = [];
    const toRemove: THREE.Object3D[] = [];
    const TREE_NAME = /^Tree_(\d+)_\d+$/;
    const TREE_INSTANCED_NAME = /^Tree_(\d+)_instances$/;

    root.traverse((o) => {
      const instanced = o as THREE.InstancedMesh;
      if (instanced.isInstancedMesh) {
        const match = instanced.name.match(TREE_INSTANCED_NAME);
        if (match) {
          const speciesIdx = match[1];
          const minQualityLevel = rawQualityValue(instanced.userData?.min_quality_level);
          const bucketKey = `${speciesIdx}:${minQualityLevel ?? 'any'}`;
          preInstanced.push({ mesh: instanced, bucketKey, minQualityLevel });
          return;
        }
      }

      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      const match = mesh.name.match(TREE_NAME);
      if (!match) return;
      if (Array.isArray(mesh.material)) return;
      const speciesIdx = match[1];
      const minQualityLevel = rawQualityValue(mesh.userData?.min_quality_level);
      const bucketKey = `${speciesIdx}:${minQualityLevel ?? 'any'}`;
      let bucket = bySpecies.get(bucketKey);
      if (!bucket) {
        bucket = {
          meshes: [],
          geometry: mesh.geometry,
          material: mesh.material as THREE.Material,
          minQualityLevel,
        };
        bySpecies.set(bucketKey, bucket);
      } else if (mesh.geometry !== bucket.geometry || mesh.material !== bucket.material) {
        // GLTFLoader shares geometry+material across nodes referencing the
        // same glTF mesh, so this should never trip. If it does, the
        // species has unexpected per-instance overrides we'd silently
        // collapse — bail out for this mesh and leave it as a Mesh.
        return;
      }
      bucket.meshes.push(mesh);
      toRemove.push(mesh);
    });

    if (bySpecies.size === 0 && preInstanced.length === 0) {
      return { group: null, speciesCount: 0, instanceCount: 0 };
    }

    // One root-wide matrixWorld update suffices for every descendant — no
    // need to re-call per-mesh inside the species loop.
    root.updateMatrixWorld(true);
    const rootInv = new THREE.Matrix4().copy(root.matrixWorld).invert();
    const tmp = new THREE.Matrix4();

    const group = new THREE.Group();
    group.name = 'Vegetation';
    const speciesKeys = new Set<string>();
    let totalInstances = 0;

    for (const [bucketKey, bucket] of bySpecies) {
      const im = new THREE.InstancedMesh(bucket.geometry, bucket.material, bucket.meshes.length);
      im.name = `Vegetation_${bucketKey.replace(':', '_q')}`;
      im.userData = {
        ...(bucket.meshes[0].userData as InstanceExtras),
        instance_count: bucket.meshes.length,
      };
      if (bucket.minQualityLevel != null) im.userData.min_quality_level = bucket.minQualityLevel;
      for (let i = 0; i < bucket.meshes.length; i++) {
        tmp.copy(rootInv).multiply(bucket.meshes[i].matrixWorld);
        im.setMatrixAt(i, tmp);
      }
      im.instanceMatrix.needsUpdate = true;
      // Compute the full instance bounding sphere so frustum culling works
      // correctly — without this, three.js culls based on a single tree's
      // geometry bbox and the entire species pops in/out as that one
      // reference tree drifts in/out of view.
      im.computeBoundingSphere();
      group.add(im);
      speciesKeys.add(bucketKey);
      totalInstances += bucket.meshes.length;
    }

    for (const n of toRemove) n.parent?.remove(n);

    for (const item of preInstanced) {
      item.mesh.updateMatrixWorld(true);
      tmp.copy(rootInv).multiply(item.mesh.matrixWorld);
      item.mesh.parent?.remove(item.mesh);
      item.mesh.matrix.copy(tmp);
      item.mesh.matrix.decompose(item.mesh.position, item.mesh.quaternion, item.mesh.scale);
      item.mesh.matrixWorldNeedsUpdate = true;
      if (item.minQualityLevel != null) item.mesh.userData.min_quality_level = item.minQualityLevel;
      item.mesh.userData.instance_count = item.mesh.count;
      group.add(item.mesh);
      speciesKeys.add(item.bucketKey);
      totalInstances += item.mesh.count;
    }

    root.add(group);

    return { group, speciesCount: speciesKeys.size, instanceCount: totalInstances };
  }

  /** Instantiate engine point lights from scene extras. Each emitted as a
   *  `THREE.PointLight` with `distance = radius`, `decay = 2` (physical
   *  inverse-square — matches "small accent radius" lighting better than
   *  the linear default). Lights are attached to the scene root so the
   *  existing teardown removes them when the scene root is removed.
   *
   *  HDR handling: the engine encodes light strength in RGB magnitude
   *  (alpha is observed ≡ 1.0 across the Okinawa sample). We normalize
   *  color to its peak channel and push the magnitude into intensity, so
   *  a dim grey light (RGB=0.3) renders as white@0.3 intensity instead
   *  of grey@1.0 (which doesn't read as dim from typical viewing angles),
   *  and an HDR amber RGB=(1.57, 0.79, 0.34) keeps its hue with
   *  intensity 1.57. */
  function instantiateLights(root: THREE.Object3D, lights: MapLight[]): THREE.PointLight[] {
    const out: THREE.PointLight[] = [];
    for (const l of lights) {
      if (l.type !== 'point') continue;
      if (!Array.isArray(l.position) || l.position.length < 3) continue;
      if (!Array.isArray(l.color) || l.color.length < 4) continue;
      const [px, py, pz] = l.position;
      if (!Number.isFinite(px) || !Number.isFinite(py) || !Number.isFinite(pz)) continue;
      const r = Math.max(0, l.color[0]);
      const g = Math.max(0, l.color[1]);
      const b = Math.max(0, l.color[2]);
      const a = Math.max(0, l.color[3]);
      const peak = Math.max(r, g, b);
      if (peak <= 0) continue;
      const color = new THREE.Color(r / peak, g / peak, b / peak);
      const intensity = a * peak;
      const radius = Number.isFinite(l.radius) && l.radius > 0 ? l.radius : 1;
      const light = new THREE.PointLight(color, intensity, radius, 2);
      light.position.set(px, py, pz);
      light.userData.min_quality = rawQualityValue(l.min_quality);
      root.add(light);
      out.push(light);
    }
    return out;
  }

  function applyLightFilter(
    lights: THREE.PointLight[],
    show: boolean,
    threshold: number,
  ): { tagged: number; hidden: number } {
    let tagged = 0;
    let hidden = 0;
    for (const lt of lights) {
      const q = rawQualityValue(lt.userData.min_quality);
      if (q != null) tagged += 1;
      const visible = show && passesQualityGate(q, threshold);
      if (q != null && threshold > q) hidden += 1;
      if (lt.visible !== visible) lt.visible = visible;
    }
    return { tagged, hidden };
  }

  type ParticleRecordResponse = {
    ok: boolean;
    path?: string;
    quality_used?: string | null;
    record?: ParticleRecord;
    error?: string;
  };

  function usableMapParticleAnchors(anchors: MapParticleAnchor[]): MapParticleAnchor[] {
    return anchors.filter((a) => {
      if (!a || typeof a.resource_path !== 'string' || !a.resource_path) return false;
      return Array.isArray(a.transform) && a.transform.length >= 16;
    });
  }

  async function fetchParticleRecord(path: string): Promise<ParticleRecord | null> {
    const qs = new URLSearchParams({ path, quality: 'high' });
    const res = await fetchJson<ParticleRecordResponse>(`/api/particles/record?${qs}`);
    return res.record ?? null;
  }

  async function fetchParticleRecords(paths: string[]): Promise<Record<string, ParticleRecord>> {
    const out: Record<string, ParticleRecord> = {};
    let next = 0;
    const workerCount = Math.min(6, paths.length);

    async function worker(): Promise<void> {
      while (next < paths.length) {
        const path = paths[next++];
        try {
          const record = await fetchParticleRecord(path);
          if (record) out[path] = record;
        } catch (err) {
          console.warn('[maps] map particle record unavailable', path, err);
        }
      }
    }

    await Promise.all(Array.from({ length: workerCount }, () => worker()));
    return out;
  }

  function disposeMapParticleScene(env: SceneEnvironment | null): void {
    if (!mapParticleScene) return;
    if (env) env.scene.remove(mapParticleScene.root);
    mapParticleScene.dispose();
    mapParticleScene = null;
  }

  function disableMapParticleScene(): void {
    mapParticleLoadToken += 1;
    mapParticleLoading = false;
    if (!mapParticleScene) return;
    mapParticleScene.setAllActive(false);
    mapParticleScene.root.visible = false;
  }

  async function loadMapParticleScene(
    env: SceneEnvironment,
    anchors: MapParticleAnchor[],
  ): Promise<void> {
    const token = ++mapParticleLoadToken;
    const usable = usableMapParticleAnchors(anchors);
    const paths = [...new Set(usable.map((a) => a.resource_path as string))].sort();
    mapParticleLoading = true;
    mapParticleError = null;

    try {
      const records = await fetchParticleRecords(paths);
      if (token !== mapParticleLoadToken) return;

      disposeMapParticleScene(env);
      const scene = new ParticleScene(env.renderer);
      scene.root.name = 'MapParticleEffects';
      env.scene.add(scene.root);

      const renderable = usable.filter((a) => records[a.resource_path as string]);
      const attachments: ParticleAttachment[] = renderable.map((a, i) => ({
        group: `map_${i}`,
        node: a.resource_id_hex ?? 'anchor',
        particle_path: a.resource_path as string,
        source: 'map',
      }));
      const handles = scene.build(attachments, records, () => new THREE.Vector3(0, 0, 0));
      const matrix = new THREE.Matrix4();
      let systems = 0;
      for (let i = 0; i < handles.length; i++) {
        const anchor = renderable[i];
        matrix.fromArray(anchor.transform as number[]);
        handles[i].group.matrix.copy(matrix);
        handles[i].group.matrixAutoUpdate = false;
        handles[i].group.matrixWorldNeedsUpdate = true;
        scene.setAttachmentActive(handles[i], true);
        systems += handles[i].systems.length;
      }

      mapParticleScene = scene;
      mapParticleStats = {
        anchors: anchors.length,
        resolvedAnchors: usable.length,
        uniquePaths: paths.length,
        records: Object.keys(records).length,
        activeAnchors: handles.length,
        systems,
        missingRecords: Math.max(0, paths.length - Object.keys(records).length),
      };
      if (viewerStats) {
        viewerStats = {
          ...viewerStats,
          mapParticleAnchors: anchors.length,
          mapParticleResolved: usable.length,
        };
      }
    } catch (err) {
      if (token === mapParticleLoadToken) {
        mapParticleError = err instanceof Error ? err.message : String(err);
        mapParticleStats = null;
      }
    } finally {
      if (token === mapParticleLoadToken) {
        mapParticleLoading = false;
      }
    }
  }

  function syncMapParticleLayer(
    show: boolean,
    env: SceneEnvironment | null,
    anchors: MapParticleAnchor[],
  ): void {
    if (!show) {
      disableMapParticleScene();
      return;
    }
    if (!env || anchors.length === 0) return;
    if (mapParticleScene) {
      mapParticleScene.root.visible = true;
      mapParticleScene.setAllActive(true);
      return;
    }
    if (untrack(() => mapParticleLoading)) return;
    void loadMapParticleScene(env, anchors);
  }

  function buildCollisionGeometry(model: CollisionModelRecord): THREE.BufferGeometry | null {
    if (!model.objects || model.objects.length === 0) return null;
    const positions: number[] = [];
    const indices: number[] = [];
    for (const object of model.objects) {
      const base = positions.length / 3;
      for (const v of object.vertices) {
        positions.push(v[0], v[1], v[2]);
      }
      for (const face of object.faces) {
        for (const tri of face.debug_fan_triangles ?? []) {
          indices.push(base + tri[0], base + tri[1], base + tri[2]);
        }
      }
    }
    if (positions.length === 0 || indices.length === 0) return null;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeBoundingSphere();
    return geometry;
  }

  function buildCollisionOverlay(manifest: CollisionManifest): {
    group: THREE.Group;
    modelCount: number;
    obstacleCount: number;
    triangleCount: number;
  } {
    const byModel = new Map<number, CollisionObstacle[]>();
    for (const obstacle of manifest.obstacles ?? []) {
      if (!obstacle.candidate_collision_model_valid) continue;
      if (!Array.isArray(obstacle.transform) || obstacle.transform.length < 16) continue;
      const index = obstacle.candidate_collision_model_index;
      let list = byModel.get(index);
      if (!list) {
        list = [];
        byModel.set(index, list);
      }
      list.push(obstacle);
    }

    const group = new THREE.Group();
    group.name = 'CollisionProxy';
    const material = new THREE.MeshBasicMaterial({
      color: 0xff4d6d,
      wireframe: true,
      transparent: true,
      opacity: 0.32,
      depthWrite: false,
    });
    const matrix = new THREE.Matrix4();
    let modelCount = 0;
    let obstacleCount = 0;
    let triangleCount = 0;

    for (const model of manifest.collision_models ?? []) {
      if (model.parse_error) continue;
      const obstacles = byModel.get(model.index);
      if (!obstacles || obstacles.length === 0) continue;
      const geometry = buildCollisionGeometry(model);
      if (!geometry) continue;
      const mesh = new THREE.InstancedMesh(geometry, material, obstacles.length);
      mesh.name = `CollisionModel_${model.index}`;
      mesh.userData.collision_model_index = model.index;
      mesh.userData.referenced_by_obstacle_count = obstacles.length;
      for (let i = 0; i < obstacles.length; i++) {
        matrix.fromArray(obstacles[i].transform);
        mesh.setMatrixAt(i, matrix);
      }
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
      group.add(mesh);
      modelCount += 1;
      obstacleCount += obstacles.length;
      triangleCount += ((geometry.getIndex()?.count ?? 0) / 3) * obstacles.length;
    }

    group.visible = showCollision;
    group.userData.collisionStats = {
      models: modelCount,
      obstacles: obstacleCount,
      triangles: triangleCount,
    };
    return { group, modelCount, obstacleCount, triangleCount };
  }

  async function loadCollisionOverlay(space: string, scene: THREE.Scene): Promise<void> {
    const token = ++collisionLoadToken;
    collisionLoading = true;
    collisionError = null;
    try {
      const resp = await fetch(mapCollisionManifestUrl(space));
      if (!resp.ok) {
        throw new Error(`collision manifest ${resp.status}`);
      }
      const manifest = (await resp.json()) as CollisionManifest;
      if (manifest.schema !== 'wows.map.collision_manifest.v1') {
        throw new Error(`unsupported collision manifest schema: ${manifest.schema}`);
      }
      const built = buildCollisionOverlay(manifest);
      if (token !== collisionLoadToken) {
        disposeTree(built.group);
        return;
      }
      if (collisionGroup) {
        scene.remove(collisionGroup);
        disposeTree(collisionGroup);
      }
      collisionGroup = built.group;
      collisionStats = {
        models: built.modelCount,
        obstacles: built.obstacleCount,
        triangles: built.triangleCount,
      };
      scene.add(collisionGroup);
      if (viewerStats) {
        viewerStats = {
          ...viewerStats,
          collisionModels: built.modelCount,
          collisionObstacles: built.obstacleCount,
          collisionTriangles: built.triangleCount,
        };
      }
    } catch (err) {
      if (token === collisionLoadToken) {
        collisionError = err instanceof Error ? err.message : String(err);
        collisionStats = null;
      }
    } finally {
      if (token === collisionLoadToken) {
        collisionLoading = false;
      }
    }
  }

  function syncCollisionOverlay(
    show: boolean,
    env: SceneEnvironment | null,
    space: string | null,
    hasManifest: boolean,
  ): void {
    if (!env || !space) return;
    if (!show) {
      const group = untrack(() => collisionGroup);
      if (group) group.visible = false;
      return;
    }
    if (!hasManifest) {
      collisionError = MISSING_COLLISION_MANIFEST_ERROR;
      return;
    }

    const staleError = untrack(() => collisionError);
    if (staleError === MISSING_COLLISION_MANIFEST_ERROR) {
      collisionError = null;
    }
    const group = untrack(() => collisionGroup);
    if (group) {
      group.visible = true;
      const stats = group.userData.collisionStats as
        | { models?: unknown; obstacles?: unknown; triangles?: unknown }
        | undefined;
      if (
        !untrack(() => collisionStats) &&
        typeof stats?.models === 'number' &&
        typeof stats?.obstacles === 'number' &&
        typeof stats?.triangles === 'number'
      ) {
        collisionStats = {
          models: stats.models,
          obstacles: stats.obstacles,
          triangles: stats.triangles,
        };
      }
      return;
    }
    if (untrack(() => collisionLoading)) return;
    void loadCollisionOverlay(space, env.scene);
  }

  /** Per-frame visibility update for LOD-tagged instances. Composes the
   *  landscape toggle (forces hidden when `is_landscape && !showLandscape`)
   *  and raw quality gate with the engine's hard far-cull at the outermost
   *  LOD extent. When `lodCullEnabled` is OFF, only the static filters apply.
   *
   *  Writes `node.visible` only when it changes — three.js doesn't care
   *  either way, but skipping ~all writes after settle avoids unnecessary
   *  property churn. */
  function updateLodVisibility(cameraPos: THREE.Vector3): void {
    const cullEnabled = lodCullEnabled;
    const showLand = showLandscape;
    const threshold = modelRuntimeQualityRaw;
    for (const inst of lodInstances) {
      let visible = true;
      if (inst.isLandscape && !showLand) {
        visible = false;
      } else if (inst.minQualityLevel != null && threshold > inst.minQualityLevel) {
        visible = false;
      } else if (cullEnabled) {
        visible = cameraPos.distanceToSquared(inst.worldPos) < inst.maxExtentSq;
      }
      if (inst.node.visible !== visible) inst.node.visible = visible;
    }
  }

  /** Force the Water plane to write depth so underwater geometry is
   *  occluded from an overview camera — matching how the engine renders
   *  water opaquely + writes depth (refraction is in the water shader,
   *  not in glTF alpha-blend). The toolkit emits Water as alphaMode=BLEND
   *  with alpha=0.85, which three.js maps to transparent=true,
   *  depthWrite=false — so submerged LNR landmasses + obstacles show
   *  through from above and create apparent "overlap". */
  function fixupWaterDepth(root: THREE.Object3D, opaqueWater: boolean): void {
    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh || mesh.name !== 'Water') return;
      const mat = mesh.material as THREE.MeshStandardMaterial;
      if (Array.isArray(mat)) return;
      if (opaqueWater) {
        mat.transparent = false;
        mat.depthWrite = true;
        // Keep the toolkit's intended blue tint but force alpha=1.
        if (mat.color) mat.color.setRGB(0.1, 0.3, 0.5);
        mat.opacity = 1.0;
      } else {
        mat.transparent = true;
        mat.depthWrite = false;
        mat.opacity = 0.85;
      }
      mat.needsUpdate = true;
    });
  }

  /** Frame the camera so `box` fills the viewport with a small margin.
   *  Sets controls.target to the box center; positions the camera on a
   *  3/4 viewing angle at the right distance for the given FOV. */
  function frameToBox(
    box: THREE.Box3,
    camera: THREE.PerspectiveCamera,
    controls: { target: THREE.Vector3; update: () => void },
  ): void {
    if (box.isEmpty()) return;
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxExtent = Math.max(size.x, size.y, size.z);
    // Distance to fit maxExtent in the larger of (vertical FOV, horiz
    // derived from aspect). Use vertical for a conservative fit.
    const fitDist = maxExtent / (2 * Math.tan((camera.fov * Math.PI) / 360));
    // Margin 1.0 = exactly fits; >1.0 pulls camera back. Maps are flat
    // (Y << X,Z) so a 3/4 oblique gives a useful initial view. Camera
    // pulled UP more than back/side so the ground reads clearly.
    const margin = 1.0;
    camera.position.set(
      center.x + fitDist * margin * 0.6,
      center.y + Math.max(maxExtent * 0.4, fitDist * margin * 0.6),
      center.z + fitDist * margin * 0.6,
    );
    camera.near = Math.max(0.1, fitDist / 10000);
    camera.far = fitDist * 50;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
  }

  $effect(() => {
    const container = canvasContainer;
    const sn = selected?.exported ? selected.name : null;
    if (!container || !sn) return;
    let cancelled = false;
    let env: SceneEnvironment | null = null;
    let loadedRoot: THREE.Object3D | null = null;
    let stopLoop: (() => void) | null = null;
    let stopResize: (() => void) | null = null;
    let pickPointerDown: { x: number; y: number } | null = null;
    let onPickPointerDown: ((event: PointerEvent) => void) | null = null;
    let onPickPointerUp: ((event: PointerEvent) => void) | null = null;

    void (async () => {
      viewerError = null;
      viewerLoading = true;
      viewerStats = null;
      pickedGeometry = null;
      disposePickHelper();
      try {
        env = createSceneEnvironment(container, {
          // Initial defaults; overridden below after we read scene
          // extras (fog color + far plane come from the engine).
          cameraPosition: [1500, 800, 1500],
          far: 50000,
          gridSize: 2000,
          gridDivisions: 20,
          axesSize: 100,
          background: 0x8aa4b8,
        });
        env.controls.target.set(0, 0, 0);
        env.controls.update();
        activeEnv = env;

        onPickPointerDown = (event: PointerEvent) => {
          if (event.button !== 0) return;
          pickPointerDown = { x: event.clientX, y: event.clientY };
        };
        onPickPointerUp = (event: PointerEvent) => {
          if (event.button !== 0 || !pickPointerDown || !env || !loadedRoot) return;
          const dx = event.clientX - pickPointerDown.x;
          const dy = event.clientY - pickPointerDown.y;
          pickPointerDown = null;
          if (dx * dx + dy * dy > 16) return;
          pickGeometryFromPointer(event, env, loadedRoot);
        };
        env.renderer.domElement.addEventListener('pointerdown', onPickPointerDown);
        env.renderer.domElement.addEventListener('pointerup', onPickPointerUp);

        stopResize = observeResize({
          container,
          renderer: env.renderer,
          camera: env.camera,
          onResize: (w, h) => env?.setSize(w, h),
        });

        stopLoop = startRenderLoop(() => {
          if (!env) return;
          env.controls.update();
          if (lodInstances.length > 0) {
            updateLodVisibility(env.camera.position);
          }
          mapParticleScene?.tick();
          env.render();
        });

        const loader = new GLTFLoader();
        const gltf = await loader.loadAsync(mapGlbUrl(sn));
        if (cancelled) {
          disposeTree(gltf.scene);
          return;
        }
        loadedRoot = gltf.scene;
        annotateGltfSourceNames(loadedRoot, gltf as unknown as GltfWithAssociations);
        env.scene.add(loadedRoot);

        // Drive fog + background + camera-far from the engine's per-map
        // params surfaced in scene.extras (see toolkit
        // build_scene_extras). Falls back to a sensible default when the
        // GLB predates the toolkit fix.
        const sceneExtras = (gltf.scene.userData ?? {}) as MapSceneExtras;
        let fogDensity: number | null = null;
        if (sceneExtras.fog) {
          const c = sceneExtras.fog.fog_color;
          const col = new THREE.Color(c[0], c[1], c[2]);
          engineFogDensity = sceneExtras.fog.fog_density;
          // Scale engine density down for the overview camera. See
          // `fogScale` for the rationale (engine tunes density for a
          // ship-level camera ~30m up; we sit ~1500m up).
          const fog = new THREE.FogExp2(col, sceneExtras.fog.fog_density / fogScale);
          env.scene.fog = fog;
          env.scene.background = col;
          activeFog = fog;
          // Use the engine far plane only if it's larger than the camera
          // would need for the playable area; some maps cap far at 5000 m
          // and we still want LOD proxies visible behind that — clamp the
          // user-visible far to scene bounds + a margin.
          env.camera.far = Math.max(sceneExtras.fog.far_plane, 10000);
          env.camera.updateProjectionMatrix();
          fogDensity = sceneExtras.fog.fog_density;
        } else {
          // Pre-fix GLB; keep a sane default so the viewer is still usable.
          env.scene.fog = new THREE.FogExp2(0x8aa4b8, 0.00015);
          engineFogDensity = null;
        }

        // Apply current fixed material state + expose the root so the
        // toggle effects below can re-apply without a full reload.
        fixupWaterDepth(loadedRoot, opaqueWater);
        activeRoot = loadedRoot;

        // Frame camera using engine bounds when present (preferred — it's
        // the authoritative playable extent). Mesh-scan fallback uses the
        // Terrain node.
        const { box, meshCount, landscapeCount, dyedInstances, materialOverrideInstances } =
          computeFrameBox(loadedRoot, sceneExtras);
        frameToBox(box, env.camera, env.controls);

        mapParticleAnchors = sceneExtras.particles ?? [];
        mapParticleStats = null;
        mapParticleError = null;

        // Collapse per-tree nodes into InstancedMesh per species. Cuts
        // ~5K draw calls down to ~5 on a typical map.
        const veg = collapseVegetation(loadedRoot);
        vegetationGroup = veg.group;
        if (vegetationGroup) vegetationGroup.visible = showVegetation;

        // Pre-collect LOD-cullable instances so the per-frame pass doesn't
        // re-traverse the scene tree. Must run after frameToBox and after
        // vegetation collapse so removed per-tree nodes are not cached.
        lodInstances = collectLodInstances(loadedRoot);
        const instanceQualityStats = applyInstanceFilters(
          loadedRoot,
          showLandscape,
          modelRuntimeQualityRaw,
        );

        // Instantiate engine point lights. Toggled visible/invisible via
        // the `showLights` $state through the effect below. Always reset
        // `lightObjects` here even when the list is empty, so a re-load
        // doesn't leak the prior selection's lights into the count.
        const sceneLights = sceneExtras.lights ?? [];
        lightObjects = sceneLights.length > 0 ? instantiateLights(loadedRoot, sceneLights) : [];
        const lightQualityStats = applyLightFilter(
          lightObjects,
          showLights,
          dynamicLightingRuntimeRaw,
        );

        viewerStats = {
          nodes: meshCount,
          landscapeCount,
          fogDensity,
          lodCullableCount: lodInstances.length,
          lightCount: lightObjects.length,
          qualityTaggedInstances: instanceQualityStats.tagged,
          qualityHiddenInstances: instanceQualityStats.hidden,
          qualityTaggedLights: lightQualityStats.tagged,
          qualityHiddenLights: lightQualityStats.hidden,
          vegetationSpecies: veg.speciesCount,
          vegetationInstances: veg.instanceCount,
          mapParticleAnchors: mapParticleAnchors.length,
          mapParticleResolved: usableMapParticleAnchors(mapParticleAnchors).length,
          collisionModels: 0,
          collisionObstacles: 0,
          collisionTriangles: 0,
          dyedInstances,
          materialOverrideInstances,
          bbox: box.isEmpty()
            ? null
            : {
                min: [box.min.x, box.min.y, box.min.z],
                max: [box.max.x, box.max.y, box.max.z],
              },
        };
      } catch (err) {
        viewerError = err instanceof Error ? err.message : String(err);
      } finally {
        viewerLoading = false;
      }
    })();

    return () => {
      cancelled = true;
      collisionLoadToken += 1;
      activeRoot = null;
      activeEnv = null;
      activeFog = null;
      engineFogDensity = null;
      lodInstances = [];
      lightObjects = [];
      vegetationGroup = null;
      mapParticleAnchors = [];
      mapParticleStats = null;
      mapParticleLoading = false;
      mapParticleError = null;
      mapParticleLoadToken += 1;
      disposeMapParticleScene(env);
      if (collisionGroup && env) {
        env.scene.remove(collisionGroup);
        disposeTree(collisionGroup);
      }
      collisionGroup = null;
      collisionStats = null;
      collisionLoading = false;
      collisionError = null;
      pickedGeometry = null;
      disposePickHelper();
      if (env && onPickPointerDown) {
        env.renderer.domElement.removeEventListener('pointerdown', onPickPointerDown);
      }
      if (env && onPickPointerUp) {
        env.renderer.domElement.removeEventListener('pointerup', onPickPointerUp);
      }
      stopLoop?.();
      stopResize?.();
      if (loadedRoot && env) {
        env.scene.remove(loadedRoot);
        disposeTree(loadedRoot);
      }
      env?.dispose();
      // Defensive: only remove our own canvas, in case anything else
      // appended into the container.
      if (env && container && env.renderer.domElement.parentElement === container) {
        container.removeChild(env.renderer.domElement);
      }
    };
  });

  // Live toggle for static instance filters. Composes landscape visibility
  // and raw quality gating on the already-loaded scene without a re-fetch.
  $effect(() => {
    const root = activeRoot;
    if (!root) return;
    const stats = applyInstanceFilters(root, showLandscape, modelRuntimeQualityRaw);
    updateViewerQualityStats(stats, null);
  });

  // Live toggle for water opacity.
  $effect(() => {
    const root = activeRoot;
    if (!root) return;
    fixupWaterDepth(root, opaqueWater);
  });

  // Live toggle for engine point lights. Native point-light minQuality is
  // driven by the DYNAMIC_LIGHTING graphics setting, which is separate from
  // model instance minimumQualityLevel.
  $effect(() => {
    const show = showLights;
    const threshold = dynamicLightingRuntimeRaw;
    if (lightObjects.length === 0) return;
    const stats = applyLightFilter(lightObjects, show, threshold);
    updateViewerQualityStats(null, stats);
  });

  // Live toggle for vegetation — single visibility flip on the parent
  // group hides all species' InstancedMeshes in one operation.
  $effect(() => {
    const show = showVegetation;
    if (vegetationGroup) vegetationGroup.visible = show;
  });

  // Live toggle for map-authored particles. The layer is lazy because it
  // joins map anchors to shared particle records and DDS textures on demand.
  $effect(() => {
    const show = showMapParticles;
    const env = activeEnv;
    const anchors = mapParticleAnchors;
    syncMapParticleLayer(show, env, anchors);
  });

  // Live collision proxy overlay. Lazily fetches the manifest only when
  // enabled; if the sidecar is missing, the toolbar exposes a re-export
  // action that asks the backend to generate it.
  $effect(() => {
    const show = showCollision;
    const env = activeEnv;
    const sn = selected?.exported ? selected.name : null;
    const hasManifest = Boolean(selected?.collision_manifest_exported);
    syncCollisionOverlay(show, env, sn, hasManifest);
  });

  // Live fog-density slider — re-scales the engine density into the
  // current FogExp2 without re-loading the scene.
  $effect(() => {
    const fog = activeFog;
    const eng = engineFogDensity;
    if (!fog || eng == null) return;
    fog.density = eng / Math.max(1, fogScale);
  });

  function formatBytes(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function formatMs(ms: number | null | undefined): string {
    if (ms == null) return '—';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
  }
</script>

<div class="flex flex-1 min-w-0 min-h-0">
  <!-- Left pane: picker -->
  <aside class="bg-card border-border w-72 flex-none border-r overflow-y-auto p-2 text-xs">
    <div class="flex items-center gap-2 px-2 pb-2">
      <span class="text-muted-foreground font-semibold">Spaces</span>
      <span class="text-muted-foreground/60">({items.length})</span>
      <Button
        variant="ghost"
        size="sm"
        class="ml-auto text-xs"
        onclick={() => void refresh()}
        disabled={loading}>Refresh</Button
      >
    </div>

    {#if loading}
      <div class="text-muted-foreground px-2 py-4 text-center">Loading…</div>
    {:else if loadError}
      <div class="text-destructive px-2 py-4">
        Failed to load spaces: {loadError}
      </div>
    {:else}
      {#each ['battle', 'dock', 'ops', 'other'] as MapCategory[] as cat (cat)}
        {#if grouped[cat].length > 0}
          <div class="text-muted-foreground/80 mt-3 mb-1 px-2 text-[10px] uppercase tracking-wider">
            {cat} ({grouped[cat].length})
          </div>
          {#each grouped[cat] as row (row.name)}
            <button
              type="button"
              onclick={() => select(row.name)}
              class="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent {selected?.name ===
              row.name
                ? 'bg-accent text-foreground'
                : 'text-muted-foreground'}"
            >
              <span class="truncate">{row.name}</span>
              {#if row.exported}
                <span
                  class="ml-auto text-[10px] text-emerald-400"
                  title={`Exported (${formatBytes(row.glb_size)})`}>●</span
                >
              {/if}
            </button>
          {/each}
        {/if}
      {/each}
    {/if}
  </aside>

  <!-- Right pane: viewer or export action -->
  <section class="flex flex-1 min-w-0 min-h-0 flex-col">
    {#if !selected}
      <div class="text-muted-foreground flex flex-1 items-center justify-center text-sm">
        Select a space from the left to preview or export.
      </div>
    {:else}
      <header class="border-border flex flex-none items-center gap-3 border-b px-4 py-2 text-sm">
        <span class="text-foreground font-semibold">{selected.name}</span>
        <span class="text-muted-foreground/60 text-xs">{selected.category}</span>
        <span class="text-muted-foreground text-xs">{selected.vfs_path}</span>
        {#if selected.exported}
          <span class="text-muted-foreground ml-auto text-xs">
            GLB: {formatBytes(selected.glb_size)} ·
            {formatMs(selected.export?.elapsed_ms)}
            {#if selected.collision_manifest_exported}
              · collision {formatBytes(selected.collision_manifest_size)}
            {/if}
          </span>
          <Button
            variant="outline"
            size="sm"
            class="text-xs"
            onclick={() => void triggerExport()}
            disabled={exporting}>Re-export</Button
          >
          {#if !selected.collision_manifest_exported}
            <Button
              variant="outline"
              size="sm"
              class="text-xs"
              onclick={() => void triggerBuildCollisionManifest()}
              disabled={exporting}>Build collision</Button
            >
          {/if}
          <Button
            variant="ghost"
            size="sm"
            class="text-xs"
            onclick={() => void triggerDelete()}
            disabled={exporting}>Delete cache</Button
          >
        {:else}
          <Button
            variant="default"
            size="sm"
            class="ml-auto text-xs"
            onclick={() => void triggerExport()}
            disabled={exporting}>{exporting ? 'Exporting…' : 'Export map'}</Button
          >
        {/if}
      </header>

      {#if exportError}
        <div class="text-destructive border-border border-b px-4 py-2 text-xs">
          Export failed: {exportError}
        </div>
      {/if}

      {#if selected.exported}
        <!-- Three.js GLB viewer; mounts via $effect when canvasContainer
             is wired AND selected.exported is true. Engine ground-truth
             rendering: fog + far-plane from scene extras plus explicit
             toggles for authored landscape, LOD, quality, light, vegetation,
             particle, and collision layers. -->
        <div
          class="text-muted-foreground border-border flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1 text-xs"
        >
          {#if viewerStats}
            <span>{viewerStats.nodes} meshes</span>
            {#if viewerStats.landscapeCount > 0}
              <label class="flex items-center gap-1.5">
                <input type="checkbox" bind:checked={showLandscape} />
                <span>
                  Show landscape ({viewerStats.landscapeCount} of {viewerStats.nodes})
                </span>
              </label>
            {/if}
            <label class="flex items-center gap-1.5">
              <input type="checkbox" bind:checked={opaqueWater} />
              <span>Opaque water</span>
            </label>
            {#if viewerStats.lodCullableCount > 0}
              <label
                class="flex items-center gap-1.5"
                title="Hide each instance once camera distance exceeds the prototype's outermost LOD extent. Engine-faithful hard cut. Landscape (LNR*) proxies often declare a 50 km sentinel so the cull rarely fires on them — the toggle is mostly visible on close-detail decoratives."
              >
                <input type="checkbox" bind:checked={lodCullEnabled} />
                <span>LOD cull ({viewerStats.lodCullableCount} eligible)</span>
              </label>
            {/if}
            {#if viewerStats.qualityTaggedInstances > 0}
              <label
                class="flex items-center gap-1.5"
                title="OBJECT_LOD runtime gate. Native raw values descend from MAXIMUM=0 to MINIMUM=6; model content draws when OBJECT_LOD raw quality is less than or equal to authored minimumQualityLevel."
              >
                <span>OBJECT_LOD</span>
                <select
                  bind:value={modelRuntimeQualityRaw}
                  class="bg-input border-border rounded border px-1 py-0.5 text-xs"
                >
                  {#each QUALITY_RUNTIME_OPTIONS as q}
                    <option value={q.value}>{q.label}</option>
                  {/each}
                </select>
                {#if viewerStats.qualityHiddenInstances > 0}
                  <span class="text-muted-foreground/70">
                    {viewerStats.qualityHiddenInstances.toLocaleString()} hidden
                  </span>
                {/if}
              </label>
            {/if}
            {#if viewerStats.lightCount > 0}
              <label
                class="flex items-center gap-1.5"
                title="Engine point lights from space.bin (small-radius accents — typically only visible when the camera is close to ground level)."
              >
                <input type="checkbox" bind:checked={showLights} />
                <span>Lights ({viewerStats.lightCount})</span>
              </label>
            {/if}
            {#if viewerStats.qualityTaggedLights > 0}
              <label
                class="flex items-center gap-1.5"
                title="Point-light minQuality gate. Native point-light lighting uses the DYNAMIC_LIGHTING graphics setting; active raw tiers observed in the callback are HIGH=2, MEDIUM=3, LOW=4, with other raw values disabling dynamic-light resources."
              >
                <span>Light q</span>
                <select
                  bind:value={dynamicLightingRuntimeRaw}
                  class="bg-input border-border rounded border px-1 py-0.5 text-xs"
                >
                  {#each DYNAMIC_LIGHTING_RUNTIME_OPTIONS as q}
                    <option value={q.value}>{q.label}</option>
                  {/each}
                </select>
                {#if viewerStats.qualityHiddenLights > 0}
                  <span class="text-muted-foreground/70">
                    {viewerStats.qualityHiddenLights.toLocaleString()} hidden
                  </span>
                {/if}
              </label>
            {/if}
            {#if viewerStats.vegetationInstances > 0}
              <label
                class="flex items-center gap-1.5"
                title="Toggle vegetation (trees + foliage). Collapsed into one InstancedMesh per species at load — far fewer draw calls than the per-instance scene nodes the toolkit emits."
              >
                <input type="checkbox" bind:checked={showVegetation} />
                <span
                  >Vegetation ({viewerStats.vegetationSpecies}sp × {viewerStats.vegetationInstances})</span
                >
              </label>
            {/if}
            {#if viewerStats.mapParticleAnchors > 0}
              <label
                class="flex items-center gap-1.5"
                title="Map-authored space.bin.particles[] anchors. Preview uses the shared particle renderer; authored six-channel intensities are preserved in metadata but not yet bound to EffectManager channel semantics."
              >
                <input type="checkbox" bind:checked={showMapParticles} />
                <span>
                  Map particles ({viewerStats.mapParticleResolved}/{viewerStats.mapParticleAnchors})
                  {#if mapParticleStats}
                    · {mapParticleStats.records} fx · {mapParticleStats.systems} systems
                  {/if}
                </span>
              </label>
            {/if}
            {#if selected.collision_manifest_exported}
              <label
                class="flex items-center gap-1.5"
                title="Diagnostic collision proxy from wows.map.collision_manifest.v1. Uses raw face loops triangulated as wireframe fan meshes; not native solver parity."
              >
                <input
                  type="checkbox"
                  checked={showCollision}
                  onchange={(event) => {
                    showCollision = (event.currentTarget as HTMLInputElement).checked;
                  }}
                />
                <span>
                  Collision
                  {#if collisionStats}
                    ({collisionStats.models} models × {collisionStats.obstacles},
                    {collisionStats.triangles.toLocaleString()} tris)
                  {/if}
                </span>
              </label>
            {/if}
            {#if collisionLoading}
              <span class="text-muted-foreground/70">loading collision…</span>
            {/if}
            {#if collisionError}
              <span class="text-destructive" title={collisionError}>collision unavailable</span>
            {/if}
            {#if mapParticleLoading}
              <span class="text-muted-foreground/70">loading map particles…</span>
            {/if}
            {#if mapParticleError}
              <span class="text-destructive" title={mapParticleError}
                >map particles unavailable</span
              >
            {/if}
            {#if viewerStats.dyedInstances > 0 || viewerStats.materialOverrideInstances > 0}
              <span
                class="text-muted-foreground/70 flex items-center gap-1.5"
                title="Per-instance overrides emitted by the toolkit but not yet applied by the webview. Dye keys are 8-byte (matter_id, replaces_id) pairs; the engine resolves them via a hash table not yet RE'd. Material override count refers to MaterialInstancePrototype records (0x70 stride) — decoding is a follow-up."
              >
                overrides: {[
                  viewerStats.dyedInstances > 0 ? `${viewerStats.dyedInstances} dyed` : null,
                  viewerStats.materialOverrideInstances > 0
                    ? `${viewerStats.materialOverrideInstances} matlInst`
                    : null,
                ]
                  .filter(Boolean)
                  .join(', ')}
              </span>
            {/if}
            {#if viewerStats.fogDensity != null}
              <label
                class="text-muted-foreground/70 flex items-center gap-1.5"
                title="Engine fog density (ρ) is tuned for ship-level cameras. Slider divides ρ for the overview camera; 1 = engine value."
              >
                <span>fog ρ ÷</span>
                <input type="range" min="1" max="200" step="1" bind:value={fogScale} class="w-20" />
                <span class="font-mono">{fogScale}</span>
                <span class="text-muted-foreground/50">
                  (engine: {viewerStats.fogDensity.toFixed(4)})
                </span>
              </label>
            {/if}
            {#if viewerStats.bbox}
              <span class="ml-auto text-muted-foreground/70">
                bbox X[{viewerStats.bbox.min[0].toFixed(0)}, {viewerStats.bbox.max[0].toFixed(0)}]
                Y[{viewerStats.bbox.min[1].toFixed(0)}, {viewerStats.bbox.max[1].toFixed(0)}] Z[{viewerStats.bbox.min[2].toFixed(
                  0,
                )}, {viewerStats.bbox.max[2].toFixed(0)}] m
              </span>
            {/if}
          {/if}
        </div>
        {#if pickedGeometry}
          <div
            class="text-muted-foreground border-border bg-background/95 flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1 text-xs"
          >
            <span class="text-foreground font-semibold">Pick</span>
            <span
              class="max-w-[22rem] truncate text-foreground"
              title={pickedGeometry.parentPath.join(' / ')}
            >
              {pickedGeometry.objectName}
            </span>
            <span>
              mesh <span class="font-mono text-foreground">{pickedGeometry.meshName}</span>
            </span>
            <span>
              geom <span class="font-mono text-foreground">{pickedGeometry.geometryName}</span>
            </span>
            <span>
              mat
              <span class="font-mono text-foreground"
                >{pickedGeometry.materialNames.join(', ')}</span
              >
            </span>
            {#if pickedGeometry.instanceId != null}
              <span>inst {pickedGeometry.instanceId}</span>
            {/if}
            {#if pickedGeometry.faceIndex != null}
              <span>face {pickedGeometry.faceIndex}</span>
            {/if}
            {#if pickedGeometry.triangles != null}
              <span>{pickedGeometry.triangles.toLocaleString()} tris</span>
            {/if}
            <span>
              pos <span class="font-mono text-foreground"
                >{formatPickPoint(pickedGeometry.point)}</span
              >
            </span>
            <Button
              variant="ghost"
              size="sm"
              class="h-6 px-2 text-xs"
              onclick={() => void copyPickedGeometry()}>Copy</Button
            >
            {#if pickedGeometry.hits.length > 1}
              <span
                class="basis-full truncate text-muted-foreground/70"
                title={pickedGeometry.hits
                  .map((h) => `${h.objectName} :: ${h.meshName} @ ${formatPickNumber(h.distance)}m`)
                  .join(' | ')}
              >
                stack:
                {pickedGeometry.hits
                  .map((h) => `${h.objectName || h.meshName} (${formatPickNumber(h.distance)}m)`)
                  .join(' | ')}
              </span>
            {/if}
          </div>
        {/if}
        <div class="relative flex flex-1 min-h-0 min-w-0">
          <div bind:this={canvasContainer} class="flex-1 cursor-crosshair"></div>
          {#if viewerLoading}
            <div
              class="text-muted-foreground absolute inset-0 flex items-center justify-center bg-background/40 text-sm"
            >
              Loading GLB ({formatBytes(selected.glb_size)})…
            </div>
          {/if}
          {#if viewerError}
            <div class="text-destructive absolute inset-x-0 top-0 bg-background/80 p-2 text-xs">
              Viewer error: {viewerError}
            </div>
          {/if}
        </div>
      {:else}
        <div
          class="text-muted-foreground flex flex-1 flex-col items-center justify-center gap-3 p-6 text-sm"
        >
          <p>This space has not been exported yet.</p>
          <div class="bg-card border-border w-96 rounded border p-3 text-xs">
            <div class="text-muted-foreground/80 mb-2 text-[10px] uppercase tracking-wider">
              Export flags
            </div>
            <label class="mb-2 flex items-center gap-2">
              <span class="w-28">Max texture size</span>
              <select
                bind:value={exportFlags.max_texture_size}
                class="bg-input border-border flex-1 rounded border px-2 py-1"
              >
                <option value={null}>Original</option>
                <option value={256}>256</option>
                <option value={512}>512</option>
                <option value={1024}>1024</option>
                <option value={2048}>2048</option>
              </select>
            </label>
            <label class="mb-2 flex items-center gap-2">
              <span class="w-28">Terrain step</span>
              <select
                bind:value={exportFlags.terrain_step}
                class="bg-input border-border flex-1 rounded border px-2 py-1"
              >
                <option value={1}>1 (full)</option>
                <option value={4}>4 (default)</option>
                <option value={8}>8 (coarse)</option>
              </select>
            </label>
            <label class="mb-2 flex items-center gap-2">
              <span class="w-28">Vegetation</span>
              <select
                bind:value={exportFlags.vegetation_density}
                class="bg-input border-border flex-1 rounded border px-2 py-1"
                disabled={exportFlags.no_vegetation}
              >
                <option value={0}>Full density</option>
                <option value={5}>5 m cell</option>
                <option value={10}>10 m cell</option>
                <option value={20}>20 m cell</option>
                <option value={40}>40 m cell</option>
              </select>
            </label>
            <label class="mb-2 flex items-center gap-2">
              <input type="checkbox" bind:checked={exportFlags.no_vegetation} />
              <span>Skip vegetation</span>
            </label>
            <label class="flex items-center gap-2">
              <input type="checkbox" bind:checked={exportFlags.no_textures} />
              <span>Skip textures (faster, smaller GLB)</span>
            </label>
            <label class="mt-2 flex items-center gap-2">
              <input type="checkbox" bind:checked={exportFlags.collision_manifest} />
              <span>Write collision manifest sidecar</span>
            </label>
          </div>
        </div>
      {/if}
    {/if}
  </section>
</div>
