<script lang="ts">
  // Asset detail panel: header (asset_id, scope/category/subcategory,
  // used-by-ships) → 3D viewer → side controls + info section.
  //
  // Owns the AccessoryViewer handle so the controls can wire directly to
  // it. Re-renders when `asset` changes; viewer instance is kept alive
  // across asset swaps for a smoother feel.

  import AccessoryViewerCmp from './AccessoryViewer.svelte';
  import type { AccessoryViewer, LoadResult, MeshInfo, SideMode } from '$lib/accessory';
  import type { LibraryAsset } from '$lib/types';
  import { repoUrl } from '$lib/api';
  import { fmtBytes } from '$lib/util/html';

  // Shared utility classes — mirror the labelled-dropdown idiom used in
  // ShipControls.svelte so the Library and Ships pages read identically.
  const labelCls = 'flex flex-col gap-0.5 text-[11px] text-muted-foreground';
  const inputBoxCls =
    'h-7 rounded border border-border bg-popover px-1.5 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring';
  const rowCls = 'flex items-center gap-1.5 text-xs text-foreground';

  interface Props {
    /** Asset_id (key in LibraryIndex.assets). */
    id: string;
    asset: LibraryAsset;
  }

  const { id, asset }: Props = $props();

  let viewer: AccessoryViewer | null = $state(null);
  let result: LoadResult | null = $state(null);
  let loadError: string | null = $state(null);
  let showingDead = $state(false);

  // Viewer state (mirrors viewer methods).
  let helpers = $state(true);
  let wireframe = $state(false);
  let side = $state<SideMode>('double');
  let lodFilter = $state<number | null>(null);
  let meshVisibility = $state<boolean[]>([]);
  let showTextures = $state(true);

  // Library context for the TextureManager. Same `asset` object the
  // page already has — passing it makes the viewer apply the DDS texture
  // pipeline on load. `variant` mirrors `showingDead` so the manager
  // picks `texture_sets.dead` (when present) over `main` for the dead
  // GLB — without it the dead geometry would render with the intact
  // albedo. The child reloads on `lib` change via its load effect.
  const libContext = $derived({
    assetId: id,
    asset,
    variant: (showingDead && asset.glb_dead ? 'dead' : 'main') as 'main' | 'dead',
  });

  // Reset side state whenever the asset changes — different assets have
  // different LOD layouts, mesh counts, and dead variants.
  $effect(() => {
    void id;
    showingDead = false;
    lodFilter = null;
    meshVisibility = [];
    loadError = null;
    result = null;
  });

  // Re-apply controls when the viewer comes online or after a reload.
  $effect(() => {
    if (!viewer || !result) return;
    viewer.setHelpers(helpers);
    viewer.setWireframe(wireframe);
    viewer.setSide(side);
    viewer.setLodFilter(lodFilter);
  });

  const url = $derived.by(() => {
    const rel = showingDead && asset.glb_dead ? asset.glb_dead : asset.glb;
    return repoUrl(`libraries/accessories/${rel}`);
  });

  // LOD bucketing for the LOD-filter buttons + info section.
  const lods = $derived.by(() => {
    if (!result) return [] as number[];
    const set = new Set<number>();
    for (const m of result.meshes) set.add(m.lod);
    return Array.from(set).sort((a, b) => a - b);
  });

  const lodBreakdown = $derived.by(() => {
    if (!result) return [];
    const by = new Map<number, { count: number; tris: number }>();
    for (const m of result.meshes) {
      const e = by.get(m.lod) ?? { count: 0, tris: 0 };
      e.count++;
      e.tris += m.triangles;
      by.set(m.lod, e);
    }
    return Array.from(by.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([lod, info]) => ({ lod, ...info }));
  });

  const totalTris = $derived.by(() => {
    if (!result) return 0;
    return result.meshes.reduce((s, m) => s + m.triangles, 0);
  });

  function onLoaded(res: LoadResult) {
    result = res;
    meshVisibility = res.meshes.map(() => true);
    loadError = null;
  }

  function onError(err: unknown) {
    loadError = err instanceof Error ? err.message : String(err);
    result = null;
  }

  function toggleHelpers(v: boolean) {
    helpers = v;
    viewer?.setHelpers(v);
  }
  function toggleWireframe(v: boolean) {
    wireframe = v;
    viewer?.setWireframe(v);
  }
  function setSide(v: SideMode) {
    side = v;
    viewer?.setSide(v);
  }
  function setLod(v: number | null) {
    lodFilter = v;
    viewer?.setLodFilter(v);
  }
  function toggleMesh(i: number, v: boolean) {
    meshVisibility[i] = v;
    viewer?.setMeshVisibleByIndex(i, v);
  }
  function toggleVariant(dead: boolean) {
    showingDead = dead;
  }
  async function toggleTextures(v: boolean) {
    showTextures = v;
    try {
      await viewer?.setShowTextures(v);
    } catch (err) {
      console.warn('[assetdetail] setShowTextures failed:', err);
    }
  }

  function truncate(s: string, n: number) {
    return s.length <= n ? s : s.slice(0, n - 1) + '…';
  }

  function meshes(): MeshInfo[] {
    return result?.meshes ?? [];
  }
