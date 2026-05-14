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
    fetchSettings,
    saveSettings,
    type SettingsPatch,
    type SettingsResponse,
    type SettingSource,
  } from '$lib/api';
  import { toast } from 'svelte-sonner';

  let data = $state<SettingsResponse | null>(null);
  let loadError = $state<string | null>(null);
  let saving = $state(false);

  // Form state. Initialised from `data.fields[k].value`, but `null` on
  // the wire maps to empty string in the input so the textbox renders
  // cleanly. `dirty` tracks whether any field diverged from the loaded
  // value so the Save button can disable when there's nothing to do.
  let form = $state({
    game_dir: '',
    toolkit_bin: '',
    toolkit_timeout_s: '',
  });
  let initial = $state({ game_dir: '', toolkit_bin: '', toolkit_timeout_s: '' });

  const dirty = $derived(
    form.game_dir !== initial.game_dir ||
      form.toolkit_bin !== initial.toolkit_bin ||
      form.toolkit_timeout_s !== initial.toolkit_timeout_s,
  );

  async function load() {
    loadError = null;
    try {
      const res = await fetchSettings();
      data = res;
      const next = {
        game_dir: res.fields.game_dir.value ?? '',
        toolkit_bin: res.fields.toolkit_bin.value ?? '',
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
        Read-only "where things live" section. Workspace is the bootstrap
        key — flagging it here so the user understands why it's not in
        the form below.
      -->
      <section class="flex flex-col gap-2">
        <div
          class="text-muted-foreground text-[11px] uppercase tracking-wider font-semibold"
        >
          Paths the backend resolved
        </div>
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-all [&_code]:font-mono [&_code]:text-[11px]"
        >
          <dt>Config file</dt>
          <dd><code>{data.config_path}</code></dd>
          <dt>Workspace</dt>
          <dd>
            <code>{data.workspace}</code>
            <span class="text-muted-foreground ml-1">
              (set via <code>--workspace</code> or <code>$WOWS_WORKSPACE</code>)
            </span>
          </dd>
          {#if data.cache_dir}
            <dt>Cache dir</dt>
            <dd><code>{data.cache_dir}</code></dd>
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
    {/if}
  </div>
</section>
