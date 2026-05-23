<script lang="ts">
  // Persistent extract queue panel. Lives below the job panel on the
  // Extract page. Three sections:
  //
  //   1. Pending  — items waiting in worker order. Per-row remove +
  //                 up/down reorder buttons.
  //   2. Running  — at most one item; live "now extracting <label>"
  //                 strip. The underlying job's stdout still surfaces
  //                 through the existing ExtractJobPanel (the queue
  //                 worker spawns via the same jobs.spawn_job path the
  //                 direct "Run extract" button uses).
  //   3. Completed — terminal items kept until clear-completed.
  //
  // Polls `/api/queue` on its own interval. The parent route subscribes
  // via `onSnapshot` so it can also kick the activeJob polling whenever
  // a new running_job_id appears.

  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import {
    clearCompletedQueue,
    dropQueueItem,
    fetchQueue,
    pauseQueue,
    reorderQueue,
    resumeQueue,
    type QueueItem,
    type QueueSnapshot,
    type QueueStatus,
  } from '$lib/api';
  import { formatElapsed } from '$lib/extract/labels';

  interface Props {
    /** Notified each time the panel reloads its snapshot. Parent uses
     *  this to discover a freshly-spawned running job (running_job_id)
     *  and start its job-polling loop. */
    onSnapshot?: (snap: QueueSnapshot) => void;
    /** Cadence at which we re-fetch `/api/queue`. Default 1.5 s — same
     *  as the existing job poll. */
    pollIntervalMs?: number;
  }

  const { onSnapshot, pollIntervalMs = 1500 }: Props = $props();

  let snap = $state<QueueSnapshot | null>(null);
  let loadError = $state<string | null>(null);
  // Per-row "in-flight mutation" flags. Disables the row's buttons so
  // the user can't double-click while a DELETE / reorder is pending.
  let mutating = $state<Record<string, boolean>>({});
  // Pause toggle in-flight flag.
  let pauseBusy = $state(false);
  let clearBusy = $state(false);

  let pollHandle: ReturnType<typeof setInterval> | null = null;

  async function reload(): Promise<void> {
    try {
      const next = await fetchQueue();
      snap = next;
      loadError = null;
      onSnapshot?.(next);
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  }

  onMount(() => {
    void reload();
    pollHandle = setInterval(reload, pollIntervalMs);
    return () => {
      if (pollHandle !== null) {
        clearInterval(pollHandle);
        pollHandle = null;
      }
    };
  });

  // Derived per-section lists. We rely on the server's canonical
  // ordering (pending first, then running, then completed).
  const pending = $derived(snap ? snap.items.filter((it) => it.status === 'pending') : []);
  const running = $derived(snap ? snap.items.filter((it) => it.status === 'running') : []);
  const completed = $derived(
    snap ? snap.items.filter((it) => isTerminal(it.status)) : [],
  );

  function isTerminal(s: QueueStatus): boolean {
    return s === 'done' || s === 'failed' || s === 'cancelled';
  }

  const statusBadgeClass: Record<QueueStatus, string> = {
    pending: 'bg-secondary text-muted-foreground',
    running: 'bg-sky-900/40 text-sky-200',
    done: 'bg-emerald-900/40 text-emerald-200',
    failed: 'bg-rose-900/40 text-rose-200',
    cancelled: 'bg-amber-900/40 text-amber-200',
  };

  // Tick "elapsed" for the running row even between server polls.
  let now = $state(Date.now());
  onMount(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  async function onDrop(item: QueueItem) {
    if (mutating[item.queue_id]) return;
    mutating[item.queue_id] = true;
    try {
      await dropQueueItem(item.queue_id);
      await reload();
    } catch (err) {
      toast.error('Drop failed', { description: String(err) });
    } finally {
      mutating[item.queue_id] = false;
    }
  }

  async function onMove(item: QueueItem, direction: -1 | 1) {
    if (mutating[item.queue_id]) return;
    const ids = untrack(() => pending).map((it) => it.queue_id);
    const idx = ids.indexOf(item.queue_id);
    const target = idx + direction;
    if (idx < 0 || target < 0 || target >= ids.length) return;
    const next = [...ids];
    [next[idx], next[target]] = [next[target], next[idx]];
    mutating[item.queue_id] = true;
    try {
      await reorderQueue(next);
      await reload();
    } catch (err) {
      toast.error('Reorder failed', { description: String(err) });
    } finally {
      mutating[item.queue_id] = false;
    }
  }

  async function onTogglePause() {
    if (pauseBusy || !snap) return;
    pauseBusy = true;
    try {
      if (snap.paused) await resumeQueue();
      else await pauseQueue();
      await reload();
    } catch (err) {
      toast.error('Pause toggle failed', { description: String(err) });
    } finally {
      pauseBusy = false;
    }
  }

  async function onClearCompleted() {
    if (clearBusy || completed.length === 0) return;
    clearBusy = true;
    try {
      const res = await clearCompletedQueue();
      if (res.dropped) {
        toast.success(`Cleared ${res.dropped} completed item${res.dropped === 1 ? '' : 's'}`);
      }
      await reload();
    } catch (err) {
      toast.error('Clear failed', { description: String(err) });
    } finally {
      clearBusy = false;
    }
  }

  function fmtPerm(it: QueueItem): string {
    if (it.permoflage === null || it.permoflage === undefined) return 'auto';
    return it.permoflage;
  }

  function fmtEnqueued(ms: number): string {
    const d = new Date(ms);
    return d.toLocaleTimeString();
  }
</script>

<section class="border-border border-t px-4 py-3">
  <header class="mb-2 flex items-baseline justify-between gap-3">
    <h3 class="text-foreground m-0 text-xs font-semibold uppercase tracking-wide">
      Extract queue
    </h3>
    {#if snap}
      <span class="text-muted-foreground text-[11px]">
        {snap.pending_count} pending
        {#if running.length}
          · 1 running
        {/if}
        {#if snap.completed_count}
          · {snap.completed_count} completed
        {/if}
        {#if snap.paused}
          <span class="text-amber-400 ml-1">· paused</span>
        {/if}
      </span>
    {/if}
  </header>

  {#if loadError}
    <div
      class="text-destructive rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs"
    >
      Queue load failed: <code>{loadError}</code>
    </div>
  {:else if !snap}
    <div class="text-muted-foreground text-[11px]">Loading queue…</div>
  {:else if snap.items.length === 0}
    <p class="text-muted-foreground m-0 text-[11px]">
      Empty. Use <strong>+ Queue</strong> in the panel above to add ships;
      the worker runs them one at a time in the background.
    </p>
  {:else}
    <!-- Header controls -->
    <div class="mb-2 flex flex-wrap items-center gap-3">
      <button
        type="button"
        disabled={pauseBusy}
        onclick={onTogglePause}
        class="border border-border hover:bg-popover/60 rounded px-2 py-0.5 text-[11px] font-medium disabled:cursor-not-allowed disabled:opacity-60"
      >
        {pauseBusy
          ? 'Updating…'
          : snap.paused
            ? 'Resume worker'
            : 'Pause after current'}
      </button>
      {#if completed.length > 0}
        <button
          type="button"
          disabled={clearBusy}
          onclick={onClearCompleted}
          class="text-muted-foreground hover:text-foreground text-[11px] underline disabled:cursor-not-allowed disabled:opacity-60"
        >
          {clearBusy ? 'Clearing…' : `Clear ${completed.length} completed`}
        </button>
      {/if}
    </div>

    <!-- Running section -->
    {#if running.length > 0}
      {@const item = running[0]}
      <div
        class="rounded border border-sky-900/40 bg-sky-950/20 px-2.5 py-1.5 text-xs mb-2"
      >
        <div class="flex items-baseline justify-between gap-3">
          <div class="flex items-baseline gap-2 min-w-0">
            <span class="rounded px-1.5 py-[1px] text-[10px] {statusBadgeClass[item.status]}">
              running
            </span>
            <code class="font-mono truncate">{item.label}</code>
            <span class="text-muted-foreground text-[11px] truncate">
              {item.vehicle} · perm={fmtPerm(item)}
            </span>
          </div>
          <span class="text-muted-foreground text-[11px] flex-shrink-0">
            {formatElapsed(item.started_at ?? item.enqueued_at, item.finished_at ?? now)}
          </span>
        </div>
      </div>
    {/if}

    <!-- Pending section -->
    {#if pending.length > 0}
      <ol class="m-0 flex flex-col gap-0.5 list-none p-0">
        {#each pending as item, i (item.queue_id)}
          {@const busy = mutating[item.queue_id]}
          <li
            class="flex items-center gap-2 rounded px-2 py-1 text-xs hover:bg-popover/40"
          >
            <span class="text-muted-foreground w-5 text-right text-[11px] font-mono">
              {i + 1}.
            </span>
            <code class="font-mono truncate flex-1 min-w-0">{item.label}</code>
            <span class="text-muted-foreground text-[11px] flex-shrink-0">
              {item.vehicle} · perm={fmtPerm(item)}
              {#if !item.build_library}
                · no-lib
              {/if}
            </span>
            <span class="text-muted-foreground text-[10px] flex-shrink-0">
              {fmtEnqueued(item.enqueued_at)}
            </span>
            <div class="flex items-center gap-0.5 flex-shrink-0">
              <button
                type="button"
                title="Move up"
                disabled={busy || i === 0}
                onclick={() => onMove(item, -1)}
                class="text-muted-foreground hover:text-foreground rounded px-1 disabled:opacity-30 disabled:cursor-not-allowed"
                aria-label="Move up"
              >
                ↑
              </button>
              <button
                type="button"
                title="Move down"
                disabled={busy || i === pending.length - 1}
                onclick={() => onMove(item, 1)}
                class="text-muted-foreground hover:text-foreground rounded px-1 disabled:opacity-30 disabled:cursor-not-allowed"
                aria-label="Move down"
              >
                ↓
              </button>
              <button
                type="button"
                title="Remove from queue"
                disabled={busy}
                onclick={() => onDrop(item)}
                class="text-muted-foreground hover:text-rose-300 rounded px-1.5 text-[11px] disabled:opacity-30 disabled:cursor-not-allowed"
                aria-label="Remove from queue"
              >
                ✕
              </button>
            </div>
          </li>
        {/each}
      </ol>
    {/if}

    <!-- Completed section -->
    {#if completed.length > 0}
      <details class="mt-2 text-xs">
        <summary class="cursor-pointer text-muted-foreground select-none text-[11px] hover:text-foreground">
          {completed.length} completed
        </summary>
        <ol class="m-0 mt-1 flex flex-col gap-0.5 list-none p-0">
          {#each completed as item (item.queue_id)}
            {@const busy = mutating[item.queue_id]}
            <li
              class="flex items-center gap-2 rounded px-2 py-1 hover:bg-popover/40"
            >
              <span class="rounded px-1.5 py-[1px] text-[10px] flex-shrink-0 {statusBadgeClass[item.status]}">
                {item.status}
              </span>
              <code class="font-mono truncate flex-1 min-w-0">{item.label}</code>
              {#if item.error}
                <span
                  class="text-rose-300 text-[11px] truncate flex-shrink min-w-0"
                  title={item.error}
                >
                  {item.error}
                </span>
              {/if}
              <span class="text-muted-foreground text-[10px] flex-shrink-0">
                {formatElapsed(item.started_at ?? item.enqueued_at, item.finished_at)}
              </span>
              <button
                type="button"
                title="Remove from history"
                disabled={busy}
                onclick={() => onDrop(item)}
                class="text-muted-foreground hover:text-rose-300 rounded px-1.5 text-[11px] disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
                aria-label="Remove from history"
              >
                ✕
              </button>
            </li>
          {/each}
        </ol>
      </details>
    {/if}
  {/if}
</section>
