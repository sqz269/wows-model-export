<script lang="ts">
  // Asset-list sidebar: scope/category/subcategory/ship filter dropdowns,
  // dead-only / no-tex / query toggles, sort selector, scrollable list.
  // Click a row → calls onSelect (parent routes via #/asset/<id>).
  //
  // Filter + sort state lives in the parent route; we receive both as
  // props + emit changes via callbacks. Keeping it lifted means the
  // parent can drive deep-link state without bidirectional sync games.

  import type { LibraryAsset, LibraryFilter, LibraryIndex } from '$lib/types';

  export type SortKey = 'id-asc' | 'id-desc' | 'built-desc' | 'built-asc';

  const SORT_LABELS: Record<SortKey, string> = {
    'id-asc': 'name (a→z)',
    'id-desc': 'name (z→a)',
    'built-desc': 'newest first',
    'built-asc': 'oldest first',
  };

  interface Props {
    index: LibraryIndex;
    filter: LibraryFilter;
    sort: SortKey;
    activeId: string | null;
    onFilterChange: (next: LibraryFilter) => void;
    onSortChange: (next: SortKey) => void;
    onSelect: (id: string) => void;
  }

  const { index, filter, sort, activeId, onFilterChange, onSortChange, onSelect }: Props = $props();

  // Derived filter dropdowns.
  const facets = $derived.by(() => {
    const scopes = new Set<string>();
    const cats = new Set<string>();
    const subs = new Set<string>();
    const ships = new Set<string>();
    for (const a of Object.values(index.assets)) {
      scopes.add(a.scope);
      cats.add(a.category);
      if (a.subcategory) subs.add(a.subcategory);
      for (const s of a.used_by_ships) ships.add(s);
    }
    return {
      scopes: Array.from(scopes).sort(),
      cats: Array.from(cats).sort(),
      subs: Array.from(subs).sort(),
      ships: Array.from(ships).sort(),
    };
  });

  // Filtered + sorted asset entries.
  const entries = $derived.by(() => {
    const all = Object.entries(index.assets);
    const matched = all.filter(([id, a]) => matches(id, a, filter));
    matched.sort((a, b) => compareAssets(a, b, sort));
    return matched;
  });

  function matches(id: string, a: LibraryAsset, f: LibraryFilter): boolean {
    if (f.scope && a.scope !== f.scope) return false;
    if (f.category && a.category !== f.category) return false;
    if (f.subcategory && a.subcategory !== f.subcategory) return false;
    if (f.ship && !a.used_by_ships.includes(f.ship)) return false;
    if (f.deadOnly && !a.glb_dead) return false;
    if (f.untexturedOnly && hasTextures(a)) return false;
    if (f.query && !id.toLowerCase().includes(f.query.toLowerCase())) return false;
    return true;
  }

  function compareAssets(
    [idA, a]: [string, LibraryAsset],
    [idB, b]: [string, LibraryAsset],
    key: SortKey,
  ): number {
    const idCmp = idA.localeCompare(idB);
    if (key === 'id-asc') return idCmp;
    if (key === 'id-desc') return -idCmp;
    // built_at sort: missing values sink to the end either direction.
    const ta = typeof a.built_at === 'number' ? a.built_at : null;
    const tb = typeof b.built_at === 'number' ? b.built_at : null;
    if (ta === null && tb === null) return idCmp;
    if (ta === null) return 1;
    if (tb === null) return -1;
    const diff = key === 'built-desc' ? tb - ta : ta - tb;
    return diff !== 0 ? diff : idCmp;
  }

  function hasTextures(a: LibraryAsset): boolean {
    const main = a.texture_sets?.main;
    if (main) {
      for (const paths of Object.values(main)) {
        if (Array.isArray(paths) && paths.length > 0) return true;
      }
    }
    return Boolean(a.textures || a.textures_dds);
  }

  function patch<K extends keyof LibraryFilter>(key: K, value: LibraryFilter[K]) {
    onFilterChange({ ...filter, [key]: value });
  }
</script>

