<script lang="ts">
  // Consumers route — top-level dispatch for downstream publisher plugins.
  //
  // Reads /api/consumers on mount; renders one card per consumer with a
  // ConsumerActionForm per declared action. Empty list (no consumers
  // installed) renders a help banner. Active job state uses the same
  // /api/extract/jobs polling path the Extract route uses — jobs of
  // kind="consumer" surface through the same handlers.

  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import {
    cancelExtractJob,
    fetchConsumers,
    fetchExtractJob,
    fetchExtractJobs,
    fetchExtractedShips,
    runConsumerAction,
  } from '$lib/api';
  import type { Consumer } from '$lib/types/consumers';
  import type { ExtractedShip, JobState } from '$lib/types/extract';
  import ConsumerActionForm from '$components/ConsumerActionForm.svelte';
  import ExtractJobPanel from '$components/ExtractJobPanel.svelte';

  interface Props {
    active: boolean;
  }
  const { active: _active }: Props = $props();

  let consumers = $state<Consumer[]>([]);
  let extractedShips = $state<ExtractedShip[]>([]);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let activeJob = $state<JobState | null>(null);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    void (async () => {
      try {
        const [cs, ships] = await Promise.all([
          fetchConsumers(),
          fetchExtractedShips().catch(() => [] as ExtractedShip[]),
        ]);
        consumers = cs;
        extractedShips = ships;
      } catch (err) {
        loadError = err instanceof Error ? err.message : String(err);
      } finally {
        loading = false;
      }
      await restoreActiveJob();
    })();

    return () => {
      if (pollHandle !== null) {
        clearInterval(pollHandle);
        pollHandle = null;
      }
    };
  });

  // Re-attach to any in-flight consumer job from the dev server. Same
  // pattern as Extract.svelte — a page refresh during a long publish
  // shouldn't lose the running job.
  async function restoreActiveJob(): Promise<void> {
    try {
      const jobs = (await fetchExtractJobs()) ?? [];
      const running = jobs
        .filter((j) => j.state === 'running' && j.kind === 'consumer')
        .sort((a, b) => b.started_at - a.started_at);
      if (running.length === 0) return;
      const detail = await fetchExtractJob(running[0].id);
      if (!detail) return;
      activeJob = detail;
      if (detail.state === 'running') startPolling();
    } catch (err) {
      console.warn('[consumers] restoreActiveJob failed:', err);
    }
  }

  function startPolling(): void {
    if (pollHandle !== null) clearInterval(pollHandle);
    const tick = async () => {
      const current = activeJob;
      if (!current) return;
      try {
        const next = await fetchExtractJob(current.id);
        if (!next) return;
        const wasRunning = current.state === 'running';
        activeJob = next;
        if (next.state !== 'running' && pollHandle !== null) {
          clearInterval(pollHandle);
          pollHandle = null;
          if (wasRunning) {
            if (next.state === 'done') {
              toast.success(`${next.label} completed`, { duration: 4000 });
            } else if (next.state === 'failed') {
              toast.error(`${next.label} failed`);
            }
          }
        }
      } catch (err) {
        console.warn('[consumers] poll failed:', err);
      }
    };
    void tick();
    pollHandle = setInterval(tick, 1500);
  }

  async function runAction(consumer: Consumer, actionId: string, body: Record<string, unknown>): Promise<void> {
    let result;
    try {
      result = await runConsumerAction(consumer.id, actionId, body);
    } catch (err) {
      toast.error(`Run failed: ${err}`);
      return;
    }
    if (!result.ok || !result.body.ok || !result.body.job_id) {
      const hint = result.body.existing_job_id ? ` (existing job: ${result.body.existing_job_id})` : '';
      toast.error(`${consumer.display_name} failed: ${result.body.error ?? result.status}${hint}`);
      return;
    }
    activeJob = {
      id: result.body.job_id,
      kind: 'consumer',
      label: `${consumer.id}__${actionId}`,
      state: 'running',
      cmd: result.body.cmd ?? [],
      started_at: Date.now(),
      finished_at: null,
      exit_code: null,
      stdout: '',
      stderr: '',
    };
    startPolling();
    toast.success(`${consumer.display_name}: ${actionId} started`, {
      description: `job ${result.body.job_id}`,
      duration: 3000,
    });
  }

  async function onCancelJob(): Promise<void> {
    if (!activeJob) return;
    try {
      const after = await cancelExtractJob(activeJob.id);
      if (after) activeJob = after;
    } catch (err) {
      toast.error(`Cancel failed: ${err}`);
    }
  }

  function onDismissJob(): void {
    activeJob = null;
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }
</script>

<section class="flex flex-1 min-w-0 flex-col">
  {#if loading}
    <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-xs">
      Loading consumers…
    </div>
  {:else if loadError}
    <div class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
      <strong>Failed to load consumers:</strong>
      <code class="font-mono text-xs">{loadError}</code>
    </div>
  {:else if consumers.length === 0}
    <div class="text-muted-foreground flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-xs">
      <strong>No downstream consumers installed.</strong>
      <p class="m-0 max-w-[60ch]">
        Install a consumer package (one that registers a descriptor under the
        <code>wows_model_export.consumers</code> entry-point group) into the same
        virtualenv as <code>wows-model-export</code>, then restart
        <code>wows-webview-serve</code>.
      </p>
    </div>
  {:else}
    <div class="flex-1 overflow-auto">
      <div class="space-y-4 p-4">
        {#each consumers as c (c.id)}
          <div class="border-border bg-card/40 rounded border p-4">
            <div class="mb-3 flex items-baseline gap-3">
              <h2 class="text-foreground text-sm font-semibold">{c.display_name}</h2>
              <code class="text-muted-foreground font-mono text-[11px]">{c.id}</code>
            </div>
            {#if c.description}
              <p class="text-muted-foreground mb-3 text-xs">{c.description}</p>
            {/if}
            <div class="space-y-3">
              {#each c.actions as a (a.id)}
                <ConsumerActionForm
                  action={a}
                  {extractedShips}
                  disabled={activeJob?.state === 'running'}
                  onSubmit={(body) => runAction(c, a.id, body)}
                />
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </div>
    {#if activeJob}
      <ExtractJobPanel
        job={activeJob}
        matchesActiveLabel={false}
        onCancel={onCancelJob}
        onDismiss={onDismissJob}
      />
    {/if}
  {/if}
</section>
