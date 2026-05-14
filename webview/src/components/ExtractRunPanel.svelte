<script lang="ts">
  // Run-options section of the Extract preview pane: checkboxes for the
  // ingest_ship flags + a live-preview command line + a "Run extract"
  // button. The composed CLI matches what /api/extract/run actually
  // spawns so the user can copy-paste to run it themselves.
  //
  // Run options are stored cross-session via `$lib/store` so a refresh
  // resumes with the user's preferred flags. The skin-pack panel reads
  // the same store key.

  import { loadState, patchState } from '$lib/store';
  import { suggestedLabel } from '$lib/extract/labels';
  import type { ExtractedShip, JobState, Permoflage, RunOptions, Vehicle } from '$lib/types/extract';

  interface Props {
    vehicle: Vehicle;
    permoflage: Permoflage | null;
    extractedShips: ExtractedShip[];
    activeJob: JobState | null;
    onRun: (opts: RunOptions) => void;
  }

  const { vehicle, permoflage, extractedShips, activeJob, onRun }: Props = $props();

  let options = $state<RunOptions>(loadState().extractRunOptions);

  function patchOption<K extends keyof RunOptions>(key: K, value: RunOptions[K]) {
    options = { ...options, [key]: value };
    patchState({ extractRunOptions: options });
  }

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
    if (options.build_library) out.push('  --build-library');
    if (options.and_publish) out.push('  --and-publish');
    if (options.publish_force) out.push('  --publish-force');
    out.push('  --non-interactive');
    return out;
  });
</script>

<section class="border-border border-t px-4 py-3">
  <h3 class="text-foreground mb-2 text-xs font-semibold uppercase tracking-wide">Run options</h3>
  <div class="flex flex-col gap-1.5 text-xs">
    <label class="text-foreground flex items-center gap-2">
      <input
        type="checkbox"
        checked={options.build_library}
        onchange={(e) => patchOption('build_library', e.currentTarget.checked)}
      />
      also rebuild accessory library (additive — slower)
    </label>
    <label class="text-foreground flex items-center gap-2">
      <input
        type="checkbox"
        checked={options.and_publish}
        onchange={(e) => patchOption('and_publish', e.currentTarget.checked)}
      />
      also publish outputs downstream
    </label>
    <label
      class="ml-5 flex items-center gap-2 transition-opacity {options.and_publish
        ? 'text-foreground opacity-100'
        : 'text-muted-foreground opacity-50'}"
    >
      <input
        type="checkbox"
        checked={options.publish_force}
        disabled={!options.and_publish}
        onchange={(e) => patchOption('publish_force', e.currentTarget.checked)}
      />
      force-overwrite (ignore mtime / size cache)
    </label>
  </div>
</section>

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
      onclick={() => onRun(options)}
      class="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
    >
      {running ? 'Run in progress…' : 'Run extract'}
    </button>
    <span class="text-muted-foreground text-[11px]">
      spawns <code>wows-ingest-ship</code> via <code>POST /api/extract/run</code>
    </span>
  </div>
</section>
