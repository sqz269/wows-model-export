<script lang="ts">
  // Left-column Vehicle picker for the Extract page.
  //
  // - Search + filter chips (nation / class / tier / permoflage type /
  //   armament + native tri-state + show-in-test).
  // - Grouped ship list: when no text query is active, vehicles sharing a
  //   model_dir collapse into expandable parents (Atago family etc.).
  //   Active text search switches to flat mode so every match is visible.
  // - Renders up to 500 rows; overflow ones show a "refine search" hint.

  import Search from '@lucide/svelte/icons/search';
  import XIcon from '@lucide/svelte/icons/x';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import SlidersHorizontal from '@lucide/svelte/icons/sliders-horizontal';
  import { defaultFilterState, deriveFilterOptions, filterVehicles, groupByModelDir } from '$lib/extract/filters';
  import {
    COMMON_ARMAMENT,
    VFS_STATUS_META,
    armamentLabel,
    fallbackPeculiarityLabel,
    nationLabel,
    statusCategory,
  } from '$lib/extract/labels';
  import type {
    ExtractFilterState,
    NativeFilter,
    PeculiarityLabel,
    Vehicle,
  } from '$lib/types/extract';

  interface Props {
    vehicles: Vehicle[];
    peculiarityLabels: Record<string, PeculiarityLabel>;
    activeTopKey: string | null;
    onSelect: (v: Vehicle) => void;
  }

  const { vehicles, peculiarityLabels, activeTopKey, onSelect }: Props = $props();

  let filterState = $state<ExtractFilterState>(defaultFilterState());

  // Filter panel collapses by default — six chip rows otherwise crowd
  // the ship list on short viewports. Auto-opens once the user sets a
  // filter so the active criteria stay visible.
  let filtersOpen = $state(false);

  // Group expansion is in-component memory only — auto-grows to include the
  // active selection's model_dir so the user always sees their pick.
  let expandedGroups = $state<Set<string>>(new Set());

  let searchEl: HTMLInputElement | null = $state(null);

  const options = $derived(deriveFilterOptions(vehicles));
  const filtered = $derived(filterVehicles(vehicles, filterState));

  // Cap rendered rows at 500 — a 2000-Vehicle corpus would overflow the
  // sidebar and tank scroll performance.
  const ROW_CAP = 500;
  const visible = $derived(filtered.slice(0, ROW_CAP));
  const overflow = $derived(Math.max(0, filtered.length - ROW_CAP));

  // Group mode flips off when the user types — flat list lets the user see
  // every match at once. Auto-expand the selected vehicle's group so the
  // active row stays visible after a click.
  const groupMode = $derived(!filterState.text.trim());
  const buckets = $derived(groupMode ? groupByModelDir(visible) : []);

  $effect(() => {
    const active = vehicles.find((v) => v.top_key === activeTopKey);
    if (active?.model_dir) {
      const md = active.model_dir;
      if (!expandedGroups.has(md)) {
        const next = new Set(expandedGroups);
        next.add(md);
        expandedGroups = next;
      }
    }
  });

  // Mutate-and-reassign keeps Svelte 5 reactivity simple for Set toggles.
  function toggleClass(c: string) {
    const next = new Set(filterState.classes);
    next.has(c) ? next.delete(c) : next.add(c);
    filterState = { ...filterState, classes: next };
  }
  function toggleTier(t: number) {
    const next = new Set(filterState.tiers);
    next.has(t) ? next.delete(t) : next.add(t);
    filterState = { ...filterState, tiers: next };
  }
  function togglePeculiarity(p: string) {
    const next = new Set(filterState.peculiarities);
    next.has(p) ? next.delete(p) : next.add(p);
    filterState = { ...filterState, peculiarities: next };
  }
  function toggleArmament(a: string) {
    const next = new Set(filterState.armaments);
    next.has(a) ? next.delete(a) : next.add(a);
    filterState = { ...filterState, armaments: next };
  }
  function setNative(val: 'has' | 'no') {
    filterState = { ...filterState, native: filterState.native === val ? 'any' : (val as NativeFilter) };
  }
  function toggleGroup(md: string) {
    const next = new Set(expandedGroups);
    next.has(md) ? next.delete(md) : next.add(md);
    expandedGroups = next;
  }
  function clearAll() {
    filterState = defaultFilterState();
  }

  export function focusSearch() {
    searchEl?.focus();
    searchEl?.select();
  }

  function peculiarityTooltip(key: string, count: number, meta?: PeculiarityLabel): string {
    const sourceTag = meta ? `via ${meta.source}` : 'no metadata';
    const samples = meta && meta.sample_names.length ? `\nsamples: ${meta.sample_names.join(' / ')}` : '';
    return `${key} · ${count} ships · ${sourceTag}${samples}`;
  }

  function groupHeadliner(group: Vehicle[]): { headliner: Vehicle; liveCount: number; headlinerLive: boolean } {
    const live = group.filter((v) => statusCategory(v.group) === 'live');
    const sorted = (live.length > 0 ? live : group)
      .slice()
      .sort((a, b) => b.permoflages_count - a.permoflages_count);
    return {
      headliner: sorted[0],
      liveCount: live.length,
      headlinerLive: live.includes(sorted[0]),
    };
  }

  const counter = $derived(
    vehicles.length === filtered.length ? `${vehicles.length} ships` : `${filtered.length} / ${vehicles.length} ships`,
  );

  // Count of *active* filters (excluding the show-in-test toggle, which
  // defaults off and is more of a corpus switch than a filter).
  const activeFilterCount = $derived(
    (filterState.nation ? 1 : 0) +
      filterState.classes.size +
      filterState.tiers.size +
      filterState.peculiarities.size +
      filterState.armaments.size +
      (filterState.native !== 'any' ? 1 : 0),
  );

  // Auto-open once a filter is set so users can see what they're applying;
  // never auto-close (manual collapse is the whole point of the toggle).
  $effect(() => {
    if (activeFilterCount > 0) filtersOpen = true;
  });
