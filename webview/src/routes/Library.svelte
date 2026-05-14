<script lang="ts">
  // Accessory library route. Fetches /api/library, wires sidebar (filters
  // + list) and right-pane detail (viewer + controls + info).
  //
  // Hash routing: `#/library` shows the picker; `#/asset/<id>` opens that
  // asset. The router parses `<id>` into `param` for both cases — when
  // `param` is non-null at the library page, it's an asset_id.

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import { fetchLibrary } from '$lib/api';
  import type { LibraryFilter, LibraryIndex } from '$lib/types';
  import AssetList from '$components/AssetList.svelte';
  import AssetDetail from '$components/AssetDetail.svelte';
  import type { SortKey } from '$components/AssetList.svelte';

  interface Props {
    param: string | null;
  }
  const { param }: Props = $props();

  let index = $state<LibraryIndex | null>(null);
  let loadError = $state<string | null>(null);

  let filter = $state<LibraryFilter>({
    scope: null,
    category: null,
    subcategory: null,
    ship: null,
    deadOnly: false,
    newOnly: false,
    untexturedOnly: false,
    query: '',
  });
  let sort = $state<SortKey>('id-asc');

  // The active asset is derived from the hash (param) once the index has
  // loaded. We don't pre-route — wait until we have data so deep links
  // never hit a stale "asset not found" flash.
  const activeId = $derived.by(() => {
    if (!index || !param) return null;
    const decoded = decodeURIComponent(param);
    return index.assets[decoded] ? decoded : null;
  });

  const activeAsset = $derived(activeId && index ? index.assets[activeId] : null);

  onMount(async () => {
    try {
      index = await fetchLibrary();
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  });

  function selectAsset(id: string) {
    navigate(`#/asset/${encodeURIComponent(id)}`);
  }
</script>

<div class="flex flex-1 min-w-0 h-full">
  {#if loadError}
    <div
      class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center"
    >
      <strong>Failed to load library:</strong>
      <code class="mx-1">{loadError}</code>
      <p class="text-muted-foreground m-0 max-w-[50ch]">
        Run <code>wows-build-accessory-library</code> against your workspace, then refresh.
      </p>
    </div>
  {:else if !index}
    <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
      Loading library index…
    </div>
  {:else}
    <AssetList
      {index}
      {filter}
      {sort}
      {activeId}
      onFilterChange={(next) => (filter = next)}
      onSortChange={(next) => (sort = next)}
      onSelect={selectAsset}
    />

    {#if activeId && activeAsset}
      {#key activeId}
        <AssetDetail id={activeId} asset={activeAsset} />
      {/key}
    {:else}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Select an asset from the list.
      </div>
    {/if}
  {/if}
</div>
