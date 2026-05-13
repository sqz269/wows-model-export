<script lang="ts">
  // Controls panel: LOD, color mode, per-section visibility, per-hull-group
  // visibility, per-seam damage state, texture toggles, active skin
  // selector. Wired directly to the ShipViewer handle — no intermediate
  // state. Toggles fire viewer methods synchronously; the viewer
  // re-applies the cascade. The texture toggle and skin radio are async
  // (DDS decoding); the panel shows a progress line during work.
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type { SeamKey, SeamState, ShipSectionKey, Skin } from '$lib/types';
  import type { ColorMode, LodPolicy, ShipViewer } from '$lib/ship';

  interface Props {
    viewer: ShipViewer;
    hullGroups: readonly string[];
    /** Tick when caller wants the panel to re-read state from the viewer. */
    revision: number;
  }

  const { viewer, hullGroups, revision }: Props = $props();

  // Local mirror of viewer state. We read once per `revision` bump so the
  // panel shows whatever the viewer is actually doing, even across ship swaps.
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
  let textureProgress = $state('');
  let skins = $state<readonly Skin[]>([]);
  let activeSkin = $state<string | null>(null);

  $effect(() => {
    void revision;
    lodPolicy = viewer.getLodPolicy();
    colorMode = viewer.getColorMode();
    damageVariants = viewer.getDamageVariantsVisible();
    helpers = viewer.getHelpersVisible();
    seamStates = { ...viewer.getSeamStates() };
    showTextures = viewer.isShowingTextures();
    aoMaps = viewer.getAoEnabled();
    mrMaps = viewer.getMrMapEnabled();
    preserveUnderwater = viewer.getPreserveUnderwater();
    skins = viewer.getSkins();
    activeSkin = viewer.getActiveSkinId();
    const next: Record<string, boolean> = {};
    for (const g of hullGroups) {
      // Hide Armor + Hitboxes by default (matches the classifier).
      next[g] = !(g === 'Armor' || g === 'Hitboxes');
    }
    groupVisible = next;
  });

  function toggleHelpers(v: boolean) {
    helpers = v;
    viewer.setHelpers(v);
  }
  function setLod(v: LodPolicy) {
    lodPolicy = v;
    viewer.setLodPolicy(v);
  }
  function setColor(v: ColorMode) {
    colorMode = v;
    viewer.setColorMode(v);
  }
  function toggleSection(k: ShipSectionKey, v: boolean) {
    sectionVisible[k] = v;
    viewer.setSectionVisible(k, v);
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
    textureProgress = v ? 'Decoding DDS textures…' : '';
    try {
      await viewer.setShowTextures(v, (msg) => (textureProgress = msg));
    } catch (err) {
      textureProgress = `Texture error: ${err instanceof Error ? err.message : String(err)}`;
    }
  }
  function toggleAo(v: boolean) {
    aoMaps = v;
    viewer.setAoEnabled(v);
  }
  function toggleMr(v: boolean) {
    mrMaps = v;
    viewer.setMrMapEnabled(v);
  }
  function togglePreserveUnderwater(v: boolean) {
    preserveUnderwater = v;
    viewer.setPreserveUnderwaterHull(v);
  }
  async function pickSkin(skinId: string) {
    activeSkin = skinId;
    textureProgress = `Activating ${skinId}…`;
    try {
      await viewer.setActiveSkin(skinId, (msg) => (textureProgress = msg));
    } catch (err) {
      textureProgress = `Skin error: ${err instanceof Error ? err.message : String(err)}`;
    }
  }
</script>

