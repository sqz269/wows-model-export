<script lang="ts">
  // Controls panel: LOD, color mode, per-section visibility, per-hull-group
  // visibility, per-seam damage state, texture toggles, active skin
  // selector. Wired directly to the ShipViewer handle — no intermediate
  // state. Toggles fire viewer methods synchronously; the viewer
  // re-applies the cascade. The texture toggle and skin radio are async
  // (DDS decoding); progress flows through svelte-sonner toasts so a
  // long decode pass doesn't block the panel UI.
  //
  // Persistence: cosmetic / cross-ship preferences (helpers, LOD,
  // colorMode, per-section visibility, texture-detail toggles, panel
  // open/close) round-trip through `$lib/store`. Per-ship inspection
  // state (seamStates, damageVariants, showTextures, activeSkin) is NOT
  // persisted — those reset per ship by design, matching the legacy v3
  // rule that bumping a seam doesn't bleed into the next ship.
  import { toast } from 'svelte-sonner';
  import { Button } from '$lib/components/ui/button';
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type { SeamKey, SeamState, ShipSectionKey, Skin } from '$lib/types';
  import type { ColorMode, LodPolicy, ShipViewer } from '$lib/ship';
  import { loadState, patchState, patchNestedState, type PanelSection } from '$lib/store';

  interface Props {
    viewer: ShipViewer;
    hullGroups: readonly string[];
    /** Tick when caller wants the panel to re-read state from the viewer. */
    revision: number;
  }

  const { viewer, hullGroups, revision }: Props = $props();

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
  let skins = $state<readonly Skin[]>([]);
  let activeSkin = $state<string | null>(null);

  // Panel open/close — UI-only; tracked separately so toggling a section
  // doesn't trigger the larger $effect that re-reads viewer state.
  let panelOpen = $state(loadState().panelOpen);

  // Sticky-toast ids for async ops. Both texture-toggle and skin-pick are
  // streaming (progress callbacks) so we hold one slot per family and
  // promote on completion. Ship-swap resets aren't strictly needed —
  // svelte-sonner garbage-collects dismissed toasts on its own — but we
  // null them on completion to avoid stale id reuse across reloads.
  let textureToastId: string | number | null = null;
  let skinToastId: string | number | null = null;

  $effect(() => {
    void revision;
    // Apply persisted preferences to the freshly-loaded ship first, then
    // read the resulting viewer state back into the panel mirror so the
    // panel reflects what the user will actually see. The set/get
    // round-trip is the source-of-truth path; persistence is one-way
    // input on each ship swap.
    const persisted = loadState();
    viewer.setHelpers(persisted.helpers);
    viewer.setLodPolicy(persisted.lodPolicy);
    viewer.setColorMode(persisted.colorMode);
    for (const k of SHIP_SECTIONS) {
      viewer.setSectionVisible(k, persisted.sectionVisible[k]);
    }
    viewer.setAoEnabled(persisted.aoMaps);
    viewer.setMrMapEnabled(persisted.mrMaps);
    viewer.setPreserveUnderwaterHull(persisted.preserveUnderwater);

    lodPolicy = viewer.getLodPolicy();
    colorMode = viewer.getColorMode();
    damageVariants = viewer.getDamageVariantsVisible();
    helpers = viewer.getHelpersVisible();
    sectionVisible = { ...persisted.sectionVisible };
    seamStates = { ...viewer.getSeamStates() };
    showTextures = viewer.isShowingTextures();
    aoMaps = viewer.getAoEnabled();
    mrMaps = viewer.getMrMapEnabled();
    preserveUnderwater = viewer.getPreserveUnderwater();
    skins = viewer.getSkins();
    activeSkin = viewer.getActiveSkinId();

    // Hull groups: defaults match the classifier (Armor + Hitboxes hidden).
    // No persistence yet — groups vary per ship, so a cross-ship
    // preference would either need per-ship-name scoping or a fleet-wide
    // pattern list. Skip for now.
    const next: Record<string, boolean> = {};
    for (const g of hullGroups) {
      next[g] = !(g === 'Armor' || g === 'Hitboxes');
    }
    groupVisible = next;
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
  }
  function resetSeams() {
    viewer.resetSeamStates();
    seamStates = { ...viewer.getSeamStates() };
  }

  async function toggleShowTextures(v: boolean) {
    showTextures = v;
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
  async function pickSkin(skinId: string) {
    activeSkin = skinId;
    skinToastId = toast.loading(`Activating ${skinId}…`, {
      duration: Number.POSITIVE_INFINITY,
    });
    try {
      await viewer.setActiveSkin(skinId, (msg) => {
        if (skinToastId !== null) {
          toast.loading(msg, { id: skinToastId, duration: Number.POSITIVE_INFINITY });
        }
      });
      if (skinToastId !== null) {
        toast.success(`Skin: ${skinId}`, { id: skinToastId, duration: 2000 });
        skinToastId = null;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (skinToastId !== null) {
        toast.error(`Failed to apply ${skinId}`, {
          id: skinToastId,
          description: msg,
          duration: 8000,
        });
        skinToastId = null;
      } else {
        toast.error(`Failed to apply ${skinId}`, { description: msg, duration: 8000 });
      }
    }
  }

  function togglePanel(key: PanelSection, open: boolean) {
    panelOpen[key] = open;
    patchNestedState('panelOpen', { [key]: open });
  }

  // Shared Tailwind class slugs for the dense controls. Pulling them
  // into named consts here keeps the markup below scannable; without
  // this each `<details>` / row repeats the same 6-utility chain.
  const detailsCls = 'border-border border-b last:border-b-0';
  const summaryCls =
    'flex items-center gap-1.5 cursor-pointer select-none px-3.5 py-2 text-[11px] uppercase tracking-wider font-semibold text-muted-foreground hover:bg-popover hover:text-foreground [&::-webkit-details-marker]:hidden before:content-[""] before:inline-block before:size-0 before:border-y-[4px] before:border-y-transparent before:border-l-[5px] before:border-l-muted-foreground before:transition-transform group-open:before:rotate-90';
  const bodyCls = 'flex flex-col gap-2 px-3.5 pb-3 pt-1';
  const rowCls = 'flex items-center gap-1.5 text-xs text-foreground';
  const labelCls = 'flex flex-col gap-0.5 text-[11px] text-muted-foreground';
  const inputBoxCls =
    'h-7 rounded border border-border bg-popover px-1.5 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring';
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
          <option value="lod0">LOD 0 only</option>
          <option value="all">All LODs</option>
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

  {#if skins.length > 1}
    <details
      open={panelOpen.skin}
      ontoggle={(e) => togglePanel('skin', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Skin</summary>
      <div class={bodyCls}>
        {#each skins as skin (skin.skin_id)}
          <label class={rowCls}>
            <input
              type="radio"
              name="active-skin"
              checked={activeSkin === skin.skin_id}
              onchange={() => pickSkin(skin.skin_id)}
            />
            <span class="overflow-hidden text-ellipsis whitespace-nowrap">
              {skin.display_name || skin.skin_id}
            </span>
          </label>
        {/each}
      </div>
    </details>
  {/if}
</section>
