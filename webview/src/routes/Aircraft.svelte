<script lang="ts">
  // Aircraft library route. Fetches /api/aircraft (libraries/aircraft/
  // index.json) and renders one airframe at a time in the shared
  // AccessoryViewer, with the DDS textures bound from the index.
  //
  // What draws follows the index, not mesh names: the authored LOD table
  // picks the LOD's meshes, the plane state (Fly / Stand / Launch: the
  // client's render presets) picks the toggled parts (gear down or up,
  // blades or blur disc), and one ordnance rack can be shown. Hardpoint
  // markers show each HP_* node's frame (X red, Y green, Z blue).
  //
  // Animation: the airframe's `wows_animation/v1` document (the clips its
  // state machines play, e.g. the bomb-bay doors) is fetched on demand and
  // played on the loaded GLB; the propellers can spin about their pivots'
  // local axis (the index's `animation.propeller_spin`).
  //
  // Hash routing: `#/aircraft` shows the picker; `#/aircraft/<id>` opens
  // that airframe.

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import { navState } from '$lib/nav_state.svelte';
  import { fetchAircraft, fetchAircraftAnimation, invalidateAircraft } from '$lib/api';
  import type { LibraryAsset } from '$lib/types';
  import {
    AIRCRAFT_PRESET_PARTS,
    type AircraftAsset,
    type AircraftIndex,
    type AircraftPreset,
    type AnimationDocument,
  } from '$lib/types/aircraft';
  import type { AccessoryViewer as AccessoryViewerClass, LibraryContext } from '$lib/accessory';
  import AccessoryViewer from '$components/AccessoryViewer.svelte';

  interface Props {
    aircraftId: string | null;
    active: boolean;
  }
  const { aircraftId, active: _active }: Props = $props();

  let index = $state<AircraftIndex | null>(null);
  let loadError = $state<string | null>(null);
  let loadHint = $state<string | null>(null);
  let query = $state('');

  let selected = $state<string | null>(null);
  $effect(() => {
    if (aircraftId) selected = aircraftId;
  });
  const activeId = $derived(index && selected && index.assets[selected] ? selected : null);
  const asset = $derived<AircraftAsset | null>(activeId && index ? index.assets[activeId] : null);
  $effect(() => {
    if (activeId) navState.lastAircraftId = activeId;
  });

  // View state (sticky across airframes; LOD and rack clamp per airframe).
  let lod = $state(0);
  let preset = $state<AircraftPreset>('fly');
  let rack = $state<string>('auto');
  let showHardpoints = $state(true);
  let showTextures = $state(true);
  let viewer = $state<AccessoryViewerClass | null>(null);
  let loadedFor = $state<string | null>(null);

  // Animation state (per airframe).
  let clipKey = $state<string>('');
  let clipLoop = $state(false);
  let spinProps = $state(false);
  let animDoc = $state<AnimationDocument | null>(null);
  let animDocFor = $state<string | null>(null);
  // The airframe the clip selection belongs to. Tracked apart from animDocFor, which moves only when a clip is
  // played: keyed on that, visiting an airframe without playing left its clip selected on the next one.
  let clipFor = $state<string | null>(null);
  let animError = $state<string | null>(null);
  let animPlaying = $state<string | null>(null);
  const clipKeys = $derived<string[]>(asset?.animation ? Object.keys(asset.animation.clips) : []);
  $effect(() => {
    // New airframe: default to its bomb-bay opening clip (else the first); drop the old document.
    if (activeId !== clipFor) {
      clipFor = activeId;
      animDoc = null;
      animDocFor = null;
      animError = null;
      animPlaying = null;
      clipKey = asset?.animation?.bomb_bay?.open ?? clipKeys[0] ?? '';
    }
  });

  async function playClip() {
    if (!viewer || !asset?.animation?.file || !clipKey || !activeId) return;
    try {
      if (!animDoc || animDocFor !== activeId) {
        animDoc = await fetchAircraftAnimation(asset.animation.file);
        animDocFor = activeId;
      }
      const clip = animDoc.clips[clipKey];
      if (!clip) throw new Error(`clip ${clipKey} not in the document`);
      const missing = viewer.playAnimationClip(clipKey, clip, { loop: clipLoop });
      animError = missing.length ? `nodes not in the model: ${missing.join(', ')}` : null;
      animPlaying = clipKey;
    } catch (err) {
      animError = err instanceof Error ? err.message : String(err);
    }
  }

  function stopClip() {
    viewer?.stopAnimation(true);
    animPlaying = null;
  }

  $effect(() => {
    if (!viewer || !asset || loadedFor !== activeId) return;
    // Two turns per second: the blur disc reads the same at any rate (the
    // client's propellerRPM is not decoded).
    viewer.setPropellerSpin(
      spinProps ? (asset.animation?.propeller_spin ?? null) : null,
      4 * Math.PI,
    );
  });

  onMount(async () => {
    try {
      index = (await fetchAircraft()).index;
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
      const body =
        err && typeof err === 'object' && 'body' in err ? (err as { body: unknown }).body : null;
      if (body && typeof body === 'object' && 'hint' in body)
        loadHint = String((body as { hint: unknown }).hint);
    }
  });

  function retry() {
    loadError = null;
    invalidateAircraft();
    void fetchAircraft()
      .then((p) => (index = p.index))
      .catch((err) => (loadError = err instanceof Error ? err.message : String(err)));
  }

  const rows = $derived.by(() => {
    if (!index) return [] as Array<[string, AircraftAsset]>;
    const q = query.trim().toLowerCase();
    return Object.entries(index.assets)
      .filter(
        ([id, a]) =>
          !q ||
          id.toLowerCase().includes(q) ||
          a.nation.includes(q) ||
          a.category.includes(q) ||
          a.gameparams.some((g) => g.key.toLowerCase().includes(q)),
      )
      .sort(([a, ea], [b, eb]) =>
        ea.nation === eb.nation ? a.localeCompare(b) : ea.nation.localeCompare(eb.nation),
      );
  });

  const glbUrl = $derived.by(() => {
    if (!asset) return null;
    return `/repo/libraries/aircraft/${asset.glb.split(/[\\/]/).map(encodeURIComponent).join('/')}`;
  });

  const libContext = $derived.by<LibraryContext | null>(() => {
    if (!activeId || !asset) return null;
    const synth: LibraryAsset = {
      scope: asset.nation,
      category: asset.category,
      subcategory: null,
      species: null,
      glb: asset.glb,
      textures_dds: asset.textures_dds ?? null,
      glb_bytes: asset.glb_bytes,
      built_at: asset.built_at ?? null,
      used_by_ships: [],
      texture_sets: asset.texture_sets,
      materials: asset.materials,
    };
    return { assetId: activeId, asset: synth, variant: 'main', libraryRoot: 'aircraft' };
  });

  // The rack shown: 'auto' = the first default rack (what the client shows
  // for an entry whose projectile names no custom rack), 'none', or one
  // render set.
  const shownRack = $derived.by<string | null>(() => {
    if (!asset || rack === 'none') return null;
    if (rack === 'auto')
      return asset.racks.find((r) => r.variant === 'default')?.render_set ?? null;
    return asset.racks.some((r) => r.render_set === rack) ? rack : null;
  });

  const visibleMeshes = $derived.by<string[]>(() => {
    if (!asset) return [];
    const l = Math.min(lod, Math.max(0, asset.lods.length - 1));
    const parts = new Set(AIRCRAFT_PRESET_PARTS[preset]);
    return Object.entries(asset.meshes)
      .filter(([, m]) => m.lods.includes(l))
      .filter(([, m]) => (m.part === 'rack' ? m.render_set === shownRack : parts.has(m.part)))
      .map(([name]) => name);
  });

  const hardpointNames = $derived.by<string[]>(() =>
    asset ? Object.values(asset.hardpoints).flat() : [],
  );

  $effect(() => {
    if (!viewer || !asset || loadedFor !== activeId) return;
    viewer.setMeshAllowList(visibleMeshes);
  });
  $effect(() => {
    if (!viewer || !asset || loadedFor !== activeId) return;
    viewer.setNodeMarkers(showHardpoints ? hardpointNames : null);
  });
  $effect(() => {
    if (!viewer) return;
    void viewer.setShowTextures(showTextures);
  });
  $effect(() => {
    // Clamp the LOD when switching to an airframe with fewer LODs.
    if (asset && lod >= asset.lods.length) lod = 0;
  });

  function fmt(n: number | undefined | null, d = 2): string {
    return typeof n === 'number' ? n.toFixed(d) : '–';
  }