<section class="controls">
  <h3>View</h3>
  <label class="inline">
    <input
      type="checkbox"
      checked={helpers}
      onchange={(e) => toggleHelpers(e.currentTarget.checked)}
    />
    Helpers (grid + axes)
  </label>

  <label>
    LOD
    <select value={lodPolicy} onchange={(e) => setLod(e.currentTarget.value as LodPolicy)}>
      <option value="lod0">LOD 0 only</option>
      <option value="all">All LODs</option>
    </select>
  </label>

  <label>
    Color mode
    <select value={colorMode} onchange={(e) => setColor(e.currentTarget.value as ColorMode)}>
      <option value="off">Original materials</option>
      <option value="category">By category</option>
      <option value="hullSection">By hull section</option>
    </select>
  </label>

  <h3>Sections</h3>
  {#each SHIP_SECTIONS as section (section)}
    <label class="inline">
      <input
        type="checkbox"
        checked={sectionVisible[section]}
        onchange={(e) => toggleSection(section, e.currentTarget.checked)}
      />
      {section}
    </label>
  {/each}

  {#if hullGroups.length > 0}
    <h3>Hull groups</h3>
    {#each hullGroups as g (g)}
      <label class="inline">
        <input
          type="checkbox"
          checked={!!groupVisible[g]}
          onchange={(e) => toggleGroup(g, e.currentTarget.checked)}
        />
        {g}
      </label>
    {/each}
  {/if}

  <h3>Damage</h3>
  <label class="inline">
    <input
      type="checkbox"
      checked={damageVariants}
      onchange={(e) => toggleDamageVariants(e.currentTarget.checked)}
    />
    Force-show patches + cracks
  </label>
  {#each SEAMS as seam (seam)}
    <div class="seam-row">
      <span>{seam}</span>
      <div class="seam-pair">
        <label class="inline">
          <input
            type="radio"
            name={`seam-${seam}`}
            checked={seamStates[seam] === 'Intact'}
            onchange={() => setSeam(seam, 'Intact')}
          />
          Intact
        </label>
        <label class="inline">
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
  <button type="button" class="reset" onclick={resetSeams}>Reset seams</button>

  <h3>Textures</h3>
  <label class="inline">
    <input
      type="checkbox"
      checked={showTextures}
      onchange={(e) => toggleShowTextures(e.currentTarget.checked)}
    />
    Show textures
  </label>
  {#if textureProgress}
    <div class="progress">{textureProgress}</div>
  {/if}
  <label class="inline" class:dim={!showTextures}>
    <input
      type="checkbox"
      checked={aoMaps}
      disabled={!showTextures}
      onchange={(e) => toggleAo(e.currentTarget.checked)}
    />
    AO maps
  </label>
  <label class="inline" class:dim={!showTextures}>
    <input
      type="checkbox"
      checked={mrMaps}
      disabled={!showTextures}
      onchange={(e) => toggleMr(e.currentTarget.checked)}
    />
    Metallic/roughness maps
  </label>
  <label class="inline" class:dim={!showTextures}>
    <input
      type="checkbox"
      checked={preserveUnderwater}
      disabled={!showTextures}
      onchange={(e) => togglePreserveUnderwater(e.currentTarget.checked)}
    />
    Preserve underwater hull
  </label>

  {#if skins.length > 1}
    <h3>Skin</h3>
    {#each skins as skin (skin.skin_id)}
      <label class="inline">
        <input
          type="radio"
          name="active-skin"
          checked={activeSkin === skin.skin_id}
          onchange={() => pickSkin(skin.skin_id)}
        />
        <span class="skin-name">{skin.display_name || skin.skin_id}</span>
      </label>
    {/each}
  {/if}
</section>

<style>
  .controls {
    flex: 0 0 280px;
    border-left: 1px solid var(--border);
    background: var(--bg-side);
    padding: 12px 14px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3 {
    margin: 8px 0 2px;
    font-size: 11px;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  label {
    display: flex;
    flex-direction: column;
    font-size: 11px;
    color: var(--fg-dim);
    gap: 2px;
  }
  label.inline {
    flex-direction: row;
    align-items: center;
    gap: 6px;
    color: var(--fg);
    font-size: 12px;
  }
  select {
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    padding: 4px 6px;
    border-radius: 4px;
    font-size: 12px;
  }
  .seam-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-top: 4px;
  }
  .seam-row > span {
    font-size: 11px;
    color: var(--fg-dim);
  }
  .seam-pair {
    display: flex;
    gap: 10px;
  }
  .reset {
    margin-top: 6px;
    background: var(--bg-elev);
    color: var(--fg-dim);
    border: 1px solid var(--border);
    padding: 5px 8px;
    border-radius: 4px;
    font-size: 11px;
  }
  .reset:hover {
    background: var(--bg-elev-2);
    color: var(--fg);
  }
  .dim {
    opacity: 0.55;
  }
  .progress {
    font-size: 11px;
    color: var(--fg-muted);
    font-variant-numeric: tabular-nums;
    padding: 2px 0;
  }
  .skin-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
