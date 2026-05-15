<script lang="ts">
  // GameParams browser route — drill into the raw WG GameParams JSON
  // the pipeline reads when generating ship sidecars. Two-pane layout:
  //
  //   Left  — type filter + search box + paginated list of summary
  //           rows. Click a row to load the full record.
  //   Right — collapsible JSON tree of the selected entity.
  //
  // Hash routing: `#/gameparams` shows the picker; `#/gameparams/<id>`
  // opens that entity. The router parses `<id>` into the typed
  // `entityId` prop (see lib/router.ts).
  //
  // First load is slow (~10 s while the backend parses the ~3 GB
  // gameparams.json); subsequent calls hit the per-process Python
  // cache and feel instant.

  import { toast } from 'svelte-sonner';
  import { navigate } from '$lib/router';
  import { navState } from '$lib/nav_state.svelte';
  import {
    fetchGameParamTypes,
    fetchGameParamList,
    fetchGameParamEntity,
  } from '$lib/api';
  import { labelCls, inputBoxCls } from '$lib/ui/controls';
  import type {
    GameParamEntity,
    GameParamListResult,
    GameParamTypeHistogram,
  } from '$lib/types';
  import JsonTree from '$components/JsonTree.svelte';

  interface Props {
    /** Entity ID from `#/gameparams/<id>`, or null when on bare
     *  `#/gameparams`. */
    entityId: string | null;
    active: boolean;
  }
  const { entityId, active }: Props = $props();

  // Filter state.
  let typeHistogram = $state<GameParamTypeHistogram | null>(null);
  let selectedType = $state<string>('Ship');
  let query = $state('');
  let listResult = $state<GameParamListResult | null>(null);
  let listError = $state<string | null>(null);
  let listLoading = $state(false);
  const PAGE_SIZE = 200;
  let offset = $state(0);

  // Selection state. Sticky like the other routes — when the user
  // navigates away the URL strips the id but the internal selection
  // survives so coming back via the topnav restores what was open.
  let selectedEntityId = $state<string | null>(null);
  $effect(() => {
    if (entityId) selectedEntityId = decodeURIComponent(entityId);
  });
  $effect(() => {
    if (selectedEntityId) navState.lastGameParamId = selectedEntityId;
  });

  let entityData = $state<GameParamEntity | null>(null);
  let entityError = $state<string | null>(null);
  let entityLoading = $state(false);

  // Debounced search — typing in the search box shouldn't fire a
  // backend call on every keystroke.
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  function onSearchInput(v: string) {
    query = v;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      offset = 0;
      void refreshList();
    }, 250);
  }

  async function refreshList(): Promise<void> {
    listLoading = true;
    listError = null;
    try {
      listResult = await fetchGameParamList({
        type: selectedType || null,
        q: query || null,
        limit: PAGE_SIZE,
        offset,
      });
    } catch (err) {
      listError = err instanceof Error ? err.message : String(err);
      listResult = null;
    } finally {
      listLoading = false;
    }
  }

  async function loadEntity(id: string): Promise<void> {
    entityLoading = true;
    entityError = null;
    try {
      entityData = await fetchGameParamEntity(id);
    } catch (err) {
      entityError = err instanceof Error ? err.message : String(err);
      entityData = null;
    } finally {
      entityLoading = false;
    }
  }

  $effect(() => {
    const id = selectedEntityId;
    if (id && id !== entityData?.id) {
      void loadEntity(id);
    } else if (!id) {
      entityData = null;
      entityError = null;
    }
  });

  function pickEntity(id: string) {
    navigate(`#/gameparams/${encodeURIComponent(id)}`);
  }

  function setType(t: string) {
    selectedType = t;
    offset = 0;
    void refreshList();
  }

  function gotoPage(nextOffset: number) {
    offset = Math.max(0, nextOffset);
    void refreshList();
  }

  // Deferred initial load: the backend's `/types` endpoint triggers a
  // ~10 s synchronous parse of the multi-GB gameparams.json that holds
  // the Python GIL the whole time, starving every other API request.
  // App.svelte mounts every route at startup (display:none for inactive
  // ones), so firing the fetch in onMount would block the *actually
  // visible* page on first app load. Wait until the user navigates here.
  let initialLoadStarted = false;
  let initialLoadInFlight = $state(false);
  let initialLoadElapsedSec = $state(0);
  $effect(() => {
    if (!active || initialLoadStarted) return;
    initialLoadStarted = true;
    void runInitialLoad();
  });

  async function runInitialLoad(): Promise<void> {
    initialLoadInFlight = true;
    initialLoadElapsedSec = 0;
    const start = performance.now();
    const tick = window.setInterval(() => {
      initialLoadElapsedSec = Math.floor((performance.now() - start) / 1000);
    }, 250);
    // Sticky toast (visible from any page) so users who navigate away
    // mid-load understand why their next action might stall — the GIL
    // hold during gameparams.json parse blocks every API request.
    // `toast.promise` handles the loading→success/error lifecycle in
    // one call, which renders more reliably than the separate
    // `toast.loading` + `toast.success` (id-handoff) pattern.
    const gpPromise = fetchGameParamTypes();
    toast.promise(gpPromise, {
      loading:
        'Loading GameParams cache (~10 s) — other API requests will stall',
      success: (data) =>
        `GameParams ready: ${data.total.toLocaleString()} entities`,
      error: (err: unknown) =>
        `Failed to load GameParams: ${err instanceof Error ? err.message : String(err)}`,
      duration: 4000,
    });
    try {
      typeHistogram = await gpPromise;
      // Pick a sensible default type — Ship is what users look at most
      // often; fall back to the first key when not present.
      if (typeHistogram && !typeHistogram.counts[selectedType]) {
        const first = Object.keys(typeHistogram.counts).sort()[0];
        if (first) selectedType = first;
      }
    } catch (err) {
      listError = err instanceof Error ? err.message : String(err);
    } finally {
      window.clearInterval(tick);
      initialLoadInFlight = false;
    }
    await refreshList();
  }

  // Type histogram sorted by count desc for the dropdown.
  const typeOptions = $derived.by(() => {
    if (!typeHistogram) return [];
    return Object.entries(typeHistogram.counts).sort((a, b) => b[1] - a[1]);
  });

  const totalPages = $derived.by(() => {
    if (!listResult) return 0;
    return Math.ceil(listResult.total / PAGE_SIZE);
  });
  const currentPage = $derived(Math.floor(offset / PAGE_SIZE));