</script>

<div class="flex flex-1 min-w-0 h-full">
  {#if loadError}
    <div
      class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center"
    >
      <strong>Failed to load the aircraft library:</strong>
      <code class="mx-1 break-all max-w-[60ch]">{loadError}</code>
      {#if loadHint}<p class="text-muted-foreground m-0 max-w-[60ch] text-xs">{loadHint}</p>{/if}
      <button
        type="button"
        onclick={retry}
        class="border border-border hover:bg-popover/60 rounded px-3 py-1.5 text-xs"
      >
        Retry
      </button>
    </div>
  {:else if !index}
    <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
      Loading aircraft…
    </div>
  {:else}
    <aside class="border-border flex w-72 flex-shrink-0 flex-col min-h-0 border-r">
      <div class="border-border flex-none border-b p-2">
        <input
          type="search"
          placeholder="Filter: id, nation, role, GameParams key"
          bind:value={query}
          class="h-7 w-full rounded border border-border bg-popover px-2 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30"
        />
        <div class="text-muted-foreground mt-1 text-[11px]">
          {rows.length} of {index.asset_count} airframes
        </div>
      </div>
      <ul class="flex-1 overflow-y-auto text-xs" data-testid="aircraft-list">
        {#each rows as [id, a] (id)}
          <li>
            <button
              type="button"
              onclick={() => navigate(`#/aircraft/${encodeURIComponent(id)}`)}
              class="w-full px-3 py-1.5 text-left hover:bg-popover/60 {id === activeId
                ? 'bg-accent text-foreground'
                : ''}"
            >
              <div class="font-mono break-all">{id}</div>
              <div class="text-muted-foreground text-[11px]">
                {a.nation} · {a.category} · {a.gameparams.length} GameParams entr{a.gameparams
                  .length === 1
                  ? 'y'
                  : 'ies'}
              </div>
            </button>
          </li>
        {/each}
      </ul>
    </aside>

    <section class="flex flex-1 min-w-0 flex-col min-h-0">
      {#if activeId && asset}
        <header
          class="border-border bg-card flex flex-none flex-wrap items-center justify-between gap-3 border-b px-4 py-2"
        >
          <div class="flex items-baseline gap-2 min-w-0">
            <code class="text-foreground font-mono text-xs break-all" data-testid="aircraft-id"
              >{activeId}</code
            >
            <span class="text-muted-foreground text-[11px]">
              {asset.nation} · {asset.category} · span {fmt(asset.dimensions_m?.span, 1)} m · length
              {fmt(asset.dimensions_m?.length, 1)} m
            </span>
          </div>
          <div class="text-muted-foreground flex flex-wrap items-center gap-3 text-[11px]">
            <label class="flex items-center gap-1.5">
              LOD
              <select
                bind:value={lod}
                class="h-6 rounded border border-border bg-popover px-1.5 text-foreground"
              >
                {#each asset.lods as l, i (i)}
                  <option value={i}>LOD {i} ({l.extent ?? '–'})</option>
                {/each}
              </select>
            </label>
            <label class="flex items-center gap-1.5">
              State
              <select
                bind:value={preset}
                class="h-6 rounded border border-border bg-popover px-1.5 text-foreground"
              >
                <option value="fly">Fly (gear up, blur disc)</option>
                <option value="launch">Launch (gear down, blur disc)</option>
                <option value="stand">Stand (gear down, blades)</option>
              </select>
            </label>
            {#if asset.racks.length > 0}
              <label class="flex items-center gap-1.5">
                Rack
                <select
                  bind:value={rack}
                  class="h-6 rounded border border-border bg-popover px-1.5 text-foreground"
                >
                  <option value="auto">Default rack</option>
                  <option value="none">None</option>
                  {#each asset.racks as r (r.render_set)}
                    <option value={r.render_set}>{r.render_set}</option>
                  {/each}
                </select>
              </label>
            {/if}
            {#if clipKeys.length > 0}
              <label class="flex items-center gap-1.5">
                Clip
                <select
                  bind:value={clipKey}
                  data-testid="aircraft-clip"
                  class="h-6 rounded border border-border bg-popover px-1.5 text-foreground"
                >
                  {#each clipKeys as k (k)}
                    <option value={k}>{asset.animation?.clips[k]?.role ?? k}</option>
                  {/each}
                </select>
              </label>
              <button
                type="button"
                data-testid="aircraft-clip-play"
                onclick={() => void playClip()}
                class="border border-border hover:bg-popover/60 h-6 rounded px-2">Play</button
              >
              <button
                type="button"
                onclick={stopClip}
                class="border border-border hover:bg-popover/60 h-6 rounded px-2">Rest</button
              >
              <label class="flex items-center gap-1"
                ><input type="checkbox" bind:checked={clipLoop} /> Loop</label
              >
            {/if}
            {#if asset.animation?.propeller_spin && asset.propellers.length > 0}
              <label class="flex items-center gap-1"
                ><input type="checkbox" bind:checked={spinProps} data-testid="aircraft-spin" /> Spin propellers</label
              >
            {/if}
            <label class="flex items-center gap-1"
              ><input type="checkbox" bind:checked={showHardpoints} /> Hardpoints</label
            >
            <label class="flex items-center gap-1"
              ><input type="checkbox" bind:checked={showTextures} /> Textures</label
            >
          </div>
        </header>
        <div class="flex flex-1 min-h-0">
          <div class="flex flex-1 min-w-0 min-h-0">
            <AccessoryViewer
              url={glbUrl}
              lib={libContext}
              bindHandle={(v) => (viewer = v)}
              onLoaded={() => (loadedFor = activeId)}
            />
          </div>
          <div
            class="border-border w-72 flex-shrink-0 overflow-y-auto border-l p-3 text-[11px]"
            data-testid="aircraft-detail"
          >
            <h3 class="text-foreground mb-1 text-xs font-semibold">Frame</h3>
            <p class="text-muted-foreground mb-3">
              Metres; nose toward −Z, up +Y; the origin is the deck contact point. Bounds {asset.bounds_m
                ? asset.bounds_m.min.map((v) => v.toFixed(1)).join(', ')
                : '–'} →
              {asset.bounds_m ? asset.bounds_m.max.map((v) => v.toFixed(1)).join(', ') : '–'}
            </p>
            <h3 class="text-foreground mb-1 text-xs font-semibold">LODs</h3>
            <ul class="text-muted-foreground mb-3">
              {#each asset.lods as l, i (i)}
                <li>
                  LOD {i}: extent {l.extent ?? '–'}{l.casts_shadow ? '' : ', no shadow'}, {l.meshes
                    .length} meshes
                </li>
              {/each}
            </ul>
            <h3 class="text-foreground mb-1 text-xs font-semibold">Parts</h3>
            <ul class="text-muted-foreground mb-3">
              {#each Object.entries(asset.parts) as [part, sets] (part)}
                <li><span class="text-foreground">{part}</span>: {(sets ?? []).join(', ')}</li>
              {/each}
            </ul>
            {#if asset.propellers.length}
              <h3 class="text-foreground mb-1 text-xs font-semibold">Propellers</h3>
              <ul class="text-muted-foreground mb-3">
                {#each asset.propellers as p (p.node)}
                  <li>{p.node} ({p.spin}): {p.render_sets.join(', ')}</li>
                {/each}
              </ul>
            {/if}
            {#if asset.animation}
              <h3 class="text-foreground mb-1 text-xs font-semibold">Animation</h3>
              <ul class="text-muted-foreground mb-3" data-testid="aircraft-animation">
                {#each Object.entries(asset.animation.clips) as [k, c] (k)}
                  <li>
                    <span class="text-foreground">{c.role ?? 'clip'}</span>: {k} ({c.duration_s.toFixed(
                      2,
                    )} s{c.loop ? ', loop' : ''}; {c.nodes.join(', ')})
                  </li>
                {/each}
                <li>
                  gear: {asset.animation.gear.mode === 'clip'
                    ? `clips ${asset.animation.gear.down ?? '–'} / ${asset.animation.gear.up ?? '–'}`
                    : 'render-set switch'}
                </li>
                <li>
                  propellers spin about local {asset.animation.propeller_spin.axis.join(', ')}: {[
                    ...asset.animation.propeller_spin.direct,
                    ...asset.animation.propeller_spin.opposite.map((n) => n + ' (opposite)'),
                  ].join(', ') || '–'}
                </li>
                {#if animPlaying}<li>playing {animPlaying}</li>{/if}
                {#if animError}<li class="text-destructive">{animError}</li>{/if}
              </ul>
            {/if}
            <h3 class="text-foreground mb-1 text-xs font-semibold">Hardpoints</h3>
            <ul class="text-muted-foreground mb-3">
              {#each Object.entries(asset.hardpoints) as [kind, names] (kind)}
                <li><span class="text-foreground">{kind}</span>: {names.length}</li>
              {/each}
            </ul>
            {#if Object.keys(asset.moving_parts).length}
              <h3 class="text-foreground mb-1 text-xs font-semibold">Moving parts</h3>
              <ul class="text-muted-foreground mb-3">
                {#each Object.entries(asset.moving_parts) as [group, names] (group)}
                  <li><span class="text-foreground">{group}</span>: {names.join(', ')}</li>
                {/each}
              </ul>
            {/if}
            <h3 class="text-foreground mb-1 text-xs font-semibold">
              GameParams entries ({asset.gameparams.length})
            </h3>
            <ul class="text-muted-foreground">
              {#each asset.gameparams as g (g.key)}
                <li class="font-mono">
                  {g.key}
                  <span class="font-sans"
                    >({g.species}, tier {g.level ?? '–'}{g.plane_subtype.length
                      ? ', ' + g.plane_subtype.join('/')
                      : ''})</span
                  >
                </li>
              {/each}
            </ul>
          </div>
        </div>
      {:else}
        <div
          class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center text-xs"
        >
          Select an airframe from the list.
        </div>
      {/if}
    </section>
  {/if}
</div>
