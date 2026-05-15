<script lang="ts">
  // Tabbed read-only inspector that lives under the ShipViewer.
  //
  // Tabs:
  //   - Overview    : display_name + nation/tier + load timing + section counts
  //   - Placements  : per-section rendered/requested totals + miscFilter drops
  //   - Unresolved  : asset_ids referenced by the ship that the library couldn't resolve
  //   - Hull        : per-hull-group mesh + triangle counts
  //   - Skins       : full list with click-to-activate (hidden when ≤ 1 skin)
  //   - Damage      : seam state matrix snapshot
  //   - Pick        : currently-picked mesh details (hidden when nothing picked)
  //
  // Resize/collapse mechanics mirror DetailBottomPanel exactly: drag the
  // handle at the top to grow/shrink, persisted to localStorage; below
  // ~36px snaps to a slim tab-strip. Tab choice is persisted; auto-
  // switching to Unresolved (via header pill) or Pick (when user clicks a
  // mesh) does NOT overwrite the persisted choice.
  //
  // External control: parent calls `selectTab(tab)` via the `bindHandle`
  // callback to trigger a tab switch from outside (e.g. the header's
  // "N unresolved" pill).

  import { onMount, untrack } from 'svelte';
  import ExternalLink from '@lucide/svelte/icons/external-link';
  import { Button } from '$lib/components/ui/button';
  import { navigate } from '$lib/router';
  import { tabBtnBase } from '$lib/ui/controls';
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type {
    LibraryIndex,
    SeamKey,
    SeamState,
    ShipSectionKey,
    ShipSummary,
    Skin,
  } from '$lib/types';
  import type { PickResult, ShipLoadStats, ShipViewer } from '$lib/ship';

  export type ShipBottomTab =
    | 'overview'
    | 'placements'
    | 'unresolved'
    | 'hull'
    | 'skins'
    | 'damage'
    | 'pick';

  export interface ShipBottomPanelHandle {
    selectTab: (tab: ShipBottomTab) => void;
  }

  interface Props {
    ship: ShipSummary;
    viewer: ShipViewer | null;
    loadStats: ShipLoadStats | null;
    library: LibraryIndex | null;
    /** Bumped by the parent whenever viewer-side mirror state may have
     *  changed (ship load, skin pick, etc.) — drives a re-read of the
     *  hull stats so the Hull tab reflects the current ship. */
    revision: number;
    skins: readonly Skin[];
    activeSkin: string | null;
    /** Async skin-activate handler (lives in the parent so the side
     *  panel + bottom panel share one toast id). */
    onPickSkin: (skinId: string) => void;
    /** Snapshot of seam states. Updated on `revision` bump. */
    seamStates: Readonly<Record<SeamKey, SeamState>>;
    /** Currently-picked mesh (null = nothing picked). Drives the Pick
     *  tab's content + auto-switching. */
    selectedPick: PickResult | null;
    /** Clear the pick selection (called by the Pick tab's close button
     *  + Esc key handler in the parent). */
    onClosePick: () => void;
    /** Expose a `selectTab` method so the parent (and the header's
     *  "N unresolved" pill) can drive the active tab from outside. */
    bindHandle?: (h: ShipBottomPanelHandle) => void;
  }

  const {
    ship,
    viewer,
    loadStats,
    library,
    revision,
    skins,
    activeSkin,
    onPickSkin,
    seamStates,
    selectedPick,
    onClosePick,
    bindHandle,
  }: Props = $props();

  const HEIGHT_KEY = 'wows-webview.ship-bottom-panel.height';
  const TAB_KEY = 'wows-webview.ship-bottom-panel.tab';
  const DEFAULT_HEIGHT = 240;
  const COLLAPSED_HEIGHT = 36;
  const COLLAPSE_THRESHOLD = 60;
  const MIN_EXPANDED = 120;
  const MAX_HEIGHT_FRAC = 0.7;

  let height = $state<number>(DEFAULT_HEIGHT);
  let activeTab = $state<ShipBottomTab>('overview');
  let dragging = $state(false);

  // Persistable tabs — the auto-switched 'pick' tab doesn't get written
  // to localStorage so closing the inspector falls back to the user's
  // last *deliberate* choice.
  const PERSISTABLE: ShipBottomTab[] = [
    'overview',
    'placements',
    'unresolved',
    'hull',
    'skins',
    'damage',
  ];

  onMount(() => {
    try {
      const stored = localStorage.getItem(HEIGHT_KEY);
      if (stored !== null) {
        const n = Number(stored);
        if (Number.isFinite(n) && n >= COLLAPSED_HEIGHT) height = n;
      }
      const t = localStorage.getItem(TAB_KEY) as ShipBottomTab | null;
      if (t && PERSISTABLE.includes(t)) activeTab = t;
    } catch {
      /* localStorage may be unavailable */
    }
    bindHandle?.({ selectTab });
  });

  function selectTab(t: ShipBottomTab) {
    activeTab = t;
  }

  // Auto-switch to Pick when something is picked. Restore the
  // persisted tab when the pick is cleared (mirrors DetailBottomPanel's
  // rig-editor auto-switch behaviour).
  let prevSelectedPick: PickResult | null = null;
  $effect(() => {
    const pick = selectedPick;
    untrack(() => {
      if (pick && !prevSelectedPick) {
        activeTab = 'pick';
      } else if (!pick && prevSelectedPick && activeTab === 'pick') {
        let t: string | null = null;
        try {
          t = localStorage.getItem(TAB_KEY);
        } catch {
          t = null;
        }
        activeTab =
          t && PERSISTABLE.includes(t as ShipBottomTab) ? (t as ShipBottomTab) : 'overview';
      }
      prevSelectedPick = pick;
    });
  });

  function setTab(t: ShipBottomTab) {
    activeTab = t;
    if (PERSISTABLE.includes(t)) {
      try {
        localStorage.setItem(TAB_KEY, t);
      } catch {
        /* ignore */
      }
    }
  }

  function persistHeight(h: number) {
    try {
      localStorage.setItem(HEIGHT_KEY, String(h));
    } catch {
      /* ignore */
    }
  }

  let dragStartY = 0;
  let dragStartHeight = 0;

  function onPointerDown(ev: PointerEvent) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    dragging = true;
    dragStartY = ev.clientY;
    dragStartHeight = height;
    (ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId);
  }

  function onPointerMove(ev: PointerEvent) {
    if (!dragging) return;
    const dy = ev.clientY - dragStartY;
    let next = dragStartHeight - dy;
    const max = Math.floor(window.innerHeight * MAX_HEIGHT_FRAC);
    if (next > max) next = max;
    if (next < COLLAPSE_THRESHOLD) {
      next = COLLAPSED_HEIGHT;
    } else if (next < MIN_EXPANDED) {
      next = MIN_EXPANDED;
    }
    height = next;
  }

  function onPointerUp(ev: PointerEvent) {
    if (!dragging) return;
    dragging = false;
    try {
      (ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId);
    } catch {
      /* ignore */
    }
    persistHeight(height);
  }

  function toggleCollapsed() {
    if (height <= COLLAPSED_HEIGHT + 4) {
      height = DEFAULT_HEIGHT;
    } else {
      height = COLLAPSED_HEIGHT;
    }
    persistHeight(height);
  }

  const collapsed = $derived(height <= COLLAPSED_HEIGHT + 4);

  // Hull stats are pulled from the viewer on every `revision` bump
  // (ship swap, skin change, etc.). Cheap: walks the hull tree once,
  // counts position-attribute / index buffer per Mesh.
  const hullGroupStats = $derived.by(() => {
    void revision;
    if (!viewer) return [];
    return viewer.getHullGroupStats();
  });

  // Unresolved asset list — sorted by descending count so the most-
  // referenced missing ids surface first. Each entry is `[asset_id, count]`.
  const unresolvedEntries = $derived.by(() => {
    if (!loadStats) return [];
    return Array.from(loadStats.unresolvedAssets.entries()).sort((a, b) => b[1] - a[1]);
  });

  // Section breakdown — pairs the ship-source `section_counts` (from
  // `<Ship>_accessories.json`) with whatever the viewer reports. The
  // ShipLoadStats doesn't currently break down rendered counts by
  // section, so we just show the source counts + the total rendered /
  // requested at the top. Future expansion: ShipViewer can emit
  // per-section rendered counts.
  const sectionRows = $derived.by(() => {
    return SHIP_SECTIONS.map((k) => ({
      key: k as ShipSectionKey,
      count: ship.section_counts[k],
    }));
  });

  const unresolvedCount = $derived(loadStats?.unresolvedAssets.size ?? 0);

  const tabs: Array<{ id: ShipBottomTab; label: string; hide?: boolean; badge?: number }> =
    $derived([
      { id: 'overview', label: 'Overview' },
      { id: 'placements', label: 'Placements' },
      {
        id: 'unresolved',
        label: 'Unresolved',
        hide: unresolvedCount === 0,
        badge: unresolvedCount,
      },
      { id: 'hull', label: 'Hull' },
      { id: 'skins', label: 'Skins', hide: skins.length <= 1 },
      { id: 'damage', label: 'Damage' },
      { id: 'pick', label: 'Pick', hide: !selectedPick },
    ]);

  // The auto-switched tab may go stale if the underlying data changes
  // (e.g. selectedPick cleared while on Pick). Reconcile here.
  $effect(() => {
    const visible = new Set(tabs.filter((t) => !t.hide).map((t) => t.id));
    if (!visible.has(activeTab)) {
      activeTab = 'overview';
    }
  });

  const pickInfo = $derived(selectedPick?.info ?? null);
  const pickLibEntry = $derived(
    pickInfo && library ? library.assets[pickInfo.asset_id] ?? null : null,
  );

  function openPickInLibrary() {
    if (!pickInfo) return;
    navigate(`#/asset/${encodeURIComponent(pickInfo.asset_id)}`);
  }
