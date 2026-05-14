<script lang="ts">
  // Skin-pack form: ingest_skin_pack.py per the wg / vfs / loose source
  // taxonomy. Pre-fills source_arg + skin_id from the currently-selected
  // permoflage when in `wg` mode; the "Fill from selected permoflage"
  // button does the same explicit reset.
  //
  // Lives at the bottom of the Extract preview pane; only renders when
  // at least one ship has been ingested (otherwise there's nowhere to
  // land the skin).

  import { suggestedSkinId } from '$lib/extract/labels';
  import type { ExtractedShip, JobState, Permoflage, SkinPackForm, SkinSource } from '$lib/types/extract';

  interface Props {
    extractedShips: ExtractedShip[];
    permoflage: Permoflage | null;
    defaultLabel: string;
    activeJob: JobState | null;
    onSubmit: (form: SkinPackForm) => void;
  }

  const { extractedShips, permoflage, defaultLabel, activeJob, onSubmit }: Props = $props();

  // Form state is per-component instance (not persisted) — different
  // permoflages need different defaults and bookkeeping a draft across
  // session restarts would confuse more than it helps.
  let form = $state<SkinPackForm>({
    ship: '',
    source: 'wg',
    source_arg: '',
    exterior_id: '',
    skin_id: '',
    display_name: '',
  });

  function defaultsForCurrent(): SkinPackForm {
    const match = extractedShips.find((s) => s.name === defaultLabel);
    const ship = match ? match.name : extractedShips[0]?.name ?? '';
    return {
      ship,
      source: 'wg',
      source_arg: permoflage?.exterior_id ?? '',
      exterior_id: '',
      skin_id: permoflage ? suggestedSkinId(permoflage.exterior_id) : '',
      display_name: '',
    };
  }

  // Reset the form when the picked permoflage changes — clean defaults
  // for a new selection beat carrying stale half-typed values forward.
  // The first run on mount also fills the initial values, so the
  // `$state` initializer above stays prop-free.
  let lastPermoId = $state<string | null | undefined>(undefined);
  $effect(() => {
    const id = permoflage?.exterior_id ?? null;
    if (id !== lastPermoId) {
      lastPermoId = id;
      form = defaultsForCurrent();
    }
  });

  function setSource(s: SkinSource) {
    if (s === form.source) return;
    // Source-specific fields mean different things in each mode, so
    // clear them on switch.
    form = { ...form, source: s, source_arg: '', exterior_id: '' };
  }

  function fillFromPermo() {
    if (!permoflage) return;
    form = {
      ...form,
      source: 'wg',
      source_arg: permoflage.exterior_id,
      skin_id: suggestedSkinId(permoflage.exterior_id),
      display_name: permoflage.display_name || form.display_name,
    };
  }

  function submit() {
    if (!form.ship || !form.source_arg || !form.skin_id) return;
    if (form.source === 'vfs' && !form.exterior_id) return;
    onSubmit(form);
  }

  const submitDisabled = $derived(
    !form.ship ||
      !form.source_arg ||
      !form.skin_id ||
      (form.source === 'vfs' && !form.exterior_id) ||
      activeJob?.state === 'running',
  );

  const SOURCE_LABELS: Record<SkinSource, string> = {
    wg: 'WG (auto)',
    vfs: 'VFS asset',
    loose: 'Loose mod',
  };
</script>