</script>

<div class="flex flex-1 min-w-0 h-full">
  <!--
    Left pane: filter controls + paginated list. Matches the
    ShipPicker / AssetList vocabulary so the three browse pages read
    consistently.
  -->
  <aside class="bg-card border-border flex w-[360px] flex-none flex-col border-r min-h-0">
    <header class="border-border border-b px-3.5 py-3">
      <h1 class="m-0 text-sm font-semibold">GameParams</h1>
      {#if typeHistogram}
        <div class="text-muted-foreground mt-1 text-[11px] tabular-nums">
          {listResult?.total ?? '?'} / {typeHistogram.total} entities
        </div>
      {:else if initialLoadInFlight}
        <div class="text-muted-foreground mt-1 flex items-center gap-1.5 text-[11px] tabular-nums">
          <span
            class="inline-block size-2 animate-pulse rounded-full bg-amber-500"
            aria-hidden="true"
          ></span>
          Loading dump… {initialLoadElapsedSec}s
        </div>
      {:else}
        <div class="text-muted-foreground mt-1 text-[11px]">Not loaded.</div>
      {/if}
    </header>

    <div class="border-border flex flex-none flex-col gap-2 border-b px-3.5 py-2.5">
      <label class={labelCls}>
        type
        <select
          value={selectedType}
          onchange={(e) => setType(e.currentTarget.value)}
          class={inputBoxCls}
        >
          {#each typeOptions as [t, n] (t)}
            <option value={t}>{t} ({n})</option>
          {/each}
        </select>
      </label>
      <input
        type="search"
        placeholder="search id / name…"
        value={query}
        oninput={(e) => onSearchInput(e.currentTarget.value)}
        class="bg-popover text-foreground border-border placeholder:text-muted-foreground focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
      />
    </div>

    <div class="flex flex-1 min-h-0 flex-col overflow-y-auto">
      {#if listError}
        <div class="text-destructive p-3 text-xs">
          {listError}
        </div>
      {:else if initialLoadInFlight && !typeHistogram}
        <!--
          One-time initial cache load. This is the dominant in-page
          signal that something is happening AND that other API calls
          will queue behind us — the toast carries the same warning
          for users who navigate away mid-load.
        -->
        <div class="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
          <span
            class="inline-block size-3 animate-pulse rounded-full bg-amber-500"
            aria-hidden="true"
          ></span>
          <div class="text-foreground text-xs font-medium tabular-nums">
            Parsing GameParams… {initialLoadElapsedSec}s
          </div>
          <div class="text-muted-foreground max-w-[260px] text-[11px] leading-snug">
            One-time per-process cache (~10 s). Other API requests
            (ship loads, library refresh, etc.) will stall until this
            finishes.
          </div>
        </div>
      {:else if listLoading && !listResult}
        <div class="text-muted-foreground p-3 text-xs">Loading…</div>
      {:else if listResult && listResult.items.length === 0}
        <div class="text-muted-foreground p-3 text-xs">No entities match.</div>
      {:else if listResult}
        {#each listResult.items as item (item.id)}
          <button
            type="button"
            onclick={() => pickEntity(item.id)}
            class="border-border flex flex-col gap-0.5 border-b px-3 py-1.5 text-left text-[11px] hover:bg-popover {selectedEntityId ===
            item.id
              ? 'bg-accent'
              : ''}"
          >
            <span class="text-foreground truncate font-mono">{item.id}</span>
            <span class="text-muted-foreground truncate text-[10px]">
              {[item.species, item.nation, item.level != null ? `T${item.level}` : null]
                .filter(Boolean)
                .join(' · ') || '—'}
            </span>
          </button>
        {/each}
      {/if}
    </div>

    {#if listResult && totalPages > 1}
      <div class="border-border flex flex-none items-center justify-between gap-2 border-t px-3 py-1.5 text-[11px] tabular-nums">
        <button
          type="button"
          disabled={currentPage === 0}
          onclick={() => gotoPage(offset - PAGE_SIZE)}
          class="rounded border border-border px-2 py-0.5 hover:bg-popover disabled:opacity-50"
        >
          ←
        </button>
        <span class="text-muted-foreground">page {currentPage + 1} / {totalPages}</span>
        <button
          type="button"
          disabled={currentPage >= totalPages - 1}
          onclick={() => gotoPage(offset + PAGE_SIZE)}
          class="rounded border border-border px-2 py-0.5 hover:bg-popover disabled:opacity-50"
        >
          →
        </button>
      </div>
    {/if}
  </aside>

  <!--
    Right pane: full record JSON for the selected entity. Falls back
    to placeholder text when nothing is selected.
  -->
  <section class="flex flex-1 min-w-0 flex-col overflow-hidden">
    {#if !selectedEntityId}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Select an entity from the list.
      </div>
    {:else if entityError}
      <div class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
        <strong>Failed to load entity:</strong>
        <code class="ml-1.5">{entityError}</code>
      </div>
    {:else if entityLoading || !entityData}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Loading {selectedEntityId}…
      </div>
    {:else}
      <header class="bg-card border-border flex flex-none items-center gap-3 border-b px-5 py-2.5">
        <h2 class="m-0 truncate font-mono text-sm font-semibold">{entityData.id}</h2>
        <button
          type="button"
          onclick={() => navigator.clipboard?.writeText(JSON.stringify(entityData?.entity, null, 2))}
          title="Copy full record JSON to clipboard"
          class="ml-auto rounded border border-border bg-popover px-2 py-1 text-xs hover:bg-accent"
        >
          Copy JSON
        </button>
      </header>
      <div class="flex-1 min-h-0 overflow-auto px-5 py-3">
        <JsonTree value={entityData.entity} defaultOpen />
      </div>
    {/if}
  </section>
</div>
