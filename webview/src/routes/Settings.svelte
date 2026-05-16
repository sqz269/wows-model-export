<script lang="ts">
  // Settings route. Reads the backend's effective config + provenance
  // from `/api/settings`, exposes the three persistable fields
  // (game_dir, toolkit_bin, toolkit_timeout_s) as a form, and PUTs back
  // through the same endpoint. Workspace + cache_dir are read-only
  // metadata — they're set via `--workspace` / $WOWS_WORKSPACE.
  //
  // Restart-required model: the backend reads its user-config file at
  // startup. PUTs persist immediately but don't hot-swap the running
  // process. The save handler surfaces a restart toast so the user
  // knows to re-launch `wows-webview-serve`.

  import { onMount } from 'svelte';
  import { Button } from '$lib/components/ui/button';
  import {
    buildBootstrapTarget,
    fetchBootstrap,
    fetchSettings,
    resetBootstrapTarget,
    saveSettings,
    waitForJob,
    type BootstrapStatus,
    type BootstrapTarget,
    type SettingsPatch,
    type SettingsResponse,
    type SettingSource,
  } from '$lib/api';
  import { invalidateLibrary } from '$lib/api/library';
  import { toast } from 'svelte-sonner';

  let data = $state<SettingsResponse | null>(null);
  let bootstrap = $state<BootstrapStatus | null>(null);
  let loadError = $state<string | null>(null);
  let saving = $state(false);
  /** Map target → in-flight job_id; truthy disables that target's button. */
  let building = $state<Record<BootstrapTarget, string | null>>({
    snapshot: null,
    library: null,
  });
  /** Map target → true while the reset POST is in flight; disables both
   *  Build and Reset for that row so the user can't double-click. */
  let resetting = $state<Record<BootstrapTarget, boolean>>({
    snapshot: false,
    library: false,
  });

  // Form state. Initialised from `data.fields[k].value`, but `null` on
  // the wire maps to empty string in the input so the textbox renders
  // cleanly. `dirty` tracks whether any field diverged from the loaded
  // value so the Save button can disable when there's nothing to do.
  let form = $state({
    game_dir: '',
    toolkit_bin: '',
    workspace: '',
    toolkit_timeout_s: '',
  });
  let initial = $state({
    game_dir: '',
    toolkit_bin: '',
    workspace: '',
    toolkit_timeout_s: '',
  });

  const dirty = $derived(
    form.game_dir !== initial.game_dir ||
      form.toolkit_bin !== initial.toolkit_bin ||
      form.workspace !== initial.workspace ||
      form.toolkit_timeout_s !== initial.toolkit_timeout_s,
  );

  /** True when the persisted (or env-overridden) workspace diverges
   *  from what the running backend booted with — i.e. saving any
   *  field will need a restart to take effect for workspace too. */
  const workspaceDrift = $derived(
    data !== null && data.fields.workspace.value !== data.running_workspace,
  );

  async function load() {
    loadError = null;
    try {
      // Settings + bootstrap are independent calls; firing them in
      // parallel halves the first-paint latency.
      const [res, boot] = await Promise.all([fetchSettings(), fetchBootstrap()]);
      data = res;
      bootstrap = boot;
      const next = {
        game_dir: res.fields.game_dir.value ?? '',
        toolkit_bin: res.fields.toolkit_bin.value ?? '',
        workspace: res.fields.workspace.value ?? '',
        toolkit_timeout_s:
          res.fields.toolkit_timeout_s.value !== null
            ? String(res.fields.toolkit_timeout_s.value)
            : '',
      };
      form = { ...next };
      initial = { ...next };
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  }

  /** Refresh just the bootstrap section — used after a Build job
   *  finishes so the presence/mtime stamps update without re-fetching
   *  the settings form (which would clobber unsaved edits). */
  async function reloadBootstrap() {
    try {
      bootstrap = await fetchBootstrap();
    } catch (err) {
      console.warn('bootstrap reload failed:', err);
    }
  }

  onMount(load);

  // The PUT body is a *patch*. We always send all three keys so the
  // server can distinguish "user cleared this field" (empty string →
  // null → drop the override) from "user didn't touch it" (key omitted
  // → backend keeps the existing value). Sending everything is simpler
  // and lets the server reason about clears unambiguously.
  function buildPatch(): SettingsPatch {
    const patch: SettingsPatch = {};
    patch.game_dir = form.game_dir.trim() || null;
    patch.toolkit_bin = form.toolkit_bin.trim() || null;
    patch.workspace = form.workspace.trim() || null;
    if (form.toolkit_timeout_s.trim() === '') {
      patch.toolkit_timeout_s = null;
    } else {
      const n = Number(form.toolkit_timeout_s);
      patch.toolkit_timeout_s = Number.isFinite(n) ? n : null;
    }
    return patch;
  }

  async function onSave() {
    if (saving) return;
    saving = true;
    const tid = toast.loading('Saving…', { duration: Number.POSITIVE_INFINITY });
    try {
      const res = await saveSettings(buildPatch());
      toast.success('Saved. Restart wows-webview-serve to apply.', {
        id: tid,
        description: res.config_path,
        duration: 8000,
      });
      await load();
    } catch (err) {
      // Server-side per-field validation errors land in
      // `err.body.errors` (`ApiError`); render them inline as a
      // multi-line toast.
      const msg = err instanceof Error ? err.message : String(err);
      let detail = msg;
      if (err && typeof err === 'object' && 'body' in err) {
        const body = (err as { body: unknown }).body;
        if (body && typeof body === 'object' && 'errors' in body) {
          const errors = (body as { errors: Record<string, string> }).errors;
          detail = Object.entries(errors)
            .map(([k, v]) => `${k}: ${v}`)
            .join('\n');
        }
      }
      toast.error('Save failed', { id: tid, description: detail, duration: 8000 });
    } finally {
      saving = false;
    }
  }

  function onReset() {
    form = { ...initial };
  }

  /** Kick off the matching CLI as a job, poll until it terminates,
   *  surface progress through a sticky toast, refresh the bootstrap
   *  status on done. */
  async function onBuild(target: BootstrapTarget) {
    if (building[target]) return;
    const targetLabel = bootstrap?.targets[target].label ?? target;
    const tid = toast.loading(`Starting ${targetLabel}…`, {
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      const { job_id } = await buildBootstrapTarget(target);
      building[target] = job_id;
      const final = await waitForJob(job_id, (j) => {
        // Show only the last-line tail to keep the toast compact; the
        // full log will be reachable from a future Jobs panel.
        const tail = (j.stdout || '').split('\n').filter(Boolean).slice(-1)[0] ?? '';
        toast.loading(`${targetLabel}…`, {
          id: tid,
          description: tail.slice(0, 140),
          duration: Number.POSITIVE_INFINITY,
        });
      });
      if (final.state === 'done') {
        toast.success(`${targetLabel} built`, {
          id: tid,
          description: final.cmd.join(' '),
          duration: 5000,
        });
      } else {
        // Failed / cancelled — surface the stderr tail. The user can
        // copy it from the toast description.
        const tail =
          (final.stderr || final.stdout || '')
            .split('\n')
            .filter(Boolean)
            .slice(-3)
            .join('\n') || `exit ${final.exit_code ?? '?'}`;
        toast.error(`${targetLabel} ${final.state}`, {
          id: tid,
          description: tail,
          duration: 12000,
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Build failed`, { id: tid, description: msg, duration: 8000 });
    } finally {
      building[target] = null;
      await reloadBootstrap();
    }
  }

  /** Wipe a bootstrap target's on-disk state. Surfaces a browser
   *  confirm() before calling the backend — the rmtree is irreversible
   *  and the user might have re-runnable but expensive artifacts in
   *  there (snapshot rebuild is ~30 s, library rebuild walks every
   *  ship). After delete, refresh the bootstrap status so the row
   *  re-renders as "not built" and the Build button re-enables. */
  async function onResetTarget(target: BootstrapTarget) {
    if (resetting[target] || building[target]) return;
    const t = bootstrap?.targets[target];
    const targetLabel = t?.label ?? target;
    const path = t?.path ?? '';
    const ok = window.confirm(
      `Reset ${targetLabel}?\n\n` +
        `This will delete:\n  ${path}\n\n` +
        `You'll need to rebuild it before tabs that depend on it work again.`,
    );
    if (!ok) return;
    resetting[target] = true;
    const tid = toast.loading(`Resetting ${targetLabel}…`, {
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      const res = await resetBootstrapTarget(target);
      // Drop the in-memory library cache so the Library/Ships pages
      // re-fetch on next mount (they'd see a stale index otherwise).
      if (target === 'library') invalidateLibrary();
      toast.success(
        res.existed ? `${targetLabel} reset` : `${targetLabel} was already empty`,
        { id: tid, description: res.path, duration: 5000 },
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Reset failed`, { id: tid, description: msg, duration: 8000 });
    } finally {
      resetting[target] = false;
      await reloadBootstrap();
    }
  }

  function fmtMtime(ms: number | null): string {
    if (!ms) return 'never built';
    const d = new Date(ms);
    return d.toLocaleString();
  }

  function fmtSize(bytes: number | null): string {
    if (bytes === null) return '';
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    return `${(kb / 1024).toFixed(1)} MB`;
  }

  function sourceLabel(s: SettingSource): string {
    switch (s) {
      case 'env':
        return 'env var (wins over file)';
      case 'file':
        return 'config file';
      case 'auto':
        return 'auto-discovered on PATH';
      case 'default':
        return 'default';
      case 'unconfigured':
        return 'not configured';
    }
  }

  function sourceColor(s: SettingSource): string {
    switch (s) {
      case 'env':
        return 'text-amber-400';
      case 'file':
        return 'text-emerald-400';
      case 'auto':
        return 'text-sky-400';
      case 'default':
        return 'text-muted-foreground';
      case 'unconfigured':
        return 'text-rose-400';
    }
  }

  // Tailwind class strings shared across the field rows. Mirrors the
  // labelled-input idiom used by ShipControls / AssetDetail so the
  // Settings page reads identically to the rest of the app.
  const labelCls = 'flex flex-col gap-1 text-xs';
  const labelTopCls = 'flex items-baseline justify-between text-muted-foreground';
  const inputCls =
    'h-8 w-full rounded border border-border bg-popover px-2 text-xs font-mono text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring';
</script>

<section class="flex flex-1 min-w-0 flex-col overflow-y-auto">
  <header class="bg-card border-border flex flex-none items-center gap-4 border-b px-5 py-3">
    <h2 class="m-0 text-sm font-semibold">Settings</h2>
    <span class="text-muted-foreground text-[11px]">
      Persisted to the user-config file the backend reads at startup.
    </span>
  </header>

  <div class="flex flex-1 min-h-0 flex-col gap-5 p-5 max-w-3xl">
    {#if loadError}
      <div
        class="text-destructive rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs"
      >
        <strong>Failed to load settings:</strong>
        <code class="ml-1">{loadError}</code>
      </div>
    {:else if !data}
      <div class="text-muted-foreground text-xs">Loading…</div>
    {:else}
      <!--
        Read-only "running snapshot" section. These reflect what the
        backend BOOTED with — useful when the persisted workspace
        (editable below) diverges from the running one, so the user
        can see the restart-required gap.
      -->
      <section class="flex flex-col gap-2">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          Backend running with
        </div>
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-all [&_code]:font-mono [&_code]:text-[11px]"
        >
          <dt>Config file</dt>
          <dd><code>{data.config_path}</code></dd>
          <dt>Workspace</dt>
          <dd>
            <code>{data.running_workspace}</code>
            {#if workspaceDrift}
              <span class="text-amber-400 ml-1">
                — persisted value below differs; restart to apply
              </span>
            {/if}
          </dd>
          {#if data.running_cache_dir}
            <dt>Cache dir</dt>
            <dd><code>{data.running_cache_dir}</code></dd>
          {/if}
        </dl>
      </section>

      <!-- Editable fields. Source badge surfaces precedence so the user
           knows whether an env var is overriding their file write. -->
      <section class="flex flex-col gap-4">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          User-configurable
        </div>

        <label class={labelCls}>
          <span class={labelTopCls}>
            <span class="text-foreground">Game directory</span>
            <span class={sourceColor(data.fields.game_dir.source)}>
              {sourceLabel(data.fields.game_dir.source)}
            </span>
          </span>
          <input
            type="text"
            placeholder="C:\Program Files (x86)\Steam\steamapps\common\World of Warships"
            bind:value={form.game_dir}
            class={inputCls}
          />
          <span class="text-muted-foreground text-[11px]">
            Directory containing <code>WorldOfWarships.exe</code>. Read by the
            toolkit when extracting. Env override:
            <code>${data.fields.game_dir.env_var}</code>.
          </span>
        </label>

        <label class={labelCls}>
          <span class={labelTopCls}>
            <span class="text-foreground">Toolkit binary (wowsunpack)</span>
            <span class={sourceColor(data.fields.toolkit_bin.source)}>
              {sourceLabel(data.fields.toolkit_bin.source)}
            </span>
          </span>
          <input
            type="text"
            placeholder="C:\path\to\wowsunpack.exe"
            bind:value={form.toolkit_bin}
            class={inputCls}
          />
          <span class="text-muted-foreground text-[11px]">
            Path to the <code>wowsunpack</code> executable. Falls back to
            <code>PATH</code> when neither this file nor
            <code>${data.fields.toolkit_bin.env_var}</code> is set.
          </span>
        </label>

        <label class={labelCls}>
          <span class={labelTopCls}>
            <span class="text-foreground">Workspace (output directory)</span>
            <span class={sourceColor(data.fields.workspace.source)}>
              {sourceLabel(data.fields.workspace.source)}
            </span>
          </span>
          <input
            type="text"
            placeholder={data.running_workspace}
            bind:value={form.workspace}
            class={inputCls}
          />
          <span class="text-muted-foreground text-[11px]">
            Where ship extracts, libraries, and caches land. Leave blank
            to fall back to <code>${data.fields.workspace.env_var}</code> or
            the directory <code>wows-webview-serve</code> was launched from.
            <code>--workspace</code> on the CLI still overrides this.
          </span>
        </label>

        <label class={labelCls}>
          <span class={labelTopCls}>
            <span class="text-foreground">Toolkit timeout (seconds)</span>
            <span class={sourceColor(data.fields.toolkit_timeout_s.source)}>
              {sourceLabel(data.fields.toolkit_timeout_s.source)}
            </span>
          </span>
          <input
            type="number"
            min="1"
            step="1"
            placeholder="default"
            bind:value={form.toolkit_timeout_s}
            class={inputCls}
          />
          <span class="text-muted-foreground text-[11px]">
            Per-subprocess timeout for <code>wowsunpack</code> calls. Leave
            blank for the toolkit default. Env override:
            <code>${data.fields.toolkit_timeout_s.env_var}</code>.
          </span>
        </label>
      </section>

      <div class="flex items-center gap-3 pt-1">
        <Button size="sm" disabled={!dirty || saving} onclick={onSave}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
        <Button size="sm" variant="ghost" disabled={!dirty || saving} onclick={onReset}>
          Reset
        </Button>
        <span class="text-muted-foreground text-[11px]">
          Restart <code>wows-webview-serve</code> after saving for the
          backend to pick up the new values.
        </span>
      </div>

      <!--
        Workspace artifacts. The Library and Extract tabs depend on
        files this section builds; without them they 404 / 503 even
        when game_dir / toolkit_bin are correctly configured.
        Build buttons are disabled when `requires_config` lists a
        field that isn't resolved yet.
      -->
      {#if bootstrap}
        <section class="flex flex-col gap-3 border-t border-border pt-5">
          <div
            class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
          >
            Workspace artifacts
          </div>
          <p class="text-muted-foreground text-xs m-0 max-w-[60ch]">
            Builds the per-workspace files the rest of the app depends on.
            Run these once after pointing at a fresh workspace; re-run when
            GameParams changes (patch day) or after extracting a new ship.
          </p>

          {#each Object.entries(bootstrap.targets) as [key, t] (key)}
            {@const target = key as BootstrapTarget}
            {@const blockedBy = t.requires_config.filter((f) =>
              bootstrap?.missing_config.includes(f),
            )}
            {@const buildDisabled =
              blockedBy.length > 0 || building[target] !== null || resetting[target]}
            {@const resetDisabled =
              building[target] !== null || resetting[target] || !t.present}
            <div
              class="rounded border border-border bg-popover/40 px-3 py-2.5 flex flex-col gap-1.5"
            >
              <div class="flex items-baseline justify-between gap-3">
                <span class="text-sm font-medium text-foreground">{t.label}</span>
                <span
                  class="text-[11px] {t.present
                    ? 'text-emerald-400'
                    : 'text-rose-400'}"
                >
                  {t.present ? `built ${fmtMtime(t.mtime_ms)}` : 'not built'}
                  {#if t.present && t.size_bytes !== null}
                    <span class="text-muted-foreground ml-1">
                      ({fmtSize(t.size_bytes)})
                    </span>
                  {/if}
                </span>
              </div>
              <p class="text-muted-foreground text-[11px] m-0 max-w-[60ch]">
                {t.description}
              </p>
              <div class="text-muted-foreground text-[11px] font-mono break-all">
                <code>{t.cmd.join(' ')}</code>
              </div>
              <div class="flex items-center gap-3 pt-1">
                <Button
                  size="sm"
                  disabled={buildDisabled}
                  onclick={() => onBuild(target)}
                >
                  {building[target] ? 'Building…' : t.present ? 'Rebuild' : 'Build'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={resetDisabled}
                  onclick={() => onResetTarget(target)}
                >
                  {resetting[target] ? 'Resetting…' : 'Reset'}
                </Button>
                {#if blockedBy.length > 0}
                  <span class="text-amber-400 text-[11px]">
                    needs {blockedBy.join(' + ')} configured first
                  </span>
                {/if}
              </div>
            </div>
          {/each}
        </section>
      {/if}
    {/if}
  </div>
</section>
