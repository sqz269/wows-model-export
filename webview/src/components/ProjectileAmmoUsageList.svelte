<script lang="ts">
  // Reverse-lookup list: every ammo profile that references the currently
  // selected mesh. Shown in the Projectiles route's mesh mode so the user
  // can go model → which projectiles use it, then click any entry to load
  // its stats into the card below.
  //
  // The shared artillery shell mesh (CPA001_Shell_Main) is used by ~1800
  // ammo profiles, so a search box appears once the list is non-trivial.
  // Parent wraps this in {#key meshId} so internal filter state resets
  // when the selected mesh changes — no manual reset wiring needed.

  import type { AmmoProfile } from '$lib/types/projectiles';

  interface Props {
    /** Ammo ids referencing the active mesh, in display order. */
    ammoIds: string[];
    /** Full profile map for per-row type/species rendering. */
    profiles: Record<string, AmmoProfile>;
    /** Currently-inspected ammo id (highlighted). */
    selectedId: string | null;
    onSelect: (id: string) => void;
  }

  const { ammoIds, profiles, selectedId, onSelect }: Props = $props();

  // Show the search box only when scanning by eye gets unwieldy.
  const SHOW_SEARCH_THRESHOLD = 8;
  const showSearch = $derived(ammoIds.length > SHOW_SEARCH_THRESHOLD);

  let filterText = $state('');

  const filtered = $derived.by(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return ammoIds;
    return ammoIds.filter((id) => id.toLowerCase().includes(q));
  });
</script>

<div class="flex h-full min-h-0 flex-col">
  <div class="border-border flex flex-none flex-col gap-1.5 border-b px-3 py-2">
    <div class="text-muted-foreground flex items-baseline justify-between text-[10px] uppercase tracking-wider font-semibold">
      <span>Ammo using this mesh</span>
      <span class="text-muted-foreground/80 normal-case tracking-normal">
        {#if showSearch && filterText.trim()}
          {filtered.length} / {ammoIds.length}
        {:else}
          {ammoIds.length}
        {/if}
      </span>
    </div>
    {#if showSearch}
      <input
        type="text"
        placeholder="filter ammo id…"
        bind:value={filterText}
        class="h-6 w-full rounded border border-border bg-popover px-1.5 text-[11px] font-mono text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30"
      />
    {/if}
  </div>

  <div class="min-h-0 flex-1 overflow-y-auto">
    {#if ammoIds.length === 0}
      <p class="text-muted-foreground m-0 px-3 py-2 text-[11px]">
        No ammo profiles reference this mesh.
      </p>
    {:else}
      <ul class="m-0 list-none p-0">
        {#each filtered as id (id)}
          {@const p = profiles[id]}
          <li>
            <button
              type="button"
              onclick={() => onSelect(id)}
              class="block w-full px-3 py-1 text-left text-[11px] hover:bg-popover/40 {selectedId === id
                ? 'bg-sky-900/30 text-sky-100'
                : ''}"
            >
              <code class="font-mono break-all">{id}</code>
              {#if p}
                <div class="text-muted-foreground text-[10px]">
                  {p.ammo_type} · {p.species}
                </div>
              {/if}
            </button>
          </li>
        {/each}
        {#if filtered.length === 0}
          <li class="text-muted-foreground px-3 py-2 text-[11px]">
            No ammo id matches “{filterText}”.
          </li>
        {/if}
      </ul>
    {/if}
  </div>
</div>
