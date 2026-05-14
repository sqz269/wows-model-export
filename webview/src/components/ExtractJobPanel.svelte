<script lang="ts">
  // Job panel: shows state + log tail for the currently-tracked extract
  // or skin-pack subprocess. Active during a run; sticks around for a
  // dismiss-click after completion so the user can copy the output.
  //
  // The tail auto-scrolls to the bottom on each render so fresh stdout
  // stays in view. `bind:this` on the <pre> + an $effect on activeJob.id
  // does the scroll.

  import { onMount } from 'svelte';
  import { formatElapsed } from '$lib/extract/labels';
  import type { JobState } from '$lib/types/extract';

  interface Props {
    job: JobState;
    /** When set, "(label ...)" hint hides because the user is already
     *  looking at the matching Vehicle's preview. */
    matchesActiveLabel: boolean;
    onCancel: () => void;
    onDismiss: () => void;
  }

  const { job, matchesActiveLabel, onCancel, onDismiss }: Props = $props();

  let tailEl: HTMLPreElement | null = $state(null);

  // Re-scroll on every render of the panel — bind:this hands back the
  // element, then we set scrollTop after Svelte applies the patch. The
  // $effect re-runs whenever stdout/stderr/state changes.
  $effect(() => {
    void job.stdout;
    void job.stderr;
    void job.state;
    if (tailEl) tailEl.scrollTop = tailEl.scrollHeight;
  });

  const tail = $derived.by(() => {
    const parts: string[] = [];
    if (job.stdout) parts.push(job.stdout);
    if (job.stderr) parts.push('━━ stderr ━━\n' + job.stderr);
    return parts.join('\n') || '(no output yet)';
  });

  const stateClass: Record<JobState['state'], string> = {
    running: 'bg-sky-900/40 text-sky-200',
    done: 'bg-emerald-900/40 text-emerald-200',
    failed: 'bg-rose-900/40 text-rose-200',
    cancelled: 'bg-amber-900/40 text-amber-200',
  };

  let now = $state(Date.now());
  onMount(() => {
    // Tick elapsed while running so the timer updates even when no
    // poll arrives (the server polls every 1.5 s; the timer ticks
    // every 1 s for smoothness).
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });
  const elapsed = $derived(formatElapsed(job.started_at, job.finished_at ?? (job.state === 'running' ? now : null)));
</script>

<section class="border-border border-t px-4 py-3">
  <h3 class="text-foreground mb-2 flex flex-wrap items-center gap-1.5 text-xs font-semibold uppercase tracking-wide">
    Job <code class="font-mono normal-case tracking-normal">{job.id}</code>
    <span class="rounded px-1.5 py-[1px] text-[10px] normal-case tracking-normal {stateClass[job.state]}">{job.state}</span>
    <span class="bg-secondary text-muted-foreground rounded px-1.5 py-[1px] text-[10px] normal-case tracking-normal">{job.kind}</span>
    {#if !matchesActiveLabel}
      <span class="text-muted-foreground ml-1 text-[11px] normal-case tracking-normal">
        (label <code class="font-mono">{job.label}</code>)
      </span>
    {/if}
  </h3>
  <div class="mb-2 flex items-center gap-3">
    {#if job.state === 'running'}
      <button
        type="button"
        onclick={onCancel}
        class="bg-destructive/10 text-destructive hover:bg-destructive/20 rounded px-2.5 py-1 text-xs font-medium"
      >
        Cancel
      </button>
    {:else}
      <button
        type="button"
        onclick={onDismiss}
        class="bg-secondary text-foreground hover:bg-secondary/80 rounded px-2.5 py-1 text-xs"
      >
        Dismiss
      </button>
    {/if}
    <span class="text-muted-foreground text-[11px] tabular-nums">
      elapsed {elapsed}
      {#if job.exit_code !== null}
        · exit {job.exit_code}
      {/if}
    </span>
  </div>
  <pre
    bind:this={tailEl}
    class="bg-popover text-foreground border-border max-h-72 min-h-32 overflow-auto rounded border px-2.5 py-2 font-mono text-[11px] leading-snug">{tail}</pre>
</section>
