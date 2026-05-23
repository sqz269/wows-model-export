<script lang="ts">
  // Sidebar list for the Projectiles route. Owns its own rendering of
  // chip filters + text search + scrollable list rows; filter STATE
  // lives in the parent so deep links + mode-toggle keep working
  // cleanly.
  //
  // Mode = "mesh" → rows are unique asset_id meshes (~dozens).
  // Mode = "ammo" → rows are ammo_id entries (~thousands).
  //
  // The two row shapes differ enough that we keep two render blocks
  // rather than smushing into one. Chip-filter facets are computed
  // per-mode (mesh mode has no ammo_type chips, etc.).

  import type {
    AmmoProfile,
    AmmoProfilesDoc,
    ProjectileChipFilter,
    ProjectileFilterState,
    ProjectileIndex,
    ProjectileListMode,
    ProjectileMesh,
  } from '$lib/types/projectiles';

  interface Props {
    index: ProjectileIndex;
    ammo: AmmoProfilesDoc;
    mode: ProjectileListMode;
    /** Active selection: an asset_id in mesh mode, an ammo_id in ammo mode. */
    activeId: string | null;
    filter: ProjectileFilterState;
    onModeChange: (mode: ProjectileListMode) => void;
    onFilterChange: (next: ProjectileFilterState) => void;
    onSelect: (id: string) => void;
  }

  const {
    index,
    ammo,
    mode,
    activeId,
    filter,
    onModeChange,
    onFilterChange,
    onSelect,
  }: Props = $props();

  // ── Derived facets — drive chip rows ────────────────────────────────

  const meshFacets = $derived.by(() => {
    const nations = new Set<string>();
    const categories = new Set<string>();
    for (const a of Object.values(index.assets)) {
      if (a.nation) nations.add(a.nation);
      if (a.category) categories.add(a.category);
    }
    return {
      nations: Array.from(nations).sort(),
      categories: Array.from(categories).sort(),
    };
  });

  const ammoFacets = $derived.by(() => {
    const ammoTypes = new Set<string>();
    const species = new Set<string>();
    // For ammo mode we also want nation + category — those come from the
    // linked mesh (via asset_id). Walk the join lazily.
    const nations = new Set<string>();
    const categories = new Set<string>();
    for (const p of Object.values(ammo.profiles)) {
      if (p.ammo_type) ammoTypes.add(p.ammo_type);
      if (p.species) species.add(p.species);
      if (p.asset_id) {
        const m = index.assets[p.asset_id];
        if (m?.nation) nations.add(m.nation);
        if (m?.category) categories.add(m.category);
      }
    }
    return {
      ammoTypes: Array.from(ammoTypes).sort(),
      species: Array.from(species).sort(),
      nations: Array.from(nations).sort(),
      categories: Array.from(categories).sort(),
    };
  });

  // ── Chip filter helpers (tri-state: off → include → exclude → off) ──

  function chipState<T>(cf: ProjectileChipFilter<T>, v: T): 'off' | 'in' | 'out' {
    if (cf.include.has(v)) return 'in';
    if (cf.exclude.has(v)) return 'out';
    return 'off';
  }

  function cycleChip<T>(cf: ProjectileChipFilter<T>, v: T): ProjectileChipFilter<T> {
    const next = {
      include: new Set(cf.include),
      exclude: new Set(cf.exclude),
    };
    const s = chipState(cf, v);
    if (s === 'off') next.include.add(v);
    else if (s === 'in') {
      next.include.delete(v);
      next.exclude.add(v);
    } else {
      next.exclude.delete(v);
    }
    return next;
  }

  function passesChips<T>(cf: ProjectileChipFilter<T>, v: T): boolean {
    if (cf.include.size > 0 && !cf.include.has(v)) return false;
    if (cf.exclude.has(v)) return false;
    return true;
  }

  function chipBtnClass(s: 'off' | 'in' | 'out'): string {
    if (s === 'in')
      return 'bg-emerald-900/40 text-emerald-200 border-emerald-700/40';
    if (s === 'out') return 'bg-rose-900/40 text-rose-200 border-rose-700/40';
    return 'border-border text-muted-foreground hover:bg-popover/60';
  }

  // ── Filtered rows ───────────────────────────────────────────────────

  type MeshRow = { id: string; mesh: ProjectileMesh };
  type AmmoRow = { id: string; profile: AmmoProfile; mesh: ProjectileMesh | null };

  const meshRows = $derived.by<MeshRow[]>(() => {
    if (mode !== 'mesh') return [];
    const q = filter.text.trim().toLowerCase();
    const out: MeshRow[] = [];
    for (const [id, mesh] of Object.entries(index.assets)) {
      if (!passesChips(filter.nations, mesh.nation)) continue;
      if (!passesChips(filter.categories, mesh.category)) continue;
      if (q && !id.toLowerCase().includes(q)) continue;
      out.push({ id, mesh });
    }
    out.sort((a, b) => a.id.localeCompare(b.id));
    return out;
  });

  const ammoRows = $derived.by<AmmoRow[]>(() => {
    if (mode !== 'ammo') return [];
    const q = filter.text.trim().toLowerCase();
    const out: AmmoRow[] = [];
    for (const [id, profile] of Object.entries(ammo.profiles)) {
      if (!passesChips(filter.ammoTypes, profile.ammo_type)) continue;
      if (!passesChips(filter.species, profile.species)) continue;
      const mesh = profile.asset_id ? index.assets[profile.asset_id] : null;
      // Nation/category filters apply via the linked mesh — VFX-only
      // ammo (asset_id=null) is excluded from those filters unless
      // both sets are empty.
      if (filter.nations.include.size > 0 || filter.nations.exclude.size > 0) {
        if (!mesh) continue;
        if (!passesChips(filter.nations, mesh.nation)) continue;
      }
      if (filter.categories.include.size > 0 || filter.categories.exclude.size > 0) {
        if (!mesh) continue;
        if (!passesChips(filter.categories, mesh.category)) continue;
      }
      if (q && !id.toLowerCase().includes(q)) continue;
      out.push({ id, profile, mesh });
    }
    out.sort((a, b) => a.id.localeCompare(b.id));
    return out;
  });

  // Visible row count for the header summary.
  const visibleCount = $derived(mode === 'mesh' ? meshRows.length : ammoRows.length);
  const totalCount = $derived(
    mode === 'mesh' ? index.asset_count : ammo.profile_count,
  );

  // ── Filter mutation helpers ─────────────────────────────────────────

  function setText(text: string) {
    onFilterChange({ ...filter, text });
  }

  function toggleNation(v: string) {
    onFilterChange({ ...filter, nations: cycleChip(filter.nations, v) });
  }
  function toggleCategory(v: string) {
    onFilterChange({ ...filter, categories: cycleChip(filter.categories, v) });
  }
  function toggleAmmoType(v: string) {
    onFilterChange({ ...filter, ammoTypes: cycleChip(filter.ammoTypes, v) });
  }
  function toggleSpecies(v: string) {
    onFilterChange({ ...filter, species: cycleChip(filter.species, v) });
  }
  function clearFilters() {
    onFilterChange({
      text: '',
      nations: { include: new Set(), exclude: new Set() },
      categories: { include: new Set(), exclude: new Set() },
      ammoTypes: { include: new Set(), exclude: new Set() },
      species: { include: new Set(), exclude: new Set() },
    });
  }

  const filtersActive = $derived(
    filter.text !== '' ||
      filter.nations.include.size + filter.nations.exclude.size > 0 ||
      filter.categories.include.size + filter.categories.exclude.size > 0 ||
      filter.ammoTypes.include.size + filter.ammoTypes.exclude.size > 0 ||
      filter.species.include.size + filter.species.exclude.size > 0,
  );
