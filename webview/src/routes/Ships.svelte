<script lang="ts">
  // Ships route: loads /api/ships + /api/library on mount, hosts the
  // ship picker + header + viewer + side controls + bottom inspector.
  // The URL hash drives ship selection so the back button + bookmarks
  // work (`#/ship/<name>` → opens that ship).
  //
  // Async load lifecycle is reported via a single sticky svelte-sonner
  // toast per ship that promotes to success / warning / error on
  // completion; the bottom inspector's Overview tab keeps the final-
  // state summary so the user can scan it without a toast hover.
  //
  // State ownership: the header pill + bottom panel's Skin tab need to
  // mirror state that used to live in ShipControls (showTextures,
  // activeSkin, skins, seamStates). Those mirrors live here now, with
  // ShipControls calling back via `onShowTexturesChange` /
  // `onSeamStatesChange` callbacks. `pickSkin` (with toast plumbing)
  // also lives here so the bottom panel's Skins tab can drive it.

  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { navigate } from '$lib/router';
  import { fetchLibrary, fetchShips, invalidateLibrary, invalidateShips } from '$lib/api';
  import { extractEvents } from '$lib/extract_events.svelte';
  import { navState } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import type {
    ExteriorRecord,
    LibraryIndex,
    SeamKey,
    SeamState,
    ShipSummary,
    Skin,
  } from '$lib/types';
  import type { PickResult, ShipLoadStats, ShipViewer, WgEnvironmentInfo } from '$lib/ship';
  import ShipPicker from '$components/ShipPicker.svelte';
  import ShipViewerCmp from '$components/ShipViewer.svelte';
  import ShipControls from '$components/ShipControls.svelte';
  import ShipHeaderBar from '$components/ShipHeaderBar.svelte';
  import ShipBottomPanel, {
    type ShipBottomPanelHandle,
  } from '$components/ShipBottomPanel.svelte';

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
  // Bumped each time the ship reloads OR a major viewer state change
  // (skin pick) so child surfaces can re-read state. Header pill and
  // bottom panel both watch this.
  let controlsRevision = $state(0);
  // Active sticky loading toast id; promoted to success/warning/error on
  // completion so each ship swap reuses one slot instead of stacking.
  let shipLoadToastId: string | number | null = null;
  // Toast id for the async skin-activate op (kept here so the side
  // panel + bottom panel share one slot).
  let skinToastId: string | number | null = null;
  // Picker binding for the `/` shortcut. Component instance exports
  // (svelte 5 supports the v4 `export function` pattern via bind:this).
  let pickerRef: ShipPicker | null = $state(null);
  // Bottom-panel handle. Used by the header's "N unresolved" pill to
  // jump the panel to the Unresolved tab.
  let bottomPanelHandle: ShipBottomPanelHandle | null = null;
  // Latest mesh-inspector pick. Cleared by clicking empty space, the
  // bottom-panel Pick tab's close button, or pressing ESC.
  let selectedPick = $state<PickResult | null>(null);

  // Mirrors of viewer-owned state that the header pill + bottom-panel
  // tabs need to read. ShipControls owns the side-panel toggles and
  // calls back here so these mirrors stay in sync.
  let showTextures = $state(false);
  let skins = $state<readonly Skin[]>([]);
  let activeSkin = $state<string | null>(null);
  // Mesh-swap permoflage selector (ship-exterior unification). Mirrors of
  // viewer.getExteriors() / getActiveExteriorId(); the bottom panel's
  // Exteriors tab drives pickExterior the same way Skins drives pickSkin.
  let exteriors = $state<readonly ExteriorRecord[]>([]);
  let activeExteriorId = $state<string | null>(null);
  let envInfo = $state<WgEnvironmentInfo | null>(null);
  let seamStates = $state<Readonly<Record<SeamKey, SeamState>>>({
    'Bow-MidFront': 'Intact',
    'MidFront-MidBack': 'Intact',
    'MidBack-Stern': 'Intact',
  });
  // Cached per-ship hull group names. `viewer.getHullGroups()` allocates
  // a new array on every call, so reading it inline at the template
  // makes the prop unstable — ShipControls' $effect would loop because
  // tracked-prop changes on every parent re-render. Refresh in
  // onViewerLoaded so it stays stable between ship swaps.
  let hullGroups = $state<readonly string[]>([]);
  // Same rationale for the per-ship LOD level list. The ShipControls
  // dropdown populates from this; ships with only level 0 + level 1
  // get a 3-option dropdown, ships with deeper LOD chains get more.
  let lodLevels = $state<readonly number[]>([0]);

  function handlePick(pick: PickResult | null, _clientX: number, _clientY: number) {
    selectedPick = pick;
  }

  async function pickSkin(skinId: string) {
    if (!viewer) return;
    activeSkin = skinId;
    skinToastId = toast.loading(`Activating ${skinId}…`, {
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      await viewer.setActiveSkin(skinId, (msg) => {
        if (skinToastId !== null) {
          toast.loading(msg, { id: skinToastId, duration: Number.POSITIVE_INFINITY });
        }
      });
      if (skinToastId !== null) {
        toast.success(`Skin: ${skinId}`, { id: skinToastId, duration: 2000 });
        skinToastId = null;
      }
      // Bump so the bottom-panel Hull tab re-reads geometry stats
      // (texture/material swap may have re-bound mesh groups).
      controlsRevision++;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (skinToastId !== null) {
        toast.error(`Failed to apply ${skinId}`, {
          id: skinToastId,
          description: msg,
          duration: 8000,
        });
        skinToastId = null;
      } else {
        toast.error(`Failed to apply ${skinId}`, { description: msg, duration: 8000 });
      }
    }
  }

  async function pickExterior(exteriorId: string) {
    if (!viewer) return;
    const toastId = toast.loading(`Switching exterior to ${exteriorId}…`, {
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      await viewer.setActiveExterior(exteriorId, (msg) => {
        toast.loading(msg, { id: toastId, duration: Number.POSITIVE_INFINITY });
      });
      // The switch may also flip the active skin (camo_scheme_key) — re-read
      // both mirrors from the viewer rather than assuming.
      activeExteriorId = viewer.getActiveExteriorId();
      activeSkin = viewer.getActiveSkinId();
      selectedPick = null; // picked mesh may have been torn down
      toast.success(`Exterior: ${exteriorId}`, { id: toastId, duration: 2000 });
      controlsRevision++;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Failed to switch exterior to ${exteriorId}`, {
        id: toastId,
        description: msg,
        duration: 8000,
      });
    }
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

  // Smart-merge so unchanged ShipSummary entries keep their object
  // identity across refreshes. `activeShip` is `ships.find(s => s.name
  // === selectedShipName)` — retaining the same object reference for
  // the currently viewed ship prevents ShipViewer's ship-prop $effect
  // from reloading the GLB and tossing camera state.
  function mergeShips(prev: ShipSummary[], next: ShipSummary[]): ShipSummary[] {
    const byName = new Map(prev.map((s) => [s.name, s]));
    return next.map((n) => {
      const old = byName.get(n.name);
      if (old && JSON.stringify(old) === JSON.stringify(n)) return old;
      return n;
    });
  }

  // Re-fetch on extract/skin-pack completion. The Extract page bumps
  // `extractEvents.completionRevision` when a job transitions to
  // `done`; we read it inside a tracked block so this effect fires on
  // each bump. Skip the initial value so we don't double-fetch on
  // mount (onMount already loaded both endpoints).
  let lastSeenRevision = extractEvents.completionRevision;
  $effect(() => {
    const rev = extractEvents.completionRevision;
    if (rev === lastSeenRevision) return;
    lastSeenRevision = rev;
    void (async () => {
      try {
        invalidateShips();
        invalidateLibrary();
        const [shipsRes, libRes] = await Promise.all([fetchShips(), fetchLibrary()]);
        ships = mergeShips(ships, shipsRes);
        library = libRes;
      } catch (err) {
        console.warn('[ships] refresh after extract failed:', err);
      }
    })();
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
    envInfo = null;
    navigate(`#/ship/${encodeURIComponent(ship.name)}`);
  }

  function refreshEnvInfo() {
    envInfo = viewer?.getWgEnvironment() ?? null;
  }

  function refreshEnvInfoSoon() {
    refreshEnvInfo();
    const expectedViewer = viewer;
    window.setTimeout(() => {
      if (viewer === expectedViewer) refreshEnvInfo();
    }, 250);
    window.setTimeout(() => {
      if (viewer === expectedViewer) refreshEnvInfo();
    }, 1000);
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
    // Read mirrors from the freshly-loaded viewer. showTextures stays
    // off per ship by design (DDS decoding is expensive); the side
    // panel + header pill pick it up on toggle via onShowTexturesChange.
    if (viewer) {
      showTextures = viewer.isShowingTextures();
      skins = viewer.getSkins();
      activeSkin = viewer.getActiveSkinId();
      // loadShip may have auto-selected the native exterior (ARP-style
      // ships) — read back rather than assuming 'default'.
      exteriors = viewer.getExteriors();
      activeExteriorId = viewer.getActiveExteriorId();
      seamStates = { ...viewer.getSeamStates() };
      hullGroups = viewer.getHullGroups();
      lodLevels = viewer.getAvailableLodLevels();
      refreshEnvInfoSoon();
    }
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

  <section class="relative flex flex-1 min-w-0 flex-col">
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
      <ShipHeaderBar
        ship={activeShip}
        {viewer}
        {loadStats}
        {showTextures}
        {skins}
        {activeSkin}
        onShowUnresolved={() => bottomPanelHandle?.selectTab('unresolved')}
      />
      <div class="relative flex flex-1 min-h-0 overflow-hidden">
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
      </div>
      <ShipBottomPanel
        ship={activeShip}
        {viewer}
        {loadStats}
        {library}
        revision={controlsRevision}
        {skins}
        {activeSkin}
        {envInfo}
        onPickSkin={pickSkin}
        {exteriors}
        {activeExteriorId}
        onPickExterior={pickExterior}
        {seamStates}
        {selectedPick}
        onClosePick={() => (selectedPick = null)}
        bindHandle={(h) => {
          bottomPanelHandle = h;
        }}
      />
    {/if}
  </section>

  {#if activeShip && viewer}
    <ShipControls
      {viewer}
      {hullGroups}
      {lodLevels}
      revision={controlsRevision}
      onShowTexturesChange={(v) => (showTextures = v)}
      onEnvironmentChange={(info) => (envInfo = info)}
      onSeamStatesChange={(s) => (seamStates = { ...s })}
    />
  {/if}
</div>
