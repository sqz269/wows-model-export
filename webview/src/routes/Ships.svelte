<script lang="ts">
  // Ships route: loads /api/ships + /api/library on mount, hosts the
  // ship picker + viewer + controls panel. The URL hash drives ship
  // selection so the back button + bookmarks work
  // (`#/ship/<name>` → opens that ship).

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import { fetchLibrary, fetchShips } from '$lib/api';
  import type { LibraryIndex, ShipSummary } from '$lib/types';
  import type { ShipLoadStats, ShipViewer } from '$lib/ship';
  import ShipPicker from '$components/ShipPicker.svelte';
  import ShipViewerCmp from '$components/ShipViewer.svelte';
  import ShipControls from '$components/ShipControls.svelte';

  interface Props {
    param: string | null;
  }
  const { param }: Props = $props();

  let ships = $state<ShipSummary[]>([]);
  let library = $state<LibraryIndex | null>(null);
  let loadError = $state<string | null>(null);
  let viewer = $state<ShipViewer | null>(null);
  let progress = $state<string>('');
  let loadStats = $state<ShipLoadStats | null>(null);
  // Bumped each time the ship reloads so child controls can re-read state.
  let controlsRevision = $state(0);

  let activeShip = $derived.by(() => {
    if (!param) return null;
    return ships.find((s) => s.name === decodeURIComponent(param)) ?? null;
  });

  onMount(async () => {
    try {
      const [shipsRes, libRes] = await Promise.all([fetchShips(), fetchLibrary()]);
      ships = shipsRes;
      library = libRes;
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  });

  function selectShip(ship: ShipSummary) {
    progress = '';
    loadStats = null;
    navigate(`#/ship/${encodeURIComponent(ship.name)}`);
  }

  function onViewerLoaded(stats: ShipLoadStats) {
    loadStats = stats;
    controlsRevision++;
  }
</script>

<div class="ships-app">
  <ShipPicker {ships} activeName={activeShip?.name ?? null} onSelect={selectShip} />

  <section class="main">
    {#if loadError}
      <div class="placeholder error">
        <strong>Failed to load workspace:</strong>
        <code>{loadError}</code>
      </div>
    {:else if !library}
      <div class="placeholder">Loading library index…</div>
    {:else if !activeShip}
      <div class="placeholder">Pick a ship from the left to load it.</div>
    {:else}
      <ShipViewerCmp
        ship={activeShip}
        {library}
        bindHandle={(v) => {
          viewer = v;
        }}
        onProgress={(m) => (progress = m)}
        onLoaded={onViewerLoaded}
        onError={(err) => (progress = `Error: ${err instanceof Error ? err.message : String(err)}`)}
      />
      <div class="status">
        <span>{progress}</span>
        {#if loadStats}
          <span class="muted">
            · {loadStats.placementsRendered}/{loadStats.placementsRequested} placements · {loadStats.attachmentsRendered}
            attached
            {#if loadStats.attachmentsFilteredByMisc > 0}
              ({loadStats.attachmentsFilteredByMisc} miscFilter-dropped)
            {/if}
            · {loadStats.hullMeshCount} hull meshes
            {#if loadStats.unresolvedAssets.size > 0}
              · <span class="warn">{loadStats.unresolvedAssets.size} unresolved</span>
            {/if}
          </span>
        {/if}
      </div>
    {/if}
  </section>

  {#if activeShip && viewer}
    <ShipControls {viewer} hullGroups={viewer.getHullGroups()} revision={controlsRevision} />
  {/if}
</div>

<style>
  .ships-app {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    height: 100%;
  }
  .main {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    position: relative;
  }
  .placeholder {
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-muted);
    padding: 24px;
    text-align: center;
  }
  .placeholder.error {
    color: var(--danger);
  }
  .placeholder code {
    margin-left: 6px;
  }
  .status {
    flex: 0 0 auto;
    padding: 6px 12px;
    font-size: 11px;
    color: var(--fg);
    background: var(--bg-side);
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .muted {
    color: var(--fg-muted);
  }
  .warn {
    color: var(--warn);
  }
</style>
