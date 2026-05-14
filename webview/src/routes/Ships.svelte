<script lang="ts">
  // Ships route: loads /api/ships + /api/library on mount, hosts the
  // ship picker + viewer + controls panel. The URL hash drives ship
  // selection so the back button + bookmarks work
  // (`#/ship/<name>` → opens that ship).
  //
  // Async load lifecycle is reported via a single sticky svelte-sonner
  // toast per ship that promotes to success / warning / error on
  // completion; the durable status bar below the viewer keeps the
  // final-state summary so the user can scan it without a toast hover.

  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { navigate } from '$lib/router';
  import { fetchLibrary, fetchShips } from '$lib/api';
  import { navState } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import type { LibraryIndex, ShipSummary } from '$lib/types';
  import type { PickResult, ShipLoadStats, ShipViewer } from '$lib/ship';
  import ShipPicker from '$components/ShipPicker.svelte';
  import ShipViewerCmp from '$components/ShipViewer.svelte';
  import ShipControls from '$components/ShipControls.svelte';
  import MeshInspector from '$components/MeshInspector.svelte';

  interface Props {
    /** Ship name from the URL (`#/ship/<name>`), or null when the user
     *  is on a different page. Domain-typed by App.svelte's discriminated
     *  union routing so an asset_id from `#/asset/<id>` can never be
     *  passed in here. */
    shipName: string | null;
    /** True iff this is the active route. Page-local keydown handlers
     *  short-circuit when false so a `/` on the Library page doesn't
     *  steal focus from the asset search to a hidden ship picker. */
    active: boolean;
  }
  const { shipName, active }: Props = $props();

  let ships = $state<ShipSummary[]>([]);
  let library = $state<LibraryIndex | null>(null);
  let loadError = $state<string | null>(null);
  let viewer = $state<ShipViewer | null>(null);
  let loadStats = $state<ShipLoadStats | null>(null);
  // Bumped each time the ship reloads so child controls can re-read state.
  let controlsRevision = $state(0);
  // Active sticky loading toast id; promoted to success/warning/error on
  // completion so each ship swap reuses one slot instead of stacking.
  let shipLoadToastId: string | number | null = null;
  // Picker binding for the `/` shortcut. Component instance exports
  // (svelte 5 supports the v4 `export function` pattern via bind:this).
  let pickerRef: ShipPicker | null = $state(null);
  // Latest mesh-inspector pick + its local-x/y inside `.main`. Cleared by
  // clicking empty space, the inspector's close button, or pressing ESC.
  let selectedPick = $state<PickResult | null>(null);
  let inspectorX = $state(0);
  let inspectorY = $state(0);
  let mainEl: HTMLElement | null = $state(null);

  function handlePick(pick: PickResult | null, clientX: number, clientY: number) {
    if (!pick || !mainEl) {
      selectedPick = null;
      return;
    }
    const rect = mainEl.getBoundingClientRect();
    // Nudge a few px below-right of the click point so the card doesn't
    // cover what was just picked. Clamp inside .main on the right + bottom
    // edges so a click near the corner doesn't push the card offscreen.
    const NUDGE = 12;
    const PANEL_W = 280;
    const PANEL_H = 220;
    const localX = Math.min(clientX - rect.left + NUDGE, rect.width - PANEL_W - 8);
    const localY = Math.min(clientY - rect.top + NUDGE, rect.height - PANEL_H - 8);
    inspectorX = Math.max(8, localX);
    inspectorY = Math.max(8, localY);
    selectedPick = pick;
  }

  // Sticky internal selection. `shipName` carries the URL claim (`Iowa`
  // from `#/ship/Iowa`). When the user switches tabs the URL changes to
  // `#/library` / `#/asset/<id>` and `shipName` goes null — but we want
  // the viewer + sidebar state to survive. Adopt `shipName` when
  // non-null, hold the previous value when it goes away. The topnav's
  // `shipsHref()` reads `navState.lastShipName` to restore the right
  // URL on tab return.
  let selectedShipName = $state<string | null>(null);
  $effect(() => {
    if (shipName) selectedShipName = decodeURIComponent(shipName);
  });

  let activeShip = $derived.by(() => {
    if (!selectedShipName) return null;
    return ships.find((s) => s.name === selectedShipName) ?? null;
  });

  // Mirror the active selection into the cross-route nav memory so the
  // topnav link to "Ships" lands back here with the right ship loaded
  // even after a detour through Library / Extract.
  $effect(() => {
    if (activeShip) navState.lastShipName = activeShip.name;
  });

  onMount(() => {
    // Fetch ships + library in the background. onMount's cleanup return
    // must be sync, so we can't `await` here directly.
    void (async () => {
      try {
        const [shipsRes, libRes] = await Promise.all([fetchShips(), fetchLibrary()]);
        ships = shipsRes;
        library = libRes;
      } catch (err) {
        loadError = err instanceof Error ? err.message : String(err);
      }
    })();

    // Page-local shortcuts. The router keeps every route mounted (see
    // App.svelte), so we gate on `active` instead of relying on
    // mount/unmount lifecycle to scope the listener to this page only.
    const onKey = (e: KeyboardEvent) => {
      if (!active) return;
      if (hasModifier(e)) return;
      if (isTypingContext(e)) return;
      switch (e.key) {
        case '/':
          pickerRef?.focusSearch();
          e.preventDefault();
          return;
        case 'Escape':
          if (selectedPick) {
            selectedPick = null;
            e.preventDefault();
          }
          return;
        case 'r':
        case 'R':
          viewer?.resetCamera();
          e.preventDefault();
          return;
        case 'f':
        case 'F': {
          if (!viewer) return;
          viewer.frameOn(selectedPick?.object ?? null);
          e.preventDefault();
          return;
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  function selectShip(ship: ShipSummary) {
    loadStats = null;
    navigate(`#/ship/${encodeURIComponent(ship.name)}`);
  }

  function onShipProgress(msg: string) {
    const ship = activeShip;
    if (!ship) return;
    if (shipLoadToastId === null) {
      shipLoadToastId = toast.loading(msg, {
        description: ship.display_name,
        duration: Number.POSITIVE_INFINITY,
      });
    } else {
      toast.loading(msg, {
        id: shipLoadToastId,
        description: ship.display_name,
        duration: Number.POSITIVE_INFINITY,
      });
    }
  }

  function onViewerLoaded(stats: ShipLoadStats) {
    loadStats = stats;
    controlsRevision++;
    const unresolved = stats.unresolvedAssets.size;
    const summary =
      `${stats.placementsRendered}/${stats.placementsRequested} placements` +
      ` · ${stats.hullMeshCount} hull meshes` +
      ` · ${(stats.loadMs / 1000).toFixed(1)}s`;
    if (shipLoadToastId !== null) {
      if (unresolved > 0) {
        toast.warning(summary, {
          id: shipLoadToastId,
          description: `${stats.ship.display_name} · ${unresolved} unresolved`,
          duration: 4500,
        });
      } else {
        toast.success(summary, {
          id: shipLoadToastId,
          description: stats.ship.display_name,
          duration: 3000,
        });
      }
      shipLoadToastId = null;
    }
  }

  function onViewerError(err: unknown) {
    const ship = activeShip;
    const label = ship ? `Failed to load ${ship.display_name}` : 'Ship load failed';
    const msg = err instanceof Error ? err.message : String(err);
    if (shipLoadToastId !== null) {
      toast.error(label, {
        id: shipLoadToastId,
        description: msg,
        duration: 8000,
      });
      shipLoadToastId = null;
    } else {
      toast.error(label, { description: msg, duration: 8000 });
    }
  }
</script>

<div class="flex flex-1 min-w-0 h-full">
  <ShipPicker
    bind:this={pickerRef}
    {ships}
    activeName={activeShip?.name ?? null}
    onSelect={selectShip}
  />

  <section class="relative flex flex-1 min-w-0 flex-col" bind:this={mainEl}>
    {#if loadError}
      <div class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
        <strong>Failed to load workspace:</strong>
        <code class="ml-1.5">{loadError}</code>
      </div>
    {:else if !library}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Loading library index…
      </div>
    {:else if !activeShip}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Pick a ship from the left to load it.
      </div>
    {:else}
      <ShipViewerCmp
        ship={activeShip}
        {library}
        bindHandle={(v) => {
          viewer = v;
        }}
        onProgress={onShipProgress}
        onLoaded={onViewerLoaded}
        onError={onViewerError}
        onPick={handlePick}
      />
      {#if selectedPick}
        <MeshInspector
          info={selectedPick.info}
          x={inspectorX}
          y={inspectorY}
          {library}
          onClose={() => (selectedPick = null)}
        />
      {/if}
      <div
        class="bg-card border-border flex flex-none items-center gap-2 border-t px-3 py-1.5 text-[11px] text-foreground"
      >
        {#if loadStats}
          <span>
            {loadStats.placementsRendered}/{loadStats.placementsRequested} placements · {loadStats.attachmentsRendered}
            attached
            {#if loadStats.attachmentsFilteredByMisc > 0}
              ({loadStats.attachmentsFilteredByMisc} miscFilter-dropped)
            {/if}
            · {loadStats.hullMeshCount} hull meshes
            {#if loadStats.unresolvedAssets.size > 0}
              · <span class="text-warning">
                {loadStats.unresolvedAssets.size} unresolved
              </span>
            {/if}
          </span>
        {:else}
          <span class="text-muted-foreground">Loading…</span>
        {/if}
      </div>
    {/if}
  </section>

  {#if activeShip && viewer}
    <ShipControls {viewer} hullGroups={viewer.getHullGroups()} revision={controlsRevision} />
  {/if}
</div>
