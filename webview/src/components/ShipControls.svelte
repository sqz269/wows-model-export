<script lang="ts">
  // Controls panel: LOD, color mode, per-section visibility, per-hull-group
  // visibility, per-seam damage state, texture toggles. Wired directly to
  // the ShipViewer handle — no intermediate state. Toggles fire viewer
  // methods synchronously; the viewer re-applies the cascade. The
  // texture toggle is async (DDS decoding); progress flows through
  // svelte-sonner toasts so a long decode pass doesn't block the panel UI.
  //
  // Skins moved to the bottom-panel `Skins` tab (richer layout + room
  // for more metadata). Frame / Reset camera moved to the header bar.
  // This component is the *view-state controls* surface now —
  // authoring/destructive actions live in the header, read-only
  // inspector info lives in the bottom panel.
  //
  // Persistence: cosmetic / cross-ship preferences (helpers, LOD,
  // colorMode, per-section visibility, texture-detail toggles, panel
  // open/close) round-trip through `$lib/store`. Per-ship inspection
  // state (seamStates, damageVariants, showTextures) is NOT persisted —
  // those reset per ship by design, matching the legacy v3 rule that
  // bumping a seam doesn't bleed into the next ship.
  import { toast } from 'svelte-sonner';
  import { untrack } from 'svelte';
  import { Button } from '$lib/components/ui/button';
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type { SeamKey, SeamState, ShipSectionKey } from '$lib/types';
  import type { ColorMode, LodPolicy, ShipViewer } from '$lib/ship';
  import { DEFAULT_BLOOM_PARAMS } from '$lib/ship';
  import { loadState, patchState, patchNestedState, type PanelSection } from '$lib/store';
  import { rowCls, labelCls, inputBoxCls } from '$lib/ui/controls';

  interface Props {
    viewer: ShipViewer;
    hullGroups: readonly string[];
    /** LOD levels present on the loaded ship (ascending; 0 = high-
     *  detail default). The dropdown adds one option per level plus an
     *  "all" mixer. */
    lodLevels: readonly number[];
    /** Tick when caller wants the panel to re-read state from the viewer. */
    revision: number;
    /** Notify the parent when textures toggle (so the header pill +
     *  any other surface mirroring this state stays in sync). */
    onShowTexturesChange?: (v: boolean) => void;
    /** Notify when seam states change so the bottom-panel Damage tab
     *  can refresh its snapshot without polling. */
    onSeamStatesChange?: (states: Readonly<Record<SeamKey, SeamState>>) => void;
  }

  const {
    viewer,
    hullGroups,
    lodLevels,
    revision,
    onShowTexturesChange,
    onSeamStatesChange,
  }: Props = $props();

  // Local mirror of viewer state. Read once per `revision` bump so the
  // panel reflects whatever the viewer is actually doing, even across
  // ship swaps. Persisted preferences are applied to the viewer first
  // so a reload picks up the user's last cross-ship choices.
  let lodPolicy = $state<LodPolicy>('lod0');
  let colorMode = $state<ColorMode>('off');
  let damageVariants = $state(false);
  let helpers = $state(true);
  let sectionVisible = $state<Record<ShipSectionKey, boolean>>({
    turrets: true,
    secondaries: true,
    antiair: true,
    torpedoes: true,
    accessories: true,
  });
  let groupVisible = $state<Record<string, boolean>>({});
  let seamStates = $state<Record<SeamKey, SeamState>>({
    'Bow-MidFront': 'Intact',
    'MidFront-MidBack': 'Intact',
    'MidBack-Stern': 'Intact',
  });
  let showTextures = $state(false);
  let aoMaps = $state(true);
  let mrMaps = $state(false);
  let preserveUnderwater = $state(true);
  let bloomEnabled = $state(false);
  let bloomStrength = $state(DEFAULT_BLOOM_PARAMS.strength);
  let bloomRadius = $state(DEFAULT_BLOOM_PARAMS.radius);
  let bloomThreshold = $state(DEFAULT_BLOOM_PARAMS.threshold);

  // Panel open/close — UI-only; tracked separately so toggling a section
  // doesn't trigger the larger $effect that re-reads viewer state.
  let panelOpen = $state(loadState().panelOpen);

  // Sticky-toast id for the texture-toggle async op. Streaming progress
  // callbacks promote on completion; one slot is held across the run.
  let textureToastId: string | number | null = null;

  $effect(() => {
    void revision;
    // Apply persisted preferences to the freshly-loaded ship first, then
    // read the resulting viewer state back into the panel mirror so the
    // panel reflects what the user will actually see. The set/get
    // round-trip is the source-of-truth path; persistence is one-way
    // input on each ship swap.
    //
    // The whole body runs inside untrack() so the only tracked dep is
    // `revision` (deliberate trigger). Without this, reading $state
    // mirrors after writing them — or reading the callback props —
    // would re-trigger the effect and the parent's mirror-write
    // callbacks would loop ad infinitum.
    untrack(() => {
      const persisted = loadState();
      viewer.setHelpers(persisted.helpers);
      // Clamp the persisted LOD policy to a level that actually exists
      // on this ship — a ship without LOD 2 meshes shouldn't get a
      // policy that hides everything just because the previous ship
      // had a deeper LOD chain.
      const effectiveLod = resolveLodPolicy(persisted.lodPolicy, lodLevels);
      viewer.setLodPolicy(effectiveLod);
      viewer.setColorMode(persisted.colorMode);
      for (const k of SHIP_SECTIONS) {
        viewer.setSectionVisible(k, persisted.sectionVisible[k]);
      }
      viewer.setAoEnabled(persisted.aoMaps);
      viewer.setMrMapEnabled(persisted.mrMaps);
      viewer.setPreserveUnderwaterHull(persisted.preserveUnderwater);
      // Bloom params must be set BEFORE enabling so the lazy composer
      // build picks them up; the params setter is a no-op until the
      // composer exists, but that's the right order regardless.
      viewer.setBloomParams({
        strength: persisted.bloomStrength,
        radius: persisted.bloomRadius,
        threshold: persisted.bloomThreshold,
      });
      viewer.setBloomEnabled(persisted.bloomEnabled);

      const newSeamStates = { ...viewer.getSeamStates() };
      const newShowTextures = viewer.isShowingTextures();

      lodPolicy = viewer.getLodPolicy();
      colorMode = viewer.getColorMode();
      damageVariants = viewer.getDamageVariantsVisible();
      helpers = viewer.getHelpersVisible();
      sectionVisible = { ...persisted.sectionVisible };
      seamStates = newSeamStates;
      showTextures = newShowTextures;
      aoMaps = viewer.getAoEnabled();
      mrMaps = viewer.getMrMapEnabled();
      preserveUnderwater = viewer.getPreserveUnderwater();
      bloomEnabled = viewer.getBloomEnabled();
      const bp = viewer.getBloomParams();
      bloomStrength = bp.strength;
      bloomRadius = bp.radius;
      bloomThreshold = bp.threshold;
      onShowTexturesChange?.(newShowTextures);
      onSeamStatesChange?.(newSeamStates);

      // Hull groups: defaults match the classifier (Armor + Hitboxes hidden).
      // No persistence yet — groups vary per ship, so a cross-ship
      // preference would either need per-ship-name scoping or a fleet-wide
      // pattern list. Skip for now.
      const next: Record<string, boolean> = {};
      for (const g of hullGroups) {
        next[g] = !(g === 'Armor' || g === 'Hitboxes');
      }
      groupVisible = next;

      // Honor the persisted Show-textures choice. Each ship's viewer
      // starts with textures off (TextureManager.clearShip()); kick off
      // the async decode here so the user's last toggle carries across
      // ship swaps. Fire-and-forget — the toast tracks progress.
      //
      // Gate on `revision > 0`: the very first $effect run happens at
      // mount, before the parent has bumped controlsRevision for a
      // completed ship-load. At that point TextureManager.entries is
      // still empty, so setShowTextures(true) would mark the pipeline
      // active without applying anything — and the post-load revision
      // bump would then see isShowingTextures()=true and skip the real
      // decode pass. The premature toast pair also raced svelte-sonner's
      // height/toast bookkeeping, corrupting the heights array and
      // breaking subsequent reactivity (ship navigation hung). Wait for
      // the parent's first real load completion before auto-restoring.
      if (revision > 0 && persisted.showTextures && !newShowTextures) {
        void toggleShowTextures(true);
      }
    });
  });

  function toggleHelpers(v: boolean) {
    helpers = v;
    viewer.setHelpers(v);
    patchState({ helpers: v });
  }
  function setLod(v: LodPolicy) {
    lodPolicy = v;
    viewer.setLodPolicy(v);
    patchState({ lodPolicy: v });
  }
  function setColor(v: ColorMode) {
    colorMode = v;
    viewer.setColorMode(v);
    patchState({ colorMode: v });
  }
  function toggleSection(k: ShipSectionKey, v: boolean) {
    sectionVisible[k] = v;
    viewer.setSectionVisible(k, v);
    patchNestedState('sectionVisible', { [k]: v });
  }
  function toggleGroup(name: string, v: boolean) {
    groupVisible[name] = v;
    viewer.setHullGroupVisible(name, v);
  }
  function toggleDamageVariants(v: boolean) {
    damageVariants = v;
    viewer.setDamageVariantsVisible(v);
  }
  function setSeam(k: SeamKey, v: SeamState) {
    seamStates[k] = v;
    viewer.setSeamState(k, v);
    onSeamStatesChange?.(seamStates);
  }
  function resetSeams() {
    viewer.resetSeamStates();
    seamStates = { ...viewer.getSeamStates() };
    onSeamStatesChange?.(seamStates);
  }

  async function toggleShowTextures(v: boolean) {
    showTextures = v;
    onShowTexturesChange?.(v);
    // Persist so ship swaps + page reloads pick up the same choice.
    // DDS decoding is expensive but the decoded textures are cached per
    // asset across ship swaps, so re-applying is usually fast.
    patchState({ showTextures: v });
    if (v) {
      textureToastId = toast.loading('Decoding DDS textures…', {
        duration: Number.POSITIVE_INFINITY,
      });
    }
    try {
      await viewer.setShowTextures(v, (msg) => {
        if (textureToastId !== null) {
          toast.loading(msg, { id: textureToastId, duration: Number.POSITIVE_INFINITY });
        }
      });
      if (textureToastId !== null) {
        toast.success(v ? 'Textures applied' : 'Textures off', {
          id: textureToastId,
          duration: 2000,
        });
        textureToastId = null;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (textureToastId !== null) {
        toast.error('Texture pipeline failed', {
          id: textureToastId,
          description: msg,
          duration: 8000,
        });
        textureToastId = null;
      } else {
        toast.error('Texture pipeline failed', { description: msg, duration: 8000 });
      }
    }
  }
  function toggleAo(v: boolean) {
    aoMaps = v;
    viewer.setAoEnabled(v);
    patchState({ aoMaps: v });
  }
  function toggleMr(v: boolean) {
    mrMaps = v;
    viewer.setMrMapEnabled(v);
    patchState({ mrMaps: v });
  }
  function togglePreserveUnderwater(v: boolean) {
    preserveUnderwater = v;
    viewer.setPreserveUnderwaterHull(v);
    patchState({ preserveUnderwater: v });
  }
  function toggleBloom(v: boolean) {
    bloomEnabled = v;
    viewer.setBloomEnabled(v);
    patchState({ bloomEnabled: v });
  }
  function setBloomStrength(v: number) {
    bloomStrength = v;
    viewer.setBloomParams({ strength: v });
    patchState({ bloomStrength: v });
  }
  function setBloomRadius(v: number) {
    bloomRadius = v;
    viewer.setBloomParams({ radius: v });
    patchState({ bloomRadius: v });
  }
  function setBloomThreshold(v: number) {
    bloomThreshold = v;
    viewer.setBloomParams({ threshold: v });
    patchState({ bloomThreshold: v });
  }
  function resetBloom() {
    setBloomStrength(DEFAULT_BLOOM_PARAMS.strength);
    setBloomRadius(DEFAULT_BLOOM_PARAMS.radius);
    setBloomThreshold(DEFAULT_BLOOM_PARAMS.threshold);
  }
  /** Snap a persisted LOD policy onto the levels available on the
   *  currently-loaded ship. Out-of-range falls back to lod0; if the
   *  ship somehow has no level 0 either, falls back to `all`. */
  function resolveLodPolicy(p: LodPolicy, available: readonly number[]): LodPolicy {
    if (p === 'all') return 'all';
    const level = parseInt(p.slice(3), 10);
    if (Number.isFinite(level) && available.includes(level)) return p;
    if (available.includes(0)) return 'lod0';
    return 'all';
  }

  function togglePanel(key: PanelSection, open: boolean) {
    panelOpen[key] = open;
    patchNestedState('panelOpen', { [key]: open });
  }

  // Page-local accents for the `<details>` collapsible vocabulary. The
  // cross-page idioms (rowCls / labelCls / inputBoxCls) come from
  // $lib/ui/controls; the chevron-summary styling below is unique to
  // this panel.
  const detailsCls = 'border-border border-b last:border-b-0';
  const summaryCls =
    'flex items-center gap-1.5 cursor-pointer select-none px-3.5 py-2 text-[11px] uppercase tracking-wider font-semibold text-muted-foreground hover:bg-popover hover:text-foreground [&::-webkit-details-marker]:hidden before:content-[""] before:inline-block before:size-0 before:border-y-[4px] before:border-y-transparent before:border-l-[5px] before:border-l-muted-foreground before:transition-transform group-open:before:rotate-90';
  const bodyCls = 'flex flex-col gap-2 px-3.5 pb-3 pt-1';
</script>

<section class="bg-card border-border flex w-[280px] flex-none flex-col gap-0 overflow-y-auto border-l">
  <details
    open={panelOpen.view}
    ontoggle={(e) => togglePanel('view', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>View</summary>
    <div class={bodyCls}>
      <label class={rowCls}>
        <input
          type="checkbox"
          checked={helpers}
          onchange={(e) => toggleHelpers(e.currentTarget.checked)}
        />
        Helpers (grid + axes)
      </label>

      <label class={labelCls}>
        LOD
        <select
          value={lodPolicy}
          onchange={(e) => setLod(e.currentTarget.value as LodPolicy)}
          class={inputBoxCls}
        >
          {#each lodLevels as level (level)}
            <option value={`lod${level}`}>LOD {level} only</option>
          {/each}
          {#if lodLevels.length > 1}
            <option value="all">All LODs</option>
          {/if}
        </select>
      </label>

      <label class={labelCls}>
        Color mode
        <select
          value={colorMode}
          onchange={(e) => setColor(e.currentTarget.value as ColorMode)}
          class={inputBoxCls}
        >
          <option value="off">Original materials</option>
          <option value="category">By category</option>
          <option value="hullSection">By hull section</option>
        </select>
      </label>
    </div>
  </details>

  <details
    open={panelOpen.sections}
    ontoggle={(e) => togglePanel('sections', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>Sections</summary>
    <div class={bodyCls}>
      {#each SHIP_SECTIONS as section (section)}
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={sectionVisible[section]}
            onchange={(e) => toggleSection(section, e.currentTarget.checked)}
          />
          {section}
        </label>
      {/each}
    </div>
  </details>

  {#if hullGroups.length > 0}
    <details
      open={panelOpen['hull-groups']}
      ontoggle={(e) => togglePanel('hull-groups', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Hull groups</summary>
      <div class={bodyCls}>
        {#each hullGroups as g (g)}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={!!groupVisible[g]}
              onchange={(e) => toggleGroup(g, e.currentTarget.checked)}
            />
            {g}
          </label>
        {/each}
      </div>
    </details>
  {/if}

  <details
    open={panelOpen.damage}
    ontoggle={(e) => togglePanel('damage', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>Damage</summary>
    <div class={bodyCls}>
      <label class={rowCls}>
        <input
          type="checkbox"
          checked={damageVariants}
          onchange={(e) => toggleDamageVariants(e.currentTarget.checked)}
        />
        Force-show patches + cracks
      </label>
      {#each SEAMS as seam (seam)}
        <div class="mt-1 flex flex-col gap-0.5">
          <span class="text-[11px] text-muted-foreground">{seam}</span>
          <div class="flex gap-2.5">
            <label class={rowCls}>
              <input
                type="radio"
                name={`seam-${seam}`}
                checked={seamStates[seam] === 'Intact'}
                onchange={() => setSeam(seam, 'Intact')}
              />
              Intact
            </label>
            <label class={rowCls}>
              <input
                type="radio"
                name={`seam-${seam}`}
                checked={seamStates[seam] === 'Broken'}
                onchange={() => setSeam(seam, 'Broken')}
              />
              Broken
            </label>
          </div>
        </div>
      {/each}
      <Button variant="outline" size="xs" class="mt-1.5 w-fit" onclick={resetSeams}>
        Reset seams
      </Button>
    </div>
  </details>

  <details
    open={panelOpen.textures}
    ontoggle={(e) => togglePanel('textures', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>Textures</summary>
    <div class={bodyCls}>
      <label class={rowCls}>
        <input
          type="checkbox"
          checked={showTextures}
          onchange={(e) => toggleShowTextures(e.currentTarget.checked)}
        />
        Show textures
      </label>
      <label class="{rowCls} {!showTextures ? 'opacity-55' : ''}">
        <input
          type="checkbox"
          checked={aoMaps}
          disabled={!showTextures}
          onchange={(e) => toggleAo(e.currentTarget.checked)}
        />
        AO maps
      </label>
      <label class="{rowCls} {!showTextures ? 'opacity-55' : ''}">
        <input
          type="checkbox"
          checked={mrMaps}
          disabled={!showTextures}
          onchange={(e) => toggleMr(e.currentTarget.checked)}
        />
        Metallic/roughness maps
      </label>
      <label class="{rowCls} {!showTextures ? 'opacity-55' : ''}">
        <input
          type="checkbox"
          checked={preserveUnderwater}
          disabled={!showTextures}
          onchange={(e) => togglePreserveUnderwater(e.currentTarget.checked)}
        />
        Preserve underwater hull
      </label>
    </div>
  </details>

  <details
    open={panelOpen.effects}
    ontoggle={(e) => togglePanel('effects', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>Effects</summary>
    <div class={bodyCls}>
      <label class={rowCls}>
        <input
          type="checkbox"
          checked={bloomEnabled}
          onchange={(e) => toggleBloom(e.currentTarget.checked)}
        />
        Bloom (emissive glow)
      </label>
      <label class="{labelCls} {!bloomEnabled ? 'opacity-55' : ''}">
        <span class="flex items-center justify-between">
          Strength
          <span class="text-muted-foreground tabular-nums text-[10px]">
            {bloomStrength.toFixed(2)}
          </span>
        </span>
        <input
          type="range"
          min="0"
          max="3"
          step="0.05"
          value={bloomStrength}
          disabled={!bloomEnabled}
          oninput={(e) => setBloomStrength(parseFloat(e.currentTarget.value))}
        />
      </label>
      <label class="{labelCls} {!bloomEnabled ? 'opacity-55' : ''}">
        <span class="flex items-center justify-between">
          Radius
          <span class="text-muted-foreground tabular-nums text-[10px]">
            {bloomRadius.toFixed(2)}
          </span>
        </span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={bloomRadius}
          disabled={!bloomEnabled}
          oninput={(e) => setBloomRadius(parseFloat(e.currentTarget.value))}
        />
      </label>
      <label class="{labelCls} {!bloomEnabled ? 'opacity-55' : ''}">
        <span class="flex items-center justify-between">
          Threshold
          <span class="text-muted-foreground tabular-nums text-[10px]">
            {bloomThreshold.toFixed(2)}
          </span>
        </span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={bloomThreshold}
          disabled={!bloomEnabled}
          oninput={(e) => setBloomThreshold(parseFloat(e.currentTarget.value))}
        />
      </label>
      <Button
        variant="outline"
        size="xs"
        class="mt-1 w-fit"
        disabled={!bloomEnabled}
        onclick={resetBloom}
      >
        Reset
      </Button>
    </div>
  </details>

</section>
