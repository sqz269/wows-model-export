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
  } | null>(null);

  /** Compute the Terrain mesh's world bbox — this is the playable map
   *  extent and the right thing to frame the camera on. Maps emit a
   *  single `Terrain` node covering the full bounds in `space.settings`
   *  (typically ±900m for 1.8km maps). Falls back to "every mesh" if
   *  no Terrain node exists. */
  function computeFrameBox(root: THREE.Object3D): {
    box: THREE.Box3;
    meshCount: number;
    usingTerrain: boolean;
  } {
    const terrainBox = new THREE.Box3();
    const allBox = new THREE.Box3();
    let meshCount = 0;
    let foundTerrain = false;
    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      meshCount++;
      const b = new THREE.Box3().setFromObject(mesh);
      allBox.union(b);
      if (mesh.name === 'Terrain') {
        terrainBox.union(b);
        foundTerrain = true;
      }
    });
    return {
      box: foundTerrain ? terrainBox : allBox,
      meshCount,
      usingTerrain: foundTerrain,
    };
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
          // Initial defaults; frameToBox overrides after GLB loads.
          cameraPosition: [1500, 800, 1500],
          far: 50000,
          gridSize: 2000,
          gridDivisions: 20,
          axesSize: 100,
          background: 0x8aa4b8, // hazy horizon color so fog blends
        });
        env.controls.target.set(0, 0, 0);
        env.controls.update();

        // Match the engine's atmospheric fade. Density 0.00015 gives
        // ~10% fog at 1 km, ~50% at 5.5 km, ~95% at 11 km — keeps the
        // playable 1.8 km area crisp while distant LOD proxies
        // (LNR*/TILEDLAND, last-LOD extent 50 km) fade into haze
        // instead of clipping through the foreground. Tunable per-map
        // once we surface space.settings fog params (`farPlane`,
        // `fogColor`).
        env.scene.fog = new THREE.FogExp2(0x8aa4b8, 0.00015);

        stopResize = observeResize({
          container,
          renderer: env.renderer,
          camera: env.camera,
          onResize: (w, h) => env?.setSize(w, h),
        });

        stopLoop = startRenderLoop(() => {
          if (!env) return;
          env.controls.update();
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

        // Frame camera to the Terrain node's bbox — that's the playable
        // map extent (typically ±900m for 1.8km maps). Landscape LOD
        // proxies (LNR*/TILEDLAND, extent up to 50 km) would swamp the
        // bbox if we framed on everything. Fog handles the clipping for
        // distant proxies; this gets the user looking at the map itself.
        const { box, meshCount, usingTerrain } = computeFrameBox(loadedRoot);
        frameToBox(box, env.camera, env.controls);
        viewerStats = {
          nodes: meshCount,
          bbox: box.isEmpty()
            ? null
            : {
                min: [box.min.x, box.min.y, box.min.z],
                max: [box.max.x, box.max.y, box.max.z],
              },
        };
        void usingTerrain;
      } catch (err) {
        viewerError = err instanceof Error ? err.message : String(err);
      } finally {
        viewerLoading = false;
      }
    })();

    return () => {
      cancelled = true;
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
             is wired AND selected.exported is true. 1:1 with the GLB:
             no content filters. -->
        {#if viewerStats}
          <div
            class="text-muted-foreground border-border flex flex-none items-center gap-3 border-b px-4 py-1 text-xs"
          >
            <span>{viewerStats.nodes} meshes</span>
            {#if viewerStats.bbox}
              <span>
                bbox X[{viewerStats.bbox.min[0].toFixed(0)}, {viewerStats.bbox.max[0].toFixed(
                  0,
                )}] Y[{viewerStats.bbox.min[1].toFixed(
                  0,
                )}, {viewerStats.bbox.max[1].toFixed(0)}] Z[{viewerStats.bbox.min[2].toFixed(
                  0,
                )}, {viewerStats.bbox.max[2].toFixed(0)}] m
              </span>
            {/if}
          </div>
        {/if}
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
