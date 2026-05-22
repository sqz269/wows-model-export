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

  import {
    listMaps,
    exportMap,
    mapGlbUrl,
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
  });

  const grouped = $derived(groupByCategory(items));
  const selected = $derived(spaceName ? items.find((i) => i.name === spaceName) ?? null : null);

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

  async function triggerExport(): Promise<void> {
    if (!selected) return;
    exporting = true;
    exportError = null;
    try {
      await exportMap(selected.name, exportFlags);
      await refresh();
    } catch (err) {
      exportError = err instanceof Error ? err.message : String(err);
    } finally {
      exporting = false;
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
    vegetationSpecies: number;
    vegetationInstances: number;
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

  // Live handles to the scene root + env so the showLandscape toggle
  // doesn't need to rebuild the whole scene. Set by the load effect;
  // cleared on teardown.
  let activeRoot = $state<THREE.Object3D | null>(null);
  let activeFog = $state<THREE.FogExp2 | null>(null);
  let engineFogDensity = $state<number | null>(null);

  // Pre-collected list of instance nodes that carry a `lod_extents` array.
  // Built once per load; the render loop walks this list per-frame instead
  // of traversing the whole scene tree. World position is cached because
  // map placements are static — they never animate.
  interface LodInstance {
    node: THREE.Object3D;
    worldPos: THREE.Vector3;
    maxExtentSq: number;
    isLandscape: boolean;
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

  /** Per-instance extras emitted by the toolkit. See
   *  `build_instance_extras`. */
  interface InstanceExtras {
    is_landscape?: boolean;
    min_quality_level?: number;
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

  /** Toggle visibility on landscape-flagged instance nodes. Covers nodes
   *  without `lod_extents`; the per-frame LOD pass owns visibility for
   *  those that have extents. */
  function applyLandscapeFilter(root: THREE.Object3D, show: boolean): void {
    root.traverse((o) => {
      const ud = o.userData as InstanceExtras;
      if (ud && ud.is_landscape) {
        o.visible = show;
      }
    });
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
      });
    });
    return out;
  }

  /** Collapse per-tree `Tree_<species>_<i>` nodes (emitted as ~5K
   *  individual meshes by the toolkit) into one `THREE.InstancedMesh`
   *  per species. Returns the new `Vegetation` Group (added under root)
   *  plus counts for stats. World matrices of the original tree nodes
   *  are baked into the per-instance matrices via the root's inverse,
   *  so the result is positionally identical regardless of any
   *  hierarchy quirks. Trees with a material array (multi-primitive
   *  glTF mesh) are left alone — `InstancedMesh` doesn't support that
   *  case and they're rare in practice. */
  function collapseVegetation(
    root: THREE.Object3D,
  ): { group: THREE.Group | null; speciesCount: number; instanceCount: number } {
    interface SpeciesBucket {
      meshes: THREE.Mesh[];
      geometry: THREE.BufferGeometry;
      material: THREE.Material;
    }
    const bySpecies = new Map<string, SpeciesBucket>();
    const toRemove: THREE.Object3D[] = [];
    const TREE_NAME = /^Tree_(\d+)_\d+$/;

    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      const match = mesh.name.match(TREE_NAME);
      if (!match) return;
      if (Array.isArray(mesh.material)) return;
      const speciesIdx = match[1];
      let bucket = bySpecies.get(speciesIdx);
      if (!bucket) {
        bucket = { meshes: [], geometry: mesh.geometry, material: mesh.material as THREE.Material };
        bySpecies.set(speciesIdx, bucket);
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

    if (bySpecies.size === 0) {
      return { group: null, speciesCount: 0, instanceCount: 0 };
    }

    // One root-wide matrixWorld update suffices for every descendant — no
    // need to re-call per-mesh inside the species loop.
    root.updateMatrixWorld(true);
    const rootInv = new THREE.Matrix4().copy(root.matrixWorld).invert();
    const tmp = new THREE.Matrix4();

    const group = new THREE.Group();
    group.name = 'Vegetation';
    let totalInstances = 0;

    for (const [speciesIdx, bucket] of bySpecies) {
      const im = new THREE.InstancedMesh(bucket.geometry, bucket.material, bucket.meshes.length);
      im.name = `Vegetation_${speciesIdx}`;
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
      totalInstances += bucket.meshes.length;
    }

    for (const n of toRemove) n.parent?.remove(n);
    root.add(group);

    return { group, speciesCount: bySpecies.size, instanceCount: totalInstances };
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
      root.add(light);
      out.push(light);
    }
    return out;
  }

  /** Per-frame visibility update for LOD-tagged instances. Composes the
   *  landscape toggle (forces hidden when `is_landscape && !showLandscape`)
   *  with the engine's hard far-cull at the outermost LOD extent. When
   *  `lodCullEnabled` is OFF, only the landscape filter applies.
   *
   *  Writes `node.visible` only when it changes — three.js doesn't care
   *  either way, but skipping ~all writes after settle avoids unnecessary
   *  property churn. */
  function updateLodVisibility(cameraPos: THREE.Vector3): void {
    const cullEnabled = lodCullEnabled;
    const showLand = showLandscape;
    for (const inst of lodInstances) {
      let visible = true;
      if (inst.isLandscape && !showLand) {
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

    void (async () => {
      viewerError = null;
      viewerLoading = true;
      viewerStats = null;
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
          env.render();
        });

        const loader = new GLTFLoader();
        const gltf = await loader.loadAsync(mapGlbUrl(sn));
        if (cancelled) {
          disposeTree(gltf.scene);
          return;
        }
        loadedRoot = gltf.scene;
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

        // Apply current filters (default ON) + expose the root so the
        // toggle effects below can re-apply without a full reload.
        applyLandscapeFilter(loadedRoot, showLandscape);
        fixupWaterDepth(loadedRoot, opaqueWater);
        activeRoot = loadedRoot;

        // Frame camera using engine bounds when present (preferred — it's
        // the authoritative playable extent). Mesh-scan fallback uses the
        // Terrain node.
        const { box, meshCount, landscapeCount, dyedInstances, materialOverrideInstances } =
          computeFrameBox(loadedRoot, sceneExtras);
        frameToBox(box, env.camera, env.controls);

        // Pre-collect LOD-cullable instances so the per-frame pass doesn't
        // re-traverse the scene tree. Must run after frameToBox so the
        // camera matrix is settled before the first cull pass.
        lodInstances = collectLodInstances(loadedRoot);

        // Instantiate engine point lights. Toggled visible/invisible via
        // the `showLights` $state through the effect below. Always reset
        // `lightObjects` here even when the list is empty, so a re-load
        // doesn't leak the prior selection's lights into the count.
        const sceneLights = sceneExtras.lights ?? [];
        lightObjects = sceneLights.length > 0 ? instantiateLights(loadedRoot, sceneLights) : [];
        for (const lt of lightObjects) lt.visible = showLights;

        // Collapse per-tree nodes into InstancedMesh per species. Cuts
        // ~5K draw calls down to ~5 on a typical map.
        const veg = collapseVegetation(loadedRoot);
        vegetationGroup = veg.group;
        if (vegetationGroup) vegetationGroup.visible = showVegetation;

        viewerStats = {
          nodes: meshCount,
          landscapeCount,
          fogDensity,
          lodCullableCount: lodInstances.length,
          lightCount: lightObjects.length,
          vegetationSpecies: veg.speciesCount,
          vegetationInstances: veg.instanceCount,
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
      activeRoot = null;
      activeFog = null;
      engineFogDensity = null;
      lodInstances = [];
      lightObjects = [];
      vegetationGroup = null;
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

  // Live toggle for the landscape filter — flips visibility on the
  // already-loaded scene without a re-fetch.
  $effect(() => {
    const root = activeRoot;
    if (!root) return;
    applyLandscapeFilter(root, showLandscape);
  });

  // Live toggle for water opacity.
  $effect(() => {
    const root = activeRoot;
    if (!root) return;
    fixupWaterDepth(root, opaqueWater);
  });

  // Live toggle for engine point lights. Cheap visibility flip — lights
  // stay attached to the scene root so reactivation is instant.
  $effect(() => {
    const show = showLights;
    if (lightObjects.length === 0) return;
    for (const lt of lightObjects) lt.visible = show;
  });

  // Live toggle for vegetation — single visibility flip on the parent
  // group hides all species' InstancedMeshes in one operation.
  $effect(() => {
    const show = showVegetation;
    if (vegetationGroup) vegetationGroup.visible = show;
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
  <aside
    class="bg-card border-border w-72 flex-none border-r overflow-y-auto p-2 text-xs"
  >
    <div class="flex items-center gap-2 px-2 pb-2">
      <span class="text-muted-foreground font-semibold">Spaces</span>
      <span class="text-muted-foreground/60">({items.length})</span>
      <Button
        variant="ghost"
        size="sm"
        class="ml-auto text-xs"
        onclick={() => void refresh()}
        disabled={loading}
        >Refresh</Button
      >
    </div>

    {#if loading}
      <div class="text-muted-foreground px-2 py-4 text-center">Loading…</div>
    {:else if loadError}
      <div class="text-destructive px-2 py-4">
        Failed to load spaces: {loadError}
      </div>
    {:else}
      {#each (['battle', 'dock', 'ops', 'other'] as MapCategory[]) as cat (cat)}
        {#if grouped[cat].length > 0}
          <div
            class="text-muted-foreground/80 mt-3 mb-1 px-2 text-[10px] uppercase tracking-wider"
          >
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
      <header
        class="border-border flex flex-none items-center gap-3 border-b px-4 py-2 text-sm"
      >
        <span class="text-foreground font-semibold">{selected.name}</span>
        <span class="text-muted-foreground/60 text-xs">{selected.category}</span>
        <span class="text-muted-foreground text-xs">{selected.vfs_path}</span>
        {#if selected.exported}
          <span class="text-muted-foreground ml-auto text-xs">
            GLB: {formatBytes(selected.glb_size)} ·
            {formatMs(selected.export?.elapsed_ms)}
          </span>
          <Button
            variant="outline"
            size="sm"
            class="text-xs"
            onclick={() => void triggerExport()}
            disabled={exporting}>Re-export</Button
          >
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
            disabled={exporting}
            >{exporting ? 'Exporting…' : 'Export map'}</Button
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
             rendering: fog + far-plane from scene extras, no content
             filtering beyond the landscape toggle. -->
        <div
          class="text-muted-foreground border-border flex flex-none items-center gap-3 border-b px-4 py-1 text-xs"
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
            {#if viewerStats.lightCount > 0}
              <label
                class="flex items-center gap-1.5"
                title="Engine point lights from space.bin (small-radius accents — typically only visible when the camera is close to ground level)."
              >
                <input type="checkbox" bind:checked={showLights} />
                <span>Lights ({viewerStats.lightCount})</span>
              </label>
            {/if}
            {#if viewerStats.vegetationInstances > 0}
              <label
                class="flex items-center gap-1.5"
                title="Toggle vegetation (trees + foliage). Collapsed into one InstancedMesh per species at load — far fewer draw calls than the per-instance scene nodes the toolkit emits."
              >
                <input type="checkbox" bind:checked={showVegetation} />
                <span>Vegetation ({viewerStats.vegetationSpecies}sp × {viewerStats.vegetationInstances})</span>
              </label>
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
                <input
                  type="range"
                  min="1"
                  max="200"
                  step="1"
                  bind:value={fogScale}
                  class="w-20"
                />
                <span class="font-mono">{fogScale}</span>
                <span class="text-muted-foreground/50">
                  (engine: {viewerStats.fogDensity.toFixed(4)})
                </span>
              </label>
            {/if}
            {#if viewerStats.bbox}
              <span class="ml-auto text-muted-foreground/70">
                bbox X[{viewerStats.bbox.min[0].toFixed(0)}, {viewerStats.bbox.max[0].toFixed(
                  0,
                )}] Y[{viewerStats.bbox.min[1].toFixed(
                  0,
                )}, {viewerStats.bbox.max[1].toFixed(0)}] Z[{viewerStats.bbox.min[2].toFixed(
                  0,
                )}, {viewerStats.bbox.max[2].toFixed(0)}] m
              </span>
            {/if}
          {/if}
        </div>
        <div class="relative flex flex-1 min-h-0 min-w-0">
          <div bind:this={canvasContainer} class="flex-1"></div>
          {#if viewerLoading}
            <div
              class="text-muted-foreground absolute inset-0 flex items-center justify-center bg-background/40 text-sm"
            >
              Loading GLB ({formatBytes(selected.glb_size)})…
            </div>
          {/if}
          {#if viewerError}
            <div
              class="text-destructive absolute inset-x-0 top-0 bg-background/80 p-2 text-xs"
            >
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
            <label class="flex items-center gap-2">
              <input
                type="checkbox"
                bind:checked={exportFlags.no_textures}
              />
              <span>Skip textures (faster, smaller GLB)</span>
            </label>
          </div>
        </div>
      {/if}
    {/if}
  </section>
</div>