<aside class="sidebar">
  <header>
    <h1>Accessory library</h1>
    <div class="counter">
      {entries.length} / {Object.keys(index.assets).length}
    </div>
  </header>

  <div class="filters">
    <label>
      scope
      <select
        value={filter.scope ?? ''}
        onchange={(e) => patch('scope', e.currentTarget.value || null)}
      >
        <option value="">all</option>
        {#each facets.scopes as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label>
      category
      <select
        value={filter.category ?? ''}
        onchange={(e) => patch('category', e.currentTarget.value || null)}
      >
        <option value="">all</option>
        {#each facets.cats as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label>
      subcategory
      <select
        value={filter.subcategory ?? ''}
        onchange={(e) => patch('subcategory', e.currentTarget.value || null)}
      >
        <option value="">all</option>
        {#each facets.subs as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label>
      used by ship
      <select
        value={filter.ship ?? ''}
        onchange={(e) => patch('ship', e.currentTarget.value || null)}
      >
        <option value="">any</option>
        {#each facets.ships as s (s)}
          <option value={s}>{s}</option>
        {/each}
      </select>
    </label>

    <label class="inline">
      <input
        type="checkbox"
        checked={filter.deadOnly}
        onchange={(e) => patch('deadOnly', e.currentTarget.checked)}
      />
      has dead variant
    </label>

    <label class="inline">
      <input
        type="checkbox"
        checked={filter.untexturedOnly}
        onchange={(e) => patch('untexturedOnly', e.currentTarget.checked)}
      />
      no textures only
    </label>

    <input
      type="search"
      placeholder="search asset_id…"
      value={filter.query}
      oninput={(e) => patch('query', e.currentTarget.value)}
    />

    <label>
      sort
      <select value={sort} onchange={(e) => onSortChange(e.currentTarget.value as SortKey)}>
        {#each Object.entries(SORT_LABELS) as [k, lbl] (k)}
          <option value={k}>{lbl}</option>
        {/each}
      </select>
    </label>
  </div>

  <ul class="list">
    {#each entries as [id, a] (id)}
      <li>
        <button
          type="button"
          class="row"
          class:active={activeId === id}
          onclick={() => onSelect(id)}
        >
          <div class="row-top">
            <span class="aid">{id}</span>
            {#if a.glb_dead}<span class="badge badge-dead">dead</span>{/if}
            {#if !hasTextures(a)}<span class="badge badge-notex">no-tex</span>{/if}
            {#if a.subcategory}<span class="badge">{a.subcategory}</span>{/if}
          </div>
          <div class="row-bot">
            {a.scope}/{a.category}
            {#if a.used_by_ships.length}
              · used by {a.used_by_ships.length}
            {/if}
          </div>
        </button>
      </li>
    {:else}
      <li class="empty">No assets match.</li>
    {/each}
  </ul>
</aside>

<style>
  .sidebar {
    flex: 0 0 320px;
    background: var(--bg-side);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  header {
    padding: 12px 14px 8px;
    border-bottom: 1px solid var(--border);
  }
  h1 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  .counter {
    margin-top: 4px;
    font-size: 11px;
    color: var(--fg-muted);
    font-variant-numeric: tabular-nums;
  }
  .filters {
    padding: 10px 14px 8px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-bottom: 1px solid var(--border);
    flex: 0 0 auto;
  }
  .filters label {
    display: flex;
    flex-direction: column;
    font-size: 11px;
    color: var(--fg-dim);
    gap: 2px;
  }
  .filters label.inline {
    flex-direction: row;
    align-items: center;
    gap: 6px;
    color: var(--fg);
    font-size: 12px;
  }
  .filters select,
  .filters input[type='search'] {
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    padding: 5px 6px;
    border-radius: 4px;
    font-size: 12px;
  }
  .filters select:focus,
  .filters input:focus {
    outline: 1px solid var(--accent);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    flex: 1 1 auto;
  }
  .row {
    display: block;
    width: 100%;
    padding: 7px 14px;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border);
    border-left: 3px solid transparent;
    text-align: left;
    color: var(--fg);
    cursor: pointer;
  }
  .row:hover {
    background: var(--bg-elev);
  }
  .row.active {
    background: var(--accent-bg);
    border-left-color: var(--accent);
  }
  .row-top {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .row-bot {
    margin-top: 2px;
    font-size: 11px;
    color: var(--fg-muted);
  }
  .aid {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 500;
  }
  .badge {
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 3px;
    background: var(--bg-elev-2);
    color: var(--fg-dim);
  }
  .badge-dead {
    background: #3e2127;
    color: #ffb3b3;
  }
  .badge-notex {
    background: #2a2540;
    color: #c4b5fd;
  }
  .empty {
    padding: 14px;
    color: var(--fg-muted);
    font-size: 12px;
  }
</style>
