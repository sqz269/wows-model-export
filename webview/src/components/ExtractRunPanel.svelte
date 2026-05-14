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
  }

  const { vehicle, permoflage, extractedShips, activeJob, onRun }: Props = $props();

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
  <div class="mt-2.5 flex items-center gap-3">
    <button
      type="button"
      disabled={running}
      onclick={() => onRun()}
      class="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
    >
      {running ? 'Run in progress…' : 'Run extract'}
    </button>
    <span class="text-muted-foreground text-[11px]">
      spawns <code>wows-ingest-ship</code> via <code>POST /api/extract/run</code>
    </span>
  </div>
</section>
