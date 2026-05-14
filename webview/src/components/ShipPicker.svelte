<script lang="ts">
  // Ship-picker sidebar. Renders the ship list with per-section badges +
  // emits a `select` event when the user clicks a row.
  //
  // A search input above the list filters on display_name / name / nation /
  // ship_class (substring, case-insensitive). The filter text persists
  // across reloads via the store so revisiting the page keeps the user's
  // working subset.

  import Search from '@lucide/svelte/icons/search';
  import XIcon from '@lucide/svelte/icons/x';
  import { loadState, patchState } from '$lib/store';
  import type { ShipSummary } from '$lib/types';

  interface Props {
    ships: ShipSummary[];
    activeName: string | null;
    onSelect: (ship: ShipSummary) => void;
  }

  const { ships, activeName, onSelect }: Props = $props();

  const TIERS = (t: number | null) => (t === null ? '' : `T${t}`);

  let query = $state(loadState().shipSearch);
  let searchEl: HTMLInputElement | null = $state(null);

  // Filter is cheap (9–50 ships expected); recompute on every keystroke.
  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return ships;
    return ships.filter((s) =>
      [s.display_name, s.name, s.nation, s.ship_class]
        .filter((x): x is string => !!x)
        .some((f) => f.toLowerCase().includes(q)),
    );
  });

  function onQueryInput(v: string) {
    query = v;
    patchState({ shipSearch: v });
  }

  function clearQuery() {
    onQueryInput('');
    searchEl?.focus();
  }

  // Exposed for the global `/` shortcut in App.svelte.
  export function focusSearch() {
    searchEl?.focus();
    searchEl?.select();
  }
</script>

<aside class="bg-card border-border flex w-[280px] flex-none flex-col border-r min-h-0">
  <header class="border-border border-b px-3.5 py-3 pb-2">
    <h1 class="m-0 text-sm font-semibold">Ships</h1>
    <div class="text-muted-foreground mt-1 text-[11px]">
      {#if query.trim()}
        {filtered.length} / {ships.length} match
      {:else}
        {ships.length} ship{ships.length === 1 ? '' : 's'} indexed
      {/if}
    </div>
    <div class="relative mt-2 flex items-center">
      <Search class="text-muted-foreground pointer-events-none absolute left-2 size-3" />
      <input
        bind:this={searchEl}
        type="search"
        value={query}
        oninput={(e) => onQueryInput(e.currentTarget.value)}
        placeholder="Search…  (press /)"
        aria-label="Filter ships"
        class="bg-popover text-foreground border-border placeholder:text-muted-foreground focus:ring-ring/30 h-7 w-full rounded border px-6 text-xs outline-none focus:border-ring focus:ring-2 [&::-webkit-search-cancel-button]:hidden"
      />
      {#if query}
        <button
          type="button"
          class="text-muted-foreground hover:bg-popover hover:text-foreground absolute right-1 flex size-[18px] items-center justify-center rounded"
          onclick={clearQuery}
          aria-label="Clear search"
        >
          <XIcon class="size-3" />
        </button>
      {/if}
    </div>
  </header>
  <ul class="m-0 flex-1 list-none overflow-y-auto p-0">
    {#each filtered as ship (ship.name)}
      <li>
        <button
          type="button"
          onclick={() => onSelect(ship)}
          class="border-border hover:bg-popover block w-full border-b border-l-[3px] border-l-transparent px-3.5 py-[7px] text-left {activeName ===
          ship.name
            ? 'bg-accent border-l-primary'
            : ''}"
        >
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="font-medium">{ship.display_name}</span>
            {#if ship.ship_class}
              <span
                class="bg-secondary text-muted-foreground rounded px-1.5 py-[1px] text-[10px] tracking-wider"
              >
                {ship.ship_class}
              </span>
            {/if}
            {#if ship.tier}
              <span
                class="bg-secondary text-muted-foreground rounded px-1.5 py-[1px] text-[10px] tracking-wider"
              >
                {TIERS(ship.tier)}
              </span>
            {/if}
          </div>
          <div
            class="text-muted-foreground mt-0.5 flex gap-2 text-[11px] tabular-nums"
          >
            <span>{ship.section_counts.turrets}T</span>
            <span>{ship.section_counts.secondaries}S</span>
            <span>{ship.section_counts.antiair}AA</span>
            <span>{ship.section_counts.torpedoes}TT</span>
            <span>{ship.section_counts.accessories}·</span>
            {#if ship.nation}
              <span class="ml-auto lowercase">{ship.nation}</span>
            {/if}
          </div>
        </button>
      </li>
    {:else}
      <li class="text-muted-foreground px-3.5 py-3.5 text-xs">
        {#if ships.length === 0}
          No ships in workspace. Run <code>wows-ingest-ship &lt;Ship&gt;</code> to add one.
        {:else}
          No ships match <code>{query}</code>.
        {/if}
      </li>
    {/each}
  </ul>
</aside>
