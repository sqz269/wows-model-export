<script lang="ts">
  // Asset detail panel: header (asset_id, scope/category/subcategory,
  // used-by-ships) → 3D viewer → side controls + info section.
  //
  // Owns the AccessoryViewer handle so the controls can wire directly to
  // it. Re-renders when `asset` changes; viewer instance is kept alive
  // across asset swaps for a smoother feel.

  import AccessoryViewerCmp from './AccessoryViewer.svelte';
  import RigEditorPanel from './RigEditorPanel.svelte';
  import type { AccessoryViewer, LoadResult, MeshInfo, SideMode } from '$lib/accessory';
  import type { LibraryAsset, RigPivots, WindingAuditEntry } from '$lib/types';
  import { fetchRigPivots, postFlipWinding, repoUrl } from '$lib/api';
  import { fmtBytes } from '$lib/util/html';
  import { onMount } from 'svelte';

  // Shared utility classes — mirror the labelled-dropdown idiom used in
  // ShipControls.svelte so the Library and Ships pages read identically.
  const labelCls = 'flex flex-col gap-0.5 text-[11px] text-muted-foreground';
  const inputBoxCls =
    'h-7 rounded border border-border bg-popover px-1.5 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring';
  const rowCls = 'flex items-center gap-1.5 text-xs text-foreground';

  interface Props {
    /** Asset_id (key in LibraryIndex.assets). */
    id: string;
    asset: LibraryAsset;
    /** Audit verdict for this asset's GLB. `null` when the audit JSON
     *  is missing or this asset isn't in the audit (e.g. unscored). */
    windingAudit?: WindingAuditEntry | null;
    /** Notify the parent to refetch the audit after a successful flip
     *  so list-level badges + counts stay in sync without a reload. */
    onWindingAuditChange?: () => void;
  }

  const { id, asset, windingAudit = null, onWindingAuditChange }: Props = $props();

  let viewer: AccessoryViewer | null = $state(null);
  let result: LoadResult | null = $state(null);
  let loadError: string | null = $state(null);
  let showingDead = $state(false);

  // Viewer state (mirrors viewer methods).
  let helpers = $state(true);
  let wireframe = $state(false);
  let side = $state<SideMode>('double');
  let lodFilter = $state<number | null>(null);
  let meshVisibility = $state<boolean[]>([]);
  let showTextures = $state(true);

  // Rig pivot overlay state. `pivots` is null when the sidecar JSON
  // isn't on disk (asset hasn't been rigged) — verdict chip + toggles
  // degrade gracefully.
  let pivots = $state<RigPivots | null>(null);
  let showRigPivots = $state(false);
  let rigFlip180 = $state(false);

  // Per-asset winding flip state. The "Flip winding" button rewrites
  // the GLB on disk (via /api/flip-winding) and toggles the asset's
  // entry in flip_overrides.json. Trivially reversible — click again
  // to undo. Distinct from the rigFlip180 viewer-only A/B toggle.
  let flipPending = $state(false);
  let flipMsg = $state<{ cls: 'ok' | 'fail' | 'working'; text: string } | null>(null);

  // Rig editor open/closed. When open, the AccessoryViewer is loaded
  // with the asset's `.rig.debug.glb` instead of the regular GLB, and
  // the picker is wired up. Closing reloads the regular GLB.
  let rigEditorOpen = $state(false);

  // Library context for the TextureManager. Same `asset` object the
  // page already has — passing it makes the viewer apply the DDS texture
  // pipeline on load. `variant` mirrors `showingDead` so the manager
  // picks `texture_sets.dead` (when present) over `main` for the dead
  // GLB — without it the dead geometry would render with the intact
  // albedo. The child reloads on `lib` change via its load effect.
  const libContext = $derived({
    assetId: id,
    asset,
    variant: (showingDead && asset.glb_dead ? 'dead' : 'main') as 'main' | 'dead',
  });

  // Asset-change reset. The AssetDetail instance now survives asset
  // switches (Library.svelte dropped the {#key activeId} wrapper) so
  // user-preference state — helpers, wireframe, side, textures, LOD
  // filter, Show pivots, Flip 180° — sticks across clicks.
  //
  // Only reset what's bound to the asset being unloaded:
  //   - geometry-shaped state (result, meshVisibility, loadError)
  //   - per-asset toggles (dead variant)
  //   - per-asset sidecar data (rig pivots — refetched in onLoaded)
  //   - in-flight UI feedback (flipMsg, rigEditorOpen, cacheBust)
  //
  // lodFilter is clamped separately once the new asset's result lands
  // — see the next $effect — so a "LOD 2 only" filter on an asset
  // without LOD 2 doesn't end up hiding every mesh.
  $effect(() => {
    void id;
    showingDead = false;
    meshVisibility = [];
    loadError = null;
    result = null;
    pivots = null;
    rigEditorOpen = false;
    flipMsg = null;
    cacheBust = 0;
  });

  // Clamp the sticky LOD filter once the new asset's meshes have
  // arrived. If the persisted choice (e.g. "LOD 2 only") doesn't
  // match any LOD on the freshly-loaded asset, fall back to "all".
  // Keeps the filter sticky in the common case while preventing the
  // empty-scene surprise on assets that don't have the chosen level.
  $effect(() => {
    if (!result) return;
    if (lodFilter !== null && !lods.includes(lodFilter)) {
      lodFilter = null;
      viewer?.setLodFilter(null);
    }
  });

  // Re-apply controls when the viewer comes online or after a reload.
  $effect(() => {
    if (!viewer || !result) return;
    viewer.setHelpers(helpers);
    viewer.setWireframe(wireframe);
    viewer.setSide(side);
    viewer.setLodFilter(lodFilter);
  });

  // Re-apply the rig overlay state when the viewer comes online and
  // when toggles flip. Pivot JSON is fetched on `onLoaded`, so this
  // is the central place to push the latest state into the viewer.
  $effect(() => {
    if (!viewer) return;
    viewer.setRigPivots(pivots);
    viewer.setRigPivotsVisible(showRigPivots);
    viewer.setRigFlip180(rigFlip180);
  });

  const url = $derived.by(() => {
    const rel = showingDead && asset.glb_dead ? asset.glb_dead : asset.glb;
    const base = repoUrl(`libraries/accessories/${rel}`);
    // Append the cache-buster on every persist-flip so the viewer
    // re-fetches the rewritten GLB. Default 0 leaves the URL clean
    // for the initial load.
    return cacheBust ? `${base}?t=${cacheBust}` : base;
  });

  // LOD bucketing for the LOD-filter buttons + info section.
  const lods = $derived.by(() => {
    if (!result) return [] as number[];
    const set = new Set<number>();
    for (const m of result.meshes) set.add(m.lod);
    return Array.from(set).sort((a, b) => a - b);
  });

  const lodBreakdown = $derived.by(() => {
    if (!result) return [];
    const by = new Map<number, { count: number; tris: number }>();
    for (const m of result.meshes) {
      const e = by.get(m.lod) ?? { count: 0, tris: 0 };
      e.count++;
      e.tris += m.triangles;
      by.set(m.lod, e);
    }
    return Array.from(by.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([lod, info]) => ({ lod, ...info }));
  });

  const totalTris = $derived.by(() => {
    if (!result) return 0;
    return result.meshes.reduce((s, m) => s + m.triangles, 0);
  });

  function onLoaded(res: LoadResult) {
    result = res;
    meshVisibility = res.meshes.map(() => true);
    loadError = null;
    // Fire-and-forget pivot fetch. Pivots apply even when the toggle
    // is off so flipping it on is instant — the JSON parse cost is
    // amortised here rather than on toggle.
    void (async () => {
      const next = await fetchRigPivots(asset.glb);
      pivots = next;
    })();
  }

  function onError(err: unknown) {
    loadError = err instanceof Error ? err.message : String(err);
    result = null;
  }

  function toggleHelpers(v: boolean) {
    helpers = v;
    viewer?.setHelpers(v);
  }
  function toggleWireframe(v: boolean) {
    wireframe = v;
    viewer?.setWireframe(v);
  }
  function setSide(v: SideMode) {
    side = v;
    viewer?.setSide(v);
  }
  function setLod(v: number | null) {
    lodFilter = v;
    viewer?.setLodFilter(v);
  }
  function toggleMesh(i: number, v: boolean) {
    meshVisibility[i] = v;
    viewer?.setMeshVisibleByIndex(i, v);
  }
  function toggleVariant(dead: boolean) {
    showingDead = dead;
  }
  async function toggleTextures(v: boolean) {
    showTextures = v;
    try {
      await viewer?.setShowTextures(v);
    } catch (err) {
      console.warn('[assetdetail] setShowTextures failed:', err);
    }
  }

  function truncate(s: string, n: number) {
    return s.length <= n ? s : s.slice(0, n - 1) + '…';
  }

  function meshes(): MeshInfo[] {
    return result?.meshes ?? [];
  }

  async function flipWinding() {
    if (flipPending || rigEditorOpen) return;
    const rel = showingDead && asset.glb_dead ? asset.glb_dead : asset.glb;
    flipPending = true;
    flipMsg = { cls: 'working', text: 'Flipping…' };
    try {
      const res = await postFlipWinding(rel);
      if (!res.ok) {
        flipMsg = {
          cls: 'fail',
          text: `Flip failed: ${res.error || res.stderr || 'unknown'}`,
        };
        return;
      }
      // GLB on disk has been rewritten. Reload it with a cache-bust
      // so the browser fetches the new bytes. The viewer's onLoaded
      // re-fetches pivots, so everything stays in sync.
      const flipped = res.override?.flipped ?? true;
      flipMsg = {
        cls: 'ok',
        text: flipped
          ? 'Flipped (persisted). Click again to undo.'
          : 'Un-flipped (persisted).',
      };
      // Bump the audit so the badge updates without a page reload.
      onWindingAuditChange?.();
      // Force-reload the viewer by re-triggering the load effect with a
      // cache-buster. `url` is derived from `asset` + `showingDead` —
      // we can't mutate `asset.glb` so we use a separate cache-bust
      // counter (added below).
      cacheBust = Date.now();
    } catch (err) {
      flipMsg = { cls: 'fail', text: `Flip failed: ${err}` };
    } finally {
      flipPending = false;
    }
  }

  // Per-asset cache-buster bumped on every persist-flip so the viewer
  // re-fetches the rewritten GLB.
  let cacheBust = $state(0);

  // F-key shortcut for "flip winding". No-op while typing in an input
  // / textarea / contenteditable, while any modifier is held (keeps
  // Ctrl+F → browser find free), or while a persist is in flight.
  onMount(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const t = ev.target as HTMLElement | null;
      if (t) {
        const tag = t.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (t.isContentEditable) return;
      }
      if (ev.key.toLowerCase() !== 'f') return;
      if (flipPending || rigEditorOpen) return;
      ev.preventDefault();
      void flipWinding();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  /** Verdict chip metadata for the rig-pivots panel. Buckets the four
   *  geometric_check states + the `auto_flipped_180_around_yaw` flag
   *  into colour-coded chips with explanatory tooltips. */
  const verdictChip = $derived.by(() => {
    if (!pivots) return null;
    const geo = pivots.geometric_check;
    const flipped = pivots.auto_flipped_180_around_yaw === true;
    if (!geo) return null;
    const v = geo.verdict;
    if (flipped || v === 'needs_flip') {
      return {
        label: 'auto-flipped 180°',
        cls: 'bg-sky-900/40 text-sky-300',
        title:
          'turret_autorig detected the pre-aim-rotation pose and baked a Ry(180°) into the emitted pivots. ' +
          'The displayed pivots are already corrected — toggling "flip 180°" would un-correct them.',
      };
    }
    if (v === 'ok') {
      return {
        label: 'mesh-aligned',
        cls: 'bg-emerald-900/40 text-emerald-300',
        title:
          'Geometric check: every muzzle landed on the alive library mesh under the as-extracted pose. No flip needed.',
      };
    }
    if (v === 'ambiguous') {
      const vs = geo.votes;
      return {
        label: 'ambiguous',
        cls: 'bg-amber-900/40 text-amber-300',
        title:
          `Geometric check could not discriminate (votes ok=${vs?.ok ?? 0}, ` +
          `flip=${vs?.flip ?? 0}, tie=${vs?.tie ?? 0}). ` +
          'Verify manually with the "flip 180°" toggle.',
      };
    }
    return {
      label: 'no mesh check',
      cls: 'bg-muted text-muted-foreground',
      title: geo.error
        ? `Geometric check skipped: ${geo.error}`
        : 'Geometric check skipped: library GLB unavailable when pivots were extracted.',
    };
  });

  /** Per-barrel ok-vs-flip distance rows. Smaller distance = better. */
  const distRows = $derived.by(() => {
    const geo = pivots?.geometric_check;
    if (!geo?.muzzle_dists?.length || !geo?.muzzle_dists_flip?.length) return [];
    return geo.muzzle_dists.map((d, i) => {
      const f = geo.muzzle_dists_flip?.[i] ?? NaN;
      const okBetter = d <= f;
      return { i, d, f, okBetter };
    });
  });

  function fmtDist(n: number): string {
    return Number.isFinite(n) ? `${n.toFixed(4)} m` : '—';
  }
</script>

<section class="flex flex-1 min-w-0 flex-col overflow-hidden">
  <header
    class="bg-card border-border flex flex-none items-start justify-between gap-4 border-b px-5 py-3"
  >
    <div>
      <h2 class="m-0 text-sm font-semibold"><code class="font-mono">{id}</code></h2>
      <div class="text-muted-foreground mt-0.5 text-[11px]">
        {asset.scope}/{asset.category}{asset.subcategory ? `/${asset.subcategory}` : ''}
        {#if asset.species}· {asset.species}{/if}
      </div>
      <div class="text-muted-foreground mt-1 max-w-[60ch] break-words text-[11px]">
        {#if asset.used_by_ships.length}
          used by {asset.used_by_ships.length}:
          <code>{asset.used_by_ships.join(', ')}</code>
        {:else}
          unused
        {/if}
      </div>
    </div>
    <div class="text-right text-[11px] tabular-nums text-foreground">
      {#if loadError}
        <span class="text-destructive">{loadError}</span>
      {:else if result}
        {result.meshes.length} mesh{result.meshes.length === 1 ? '' : 'es'} ·
        {totalTris.toLocaleString()} tris
      {:else}
        Loading…
      {/if}
    </div>
  </header>

  <div class="flex flex-1 min-h-0 overflow-hidden">
    <AccessoryViewerCmp
      {url}
      lib={libContext}
      bindHandle={(v) => {
        viewer = v;
      }}
      {onLoaded}
      {onError}
    />

    <aside
      class="bg-card border-border flex w-[260px] flex-none flex-col gap-3 overflow-y-auto border-l p-3.5"
    >
      <!--
        View section: matches ShipControls.svelte's idiom — checkboxes for
        binary toggles, labelled <select>s for n-of-many. Replaces the
        earlier toggle-button groups (faces / variant / LOD filter) so the
        Library and Ships pages share one visual + interaction vocabulary.
      -->
      <div class="flex flex-col gap-2">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          view
        </div>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={helpers}
            onchange={(e) => toggleHelpers(e.currentTarget.checked)}
          />
          Helpers (grid + axes)
        </label>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={wireframe}
            onchange={(e) => toggleWireframe(e.currentTarget.checked)}
          />
          Wireframe
        </label>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={showTextures}
            onchange={(e) => toggleTextures(e.currentTarget.checked)}
          />
          Textures
        </label>
        {#if asset.glb_dead}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={showingDead}
              onchange={(e) => toggleVariant(e.currentTarget.checked)}
            />
            Show destroyed variant
          </label>
        {/if}
        <label class={labelCls}>
          Faces
          <select
            value={side}
            onchange={(e) => setSide(e.currentTarget.value as SideMode)}
            class={inputBoxCls}
          >
            <option value="double">Double-sided</option>
            <option value="front">Front only</option>
            <option value="back">Back only</option>
          </select>
        </label>
        {#if lods.length > 1}
          <label class={labelCls}>
            LOD filter
            <select
              value={lodFilter === null ? 'all' : String(lodFilter)}
              onchange={(e) => {
                const v = e.currentTarget.value;
                setLod(v === 'all' ? null : Number(v));
              }}
              class={inputBoxCls}
            >
              <option value="all">All LODs</option>
              {#each lods as lod (lod)}
                <option value={String(lod)}>LOD {lod} only</option>
              {/each}
            </select>
          </label>
        {/if}
      </div>

      <!--
        Winding section: persist-flip button + F-shortcut hint, plus
        the audit verdict line when scoring is available. The button
        is disabled while a flip is in flight or while the rig editor
        is open (the debug scene has its own materials/winding state).
      -->
      <div class="flex flex-col gap-1.5">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          winding
        </div>
        <button
          type="button"
          disabled={flipPending || rigEditorOpen}
          onclick={flipWinding}
          title="Reverse triangle winding and rewrite the GLB on disk. Click again to undo. Shortcut: F"
          class="rounded border border-border bg-popover px-2 py-1 text-xs hover:bg-accent disabled:opacity-60 text-left"
        >
          Flip winding <span class="text-muted-foreground">F</span>
        </button>
        {#if windingAudit}
          {@const w = windingAudit}
          {@const label =
            w.in_overrides && w.verdict === 'keep'
              ? 'manual'
              : w.in_overrides && w.verdict === 'flip'
                ? 'dispute'
                : w.verdict}
          <div
            class="rounded bg-popover/50 px-2 py-1 text-[11px]"
            title={`Joint A+B winding heuristic — correctness ${w.correctness.toFixed(3)} ` +
              `(B=${w.signal_b.toFixed(3)} geom·outward, A=${w.signal_a.toFixed(3)} geom·stored). ` +
              `>0.5 = correct, <0.5 = inverted.`}
          >
            auto-detect:
            <strong
              class:text-rose-400={label === 'flip' || label === 'dispute'}
              class:text-amber-400={label === 'ambiguous'}
              class:text-emerald-400={label === 'manual' || label === 'keep'}
            >{label}</strong>
            <span class="text-muted-foreground"> {w.correctness.toFixed(2)}</span>
            <span class="text-muted-foreground"
              > B{w.signal_b.toFixed(2)} A{w.signal_a.toFixed(2)}</span
            >
          </div>
        {/if}
        {#if flipMsg}
          <div
            class="text-[11px] leading-tight"
            class:text-emerald-400={flipMsg.cls === 'ok'}
            class:text-destructive={flipMsg.cls === 'fail'}
            class:text-muted-foreground={flipMsg.cls === 'working'}
          >
            {flipMsg.text}
          </div>
        {/if}
      </div>

      <!--
        Rig pivots: yaw / elev / muzzle markers from the
        `<asset>.rig_pivots.json` sidecar. Hidden entirely when no
        sidecar exists on disk. Verdict chip + per-barrel distances
        only render when the rigger emitted a `geometric_check`.
      -->
      <div class="flex flex-col gap-1.5">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          rig pivots
        </div>
        {#if !pivots}
          <div class="text-muted-foreground text-[11px]">
            no <code>rig_pivots.json</code>
          </div>
        {:else}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={showRigPivots}
              onchange={(e) => (showRigPivots = e.currentTarget.checked)}
            />
            Show pivots
          </label>
          <label
            class={rowCls}
            title="Rotate the rig 180° around the yaw axis. Quick A/B for forward-axis mismatches with the mesh. Not saved."
          >
            <input
              type="checkbox"
              checked={rigFlip180}
              onchange={(e) => (rigFlip180 = e.currentTarget.checked)}
            />
            Flip 180°
          </label>
          <div class="text-[11px]">
            {pivots.barrel_count} barrel{pivots.barrel_count === 1 ? '' : 's'}
            · {pivots.shared_elev ? 'shared elev' : 'indep. elev'}
          </div>
          {#if verdictChip}
            <span
              class="self-start rounded px-1.5 py-[1px] text-[10px] {verdictChip.cls}"
              title={verdictChip.title}
            >
              {verdictChip.label}
            </span>
          {/if}
          {#if distRows.length > 0}
            <table class="w-full text-[10px] tabular-nums">
              <thead>
                <tr class="text-muted-foreground">
                  <th class="text-left"></th>
                  <th class="text-right">ok</th>
                  <th class="text-right">flip</th>
                </tr>
              </thead>
              <tbody>
                {#each distRows as r (r.i)}
                  <tr>
                    <td class="text-muted-foreground">b{r.i}</td>
                    <td
                      class="text-right"
                      class:text-emerald-400={r.okBetter}
                      class:text-muted-foreground={!r.okBetter}>{fmtDist(r.d)}</td
                    >
                    <td
                      class="text-right"
                      class:text-emerald-400={!r.okBetter}
                      class:text-muted-foreground={r.okBetter}>{fmtDist(r.f)}</td
                    >
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}
          {#if pivots.warnings?.length}
            <ul class="m-0 list-disc pl-4 text-[10px] text-amber-300">
              {#each pivots.warnings as w (w)}<li>{w}</li>{/each}
            </ul>
          {/if}
        {/if}
      </div>

      <!--
        Rig editor toggle. The picker lives inside RigEditorPanel which
        owns the debug-scene load + override-staging state. Opening
        the editor swaps the viewer's loaded GLB so `onLoaded` fires
        again on close; that re-fetches pivots and resets the toggles.
      -->
      <div class="flex flex-col gap-1.5">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          rig editor
        </div>
        <label
          class={rowCls}
          title="Swap viewer to the pre-merge debug scene with each piece colour-coded by category. Click pieces to override their category or set as the mantlet face-plate."
        >
          <input
            type="checkbox"
            checked={rigEditorOpen}
            onchange={(e) => (rigEditorOpen = e.currentTarget.checked)}
          />
          Edit rig
        </label>
        {#if rigEditorOpen && viewer}
          <RigEditorPanel
            assetId={id}
            assetGlb={asset.glb}
            {viewer}
            onClose={() => (rigEditorOpen = false)}
          />
        {/if}
      </div>

      <div class="flex flex-col gap-1">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          meshes
        </div>
        <ul class="m-0 flex max-h-[320px] flex-col gap-0.5 overflow-y-auto p-0 list-none">
          {#each meshes() as m, i (i)}
            <li>
              <label
                title={m.name}
                class="grid grid-cols-[auto_1fr_auto_auto] items-center gap-1.5 text-[11px]"
              >
                <input
                  type="checkbox"
                  checked={meshVisibility[i] ?? true}
                  onchange={(e) => toggleMesh(i, e.currentTarget.checked)}
                />
                <span class="overflow-hidden text-ellipsis whitespace-nowrap text-foreground">
                  {truncate(m.name, 36)}
                </span>
                <span class="text-muted-foreground">lod{m.lod}</span>
                <span class="text-muted-foreground tabular-nums">{m.triangles.toLocaleString()}</span>
              </label>
            </li>
          {/each}
        </ul>
      </div>
    </aside>
  </div>

  <div
    class="bg-background border-border grid max-h-[30%] flex-none grid-cols-2 gap-6 overflow-y-auto border-t px-5 py-3"
  >
    <section>
      <h3
        class="text-muted-foreground mb-1.5 text-[11px] font-semibold uppercase tracking-wider"
      >
        Library
      </h3>
      <dl
        class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-words [&_code]:font-mono [&_code]:text-[11px]"
      >
        <dt>GLB</dt>
        <dd>
          <code>{asset.glb}</code> <span class="text-muted-foreground">({fmtBytes(asset.glb_bytes)})</span>
        </dd>
        {#if asset.glb_dead}
          <dt>Dead GLB</dt>
          <dd>
            <code>{asset.glb_dead}</code>
            <span class="text-muted-foreground">({fmtBytes(asset.glb_dead_bytes ?? 0)})</span>
          </dd>
        {/if}
        <dt>Textures</dt>
        <dd>
          {#if asset.textures}
            <code>{asset.textures}</code>
          {:else}
            <span class="text-muted-foreground">(none)</span>
          {/if}
        </dd>
        <dt>Textures (DDS)</dt>
        <dd>
          {#if asset.textures_dds}
            <code>{asset.textures_dds}</code>
          {:else}
            <span class="text-muted-foreground">(none)</span>
          {/if}
        </dd>
      </dl>
    </section>

    <section>
      <h3
        class="text-muted-foreground mb-1.5 text-[11px] font-semibold uppercase tracking-wider"
      >
        LOD breakdown
      </h3>
      <table
        class="w-full border-collapse text-xs [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:text-left [&_td]:py-0.5 [&_th]:py-0.5 [&_th]:pr-2 [&_td]:pr-2 [&_td:not(:first-child)]:text-right [&_th:not(:first-child)]:text-right [&_td:not(:first-child)]:tabular-nums [&_tfoot_td]:border-t [&_tfoot_td]:border-border [&_tfoot_td]:text-muted-foreground"
      >
        <thead>
          <tr><th>level</th><th>meshes</th><th>triangles</th></tr>
        </thead>
        <tbody>
          {#each lodBreakdown as row (row.lod)}
            <tr>
              <td>lod{row.lod}</td>
              <td>{row.count}</td>
              <td>{row.tris.toLocaleString()}</td>
            </tr>
          {/each}
        </tbody>
        <tfoot>
          <tr>
            <td>total</td>
            <td>{meshes().length}</td>
            <td>{totalTris.toLocaleString()}</td>
          </tr>
        </tfoot>
      </table>
    </section>
  </div>
</section>