</script>

<section class="bg-card border-border flex flex-none flex-col border-t" style="height: {height}px">
  <div
    role="separator"
    aria-orientation="horizontal"
    aria-label="Resize inspector"
    class="h-1.5 cursor-row-resize bg-border/40 hover:bg-border flex-none"
    class:bg-primary={dragging}
    class:hover:bg-primary={dragging}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    ondblclick={toggleCollapsed}
  ></div>

  <div class="flex flex-none items-center justify-between border-border border-b">
    <div role="tablist" class="flex">
      {#each tabs as t (t.id)}
        {#if !t.hide}
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === t.id}
            onclick={() => setTab(t.id)}
            class="{tabBtnBase} {activeTab === t.id
              ? 'border-primary text-foreground'
              : 'border-transparent text-muted-foreground hover:text-foreground'}"
          >
            {t.label}
            {#if t.badge != null && t.badge > 0}
              <span class="ml-1 rounded bg-amber-950/60 px-1 py-[1px] text-[9px] text-amber-300">
                {t.badge}
              </span>
            {/if}
          </button>
        {/if}
      {/each}
    </div>
    <button
      type="button"
      onclick={toggleCollapsed}
      title={collapsed ? 'Expand inspector' : 'Collapse inspector'}
      class="text-muted-foreground hover:text-foreground px-3 py-1 text-[11px]"
    >
      {collapsed ? '▲' : '▼'}
    </button>
  </div>

  {#if !collapsed}
    <div class="flex-1 min-h-0 overflow-y-auto px-5 py-3 text-xs">
      {#if activeTab === 'overview'}
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-words"
        >
          <dt>display name</dt>
          <dd>{ship.display_name}</dd>
          <dt>internal name</dt>
          <dd><code class="font-mono text-[11px]">{ship.name}</code></dd>
          {#if ship.nation}
            <dt>nation</dt>
            <dd>{ship.nation}</dd>
          {/if}
          {#if ship.ship_class}
            <dt>class</dt>
            <dd>{ship.ship_class}</dd>
          {/if}
          {#if ship.tier != null}
            <dt>tier</dt>
            <dd>{ship.tier}</dd>
          {/if}
          <dt>hull glb</dt>
          <dd><code class="font-mono text-[11px]">{ship.hull_glb}</code></dd>
          {#if loadStats}
            <dt>load time</dt>
            <dd class="tabular-nums">{(loadStats.loadMs / 1000).toFixed(2)}s</dd>
            <dt>hull meshes</dt>
            <dd class="tabular-nums">{loadStats.hullMeshCount}</dd>
            <dt>placements</dt>
            <dd class="tabular-nums">
              {loadStats.placementsRendered} / {loadStats.placementsRequested} rendered
            </dd>
            <dt>attached children</dt>
            <dd class="tabular-nums">
              {loadStats.attachmentsRendered} rendered{#if loadStats.attachmentsFilteredByMisc > 0},
                {loadStats.attachmentsFilteredByMisc} dropped by miscFilter
              {/if}
            </dd>
            <dt>skins</dt>
            <dd class="tabular-nums">{loadStats.skinCount}</dd>
          {/if}
        </dl>
      {:else if activeTab === 'placements'}
        <div class="flex flex-col gap-3">
          {#if loadStats}
            <div class="text-muted-foreground text-[11px]">
              Totals: <span class="text-foreground tabular-nums"
                >{loadStats.placementsRendered} / {loadStats.placementsRequested}</span
              >
              placements rendered ·
              <span class="text-foreground tabular-nums">{loadStats.attachmentsRendered}</span>
              attached children
              {#if loadStats.attachmentsFilteredByMisc > 0}
                · <span class="text-amber-300 tabular-nums"
                  >{loadStats.attachmentsFilteredByMisc}</span
                >
                miscFilter-dropped
              {/if}
            </div>
          {/if}
          <table
            class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">section</th>
                <th class="text-right">placements</th>
              </tr>
            </thead>
            <tbody>
              {#each sectionRows as row (row.key)}
                <tr>
                  <td class="text-muted-foreground">{row.key}</td>
                  <td class="text-right">{row.count}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="text-muted-foreground text-[10px] leading-tight max-w-[60ch]">
            Counts come from the ship's source <code class="font-mono text-[10px]"
              >accessories.json</code
            > — the per-section breakdown of rendered placements isn't currently surfaced through ShipLoadStats.
          </div>
        </div>
      {:else if activeTab === 'unresolved'}
        {#if unresolvedEntries.length === 0}
          <div class="text-muted-foreground">
            All asset_ids referenced by this ship resolved to library entries.
          </div>
        {:else}
          <div class="flex flex-col gap-2">
            <div class="text-muted-foreground text-[11px]">
              {unresolvedEntries.length} asset_id(s) referenced by this ship had no matching entry
              in the accessory library. Most often this means the asset wasn't extracted yet — re-run
              <code class="font-mono text-[11px]">wows-build-accessory-library</code> after extracting
              the missing source.
            </div>
            <table
              class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
            >
              <thead>
                <tr>
                  <th class="text-left">asset_id</th>
                  <th class="text-right">placements</th>
                </tr>
              </thead>
              <tbody>
                {#each unresolvedEntries as [id, count] (id)}
                  <tr>
                    <td>
                      <button
                        type="button"
                        class="font-mono text-[11px] hover:underline"
                        title="Copy asset_id to clipboard"
                        onclick={() => navigator.clipboard?.writeText(id)}
                      >
                        {id}
                      </button>
                    </td>
                    <td class="text-right">{count}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {:else if activeTab === 'hull'}
        {#if hullGroupStats.length === 0}
          <div class="text-muted-foreground">No hull groups classified.</div>
        {:else}
          <table
            class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">group</th>
                <th class="text-right">meshes</th>
                <th class="text-right">triangles</th>
              </tr>
            </thead>
            <tbody>
              {#each hullGroupStats as g (g.name)}
                <tr>
                  <td class="text-muted-foreground">{g.name}</td>
                  <td class="text-right">{g.meshes}</td>
                  <td class="text-right">{g.triangles.toLocaleString()}</td>
                </tr>
              {/each}
            </tbody>
            <tfoot>
              <tr class="border-border [&_td]:border-t [&_td]:text-muted-foreground">
                <td>total</td>
                <td class="text-right">
                  {hullGroupStats.reduce((a, g) => a + g.meshes, 0)}
                </td>
                <td class="text-right">
                  {hullGroupStats.reduce((a, g) => a + g.triangles, 0).toLocaleString()}
                </td>
              </tr>
            </tfoot>
          </table>
        {/if}
      {:else if activeTab === 'skins'}
        {#if skins.length === 0}
          <div class="text-muted-foreground">No skins available.</div>
        {:else}
          <div class="flex flex-col gap-1">
            {#each skins as skin (skin.skin_id)}
              <button
                type="button"
                onclick={() => onPickSkin(skin.skin_id)}
                class="flex items-center gap-2 rounded border px-2 py-1 text-left text-[11px] {activeSkin ===
                skin.skin_id
                  ? 'border-primary bg-primary/10'
                  : 'border-border bg-popover hover:bg-accent'}"
                title={skin.skin_id}
              >
                <span
                  class="inline-flex size-3 flex-none items-center justify-center rounded-full border {activeSkin ===
                  skin.skin_id
                    ? 'border-primary bg-primary'
                    : 'border-border'}"
                ></span>
                <span class="min-w-0 flex-1">
                  <span class="block truncate font-medium text-foreground">
                    {skin.display_name || skin.skin_id}
                  </span>
                  <span class="text-muted-foreground block truncate font-mono text-[10px]">
                    {skin.skin_id}
                  </span>
                </span>
                <span class="text-muted-foreground flex-none text-[10px] uppercase tracking-wider">
                  {skin.scheme_key}
                </span>
              </button>
            {/each}
          </div>
        {/if}
      {:else if activeTab === 'damage'}
        <div class="flex flex-col gap-2">
          <div class="text-muted-foreground text-[11px] max-w-[60ch]">
            Per-seam damage state. Toggling a seam in the side panel cascades hull patches +
            cracks via <code class="font-mono text-[11px]">damage_cascade</code>; this tab
            shows the current snapshot.
          </div>
          <table
            class="w-fit text-[11px] [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">seam</th>
                <th class="text-left">state</th>
              </tr>
            </thead>
            <tbody>
              {#each SEAMS as seam (seam)}
                <tr>
                  <td class="text-muted-foreground">{seam}</td>
                  <td
                    class:text-emerald-400={seamStates[seam] === 'Intact'}
                    class:text-rose-400={seamStates[seam] === 'Broken'}
                  >
                    {seamStates[seam]}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else if activeTab === 'pick'}
        {#if pickInfo}
          <div class="flex flex-col gap-2">
            <header class="border-border flex items-center gap-2 border-b pb-1.5">
              <button
                type="button"
                onclick={openPickInLibrary}
                title="Open this asset in the Library"
                class="text-primary inline-flex min-w-0 flex-1 items-center gap-1.5 bg-transparent p-0 text-left font-mono text-xs font-medium hover:underline"
              >
                <span class="overflow-hidden text-ellipsis whitespace-nowrap">
                  {pickInfo.asset_id}
                </span>
                <ExternalLink class="size-3 shrink-0" />
              </button>
              <Button
                variant="ghost"
                size="icon-xs"
                onclick={onClosePick}
                aria-label="Clear selection"
                class="size-[18px]"
              >
                ×
              </Button>
            </header>
            <dl
              class="m-0 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1 [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:overflow-hidden [&_dd]:text-ellipsis [&_dd]:whitespace-nowrap"
            >
              {#if pickInfo.section}
                <dt>section</dt>
                <dd>{pickInfo.section}</dd>
              {/if}
              {#if pickInfo.parent_section}
                <dt>hull anchor</dt>
                <dd>{pickInfo.parent_section}</dd>
              {/if}
              {#if pickInfo.parent_mesh}
                <dt>parent mesh</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.parent_mesh}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.instance_id}
                <dt>instance</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.instance_id}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.attached_to_instance_id}
                <dt>attached to</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.attached_to_instance_id}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.attached_placement_id}
                <dt>placement</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.attached_placement_id}
                  </code>
                </dd>
              {/if}
              {#if pickLibEntry}
                <dt>scope</dt>
                <dd>
                  {pickLibEntry.scope}/{pickLibEntry.category}{pickLibEntry.subcategory
                    ? `/${pickLibEntry.subcategory}`
                    : ''}
                </dd>
                <dt>used by</dt>
                <dd>
                  {pickLibEntry.used_by_ships.length} ship{pickLibEntry.used_by_ships.length === 1
                    ? ''
                    : 's'}
                </dd>
              {:else}
                <dt>library</dt>
                <dd class="text-amber-300">unresolved</dd>
              {/if}
            </dl>
          </div>
        {:else}
          <div class="text-muted-foreground">
            Click a mesh in the viewer to inspect it. Press <kbd>Esc</kbd> to clear.
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</section>
