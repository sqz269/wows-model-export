<script lang="ts">
  // Accessory library route. Fetches /api/library, wires sidebar (filters
  // + list) and right-pane detail (viewer + controls + info).
  //
  // Hash routing: `#/library` shows the picker; `#/asset/<id>` opens
  // that asset. The router parses `<id>` into the typed `assetId` prop.

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import { fetchLibrary } from '$lib/api';
  import { settingsHref } from '$lib/nav_state.svelte';
  import { navState } from '$lib/nav_state.svelte';
  import type { LibraryFilter, LibraryIndex } from '$lib/types';
  import AssetList from '$components/AssetList.svelte';
  import AssetDetail from '$components/AssetDetail.svelte';
  import type { SortKey } from '$components/AssetList.svelte';

  interface Props {
    /** Asset_id from the URL (`#/asset/<id>`), or null when the user
     *  is on a different page or on bare `#/library`. Domain-typed by
     *  App.svelte's discriminated union routing. */
    assetId: string | null;
    /** True iff this is the active route. Reserved for the asset
     *  search `/` shortcut when it lands. */
    active: boolean;
  }
  const { assetId, active: _active }: Props = $props();

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

  // Sticky internal selection (see Ships.svelte for the same pattern).
  // `assetId` is the URL claim (`AGM034_…` from `#/asset/AGM034_…`);
  // when the user navigates to another tab the URL changes and
  // `assetId` goes null, but the selection should survive so coming
  // back via the topnav restores exactly what was open.
  let selectedAssetId = $state<string | null>(null);
  $effect(() => {
    if (assetId) selectedAssetId = decodeURIComponent(assetId);
  });

  // The active asset is derived from the index (once loaded) +
  // selectedAssetId. We don't pre-route — wait until we have data so
  // deep links never hit a stale "asset not found" flash.
  const activeId = $derived.by(() => {
    if (!index || !selectedAssetId) return null;
    return index.assets[selectedAssetId] ? selectedAssetId : null;
  });

  const activeAsset = $derived(activeId && index ? index.assets[activeId] : null);

  // Mirror to cross-route nav memory so the topnav "Library" link lands
  // back here with the right asset open after a detour through Ships.
  $effect(() => {
    if (activeId) navState.lastAssetId = activeId;
  });

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
        The accessory library index doesn't exist yet. Open
        <a
          href={settingsHref()}
          onclick={(e) => {
            e.preventDefault();
            navigate(settingsHref());
          }}
          class="text-foreground underline hover:no-underline"
        >Settings → Workspace artifacts</a>
        and click <em>Build</em> next to “Accessory library index”.
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