<section class="border-border border-t px-4 py-3">
  <h3 class="text-foreground mb-2 text-xs font-semibold uppercase tracking-wide">Add skin pack</h3>
  {#if extractedShips.length === 0}
    <div class="text-muted-foreground text-[11px]">
      No ships extracted yet — run an extract first, then you can ingest skin packs into the resulting
      <code>ships/&lt;label&gt;/</code> tree.
    </div>
  {:else}
    <div class="flex flex-col gap-2.5 text-xs">
      <div class="flex flex-wrap items-end gap-2">
        <label class="flex flex-1 min-w-0 flex-col gap-0.5">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Target ship</span>
          <select
            value={form.ship}
            onchange={(e) => (form = { ...form, ship: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 rounded border px-1.5 py-1 text-xs focus:outline-none focus:ring-2"
          >
            {#each extractedShips as s (s.name)}
              <option value={s.name}>{s.name} ({s.display_name})</option>
            {/each}
          </select>
        </label>
        {#if permoflage}
          <button
            type="button"
            onclick={fillFromPermo}
            class="bg-secondary text-foreground hover:bg-secondary/80 h-7 rounded px-2.5 text-[11px]"
          >
            Fill from selected permoflage
          </button>
        {/if}
      </div>

      <div>
        <div class="text-muted-foreground mb-1 text-[10px] uppercase tracking-wide">Source</div>
        <div class="flex gap-1">
          {#each ['wg', 'vfs', 'loose'] as s (s)}
            {@const src = s as SkinSource}
            <button
              type="button"
              onclick={() => setSource(src)}
              class="bg-popover text-foreground border-border hover:bg-accent rounded border px-2 py-[3px] text-[11px] {form.source ===
              src
                ? 'bg-accent border-l-primary border-l-[2px]'
                : ''}"
            >
              {SOURCE_LABELS[src]}
            </button>
          {/each}
        </div>
      </div>

      {#if form.source === 'wg'}
        <label class="flex flex-col gap-0.5">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Vehicle / Exterior id</span>
          <input
            type="text"
            placeholder="PAES329_AZUR_New_Jersey or PJSC708"
            value={form.source_arg}
            oninput={(e) => (form = { ...form, source_arg: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
        <div class="text-muted-foreground text-[11px]">
          Resolves the Vehicle's <code>nativePermoflage</code> (or the Exterior's <code>peculiarityModels</code>
          <code>/ship/</code> entry) automatically. Falls back: Baltimore Azur etc. need the VFS-asset path instead.
        </div>
      {:else if form.source === 'vfs'}
        <label class="flex flex-col gap-0.5">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Variant asset_id</span>
          <input
            type="text"
            placeholder="ASC080_Baltimore_1944_Azur"
            value={form.source_arg}
            oninput={(e) => (form = { ...form, source_arg: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
        <label class="flex flex-col gap-0.5">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Exterior id</span>
          <input
            type="text"
            placeholder="PAES488_Azur_Baltimore"
            value={form.exterior_id}
            oninput={(e) => (form = { ...form, exterior_id: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
        <div class="text-muted-foreground text-[11px]">
          Use this when the Exterior has no <code>/ship/</code> peculiarityModel (separate-VFS-hull case,
          e.g. Baltimore Azur Lane).
        </div>
      {:else}
        <label class="flex flex-col gap-0.5">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Loose mod folder path</span>
          <input
            type="text"
            placeholder="C:/path/to/mod_dir"
            value={form.source_arg}
            oninput={(e) => (form = { ...form, source_arg: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
        <div class="text-muted-foreground text-[11px]">
          Server-side absolute path to a content-SDK mod folder containing <code>*.dds</code> overrides.
        </div>
      {/if}

      <div class="flex flex-wrap gap-2">
        <label class="flex flex-1 flex-col gap-0.5 min-w-[160px]">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Skin id</span>
          <input
            type="text"
            placeholder="azur_new_jersey"
            value={form.skin_id}
            oninput={(e) => (form = { ...form, skin_id: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
        <label class="flex flex-1 flex-col gap-0.5 min-w-[160px]">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Display name (optional)</span>
          <input
            type="text"
            placeholder="Azur Lane: New Jersey"
            value={form.display_name}
            oninput={(e) => (form = { ...form, display_name: e.currentTarget.value })}
            class="bg-popover text-foreground border-border focus:border-ring focus:ring-ring/30 h-7 rounded border px-2 text-xs focus:outline-none focus:ring-2"
          />
        </label>
      </div>

      <div class="flex items-center gap-3">
        <button
          type="button"
          onclick={submit}
          disabled={submitDisabled}
          class="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
        >
          {activeJob?.state === 'running' ? 'Job in progress…' : 'Ingest skin pack'}
        </button>
        <span class="text-muted-foreground text-[11px]">
          spawns <code>wows-ingest-skin-pack</code> via <code>POST /api/extract/skin</code>
        </span>
      </div>
    </div>
  {/if}
</section>
