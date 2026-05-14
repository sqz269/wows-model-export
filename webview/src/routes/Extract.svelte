<script lang="ts">
  // Extract route: read-only browser for picking a Vehicle + permoflage,
  // previewing the resolved `wows-ingest-ship` invocation, and kicking
  // off the actual run (or a `wows-ingest-skin-pack` for texture-only
  // permoflages on an already-extracted ship).
  //
  // Layout: three columns
  //   1. VehiclePicker         (search + filters + grouped ship list)
  //   2. PermoflagePicker      (topology badges + "Base ship" synthetic row)
  //   3. ExtractPreview etc.   (header + warnings + run / job / skin panels)
  //
  // Server-side: /api/extract/snapshot pays a ~30 s GameParams parse on
  // cold cache, then serves the picker payload from memory. Job state
  // (stdout / stderr / exit code) lives in the dev-server process so a
  // page refresh during a long run re-attaches via /api/extract/jobs.

  import { onMount, untrack } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { navigate } from '$lib/router';
  import { navState, settingsHref } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import {
    cancelExtractJob,
    fetchExtractJob,
    fetchExtractJobs,
    fetchExtractSnapshot,
    fetchExtractedShips,
    fetchGameparamsStatus,
    runExtract,
    runSkinPack,
  } from '$lib/api';
  import { formatRelativeTime, suggestedLabel } from '$lib/extract/labels';
  import type {
    ExtractedShip,
    GpStatus,
    JobState,
    PeculiarityLabel,
    Permoflage,
    RunOptions,
    SkinPackForm,
    Vehicle,
  } from '$lib/types/extract';
  import VehiclePicker from '$components/VehiclePicker.svelte';
  import PermoflagePicker from '$components/PermoflagePicker.svelte';
  import ExtractPreview from '$components/ExtractPreview.svelte';
  import ExtractRunPanel from '$components/ExtractRunPanel.svelte';
  import ExtractSkinPackForm from '$components/ExtractSkinPackForm.svelte';
  import ExtractJobPanel from '$components/ExtractJobPanel.svelte';

  interface Props {
    /** Vehicle id from the URL (`#/extract/<top_key_or_param_index>`),
     *  or null when the user is on a different page. App.svelte routing
     *  domain-types this so an asset_id from `#/asset/<id>` can never
     *  arrive here. */
    vehicleId: string | null;
    /** True iff this is the route the user is looking at. Page-local
     *  keydown listeners short-circuit when false. */
    active: boolean;
  }

  const { vehicleId, active }: Props = $props();

  // Server state.
  let vehicles = $state<Vehicle[]>([]);
  let permoflagesByVehicle = $state<Map<string, Permoflage[]>>(new Map());
  let peculiarityLabels = $state<Record<string, PeculiarityLabel>>({});
  let gpStatus = $state<GpStatus | null>(null);
  let extractedShips = $state<ExtractedShip[]>([]);
  let loadError = $state<string | null>(null);
  let loading = $state(true);

  // Selection state. `selectedVehicle` adopts the URL claim on mount /
  // route change, but is sticky against `vehicleId` going null so a tab
  // switch (away then back via the topnav) preserves the picker state.
  let selectedVehicle = $state<Vehicle | null>(null);
  let selectedPermoflage = $state<Permoflage | null>(null);

  // Active job (running / done / failed / cancelled). Stays populated
  // until the user dismisses the panel.
  let activeJob = $state<JobState | null>(null);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  // Picker ref for the `/` shortcut.
  let pickerRef: VehiclePicker | null = $state(null);

  const activePermoflages = $derived(
    selectedVehicle ? permoflagesByVehicle.get(selectedVehicle.top_key) ?? [] : [],
  );

  const suggested = $derived(selectedVehicle ? suggestedLabel(selectedVehicle, selectedPermoflage) : '');

  // ── Mount: parallel fetches + restore any in-flight job ───────────────
  onMount(() => {
    void (async () => {
      try {
        const [gp, snap, ships] = await Promise.all([
          fetchGameparamsStatus().catch((err) => ({
            exists: false,
            path: '?',
            hint: String(err),
          })) as Promise<GpStatus>,
          fetchExtractSnapshot(),
          fetchExtractedShips().catch(() => [] as ExtractedShip[]),
        ]);
        gpStatus = gp;
        vehicles = snap.vehicles ?? [];
        const map = new Map<string, Permoflage[]>();
        const perm = snap.permoflages_by_vehicle ?? {};
        for (const [k, v] of Object.entries(perm)) map.set(k, v);
        permoflagesByVehicle = map;
        peculiarityLabels = snap.peculiarity_labels ?? {};
        extractedShips = ships;
        if (snap.error) loadError = snap.error;
      } catch (err) {
        loadError = err instanceof Error ? err.message : String(err);
      } finally {
        loading = false;
      }
      // Restore any in-flight job from the dev server. State lives in
      // the process memory of vite — a page refresh loses our client-side
      // tracker but the subprocess keeps running and stdout/stderr keep
      // accumulating, so re-attaching is the right behaviour.
      await restoreActiveJob();
    })();

    const onKey = (e: KeyboardEvent) => {
      if (!active) return;
      if (hasModifier(e)) return;
      if (isTypingContext(e)) return;
      if (e.key === '/') {
        pickerRef?.focusSearch();
        e.preventDefault();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (pollHandle !== null) {
        clearInterval(pollHandle);
        pollHandle = null;
      }
    };
  });

  // Adopt the URL claim into local selection. Sticky: when the user
  // navigates to a different tab, `vehicleId` goes null but the picker
  // state should survive so a return click on the topnav restores it.
  $effect(() => {
    if (!vehicleId) return;
    const id = decodeURIComponent(vehicleId);
    // Match top_key first, then param_index — the URL may carry either
    // (Library-side URLs tend to use top_key, hand-typed deep links
    // sometimes use param_index).
    const target = untrack(() => vehicles).find((v) => v.top_key === id || v.param_index === id);
    if (target && target !== untrack(() => selectedVehicle)) {
      selectedVehicle = target;
      selectedPermoflage = null;
    }
  });

  // Mirror the active selection into nav memory so the topnav link to
  // "Extract" lands back here with the right ship on tab return.
  $effect(() => {
    if (selectedVehicle) navState.lastVehicleId = selectedVehicle.top_key;
  });

  function selectVehicle(v: Vehicle) {
    selectedVehicle = v;
    selectedPermoflage = null;
    navigate(`#/extract/${encodeURIComponent(v.top_key)}`);
  }

  function selectPermoflage(p: Permoflage | null) {
    selectedPermoflage = p;
  }

  // ── Job lifecycle ─────────────────────────────────────────────────────
  async function restoreActiveJob() {
    try {
      const jobs = (await fetchExtractJobs()) ?? [];
      const running = jobs
        .filter((j) => j.state === 'running')
        .sort((a, b) => b.started_at - a.started_at);
      if (running.length === 0) return;
      const head = running[0];
      const detail = await fetchExtractJob(head.id);
      if (!detail) return;
      activeJob = detail;
      if (detail.state === 'running') startPolling();
    } catch (err) {
      console.warn('[extract] restoreActiveJob failed:', err);
    }
  }

  function startPolling() {
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
          // Job finished. If it was an extract, the ship folder may now
          // exist on disk; refresh /api/ships so the skin-pack target
          // dropdown picks up the new entry.
          if (wasRunning && next.kind === 'extract') {
            void fetchExtractedShips()
              .then((ships) => (extractedShips = ships))
              .catch(() => undefined);
          }
        }
      } catch (err) {
        console.warn('[extract] poll failed:', err);
      }
    };
    void tick();
    pollHandle = setInterval(tick, 1500);
  }

  async function onRunExtract(opts: RunOptions) {
    const v = selectedVehicle;
    if (!v) return;
    const p = selectedPermoflage;
    const label = suggestedLabel(v, p);
    const body = {
      vehicle: v.top_key || v.param_index,
      label,
      permoflage: p ? p.exterior_id : 'none',
      build_library: opts.build_library,
      and_publish: opts.and_publish,
      publish_force: opts.publish_force,
    };
    let result;
    try {
      result = await runExtract(body);
    } catch (err) {
      toast.error(`Failed to start extract: ${err}`);
      return;
    }
    if (!result.ok || !result.body.ok || !result.body.job_id) {
      const hint = result.body.existing_job_id
        ? ` (existing job: ${result.body.existing_job_id})`
        : '';
      toast.error(`Run extract failed: ${result.body.error || result.status}${hint}`);
      return;
    }
    activeJob = {
      id: result.body.job_id,
      kind: 'extract',
      label,
      state: 'running',
      cmd: result.body.cmd ?? [],
      started_at: Date.now(),
      finished_at: null,
      exit_code: null,
      stdout: '',
      stderr: '',
    };
    startPolling();
    toast.success(`Extract started: ${label}`, { description: `job ${result.body.job_id}`, duration: 3000 });
  }

  async function onRunSkin(form: SkinPackForm) {
    const body = {
      ship: form.ship,
      source: form.source,
      source_arg: form.source_arg,
      skin_id: form.skin_id,
      exterior_id: form.exterior_id || undefined,
      display_name: form.display_name || undefined,
    };
    let result;
    try {
      result = await runSkinPack(body);
    } catch (err) {
      toast.error(`Skin pack ingest failed: ${err}`);
      return;
    }
    if (!result.ok || !result.body.ok || !result.body.job_id) {
      const hint = result.body.existing_job_id
        ? ` (existing job: ${result.body.existing_job_id})`
        : '';
      toast.error(`Ingest failed: ${result.body.error || result.status}${hint}`);
      return;
    }
    activeJob = {
      id: result.body.job_id,
      kind: 'skin',
      label: `${form.ship}__skin__${form.skin_id}`,
      state: 'running',
      cmd: result.body.cmd ?? [],
      started_at: Date.now(),
      finished_at: null,
      exit_code: null,
      stdout: '',
      stderr: '',
    };
    startPolling();
    toast.success(`Skin pack started: ${form.skin_id}`, {
      description: `target ${form.ship}`,
      duration: 3000,
    });
  }

  async function onCancelJob() {
    if (!activeJob) return;
    try {
      const after = await cancelExtractJob(activeJob.id);
      if (after) activeJob = after;
    } catch (err) {
      toast.error(`Cancel failed: ${err}`);
    }
  }

  function onDismissJob() {
    activeJob = null;
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  // ── Status banner ─────────────────────────────────────────────────────
  const statusText = $derived.by(() => {
    if (!gpStatus) return '';
    if (!gpStatus.exists) return `GameParams cache missing${gpStatus.hint ? ` — ${gpStatus.hint}` : ''}`;
    const ago = gpStatus.mtime ? formatRelativeTime(gpStatus.mtime) : '?';
    return `cache · ${gpStatus.size_mb ?? '?'} MB · ${ago}`;
  });
</script>

<div class="flex flex-1 min-w-0 h-full">
  <VehiclePicker
    bind:this={pickerRef}
    {vehicles}
    {peculiarityLabels}
    activeTopKey={selectedVehicle?.top_key ?? null}
    onSelect={selectVehicle}
  />

  <PermoflagePicker
    vehicle={selectedVehicle}
    permoflages={activePermoflages}
    selectedExteriorId={selectedPermoflage?.exterior_id ?? null}
    onSelect={selectPermoflage}
  />

  <section class="relative flex flex-1 min-w-0 flex-col min-h-0">
    {#if statusText}
      <div
        class="text-muted-foreground border-border bg-card flex flex-none items-center gap-2 border-b px-4 py-1.5 text-[11px] {gpStatus &&
        !gpStatus.exists
          ? 'text-rose-200 bg-rose-950/30'
          : ''}"
      >
        {statusText}
      </div>
    {/if}

    {#if loadError}
      <div class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
        <strong>Failed to load snapshot:</strong>
        <code class="font-mono text-xs">{loadError}</code>
        <p class="text-muted-foreground m-0 max-w-[60ch] text-xs">
          The GameParams snapshot hasn't been built yet (or game_dir /
          toolkit_bin aren't configured). Open
          <a
            href={settingsHref()}
            onclick={(e) => {
              e.preventDefault();
              navigate(settingsHref());
            }}
            class="text-foreground underline hover:no-underline"
          >Settings → Workspace artifacts</a>
          and click <em>Build</em> next to “GameParams + snapshot cache”.
        </p>
      </div>
    {:else if loading}
      <div class="text-muted-foreground flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-xs">
        Loading GameParams snapshot…
        <div class="text-muted-foreground/80 max-w-md">
          First load parses the ~2.8 GB GameParams cache (~30 s). Cached after that, so subsequent visits are
          instant.
        </div>
      </div>
    {:else if !selectedVehicle}
      <div class="flex-1 overflow-auto">
        <div class="text-muted-foreground flex h-full items-center justify-center px-6 py-8 text-xs">
          Pick a ship (and optionally a permoflage) to see the resolved extract command.
        </div>
        {#if activeJob}
          <ExtractJobPanel
            job={activeJob}
            matchesActiveLabel={false}
            onCancel={onCancelJob}
            onDismiss={onDismissJob}
          />
        {/if}
      </div>
    {:else}
      <div class="flex-1 overflow-auto">
        <ExtractPreview
          vehicle={selectedVehicle}
          permoflage={selectedPermoflage}
          allPermoflages={activePermoflages}
        />
        <ExtractRunPanel
          vehicle={selectedVehicle}
          permoflage={selectedPermoflage}
          {extractedShips}
          {activeJob}
          onRun={onRunExtract}
        />
        <ExtractSkinPackForm
          {extractedShips}
          permoflage={selectedPermoflage}
          defaultLabel={suggested}
          {activeJob}
          onSubmit={onRunSkin}
        />
        {#if activeJob}
          <ExtractJobPanel
            job={activeJob}
            matchesActiveLabel={activeJob.label === suggested}
            onCancel={onCancelJob}
            onDismiss={onDismissJob}
          />
        {/if}
      </div>
    {/if}
  </section>
</div>
