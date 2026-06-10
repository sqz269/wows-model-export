<script lang="ts">
  // Resolved-command preview + "Run extract" button. The composed CLI
  // matches what /api/extract/run actually spawns so the user can
  // copy-paste to run it themselves.

  import { suggestedLabel } from '$lib/extract/labels';
  import type { ExtractedShip, JobState, Permoflage, Vehicle } from '$lib/types/extract';

  interface Props {
    vehicle: Vehicle;
    permoflage: Permoflage | null;
    extractedShips: ExtractedShip[];
    activeJob: JobState | null;
    onRun: () => void;
    /** Append this ship's resolved args to the persistent extract queue.
     *  Picker selection is preserved so the user can pick another ship
     *  immediately. */
    onEnqueue: () => void;
    /** True while an enqueue POST is in flight. Disables the button. */
    enqueueBusy: boolean;
    /** HullDelta: export hull-swap exteriors' variant hulls on base
     *  extracts. Owned by the parent route (shared with Run + Queue). */
    exteriorHulls: boolean;
    onExteriorHullsChange: (on: boolean) => void;
  }

  const {
    vehicle,
    permoflage,
    extractedShips,
    activeJob,
    onRun,
    onEnqueue,
    enqueueBusy,
    exteriorHulls,
    onExteriorHullsChange,
  }: Props = $props();

  // Hull export only applies to BASE scaffolds — a mesh-swap permoflage
  // pick is a variant-routed legacy extract that skips the exteriors emit.
  const exteriorHullsApplies = $derived(!(permoflage && permoflage.topology === 'mesh_swap'));

  const label = $derived(suggestedLabel(vehicle, permoflage));
  const alreadyExtracted = $derived(extractedShips.some((s) => s.name === label));
  const running = $derived(activeJob?.state === 'running');

  const positional = $derived(vehicle.model_dir || vehicle.param_index);
  const topKey = $derived(vehicle.top_key || vehicle.param_index);

  const commandLines = $derived.by(() => {
    const out: string[] = [
      `wows-ingest-ship ${positional}`,
      `  --label ${label}`,
      `  --toolkit-ship ${vehicle.model_dir || vehicle.param_index}`,
      `  --gameparams-ship-id ${topKey}`,
    ];
    if (permoflage && permoflage.topology === 'mesh_swap') {
      out.push(`  --variant-permoflage ${permoflage.exterior_id}`);
    } else if (!permoflage) {
      out.push(`  --variant-permoflage none`);
    }
    if (exteriorHulls && exteriorHullsApplies) out.push('  --exterior-hulls');
    out.push('  --build-library');
    out.push('  --non-interactive');
    return out;
  });
</script>

<section class="border-border border-t px-4 py-3">
  <h3 class="text-foreground mb-2 text-xs font-semibold uppercase tracking-wide">Resolved command</h3>
  <div class="text-muted-foreground mb-2 flex flex-wrap items-center gap-1.5 text-[11px]">
    <span>Output folder:</span>
    <code class="text-foreground font-mono">ships/{label}/</code>
    {#if alreadyExtracted}
      <span class="bg-amber-900/40 rounded px-1.5 py-[1px] text-[10px] text-amber-200">
        already exists — will update in place
      </span>
    {:else}
      <span class="bg-secondary text-muted-foreground rounded px-1.5 py-[1px] text-[10px]">new folder</span>
    {/if}
  </div>
  <pre
    class="bg-popover text-foreground border-border max-h-60 overflow-auto rounded border px-2.5 py-2 font-mono text-[11px] leading-snug">{commandLines.join(' \\\n')}</pre>
  {#if exteriorHullsApplies}
    <label class="mt-2 flex items-start gap-2 text-[11px]">
      <input
        type="checkbox"
        checked={exteriorHulls}
        onchange={(e) => onExteriorHullsChange(e.currentTarget.checked)}
        class="mt-0.5"
      />
      <span>
        <strong>Export exterior hulls</strong>
        <span class="text-muted-foreground">
          (HullDelta) — also export each hull-swap exterior's variant hull into
          <code class="font-mono">models/exteriors/</code> so the ship viewer's Exteriors tab can
          swap hulls. One extra <code class="font-mono">export-ship</code> per hull-swap exterior;
          skipped for hulls already on disk.
        </span>
      </span>
    </label>
  {/if}
  <div class="mt-2.5 flex flex-wrap items-center gap-3">
    <button
      type="button"
      disabled={running}
      onclick={() => onRun()}
      class="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
    >
      {running ? 'Run in progress…' : 'Run extract'}
    </button>
    <button
      type="button"
      disabled={enqueueBusy}
      onclick={() => onEnqueue()}
      title="Append to the persistent queue and keep this selection so you can pick another ship."
      class="border border-border hover:bg-popover/60 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
    >
      {enqueueBusy ? 'Queueing…' : '+ Queue'}
    </button>
    <span class="text-muted-foreground text-[11px]">
      <strong>Run</strong> spawns immediately; <strong>+ Queue</strong> appends to the queue panel below.
    </span>
  </div>
</section>