</script>

{#snippet chipBtn(active: boolean, onclick: () => void, title: string | null, label: string)}
  <button
    type="button"
    onclick={onclick}
    title={title ?? undefined}
    class="bg-popover text-foreground border-border hover:bg-accent rounded border px-2 py-[3px] text-[11px] {active
      ? 'bg-accent border-l-primary border-l-[2px]'
      : ''}"
  >
    {label}
  </button>
{/snippet}

{#snippet badge(text: string, klass: string, title?: string | null)}
  <span class="rounded px-1.5 py-[1px] text-[10px] {klass}" title={title ?? undefined}>{text}</span>
{/snippet}

{#snippet rowBadges(v: Vehicle, nested: boolean)}
  {@const cat = statusCategory(v.group)}
  {#if !nested && v.nation}
    {@render badge(v.nation, 'bg-secondary text-muted-foreground')}
  {/if}
  {#if v.class}
    {@render badge(v.class, 'bg-secondary text-muted-foreground')}
  {/if}
  {#if v.tier}
    {@render badge(`T${v.tier}`, 'bg-secondary text-muted-foreground')}
  {/if}
  {#if v.group}
    {@render badge(
      v.group,
      cat === 'live'
        ? 'text-emerald-200 bg-emerald-900/40'
        : cat === 'dev'
          ? 'text-amber-200 bg-amber-900/40'
          : cat === 'restricted'
            ? 'text-sky-200 bg-sky-900/40'
            : cat === 'retired'
              ? 'text-rose-200 bg-rose-900/40'
              : 'bg-secondary text-muted-foreground',
      `WG Vehicle.group = ${v.group} (${cat})`,
    )}
  {/if}
  {#if v.is_paper}
    {@render badge('paper', 'bg-indigo-900/40 text-indigo-200', 'WG isPaperShip = true')}
  {/if}
  {#each (v.armaments ?? []).filter((a) => !COMMON_ARMAMENT.has(a)) as a (a)}
    {@render badge(armamentLabel(a), 'bg-purple-900/40 text-purple-200', `armament: ${a}`)}
  {/each}
  {#if v.vfs_status && v.vfs_status !== 'ok' && v.vfs_status !== 'unknown'}
    {@const meta = VFS_STATUS_META[v.vfs_status]}
    {@render badge(
      meta.label,
      meta.sev === 'warn' ? 'bg-amber-900/40 text-amber-200' : 'bg-rose-900/40 text-rose-200',
      meta.title,
    )}
  {/if}
  {#if !nested && v.shares_model_dir_with.length > 0}
    {@render badge(
      '⚠ collision',
      'bg-amber-900/40 text-amber-200',
      `model_dir shared with: ${v.shares_model_dir_with.join(', ')}`,
    )}
  {/if}
{/snippet}

<aside class="bg-card border-border flex w-[340px] flex-none flex-col border-r min-h-0">
  <header class="border-border border-b px-3.5 py-3 pb-2">
    <h1 class="m-0 text-sm font-semibold">Ships</h1>
    <div class="text-muted-foreground mt-1 text-[11px] tabular-nums">{counter}</div>
    <div class="relative mt-2 flex items-center">
      <Search class="text-muted-foreground pointer-events-none absolute left-2 size-3" />
      <input
        bind:this={searchEl}
        type="search"
        value={filterState.text}
        oninput={(e) => (filterState = { ...filterState, text: e.currentTarget.value })}
        placeholder="name / param_index / model_dir"
        class="bg-popover text-foreground border-border placeholder:text-muted-foreground focus:border-ring focus:ring-ring/30 h-7 w-full rounded border px-6 text-xs outline-none focus:ring-2 [&::-webkit-search-cancel-button]:hidden"
      />
      {#if filterState.text}
        <button
          type="button"
          class="text-muted-foreground hover:bg-popover hover:text-foreground absolute right-1 flex size-[18px] items-center justify-center rounded"
          onclick={() => {
            filterState = { ...filterState, text: '' };
            searchEl?.focus();
          }}
          aria-label="Clear search"
        >
          <XIcon class="size-3" />
        </button>
      {/if}
    </div>
  </header>

  <button
    type="button"
    onclick={() => (filtersOpen = !filtersOpen)}
    class="border-border hover:bg-popover text-muted-foreground flex flex-none items-center gap-1.5 border-b px-3.5 py-1.5 text-left text-[11px]"
    aria-expanded={filtersOpen}
  >
    <SlidersHorizontal class="size-3" />
    <span class="font-semibold uppercase tracking-wide">Filters</span>
    {#if activeFilterCount > 0}
      <span class="bg-accent text-foreground rounded px-1.5 py-[1px] text-[10px] tabular-nums">
        {activeFilterCount}
      </span>
    {/if}
    <span class="ml-auto">
      {#if filtersOpen}
        <ChevronDown class="size-3" />
      {:else}
        <ChevronRight class="size-3" />
      {/if}
    </span>
  </button>

  {#if filtersOpen}
  <div class="border-border flex flex-none flex-col gap-2 border-b px-3.5 py-2.5 text-xs">
    <label class="text-muted-foreground flex flex-col gap-0.5 text-[11px]">
      Nation
      <select
        value={filterState.nation ?? ''}
        onchange={(e) => (filterState = { ...filterState, nation: e.currentTarget.value || null })}
        class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
      >
        <option value="">All nations</option>
        {#each options.nations as n (n)}
          <option value={n}>{nationLabel(n)}</option>
        {/each}
      </select>
    </label>

    {#if options.classes.length > 0}
      <div>
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Class</div>
        <div class="flex flex-wrap gap-1">
          {#each options.classes as c (c)}
            {@render chipBtn(filterState.classes.has(c), () => toggleClass(c), null, c)}
          {/each}
        </div>
      </div>
    {/if}

    {#if options.tiers.length > 0}
      <div>
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Tier</div>
        <div class="flex flex-wrap gap-1">
          {#each options.tiers as t (t)}
            {@render chipBtn(filterState.tiers.has(t), () => toggleTier(t), null, String(t))}
          {/each}
        </div>
      </div>
    {/if}

    <div>
      <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Native permoflage</div>
      <div class="flex flex-wrap gap-1">
        {@render chipBtn(filterState.native === 'has', () => setNative('has'), null, 'has native')}
        {@render chipBtn(filterState.native === 'no', () => setNative('no'), null, 'no native')}
      </div>
    </div>

    {#if options.peculiarities.length > 0}
      <div>
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Permoflage type</div>
        <div class="flex flex-wrap gap-1">
          {#each options.peculiarities as { key, count } (key)}
            {@const meta = peculiarityLabels[key]}
            {@render chipBtn(
              filterState.peculiarities.has(key),
              () => togglePeculiarity(key),
              peculiarityTooltip(key, count, meta),
              meta?.label ?? fallbackPeculiarityLabel(key),
            )}
          {/each}
        </div>
      </div>
    {/if}

    {#if options.armaments.length > 0}
      <div>
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Armament</div>
        <div class="flex flex-wrap gap-1">
          {#each options.armaments as { key, count } (key)}
            {@render chipBtn(
              filterState.armaments.has(key),
              () => toggleArmament(key),
              `${key} · ${count} ships`,
              armamentLabel(key),
            )}
          {/each}
        </div>
      </div>
    {/if}

    <div class="flex items-center justify-between gap-2">
      <label class="text-foreground flex items-center gap-1.5 text-xs">
        <input
          type="checkbox"
          checked={filterState.showTest}
          onchange={(e) => (filterState = { ...filterState, showTest: e.currentTarget.checked })}
        />
        show in-test
      </label>
      <button
        type="button"
        onclick={clearAll}
        class="text-muted-foreground hover:text-foreground text-[11px] underline"
      >
        clear filters
      </button>
    </div>
  </div>
  {/if}

  <ul class="m-0 flex-1 list-none overflow-y-auto p-0">
    {#if filtered.length === 0}
      <li class="text-muted-foreground px-3.5 py-3.5 text-xs">No ships match.</li>
    {:else if !groupMode}
      {#each visible as v (v.top_key)}
        {@render shipRow(v, false)}
      {/each}
    {:else}
      {#each buckets as bucket (bucket.modelDir)}
        {#if bucket.vehicles.length === 1}
          {@render shipRow(bucket.vehicles[0], false)}
        {:else}
          {@render groupHeader(bucket.modelDir, bucket.vehicles)}
          {#if expandedGroups.has(bucket.modelDir)}
            {#each bucket.vehicles as child (child.top_key)}
              {@render shipRow(child, true)}
            {/each}
          {/if}
        {/if}
      {/each}
    {/if}
    {#if overflow > 0}
      <li class="text-muted-foreground px-3.5 py-3 text-xs italic">
        +{overflow} more — refine search
      </li>
    {/if}
  </ul>
</aside>

{#snippet shipRow(v: Vehicle, nested: boolean)}
  {@const active = v.top_key === activeTopKey}
  {@const cat = statusCategory(v.group)}
  <li>
    <button
      type="button"
      onclick={() => onSelect(v)}
      class="border-border hover:bg-popover block w-full border-b border-l-[3px] border-l-transparent px-3.5 py-[7px] text-left {nested
        ? 'pl-7'
        : ''} {active ? 'bg-accent border-l-primary' : ''} {cat === 'live' ? '' : 'opacity-60'}"
    >
      <div class="flex flex-wrap items-center gap-1.5">
        <code class="text-muted-foreground font-mono text-[10px]">{v.param_index}</code>
        <span class="font-medium">{v.display_name}</span>
      </div>
      <div class="mt-0.5 flex flex-wrap items-center gap-1">
        {@render rowBadges(v, nested)}
      </div>
    </button>
  </li>
{/snippet}

{#snippet groupHeader(modelDir: string, group: Vehicle[])}
  {@const expanded = expandedGroups.has(modelDir)}
  {@const tiers = Array.from(new Set(group.map((v) => v.tier).filter((t): t is number => t != null))).sort(
    (a, b) => a - b,
  )}
  {@const classes = Array.from(new Set(group.map((v) => v.class).filter((c): c is string => !!c)))}
  {@const nations = Array.from(new Set(group.map((v) => v.nation).filter((n): n is string => !!n)))}
  {@const { headliner, liveCount, headlinerLive } = groupHeadliner(group)}
  <li>
    <button
      type="button"
      onclick={() => toggleGroup(modelDir)}
      class="border-border hover:bg-popover block w-full border-b px-3.5 py-[7px] text-left"
    >
      <div class="flex flex-wrap items-center gap-1.5">
        {#if expanded}
          <ChevronDown class="text-muted-foreground size-3" />
        {:else}
          <ChevronRight class="text-muted-foreground size-3" />
        {/if}
        <code class="text-foreground font-mono text-xs">{modelDir}</code>
        <span class="text-muted-foreground text-[11px]">{group.length} Vehicles</span>
        {#if liveCount > 0}
          {@render badge(
            `${liveCount} live`,
            'bg-emerald-900/40 text-emerald-200',
            'Vehicles in the live game (upgradeable/special/ultimate/superShip/premium/specialUnsellable/start)',
          )}
        {:else}
          {@render badge(
            'none live',
            'bg-rose-900/40 text-rose-200',
            'No Vehicle in this hull family is currently in the live game',
          )}
        {/if}
      </div>
      <div class="mt-0.5 flex flex-wrap items-center gap-1 text-[11px]">
        {#if nations.length === 1}
          {@render badge(nations[0], 'bg-secondary text-muted-foreground')}
        {/if}
        {#if classes.length === 1}
          {@render badge(classes[0], 'bg-secondary text-muted-foreground')}
        {/if}
        {#if tiers.length === 1}
          {@render badge(`T${tiers[0]}`, 'bg-secondary text-muted-foreground')}
        {:else if tiers.length > 1}
          {@render badge(`T${tiers[0]}–T${tiers[tiers.length - 1]}`, 'bg-secondary text-muted-foreground')}
        {/if}
        <span class="text-muted-foreground">
          {headlinerLive ? 'live: ' : 'e.g. '}
          {headliner.display_name}
        </span>
      </div>
    </button>
  </li>
{/snippet}