</script>

<aside
  class="border-border bg-card flex w-72 flex-shrink-0 flex-col border-r min-h-0"
>
  <!-- Mode toggle + count header -->
  <header class="border-border flex flex-col gap-1.5 border-b px-3 py-2">
    <div class="flex items-center gap-1">
      <button
        type="button"
        class="flex-1 rounded border px-2 py-1 text-[11px] font-medium {mode === 'mesh'
          ? 'border-sky-700/50 bg-sky-900/40 text-sky-100'
          : 'border-border text-muted-foreground hover:bg-popover/60'}"
        onclick={() => onModeChange('mesh')}
      >
        Meshes
      </button>
      <button
        type="button"
        class="flex-1 rounded border px-2 py-1 text-[11px] font-medium {mode === 'ammo'
          ? 'border-sky-700/50 bg-sky-900/40 text-sky-100'
          : 'border-border text-muted-foreground hover:bg-popover/60'}"
        onclick={() => onModeChange('ammo')}
      >
        Ammo
      </button>
    </div>
    <div class="text-muted-foreground flex items-center justify-between text-[11px]">
      <span>
        {visibleCount} / {totalCount}
        {mode === 'mesh' ? 'mesh(es)' : 'ammo profile(s)'}
      </span>
      {#if filtersActive}
        <button
          type="button"
          onclick={clearFilters}
          class="text-muted-foreground hover:text-foreground underline"
        >
          clear filters
        </button>
      {/if}
    </div>
  </header>

  <!-- Text search -->
  <div class="border-border border-b px-3 py-1.5">
    <input
      type="text"
      placeholder="filter by id…"
      value={filter.text}
      oninput={(e) => setText((e.currentTarget as HTMLInputElement).value)}
      class="h-7 w-full rounded border border-border bg-popover px-2 text-[11px] font-mono text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30"
    />
  </div>

  <!-- Chip rows. Different chip sets per mode. -->
  {#if mode === 'mesh'}
    {#if meshFacets.nations.length > 0}
      <div class="border-border border-b px-3 py-1.5">
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wider">
          nation
        </div>
        <div class="flex flex-wrap gap-1">
          {#each meshFacets.nations as n (n)}
            {@const s = chipState(filter.nations, n)}
            <button
              type="button"
              onclick={() => toggleNation(n)}
              class="rounded border px-1.5 py-0.5 text-[10px] {chipBtnClass(s)}"
            >
              {n}
            </button>
          {/each}
        </div>
      </div>
    {/if}
    {#if meshFacets.categories.length > 0}
      <div class="border-border border-b px-3 py-1.5">
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wider">
          category
        </div>
        <div class="flex flex-wrap gap-1">
          {#each meshFacets.categories as c (c)}
            {@const s = chipState(filter.categories, c)}
            <button
              type="button"
              onclick={() => toggleCategory(c)}
              class="rounded border px-1.5 py-0.5 text-[10px] {chipBtnClass(s)}"
            >
              {c}
            </button>
          {/each}
        </div>
      </div>
    {/if}
  {:else}
    <!-- ammo mode -->
    {#if ammoFacets.ammoTypes.length > 0}
      <div class="border-border border-b px-3 py-1.5">
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wider">
          ammo type
        </div>
        <div class="flex flex-wrap gap-1">
          {#each ammoFacets.ammoTypes as t (t)}
            {@const s = chipState(filter.ammoTypes, t)}
            <button
              type="button"
              onclick={() => toggleAmmoType(t)}
              class="rounded border px-1.5 py-0.5 text-[10px] {chipBtnClass(s)}"
            >
              {t}
            </button>
          {/each}
        </div>
      </div>
    {/if}
    {#if ammoFacets.species.length > 0}
      <div class="border-border border-b px-3 py-1.5">
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wider">
          species
        </div>
        <div class="flex flex-wrap gap-1">
          {#each ammoFacets.species as sp (sp)}
            {@const s = chipState(filter.species, sp)}
            <button
              type="button"
              onclick={() => toggleSpecies(sp)}
              class="rounded border px-1.5 py-0.5 text-[10px] {chipBtnClass(s)}"
            >
              {sp}
            </button>
          {/each}
        </div>
      </div>
    {/if}
    {#if ammoFacets.nations.length > 0}
      <div class="border-border border-b px-3 py-1.5">
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wider">
          nation (from linked mesh)
        </div>
        <div class="flex flex-wrap gap-1">
          {#each ammoFacets.nations as n (n)}
            {@const s = chipState(filter.nations, n)}
            <button
              type="button"
              onclick={() => toggleNation(n)}
              class="rounded border px-1.5 py-0.5 text-[10px] {chipBtnClass(s)}"
            >
              {n}
            </button>
          {/each}
        </div>
      </div>
    {/if}
  {/if}

  <!-- Scrollable list -->
  <div class="flex-1 min-h-0 overflow-y-auto">
    {#if mode === 'mesh'}
      <ul class="m-0 list-none p-0">
        {#each meshRows as row (row.id)}
          <li>
            <button
              type="button"
              onclick={() => onSelect(row.id)}
              class="block w-full px-3 py-1.5 text-left text-[11px] hover:bg-popover/40 {activeId === row.id
                ? 'bg-sky-900/30 text-sky-100'
                : ''}"
            >
              <code class="font-mono break-all">{row.id}</code>
              <div class="text-muted-foreground mt-0.5 text-[10px]">
                {row.mesh.nation} · {row.mesh.category}
              </div>
            </button>
          </li>
        {/each}
        {#if meshRows.length === 0}
          <li class="text-muted-foreground p-3 text-[11px]">
            No meshes match the current filters.
          </li>
        {/if}
      </ul>
    {:else}
      <ul class="m-0 list-none p-0">
        {#each ammoRows as row (row.id)}
          <li>
            <button
              type="button"
              onclick={() => onSelect(row.id)}
              class="block w-full px-3 py-1.5 text-left text-[11px] hover:bg-popover/40 {activeId === row.id
                ? 'bg-sky-900/30 text-sky-100'
                : ''}"
            >
              <code class="font-mono break-all">{row.id}</code>
              <div class="text-muted-foreground mt-0.5 text-[10px]">
                {row.profile.ammo_type} · {row.profile.species}
                {#if row.mesh}
                  · {row.mesh.nation}/{row.mesh.category}
                {:else}
                  · <span class="text-amber-400">no mesh</span>
                {/if}
              </div>
            </button>
          </li>
        {/each}
        {#if ammoRows.length === 0}
          <li class="text-muted-foreground p-3 text-[11px]">
            No ammo profiles match the current filters.
          </li>
        {/if}
      </ul>
    {/if}
  </div>
</aside>
