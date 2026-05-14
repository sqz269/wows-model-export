<script lang="ts">
  // Accessory library route. Fetches /api/library, wires sidebar (filters
  // + list) and right-pane detail (viewer + controls + info).
  //
  // Hash routing: `#/library` shows the picker; `#/asset/<id>` opens
  // that asset. The router parses `<id>` into the typed `assetId` prop.

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import {
    fetchLibrary,
    invalidateLibrary,
    fetchWindingAudit,
    postAutoFlipWinding,
  } from '$lib/api';
  import { extractEvents } from '$lib/extract_events.svelte';
  import { settingsHref } from '$lib/nav_state.svelte';
  import { navState } from '$lib/nav_state.svelte';
  import type {
    LibraryAsset,
    LibraryFilter,
    LibraryIndex,
    WindingAuditEntry,
  } from '$lib/types';
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

  // Winding-audit verdicts keyed by GLB-relative path. Best-effort —
  // missing audit JSON resolves to an empty map and the row badges
  // just don't render. Refresh after a bulk auto-flip so the badges
  // reflect the new on-disk state without a page reload.
  let windingAudit = $state<Map<string, WindingAuditEntry>>(new Map());
  let auditMsg = $state<{ cls: 'ok' | 'fail' | 'working'; text: string } | null>(null);
  let autoFlipPending = $state(false);

  async function refreshWindingAudit(): Promise<void> {
    windingAudit = await fetchWindingAudit();
  }

  /** Count of assets the audit recommends flipping that the user
   *  hasn't already manually decided on. Drives the bulk button label. */
  const flipCandidateCount = $derived.by(() => {
    let n = 0;
    for (const w of windingAudit.values()) {
      if (w.verdict === 'flip' && !w.in_overrides) n += 1;
    }
    return n;
  });

  async function runAutoFlip() {
    const n = flipCandidateCount;
    if (n === 0 || autoFlipPending) return;
    if (
      !confirm(
        `Apply ${n} auto-flip recommendation(s)?\n\n` +
          `Each rewrites the GLB on disk and adds a "source: auto" entry to ` +
          `flip_overrides.json. Reversible per-asset via the F-key.`,
      )
    )
      return;
    autoFlipPending = true;
    auditMsg = { cls: 'working', text: `Auto-flipping ${n}…` };
    try {
      const res = await postAutoFlipWinding();
      if (!res.ok) {
        auditMsg = {
          cls: 'fail',
          text: `Auto-flip failed: ${res.error || res.stderr || 'unknown'}`,
        };
        return;
      }
      await refreshWindingAudit();
      const still = flipCandidateCount;
      const applied = n - still;
      auditMsg = {
        cls: 'ok',
        text:
          still === 0
            ? `Auto-flipped ${applied} asset(s). All FLIP verdicts cleared.`
            : `Auto-flipped ${applied} asset(s); ${still} still flagged.`,
      };
      // GLBs were rewritten — invalidate any cached library handle so
      // the next page-load re-fetches.
      invalidateLibrary();
    } catch (err) {
      auditMsg = { cls: 'fail', text: `Auto-flip failed: ${err}` };
    } finally {
      autoFlipPending = false;
    }
  }

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

  // Smart-merge so unchanged LibraryAsset entries keep their object
  // identity across refreshes. `AssetDetail` is wrapped in
  // `{#key activeId}` so it survives prop-reference churn on the
  // index itself, but reusing per-asset refs avoids needless prop
  // diffs for downstream consumers.
  function mergeLibrary(prev: LibraryIndex | null, next: LibraryIndex): LibraryIndex {
    if (!prev) return next;
    const mergedAssets: Record<string, LibraryAsset> = {};
    for (const [id, n] of Object.entries(next.assets)) {
      const old = prev.assets[id];
      mergedAssets[id] = old && JSON.stringify(old) === JSON.stringify(n) ? old : n;
    }
    return { ...next, assets: mergedAssets };
  }

  // Re-fetch on extract/skin-pack completion. Mirrors the Ships route;
  // see extract_events.svelte.ts for the cross-route signal contract.
  let lastSeenRevision = extractEvents.completionRevision;
  $effect(() => {
    const rev = extractEvents.completionRevision;
    if (rev === lastSeenRevision) return;
    lastSeenRevision = rev;
    void (async () => {
      try {
        invalidateLibrary();
        const next = await fetchLibrary();
        index = mergeLibrary(index, next);
      } catch (err) {
        console.warn('[library] refresh after extract failed:', err);
      }
    })();
  });

  onMount(async () => {
    try {
      index = await fetchLibrary();
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
    // Best-effort, don't block the page on a missing audit file —
    // the sidebar renders without badges in that case.
    void refreshWindingAudit();
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
      {windingAudit}
      {flipCandidateCount}
      {autoFlipPending}
      {auditMsg}
      onAutoFlip={runAutoFlip}
      onFilterChange={(next) => (filter = next)}
      onSortChange={(next) => (sort = next)}
      onSelect={selectAsset}
    />

    {#if activeId && activeAsset}
      <!--
        No {#key activeId} here — we want the AssetDetail (and its
        AccessoryViewer + WebGL context) to survive asset switches so
        sidebar settings (LOD filter, Show pivots, Flip 180°, etc.)
        stay sticky. AssetDetail's id-change $effect handles the
        per-asset reset of asset-bound state (meshes, dead variant,
        pivots, rig editor).
      -->
      <AssetDetail
        id={activeId}
        asset={activeAsset}
        windingAudit={windingAudit.get(activeAsset.glb) ?? null}
        onWindingAuditChange={refreshWindingAudit}
      />
    {:else}
      <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
        Select an asset from the list.
      </div>
    {/if}
  {/if}
</div>
