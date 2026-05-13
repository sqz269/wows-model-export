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

  function truncate(s: string, n: number) {
    return s.length <= n ? s : s.slice(0, n - 1) + '…';
  }

  function meshes(): MeshInfo[] {
    return result?.meshes ?? [];
  }
</script>

<section class="detail">
  <header class="header">
    <div class="title">
      <h2><code>{id}</code></h2>
      <div class="meta">
        {asset.scope}/{asset.category}{asset.subcategory ? `/${asset.subcategory}` : ''}
        {#if asset.species}· {asset.species}{/if}
      </div>
      <div class="ships muted">
        {#if asset.used_by_ships.length}
          used by {asset.used_by_ships.length}:
          <code>{asset.used_by_ships.join(', ')}</code>
        {:else}
          unused
        {/if}
      </div>
    </div>
    <div class="status">
      {#if loadError}
        <span class="error">{loadError}</span>
      {:else if result}
        {result.meshes.length} mesh{result.meshes.length === 1 ? '' : 'es'} ·
        {totalTris.toLocaleString()} tris
      {:else}
        Loading…
      {/if}
    </div>
  </header>

  <div class="viewer-wrap">
    <AccessoryViewerCmp
      {url}
      bindHandle={(v) => {
        viewer = v;
      }}
      {onLoaded}
      {onError}
    />

    <aside class="controls">
      <div class="group">
        <div class="label">view</div>
        <label class="inline">
          <input
            type="checkbox"
            checked={helpers}
            onchange={(e) => toggleHelpers(e.currentTarget.checked)}
          />
          grid + axes
        </label>
        <label class="inline">
          <input
            type="checkbox"
            checked={wireframe}
            onchange={(e) => toggleWireframe(e.currentTarget.checked)}
          />
          wireframe
        </label>
      </div>

      <div class="group">
        <div class="label">faces</div>
        <div class="row">
          {#each ['double', 'front', 'back'] as s (s)}
            <button
              type="button"
              class="tog"
              class:active={side === s}
              onclick={() => setSide(s as SideMode)}
            >
              {s}
            </button>
          {/each}
        </div>
      </div>

      {#if asset.glb_dead}
        <div class="group">
          <div class="label">variant</div>
          <div class="row">
            <button
              type="button"
              class="tog"
              class:active={!showingDead}
              onclick={() => toggleVariant(false)}
            >
              intact
            </button>
            <button
              type="button"
              class="tog"
              class:active={showingDead}
              onclick={() => toggleVariant(true)}
            >
              dead
            </button>
          </div>
        </div>
      {/if}

      {#if lods.length > 1}
        <div class="group">
          <div class="label">LOD filter</div>
          <div class="row">
            <button
              type="button"
              class="tog"
              class:active={lodFilter === null}
              onclick={() => setLod(null)}
            >
              all
            </button>
            {#each lods as lod (lod)}
              <button
                type="button"
                class="tog"
                class:active={lodFilter === lod}
                onclick={() => setLod(lod)}
              >
                lod{lod}
              </button>
            {/each}
          </div>
        </div>
      {/if}

      <div class="group">
        <div class="label">meshes</div>
        <ul class="mesh-list">
          {#each meshes() as m, i (i)}
            <li>
              <label title={m.name}>
                <input
                  type="checkbox"
                  checked={meshVisibility[i] ?? true}
                  onchange={(e) => toggleMesh(i, e.currentTarget.checked)}
                />
                <span class="mesh-name">{truncate(m.name, 36)}</span>
                <span class="mesh-lod">lod{m.lod}</span>
                <span class="mesh-tri">{m.triangles.toLocaleString()}</span>
              </label>
            </li>
          {/each}
        </ul>
      </div>
    </aside>
  </div>

  <div class="info">
    <section>
      <h3>Library</h3>
      <dl>
        <dt>GLB</dt>
        <dd>
          <code>{asset.glb}</code> <span class="muted">({fmtBytes(asset.glb_bytes)})</span>
        </dd>
        {#if asset.glb_dead}
          <dt>Dead GLB</dt>
          <dd>
            <code>{asset.glb_dead}</code>
            <span class="muted">({fmtBytes(asset.glb_dead_bytes ?? 0)})</span>
          </dd>
        {/if}
        <dt>Textures</dt>
        <dd>
          {#if asset.textures}
            <code>{asset.textures}</code>
          {:else}
            <span class="muted">(none)</span>
          {/if}
        </dd>
        <dt>Textures (DDS)</dt>
        <dd>
          {#if asset.textures_dds}
            <code>{asset.textures_dds}</code>
          {:else}
            <span class="muted">(none)</span>
          {/if}
        </dd>
      </dl>
    </section>

    <section>
      <h3>LOD breakdown</h3>
      <table class="lod-table">
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

<style>
  .detail {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .header {
    flex: 0 0 auto;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    background: var(--bg-side);
  }
  .title h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  .title code {
    font-family: var(--font-mono);
  }
  .title .meta {
    margin-top: 2px;
    font-size: 11px;
    color: var(--fg-dim);
  }
  .ships {
    margin-top: 4px;
    font-size: 11px;
    max-width: 60ch;
    word-break: break-word;
  }
  .status {
    font-size: 11px;
    color: var(--fg);
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .status .error {
    color: var(--danger);
  }
  .viewer-wrap {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    overflow: hidden;
  }
  .controls {
    flex: 0 0 260px;
    border-left: 1px solid var(--border);
    background: var(--bg-side);
    padding: 12px 14px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .label {
    font-size: 11px;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .row {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }
  .tog {
    background: var(--bg-elev);
    color: var(--fg-dim);
    border: 1px solid var(--border);
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
  }
  .tog:hover {
    background: var(--bg-elev-2);
    color: var(--fg);
  }
  .tog.active {
    background: var(--accent-bg);
    color: var(--fg);
    border-color: var(--accent);
  }
  .inline {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
  }
  .mesh-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 320px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .mesh-list label {
    display: grid;
    grid-template-columns: auto 1fr auto auto;
    align-items: center;
    gap: 6px;
    font-size: 11px;
  }
  .mesh-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--fg);
  }
  .mesh-lod {
    color: var(--fg-muted);
  }
  .mesh-tri {
    color: var(--fg-muted);
    font-variant-numeric: tabular-nums;
  }
  .info {
    flex: 0 0 auto;
    max-height: 30%;
    overflow-y: auto;
    padding: 12px 18px;
    border-top: 1px solid var(--border);
    background: var(--bg);
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }
  .info h3 {
    margin: 0 0 6px;
    font-size: 11px;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }
  dl {
    margin: 0;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    font-size: 12px;
  }
  dt {
    color: var(--fg-dim);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  dd code {
    font-family: var(--font-mono);
    font-size: 11px;
  }
  .lod-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .lod-table th,
  .lod-table td {
    text-align: left;
    padding: 3px 8px 3px 0;
  }
  .lod-table tfoot td {
    border-top: 1px solid var(--border);
    color: var(--fg-dim);
  }
  .lod-table td:not(:first-child),
  .lod-table th:not(:first-child) {
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
</style>
