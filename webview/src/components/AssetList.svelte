<script lang="ts">
  // Asset-list sidebar: scope/category/subcategory/ship filter dropdowns,
  // dead-only / no-tex / query toggles, sort selector, scrollable list.
  // Click a row → calls onSelect (parent routes via #/asset/<id>).
  //
  // Filter + sort state lives in the parent route; we receive both as
  // props + emit changes via callbacks. Keeping it lifted means the
  // parent can drive deep-link state without bidirectional sync games.

  import type {
    LibraryAsset,
    LibraryFilter,
    LibraryIndex,
  } from '$lib/types';

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

  const {
    index,
    filter,
    sort,
    activeId,
    onFilterChange,
    onSortChange,
    onSelect,
  }: Props = $props();

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
    return Boolean(a.textures_dds);
  }

  function patch<K extends keyof LibraryFilter>(key: K, value: LibraryFilter[K]) {
    onFilterChange({ ...filter, [key]: value });
  }
</script>

{#snippet selectBox(
  value: string,
  onchange: (v: string) => void,
  options: string[],
  emptyLabel: string,
)}
  <select
    {value}
    onchange={(e: Event & { currentTarget: HTMLSelectElement }) => onchange(e.currentTarget.value)}
    class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
  >
    <option value="">{emptyLabel}</option>
    {#each options as opt (opt)}
      <option value={opt}>{opt}</option>
    {/each}
  </select>
{/snippet}

<aside
  class="bg-card border-border flex w-[320px] flex-none flex-col border-r min-h-0"
>
  <header class="border-border border-b px-3.5 py-3 pb-2">
    <h1 class="m-0 text-sm font-semibold">Accessory library</h1>
    <div class="text-muted-foreground mt-1 text-[11px] tabular-nums">
      {entries.length} / {Object.keys(index.assets).length}
    </div>
  </header>

  <div class="border-border flex flex-none flex-col gap-2 border-b px-3.5 py-2.5">
    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      scope
      {@render selectBox(filter.scope ?? '', (v) => patch('scope', v || null), facets.scopes, 'all')}
    </label>

    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      category
      {@render selectBox(
        filter.category ?? '',
        (v) => patch('category', v || null),
        facets.cats,
        'all',
      )}
    </label>

    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      subcategory
      {@render selectBox(
        filter.subcategory ?? '',
        (v) => patch('subcategory', v || null),
        facets.subs,
        'all',
      )}
    </label>

    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      used by ship
      {@render selectBox(filter.ship ?? '', (v) => patch('ship', v || null), facets.ships, 'any')}
    </label>

    <label class="text-foreground flex items-center gap-1.5 text-xs">
      <input
        type="checkbox"
        checked={filter.deadOnly}
        onchange={(e) => patch('deadOnly', e.currentTarget.checked)}
      />
      has dead variant
    </label>

    <label class="text-foreground flex items-center gap-1.5 text-xs">
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
      class="bg-popover text-foreground border-border placeholder:text-muted-foreground focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
    />

    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      sort
      <select
        value={sort}
        onchange={(e) => onSortChange(e.currentTarget.value as SortKey)}
        class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
      >
        {#each Object.entries(SORT_LABELS) as [k, lbl] (k)}
          <option value={k}>{lbl}</option>
        {/each}
      </select>
    </label>
  </div>

  <ul class="m-0 flex-1 list-none overflow-y-auto p-0">
    {#each entries as [id, a] (id)}
      <li>
        <button
          type="button"
          onclick={() => onSelect(id)}
          class="border-border hover:bg-popover block w-full border-b border-l-[3px] border-l-transparent px-3.5 py-[7px] text-left {activeId ===
          id
            ? 'bg-accent border-l-primary'
            : ''}"
        >
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="font-mono text-xs font-medium">{id}</span>
            {#if a.glb_dead}
              <span
                class="rounded px-1.5 py-[1px] text-[10px]"
                style="background: #3e2127; color: #ffb3b3"
              >
                dead
              </span>
            {/if}
            {#if !hasTextures(a)}
              <span
                class="rounded px-1.5 py-[1px] text-[10px]"
                style="background: #2a2540; color: #c4b5fd"
              >
                no-tex
              </span>
            {/if}
            {#if a.subcategory}
              <span class="bg-secondary text-muted-foreground rounded px-1.5 py-[1px] text-[10px]">
                {a.subcategory}
              </span>
            {/if}
          </div>
          <div class="text-muted-foreground mt-0.5 text-[11px]">
            {a.scope}/{a.category}
            {#if a.used_by_ships.length}
              · used by {a.used_by_ships.length}
            {/if}
          </div>
        </button>
      </li>
    {:else}
      <li class="text-muted-foreground px-3.5 py-3.5 text-xs">No assets match.</li>
    {/each}
  </ul>
</aside>