</script>

<section class="flex flex-1 min-w-0 flex-col overflow-hidden">
  <header
    class="bg-card border-border flex flex-none items-start justify-between gap-4 border-b px-5 py-3"
  >
    <div>
      <h2 class="m-0 text-sm font-semibold"><code class="font-mono">{id}</code></h2>
      <div class="text-muted-foreground mt-0.5 text-[11px]">
        {asset.scope}/{asset.category}{asset.subcategory ? `/${asset.subcategory}` : ''}
        {#if asset.species}· {asset.species}{/if}
      </div>
      <div class="text-muted-foreground mt-1 max-w-[60ch] break-words text-[11px]">
        {#if asset.used_by_ships.length}
          used by {asset.used_by_ships.length}:
          <code>{asset.used_by_ships.join(', ')}</code>
        {:else}
          unused
        {/if}
      </div>
    </div>
    <div class="text-right text-[11px] tabular-nums text-foreground">
      {#if loadError}
        <span class="text-destructive">{loadError}</span>
      {:else if result}
        {result.meshes.length} mesh{result.meshes.length === 1 ? '' : 'es'} ·
        {totalTris.toLocaleString()} tris
      {:else}
        Loading…
      {/if}
    </div>
  </header>

  <div class="flex flex-1 min-h-0 overflow-hidden">
    <AccessoryViewerCmp
      {url}
      lib={libContext}
      bindHandle={(v) => {
        viewer = v;
      }}
      {onLoaded}
      {onError}
    />

    <aside
      class="bg-card border-border flex w-[260px] flex-none flex-col gap-3 overflow-y-auto border-l p-3.5"
    >
      <!--
        View section: matches ShipControls.svelte's idiom — checkboxes for
        binary toggles, labelled <select>s for n-of-many. Replaces the
        earlier toggle-button groups (faces / variant / LOD filter) so the
        Library and Ships pages share one visual + interaction vocabulary.
      -->
      <div class="flex flex-col gap-2">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          view
        </div>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={helpers}
            onchange={(e) => toggleHelpers(e.currentTarget.checked)}
          />
          Helpers (grid + axes)
        </label>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={wireframe}
            onchange={(e) => toggleWireframe(e.currentTarget.checked)}
          />
          Wireframe
        </label>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={showTextures}
            onchange={(e) => toggleTextures(e.currentTarget.checked)}
          />
          Textures
        </label>
        {#if asset.glb_dead}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={showingDead}
              onchange={(e) => toggleVariant(e.currentTarget.checked)}
            />
            Show destroyed variant
          </label>
        {/if}
        <label class={labelCls}>
          Faces
          <select
            value={side}
            onchange={(e) => setSide(e.currentTarget.value as SideMode)}
            class={inputBoxCls}
          >
            <option value="double">Double-sided</option>
            <option value="front">Front only</option>
            <option value="back">Back only</option>
          </select>
        </label>
        {#if lods.length > 1}
          <label class={labelCls}>
            LOD filter
            <select
              value={lodFilter === null ? 'all' : String(lodFilter)}
              onchange={(e) => {
                const v = e.currentTarget.value;
                setLod(v === 'all' ? null : Number(v));
              }}
              class={inputBoxCls}
            >
              <option value="all">All LODs</option>
              {#each lods as lod (lod)}
                <option value={String(lod)}>LOD {lod} only</option>
              {/each}
            </select>
          </label>
        {/if}
      </div>

      <div class="flex flex-col gap-1">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          meshes
        </div>
        <ul class="m-0 flex max-h-[320px] flex-col gap-0.5 overflow-y-auto p-0 list-none">
          {#each meshes() as m, i (i)}
            <li>
              <label
                title={m.name}
                class="grid grid-cols-[auto_1fr_auto_auto] items-center gap-1.5 text-[11px]"
              >
                <input
                  type="checkbox"
                  checked={meshVisibility[i] ?? true}
                  onchange={(e) => toggleMesh(i, e.currentTarget.checked)}
                />
                <span class="overflow-hidden text-ellipsis whitespace-nowrap text-foreground">
                  {truncate(m.name, 36)}
                </span>
                <span class="text-muted-foreground">lod{m.lod}</span>
                <span class="text-muted-foreground tabular-nums">{m.triangles.toLocaleString()}</span>
              </label>
            </li>
          {/each}
        </ul>
      </div>
    </aside>
  </div>

  <div
    class="bg-background border-border grid max-h-[30%] flex-none grid-cols-2 gap-6 overflow-y-auto border-t px-5 py-3"
  >
    <section>
      <h3
        class="text-muted-foreground mb-1.5 text-[11px] font-semibold uppercase tracking-wider"
      >
        Library
      </h3>
      <dl
        class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-words [&_code]:font-mono [&_code]:text-[11px]"
      >
        <dt>GLB</dt>
        <dd>
          <code>{asset.glb}</code> <span class="text-muted-foreground">({fmtBytes(asset.glb_bytes)})</span>
        </dd>
        {#if asset.glb_dead}
          <dt>Dead GLB</dt>
          <dd>
            <code>{asset.glb_dead}</code>
            <span class="text-muted-foreground">({fmtBytes(asset.glb_dead_bytes ?? 0)})</span>
          </dd>
        {/if}
        <dt>Textures</dt>
        <dd>
          {#if asset.textures}
            <code>{asset.textures}</code>
          {:else}
            <span class="text-muted-foreground">(none)</span>
          {/if}
        </dd>
        <dt>Textures (DDS)</dt>
        <dd>
          {#if asset.textures_dds}
            <code>{asset.textures_dds}</code>
          {:else}
            <span class="text-muted-foreground">(none)</span>
          {/if}
        </dd>
      </dl>
    </section>

    <section>
      <h3
        class="text-muted-foreground mb-1.5 text-[11px] font-semibold uppercase tracking-wider"
      >
        LOD breakdown
      </h3>
      <table
        class="w-full border-collapse text-xs [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:text-left [&_td]:py-0.5 [&_th]:py-0.5 [&_th]:pr-2 [&_td]:pr-2 [&_td:not(:first-child)]:text-right [&_th:not(:first-child)]:text-right [&_td:not(:first-child)]:tabular-nums [&_tfoot_td]:border-t [&_tfoot_td]:border-border [&_tfoot_td]:text-muted-foreground"
      >
        <thead>
          <tr><th>level</th><th>meshes</th><th>triangles</th></tr>
        </thead>
        <tbody>
          {#each lodBreakdown as row (row.lod)}
            <tr>
              <td>lod{row.lod}</td>
              <td>{row.count}</td>
              <td>{row.tris.toLocaleString()}</td>
            </tr>
          {/each}
        </tbody>
        <tfoot>
          <tr>
            <td>total</td>
            <td>{meshes().length}</td>
            <td>{totalTris.toLocaleString()}</td>
          </tr>
        </tfoot>
      </table>
    </section>
  </div>
</section>
