<script lang="ts">
  // Middle-column permoflage picker. Lists every permoflage for the
  // selected Vehicle plus a synthetic "Base ship" row at the top
  // (selects `--variant-permoflage none`).
  //
  // The selection is exposed via callback — the parent route holds the
  // single source of truth, so flipping back from a permoflage to "Base
  // ship" round-trips cleanly through the URL or via direct prop.

  import { TOPOLOGY_LABELS } from '$lib/extract/labels';
  import type { Permoflage, Vehicle } from '$lib/types/extract';

  interface Props {
    vehicle: Vehicle | null;
    permoflages: Permoflage[];
    selectedExteriorId: string | null;
    /** Null = "Base ship". */
    onSelect: (p: Permoflage | null) => void;
  }

  const { vehicle, permoflages, selectedExteriorId, onSelect }: Props = $props();

  const topoClass: Record<string, string> = {
    mesh_swap: 'bg-violet-900/40 text-violet-200',
    mat_albedo: 'bg-amber-900/40 text-amber-200',
    mat_palette: 'bg-amber-900/40 text-amber-200',
    hull_palette: 'bg-cyan-900/40 text-cyan-200',
    tile_broadcast: 'bg-teal-900/40 text-teal-200',
    other: 'bg-secondary text-muted-foreground',
    base: 'bg-secondary text-muted-foreground',
  };
</script>

<section
  class="bg-card border-border flex w-[340px] flex-none flex-col border-r min-h-0"
>
  <header class="border-border border-b px-3.5 py-3 pb-2">
    <h1 class="m-0 text-sm font-semibold">Permoflages</h1>
    <div class="text-muted-foreground mt-1 text-[11px] tabular-nums">
      {#if vehicle}
        {permoflages.length} entries
      {:else}
        —
      {/if}
    </div>
  </header>
  {#if !vehicle}
    <div class="text-muted-foreground flex-1 px-3.5 py-6 text-xs">
      Pick a ship to see its permoflages.
    </div>
  {:else}
    <ul class="m-0 flex-1 list-none overflow-y-auto p-0">
      <li>
        <button
          type="button"
          onclick={() => onSelect(null)}
          class="border-border hover:bg-popover block w-full border-b border-l-[3px] border-l-transparent px-3.5 py-[7px] text-left {selectedExteriorId ===
          null
            ? 'bg-accent border-l-primary'
            : ''}"
        >
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="font-medium">Base ship</span>
            <span class="rounded px-1.5 py-[1px] text-[10px] {topoClass.base}">no permoflage</span>
          </div>
          <div class="text-muted-foreground mt-0.5 text-[11px]">
            passes <code>--variant-permoflage none</code>
          </div>
        </button>
      </li>
      {#each permoflages as p (p.exterior_id)}
        <li>
          <button
            type="button"
            onclick={() => onSelect(p)}
            class="border-border hover:bg-popover block w-full border-b border-l-[3px] border-l-transparent px-3.5 py-[7px] text-left {selectedExteriorId ===
            p.exterior_id
              ? 'bg-accent border-l-primary'
              : ''}"
          >
            <div class="flex flex-wrap items-center gap-1.5">
              <span class="font-medium">{p.display_name}</span>
              {#if p.is_native}
                <span class="bg-emerald-900/40 rounded px-1.5 py-[1px] text-[10px] text-emerald-200">
                  native
                </span>
              {:else if p.topology === 'mesh_swap'}
                <span
                  class="bg-amber-900/40 rounded px-1.5 py-[1px] text-[10px] text-amber-200"
                  title="Extracting this creates a separate legacy __<Variant> folder. The base ship's exteriors[] already carries this permoflage as a switchable entry — prefer extracting the Base ship."
                >
                  legacy folder
                </span>
              {/if}
            </div>
            <div class="mt-0.5 flex flex-wrap items-center gap-1 text-[11px]">
              <span class="rounded px-1.5 py-[1px] text-[10px] {topoClass[p.topology] ?? topoClass.other}">
                {TOPOLOGY_LABELS[p.topology]}
              </span>
              <code class="text-muted-foreground font-mono text-[10px]">{p.exterior_id}</code>
            </div>
            {#if p.mesh_swap_dir}
              <div class="text-muted-foreground mt-0.5 text-[11px]">
                → <code class="font-mono">{p.mesh_swap_dir}</code>
              </div>
            {/if}
          </button>
        </li>
      {:else}
        <li class="text-muted-foreground px-3.5 py-3.5 text-xs">
          No permoflages on this Vehicle.
        </li>
      {/each}
    </ul>
  {/if}
</section>
