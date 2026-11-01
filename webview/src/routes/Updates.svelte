<script lang="ts">
  // Updates route: what a game update changed, and which exports it made
  // stale. Backed by `/api/builds` (see compose/build_tracking.py).
  //
  // Top: the installed builds, saved manifests and cache stamps. Middle:
  // the diff between the newest two saved manifests (or any pair). Bottom:
  // every ship / map export with its state against the current build, the
  // inputs that changed, and a re-export action that replays the original
  // arguments as a background job.

  import { Button } from '$lib/components/ui/button';
  import {
    fetchBuildDiff,
    fetchBuildsStatus,
    runBuildSnapshot,
    runReexport,
    waitForJob,
    type BuildDiff,
    type BuildsStatus,
    type ExportState,
    type ExportStatus,
  } from '$lib/api';
  import { invalidateShips } from '$lib/api/ships';
  import { toast } from 'svelte-sonner';

  let { active }: { active: boolean } = $props();

  let status = $state<BuildsStatus | null>(null);
  let diff = $state<BuildDiff | null>(null);
  let loadError = $state<string | null>(null);
  let diffError = $state<string | null>(null);
  let loading = $state(false);
  let loaded = $state(false);
  let diffFrom = $state<number | null>(null);
  let diffTo = $state<number | null>(null);

  /** In-flight job id; disables every action button. */
  let jobRunning = $state<string | null>(null);

  let stateFilter = $state<'attention' | ExportState | 'all'>('attention');
  let kindFilter = $state<'all' | 'ship' | 'map'>('all');
  let selected = $state<Record<string, boolean>>({});
  let expanded = $state<Record<string, boolean>>({});
  let includeUntracked = $state(false);
  let replaySkins = $state(true);
  let buildLibrary = $state(true);

  const exports = $derived(status?.exports ?? []);
  const shown = $derived(
    exports.filter(
      (e) =>
        (kindFilter === 'all' || e.kind === kindFilter) &&
        (stateFilter === 'all' ||
          (stateFilter === 'attention' ? e.state !== 'current' : e.state === stateFilter)),
    ),
  );
  const selectedNames = $derived(
    exports.filter((e) => selected[key(e)]).map((e) => e.name),
  );
  const staleCount = $derived(exports.filter((e) => e.state === 'stale').length);
  const exportedMaps = $derived(
    new Set(exports.filter((e) => e.kind === 'map').map((e) => e.name)),
  );

  function key(e: ExportStatus): string {
    return `${e.kind}:${e.name}`;
  }

  async function load() {
    loading = true;
    loadError = null;
    try {
      status = await fetchBuildsStatus();
      loaded = true;
      const stored = status.stored_manifests;
      if (diffTo === null && stored.length >= 2) {
        diffTo = stored[stored.length - 1];
        diffFrom = stored[stored.length - 2];
      }
      await loadDiff();
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function loadDiff() {
    diffError = null;
    if (!status || status.stored_manifests.length < 2 || diffFrom === null || diffTo === null) {
      diff = null;
      return;
    }
    try {
      diff = await fetchBuildDiff(diffFrom, diffTo);
    } catch (err) {
      diff = null;
      diffError = err instanceof Error ? err.message : String(err);
    }
  }

  $effect(() => {
    if (active && !loaded && !loading) void load();
  });

  async function runJob(label: string, start: () => Promise<{ job_id: string }>) {
    if (jobRunning) return;
    const tid = toast.loading(`${label}…`, { duration: Number.POSITIVE_INFINITY });
    try {
      const { job_id } = await start();
      jobRunning = job_id;
      const final = await waitForJob(job_id, (j) => {
        const tail = (j.stdout || '').split('\n').filter((s) => s.trim()).slice(-1)[0] ?? '';
        toast.loading(`${label}…`, {
          id: tid,
          description: tail.slice(0, 160),
          duration: Number.POSITIVE_INFINITY,
        });
      });
      const tail = (final.stdout || final.stderr || '')
        .split('\n')
        .filter((s) => s.trim())
        .slice(-1)[0];
      if (final.state === 'done') {
        toast.success(`${label} finished`, { id: tid, description: tail, duration: 8000 });
      } else {
        toast.error(`${label} ${final.state}`, {
          id: tid,
          description: (final.stderr || tail || '').split('\n').filter(Boolean).slice(-2).join('\n'),
          duration: Number.POSITIVE_INFINITY,
        });
      }
    } catch (err) {
      toast.error(`${label} failed to start`, {
        id: tid,
        description: err instanceof Error ? err.message : String(err),
        duration: 10000,
      });
    } finally {
      jobRunning = null;
      invalidateShips();
      selected = {};
      await load();
    }
  }

  function snapshot(refresh = false) {
    void runJob(refresh ? 'Rebuild manifest' : 'Snapshot build', () => runBuildSnapshot(refresh));
  }

  function reexportSelected() {
    const names = selectedNames;
    if (!names.length) return;
    if (!confirm(`Re-export ${names.length} export(s)?\n\n${names.join('\n')}`)) return;
    void runJob(`Re-export ${names.length}`, () =>
      runReexport({ names, replay_skins: replaySkins, build_library: buildLibrary }),
    );
  }

  function reexportAll() {
    const n = staleCount + (includeUntracked ? (status?.counts.untracked ?? 0) : 0);
    if (!n) return;
    const what = includeUntracked ? 'stale and untracked' : 'stale';
    if (!confirm(`Re-export all ${n} ${what} export(s)? This can take a long time.`)) return;
    void runJob(`Re-export ${n}`, () =>
      runReexport({
        include_untracked: includeUntracked,
        kind: kindFilter === 'all' ? null : kindFilter,
        replay_skins: replaySkins,
        build_library: buildLibrary,
      }),
    );
  }

  function toggleAllShown(on: boolean) {
    const next = { ...selected };
    for (const e of shown) next[key(e)] = on;
    selected = next;
  }

  const STATE_CLS: Record<ExportState, string> = {
    current: 'bg-emerald-500/15 text-emerald-400',
    stale: 'bg-amber-500/15 text-amber-400',
    untracked: 'bg-muted text-muted-foreground',
    error: 'bg-destructive/15 text-destructive',
  };

  function topEntries(rec: Record<string, number>, n = 12): [string, number][] {
    return Object.entries(rec)
      .sort((a, b) => b[1] - a[1])
      .slice(0, n);
  }

  const labelCls = 'text-muted-foreground text-[11px] uppercase tracking-wider font-semibold';
  const selectCls =
    'h-7 rounded border border-border bg-popover px-1.5 text-xs text-foreground focus:outline-none';
</script>

<section class="flex flex-1 min-w-0 flex-col overflow-y-auto">
  <header class="bg-card border-border flex flex-none items-center gap-4 border-b px-5 py-3">
    <h2 class="m-0 text-sm font-semibold">Updates</h2>
    <span class="text-muted-foreground text-[11px]">
      What each game update changed, and which exports need re-running.
    </span>
    <Button
      size="sm"
      variant="ghost"
      class="ml-auto"
      disabled={loading || jobRunning !== null}
      onclick={() => void load()}
    >
      {loading ? 'Loading…' : 'Refresh'}
    </Button>
  </header>

  <div class="flex flex-col gap-6 p-5 max-w-5xl">
    {#if loadError}
      <div
        class="text-destructive rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs"
      >
        <strong>Failed to load build status:</strong>
        <code class="ml-1">{loadError}</code>
      </div>
    {:else if !status}
      <div class="text-muted-foreground text-xs">Loading…</div>
    {:else}
      <!-- ── Builds + caches ─────────────────────────────────────── -->
      <section class="flex flex-col gap-2">
        <div class={labelCls}>Game builds</div>
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0"
        >
          <dt>Current build</dt>
          <dd class="font-mono">{status.current_build ?? '—'}</dd>
          <dt>Installed (idx)</dt>
          <dd class="font-mono">{status.builds_with_idx.join(', ') || '—'}</dd>
          <dt>Saved manifests</dt>
          <dd class="font-mono">{status.stored_manifests.join(', ') || '—'}</dd>
          <dt>Caches</dt>
          <dd class="flex flex-wrap gap-1.5">
            {#each status.caches as c (c.name)}
              <span
                class="rounded px-1.5 py-0.5 font-mono text-[11px] {c.current
                  ? 'bg-emerald-500/10 text-emerald-400'
                  : c.exists
                    ? 'bg-amber-500/10 text-amber-400'
                    : 'bg-muted text-muted-foreground'}"
                title={c.current
                  ? 'Made from the current build'
                  : c.exists
                    ? `Made from ${c.build ?? 'an unknown (unstamped) build'}; rebuilt on next use`
                    : 'Not built yet'}
              >
                {c.name}{c.exists && !c.current ? ` · ${c.build ?? 'old'}` : ''}
              </span>
            {/each}
          </dd>
        </dl>
        {#if status.needs_snapshot}
          <div
            class="flex items-center gap-3 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs"
          >
            <span>
              Build {status.current_build} has no saved manifest yet. Snapshot it (about a
              minute) to diff it and check exports against it.
            </span>
            <Button size="sm" disabled={jobRunning !== null} onclick={() => snapshot()}>
              Snapshot build
            </Button>
          </div>
        {:else}
          <p class="text-muted-foreground text-[11px] m-0">
            The launcher deletes a build's idx files one update later, so the current build's
            manifest is saved automatically the first time it's needed.
            <button
              class="underline hover:text-foreground disabled:opacity-50"
              disabled={jobRunning !== null}
              onclick={() => snapshot(true)}>Rebuild it</button
            > after a toolkit hashing change.
          </p>
        {/if}
      </section>

      <!-- ── Diff ────────────────────────────────────────────────── -->
      <section class="flex flex-col gap-3 border-t border-border pt-5">
        <div class="flex items-center gap-2">
          <div class={labelCls}>What changed</div>
          {#if status.stored_manifests.length >= 2}
            <select
              class={selectCls}
              bind:value={diffFrom}
              onchange={() => void loadDiff()}
              aria-label="Older build"
            >
              {#each status.stored_manifests as b (b)}
                <option value={b}>{b}</option>
              {/each}
            </select>
            <span class="text-muted-foreground text-xs">→</span>
            <select
              class={selectCls}
              bind:value={diffTo}
              onchange={() => void loadDiff()}
              aria-label="Newer build"
            >
              {#each status.stored_manifests as b (b)}
                <option value={b}>{b}</option>
              {/each}
            </select>
          {/if}
        </div>

        {#if status.stored_manifests.length < 2}
          <p class="text-muted-foreground text-xs m-0">
            Only one build has a saved manifest. The diff appears after the next game update.
          </p>
        {:else if diffError}
          <p class="text-destructive text-xs m-0">{diffError}</p>
        {:else if diff}
          {@const s = diff.summary}
          <table
            class="self-start text-xs w-auto border-collapse [&_td]:px-3 [&_td]:py-0.5 [&_th]:px-3"
          >
            <thead>
              <tr class="text-muted-foreground text-[11px]">
                <th class="text-left font-normal"></th>
                <th class="text-right font-normal">added</th>
                <th class="text-right font-normal">removed</th>
                <th class="text-right font-normal">changed</th>
              </tr>
            </thead>
            <tbody class="font-mono">
              {#each [['Game files', s.files], ['assets.bin records', s.records], ['GameParams entities', s.gameparams], ['camouflages.xml entries', s.camouflages]] as const as [label, c] (label)}
                <tr>
                  <td class="font-sans text-muted-foreground">{label}</td>
                  <td class="text-right">{c.added}</td>
                  <td class="text-right">{c.removed}</td>
                  <td class="text-right">{c.changed}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          {#if Object.keys(s.records_changed_by_type).length}
            <p class="text-muted-foreground text-[11px] m-0">
              Records changed by type:
              {topEntries(s.records_changed_by_type, 20)
                .map(([t, n]) => `${t} ${n}`)
                .join(', ')}
            </p>
          {/if}
          {#if diff.incomparable.length}
            <p class="text-amber-400 text-[11px] m-0">
              Hashing differs between these manifests; changes not compared for
              {diff.incomparable.join(', ')}. Rebuild the older manifest while its build is still
              installed (<code>wows-builds snapshot --build {diff.from_build} --refresh</code>).
            </p>
          {/if}
          {#if diff.format_signals.length}
            <div
              class="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs flex flex-col gap-1"
            >
              <strong class="text-amber-400">Possible format changes</strong>
              <span class="text-muted-foreground text-[11px]">
                Nearly every file of these kinds changed. Check the toolkit parser before trusting
                new exports.
              </span>
              <span class="font-mono">
                {diff.format_signals.map((f) => `${f.kind} ${f.changed}/${f.total}`).join(' · ')}
              </span>
            </div>
          {/if}

          <div class="grid gap-4 md:grid-cols-2">
            <details class="text-xs" open>
              <summary class="cursor-pointer">
                Maps touched ({Object.keys(diff.spaces).length})
              </summary>
              <ul class="m-0 mt-1 max-h-64 overflow-y-auto pl-0 list-none font-mono text-[11px]">
                {#each Object.entries(diff.spaces) as [name, c] (name)}
                  <li class={exportedMaps.has(name) ? 'text-amber-400' : ''}>
                    {name}
                    <span class="text-muted-foreground">
                      {Object.entries(c)
                        .map(([k, n]) => `${k} ${n}`)
                        .join(', ')}
                    </span>
                    {#if exportedMaps.has(name)}<span> · exported</span>{/if}
                  </li>
                {/each}
              </ul>
            </details>
            <details class="text-xs" open>
              <summary class="cursor-pointer">
                Ship model dirs touched ({Object.keys(diff.ship_model_dirs).length})
              </summary>
              <ul class="m-0 mt-1 max-h-64 overflow-y-auto pl-0 list-none font-mono text-[11px]">
                {#each topEntries(diff.ship_model_dirs, 500) as [name, n] (name)}
                  <li>{name} <span class="text-muted-foreground">{n}</span></li>
                {/each}
              </ul>
            </details>
            <details class="text-xs">
              <summary class="cursor-pointer">
                Particle records changed or added ({diff.particles.total})
              </summary>
              <ul class="m-0 mt-1 max-h-64 overflow-y-auto pl-0 list-none font-mono text-[11px]">
                {#each diff.particles.items as p (p)}<li>{p}</li>{/each}
              </ul>
            </details>
            <details class="text-xs">
              <summary class="cursor-pointer">
                GameParams: {diff.gameparams.added.total} added,
                {diff.gameparams.changed.total} changed, {diff.gameparams.removed.total} removed
              </summary>
              <div class="mt-1 max-h-64 overflow-y-auto font-mono text-[11px]">
                {#each ['added', 'changed', 'removed'] as const as k (k)}
                  {#if diff.gameparams[k].total}
                    <div class="text-muted-foreground mt-1">{k}</div>
                    {#each diff.gameparams[k].items as id (id)}<div>{id}</div>{/each}
                  {/if}
                {/each}
              </div>
            </details>
            <details class="text-xs">
              <summary class="cursor-pointer">
                Files changed by area ({s.files.changed})
              </summary>
              <ul class="m-0 mt-1 pl-0 list-none font-mono text-[11px]">
                {#each topEntries(diff.by_area.files_changed, 40) as [area, n] (area)}
                  <li>{area} <span class="text-muted-foreground">{n}</span></li>
                {/each}
              </ul>
            </details>
            <details class="text-xs">
              <summary class="cursor-pointer">
                camouflages.xml: {diff.camouflages.added.total} added,
                {diff.camouflages.changed.total} changed
              </summary>
              <div class="mt-1 max-h-64 overflow-y-auto font-mono text-[11px]">
                {#each ['added', 'changed', 'removed'] as const as k (k)}
                  {#if diff.camouflages[k].total}
                    <div class="text-muted-foreground mt-1">{k}</div>
                    {#each diff.camouflages[k].items as id (id)}<div>{id}</div>{/each}
                  {/if}
                {/each}
              </div>
            </details>
          </div>
        {:else}
          <p class="text-muted-foreground text-xs m-0">Loading diff…</p>
        {/if}
      </section>

      <!-- ── Exports ─────────────────────────────────────────────── -->
      <section class="flex flex-col gap-3 border-t border-border pt-5">
        <div class="flex flex-wrap items-center gap-2">
          <div class={labelCls}>Exports</div>
          <span class="text-muted-foreground text-xs">
            {Object.entries(status.counts)
              .map(([k, n]) => `${n} ${k}`)
              .join(' · ') || (status.needs_snapshot ? 'snapshot needed' : 'none')}
          </span>
          <select class="{selectCls} ml-auto" bind:value={stateFilter} aria-label="State filter">
            <option value="attention">Needs attention</option>
            <option value="stale">Stale</option>
            <option value="untracked">Untracked</option>
            <option value="current">Current</option>
            <option value="all">All</option>
          </select>
          <select class={selectCls} bind:value={kindFilter} aria-label="Kind filter">
            <option value="all">Ships + maps</option>
            <option value="ship">Ships</option>
            <option value="map">Maps</option>
          </select>
        </div>

        <p class="text-muted-foreground text-[11px] m-0 max-w-[80ch]">
          An export is <strong>stale</strong> when a game file, assets.bin record, GameParams
          entity or camouflage entry it read has changed since it was made.
          <strong>Untracked</strong> exports were made before tracking existed; re-export once to
          start tracking them.
        </p>

        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          <label class="inline-flex items-center gap-1.5">
            <input type="checkbox" bind:checked={replaySkins} disabled={jobRunning !== null} />
            Replay skin packs
          </label>
          <label class="inline-flex items-center gap-1.5">
            <input type="checkbox" bind:checked={buildLibrary} disabled={jobRunning !== null} />
            Build accessory library at the end
          </label>
          <label class="inline-flex items-center gap-1.5">
            <input
              type="checkbox"
              bind:checked={includeUntracked}
              disabled={jobRunning !== null}
            />
            "Re-export all" includes untracked
          </label>
          <div class="ml-auto flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={jobRunning !== null || selectedNames.length === 0}
              onclick={reexportSelected}
            >
              Re-export selected ({selectedNames.length})
            </Button>
            <Button
              size="sm"
              disabled={jobRunning !== null ||
                staleCount + (includeUntracked ? (status.counts.untracked ?? 0) : 0) === 0}
              onclick={reexportAll}
            >
              {jobRunning !== null ? 'Running…' : 'Re-export all'}
            </Button>
          </div>
        </div>

        {#if status.exports === null}
          <p class="text-muted-foreground text-xs m-0">
            Export states appear once the current build has a manifest.
          </p>
        {:else if shown.length === 0}
          <p class="text-muted-foreground text-xs m-0">Nothing here.</p>
        {:else}
          <table class="w-full text-xs border-collapse">
            <thead>
              <tr class="text-muted-foreground text-[11px] text-left border-b border-border">
                <th class="w-6 py-1">
                  <input
                    type="checkbox"
                    aria-label="Select all shown"
                    checked={shown.every((e) => selected[key(e)])}
                    onchange={(ev) => toggleAllShown((ev.currentTarget as HTMLInputElement).checked)}
                  />
                </th>
                <th class="font-normal">Kind</th>
                <th class="font-normal">Name</th>
                <th class="font-normal">State</th>
                <th class="font-normal">Made on</th>
                <th class="font-normal">Exported</th>
                <th class="font-normal text-right">Changed inputs</th>
              </tr>
            </thead>
            <tbody>
              {#each shown as e (key(e))}
                {@const k = key(e)}
                <tr class="border-b border-border/50 hover:bg-popover/40">
                  <td class="py-1">
                    <input type="checkbox" bind:checked={selected[k]} aria-label="Select {e.name}" />
                  </td>
                  <td class="text-muted-foreground">{e.kind}</td>
                  <td class="font-mono">
                    {#if e.changed_count || e.note}
                      <button
                        class="hover:underline text-left"
                        onclick={() => (expanded = { ...expanded, [k]: !expanded[k] })}
                      >
                        {expanded[k] ? '▾' : '▸'} {e.name}
                      </button>
                    {:else}
                      <span class="pl-3">{e.name}</span>
                    {/if}
                  </td>
                  <td>
                    <span class="rounded px-1.5 py-0.5 text-[11px] {STATE_CLS[e.state]}">
                      {e.state}
                    </span>
                    {#if e.incomparable.length}
                      <span class="text-amber-400 text-[11px]" title={e.note}>*</span>
                    {/if}
                  </td>
                  <td class="font-mono text-muted-foreground">{e.build ?? '—'}</td>
                  <td class="text-muted-foreground">{e.exported_at?.slice(0, 10) ?? '—'}</td>
                  <td class="text-right font-mono">{e.changed_count || ''}</td>
                </tr>
                {#if expanded[k]}
                  <tr class="border-b border-border/50">
                    <td></td>
                    <td colspan="6" class="pb-2">
                      {#if e.note}
                        <p class="text-muted-foreground text-[11px] m-0 mb-1">{e.note}</p>
                      {/if}
                      {#each Object.entries(e.changed) as [section, keys] (section)}
                        <div class="text-[11px]">
                          <span class="text-muted-foreground">{section} changed:</span>
                          <span class="font-mono break-all">{keys?.join(', ')}</span>
                        </div>
                      {/each}
                      {#each Object.entries(e.removed) as [section, keys] (section)}
                        <div class="text-[11px]">
                          <span class="text-muted-foreground">{section} removed:</span>
                          <span class="font-mono break-all">{keys?.join(', ')}</span>
                        </div>
                      {/each}
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        {/if}
      </section>
    {/if}
  </div>
</section>
