<script lang="ts">
  // Tabbed read-only inspector that lives under the AccessoryViewer.
  //
  // Tabs:
  //   - Files       : GLB / dead GLB / textures_dds paths
  //   - LODs        : LOD breakdown table
  //   - Winding     : audit numbers + heuristic tooltip
  //   - Rig         : pivots verdict chip + per-barrel distances + warnings
  //   - Rig editor  : hosts <RigEditorPanel> when the parent's editor toggle is on
  //
  // The panel is resizable: drag handle at the top to grow/shrink, persisted
  // to localStorage. Collapses to a slim tab strip when dragged below ~36px.

  import { onMount, untrack } from 'svelte';
  import { fmtBytes } from '$lib/util/html';
  import { tabBtnBase } from '$lib/ui/controls';
  import RigEditorPanel from './RigEditorPanel.svelte';
  import type { AccessoryViewer } from '$lib/accessory';
  import type { LibraryAsset, RigPivots, WindingAuditEntry } from '$lib/types';

  export type BottomTab = 'files' | 'lods' | 'winding' | 'rig' | 'rig-editor';

  interface VerdictChip {
    label: string;
    cls: string;
    title: string;
  }

  interface DistRow {
    i: number;
    d: number;
    f: number;
    okBetter: boolean;
  }

  interface LodRow {
    lod: number;
    count: number;
    tris: number;
  }

  interface Props {
    asset: LibraryAsset;
    /** Audit verdict for the GLB at `asset.glb`. `null` when missing. */
    windingAudit: WindingAuditEntry | null;
    /** Rig pivot sidecar data. `null` when the JSON isn't on disk. */
    pivots: RigPivots | null;
    /** Derived in parent — verdict chip metadata for the Rig tab. */
    verdictChip: VerdictChip | null;
    /** Derived in parent — per-barrel distance rows for the Rig tab. */
    distRows: DistRow[];
    /** Derived in parent — LOD-by-LOD breakdown for the LODs tab. */
    lodBreakdown: LodRow[];
    totalMeshes: number;
    totalTris: number;
    /** Whether the rig editor is open in the parent. Drives the
     *  rig-editor tab visibility + the active-tab autoswitch. */
    rigEditorOpen: boolean;
    /** Live viewer handle for the rig editor. Only used while
     *  `rigEditorOpen` is true. */
    viewer: AccessoryViewer | null;
    /** Asset key for the rig editor + override file naming. */
    assetId: string;
    onCloseRigEditor: () => void;
  }

  const {
    asset,
    windingAudit,
    pivots,
    verdictChip,
    distRows,
    lodBreakdown,
    totalMeshes,
    totalTris,
    rigEditorOpen,
    viewer,
    assetId,
    onCloseRigEditor,
  }: Props = $props();

  // Storage keys mirror the Library page namespace.
  const HEIGHT_KEY = 'wows-webview.detail-bottom-panel.height';
  const TAB_KEY = 'wows-webview.detail-bottom-panel.tab';
  const DEFAULT_HEIGHT = 240;
  const COLLAPSED_HEIGHT = 36;
  const COLLAPSE_THRESHOLD = 60;
  const MIN_EXPANDED = 120;
  const MAX_HEIGHT_FRAC = 0.7;

  let height = $state<number>(DEFAULT_HEIGHT);
  let activeTab = $state<BottomTab>('files');
  let dragging = $state(false);

  onMount(() => {
    try {
      const stored = localStorage.getItem(HEIGHT_KEY);
      if (stored !== null) {
        const n = Number(stored);
        if (Number.isFinite(n) && n >= COLLAPSED_HEIGHT) height = n;
      }
      const t = localStorage.getItem(TAB_KEY);
      if (t === 'files' || t === 'lods' || t === 'winding' || t === 'rig') {
        activeTab = t;
      }
    } catch {
      /* localStorage may be unavailable (private mode, etc.) */
    }
  });

  // Auto-switch to the rig-editor tab whenever the parent flips the
  // editor on. The persisted tab choice is restored on next load.
  let prevRigEditorOpen = false;
  $effect(() => {
    const open = rigEditorOpen;
    untrack(() => {
      if (open && !prevRigEditorOpen) {
        activeTab = 'rig-editor';
      } else if (!open && prevRigEditorOpen && activeTab === 'rig-editor') {
        // Drop back to the previously-persisted tab (or files).
        let t: string | null = null;
        try {
          t = localStorage.getItem(TAB_KEY);
        } catch {
          t = null;
        }
        activeTab = t === 'files' || t === 'lods' || t === 'winding' || t === 'rig' ? t : 'files';
      }
      prevRigEditorOpen = open;
    });
  });

  function setTab(t: BottomTab) {
    activeTab = t;
    if (t !== 'rig-editor') {
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

  // Drag-to-resize via pointer events. The handle is at the top of the
  // panel; dragging up grows, dragging down shrinks. Below the collapse
  // threshold we snap to the slim tab-strip height.
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

  const tabs: Array<{ id: BottomTab; label: string; hide?: boolean }> = $derived([
    { id: 'files', label: 'Files' },
    { id: 'lods', label: 'LODs' },
    { id: 'winding', label: 'Winding' },
    { id: 'rig', label: 'Rig' },
    { id: 'rig-editor', label: 'Rig editor', hide: !rigEditorOpen },
  ]);

  function fmtDist(n: number): string {
    return Number.isFinite(n) ? `${n.toFixed(4)} m` : '—';
  }
</script>

<section class="bg-card border-border flex flex-none flex-col border-t" style="height: {height}px">
  <!--
    Drag handle / tab bar row. The drag handle is a thin strip above the
    tabs; click-and-drag to resize, double-click to toggle collapsed.
  -->
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
      {#if activeTab === 'files'}
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-words [&_code]:font-mono [&_code]:text-[11px]"
        >
          <dt>GLB</dt>
          <dd>
            <code>{asset.glb}</code>
            <span class="text-muted-foreground">({fmtBytes(asset.glb_bytes)})</span>
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
            {#if asset.textures_dds}
              <code>{asset.textures_dds}</code>
            {:else}
              <span class="text-muted-foreground">(none)</span>
            {/if}
          </dd>
        </dl>
      {:else if activeTab === 'lods'}
        <table
          class="w-full border-collapse text-xs [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:text-left [&_td]:py-0.5 [&_th]:py-0.5 [&_th]:pr-3 [&_td]:pr-3 [&_td:not(:first-child)]:text-right [&_th:not(:first-child)]:text-right [&_td:not(:first-child)]:tabular-nums [&_tfoot_td]:border-t [&_tfoot_td]:border-border [&_tfoot_td]:text-muted-foreground"
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
              <td>{totalMeshes}</td>
              <td>{totalTris.toLocaleString()}</td>
            </tr>
          </tfoot>
        </table>
      {:else if activeTab === 'winding'}
        {#if windingAudit}
          {@const w = windingAudit}
          {@const label =
            w.in_overrides && w.verdict === 'keep'
              ? 'manual'
              : w.in_overrides && w.verdict === 'flip'
                ? 'dispute'
                : w.verdict}
          <div class="flex flex-col gap-1.5">
            <div class="flex items-baseline gap-3">
              <span class="text-muted-foreground text-[10px] uppercase tracking-wider">verdict</span
              >
              <strong
                class:text-rose-400={label === 'flip' || label === 'dispute'}
                class:text-amber-400={label === 'ambiguous'}
                class:text-emerald-400={label === 'manual' || label === 'keep'}>{label}</strong
              >
              <span
                class="text-muted-foreground cursor-help text-[11px]"
                title={'Joint A+B winding heuristic.\n' +
                  'Signal B = geom · outward (geometry vs. surface-outward direction).\n' +
                  'Signal A = geom · stored (geometry vs. stored normals).\n' +
                  'Correctness = combined score, range 0..1.\n' +
                  '> 0.5 → winding is correct.\n' +
                  '< 0.5 → winding is inverted; press F to flip.'}>what does this mean?</span
              >
            </div>
            <dl
              class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:tabular-nums"
            >
              <dt>correctness</dt>
              <dd>{w.correctness.toFixed(3)}</dd>
              <dt>signal_b (geom · outward)</dt>
              <dd>{w.signal_b.toFixed(3)}</dd>
              <dt>signal_a (geom · stored)</dt>
              <dd>{w.signal_a.toFixed(3)}</dd>
              <dt>n_prim</dt>
              <dd>{w.n_prim}</dd>
              <dt>in_overrides</dt>
              <dd>{w.in_overrides ? 'yes' : 'no'}</dd>
            </dl>
          </div>
        {:else}
          <div class="text-muted-foreground">
            no <code class="font-mono text-[11px]">winding_audit.json</code> entry for this asset
          </div>
        {/if}
      {:else if activeTab === 'rig'}
        {#if !pivots}
          <div class="text-muted-foreground">
            no <code class="font-mono text-[11px]">{assetId}.rig_pivots.json</code>
          </div>
        {:else}
          <div class="flex flex-col gap-2">
            <div class="flex items-center gap-3 text-xs">
              {#if verdictChip}
                <span
                  class="rounded px-1.5 py-[1px] text-[10px] {verdictChip.cls}"
                  title={verdictChip.title}
                >
                  {verdictChip.label}
                </span>
              {/if}
              <span class="text-muted-foreground">
                {pivots.barrel_count} barrel{pivots.barrel_count === 1 ? '' : 's'} ·
                {pivots.shared_elev ? 'shared elev' : 'indep. elev'}
              </span>
            </div>
            {#if distRows.length > 0}
              <table
                class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-4 [&_td]:pr-4 [&_td]:py-0.5"
              >
                <thead>
                  <tr>
                    <th class="text-left">barrel</th>
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
              <ul class="m-0 list-disc pl-5 text-[11px] text-amber-300">
                {#each pivots.warnings as w (w)}<li>{w}</li>{/each}
              </ul>
            {/if}
          </div>
        {/if}
      {:else if activeTab === 'rig-editor'}
        {#if rigEditorOpen && viewer}
          <RigEditorPanel {assetId} assetGlb={asset.glb} {viewer} onClose={onCloseRigEditor} />
        {:else}
          <div class="text-muted-foreground">Rig editor is off.</div>
        {/if}
      {/if}
    </div>
  {/if}
</section>
