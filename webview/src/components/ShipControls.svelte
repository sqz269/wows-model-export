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
  import { onMount, untrack } from 'svelte';
  import { Button } from '$lib/components/ui/button';
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type { SeamKey, SeamState, ShipSectionKey } from '$lib/types';
  import type {
    ColorMode,
    LodPolicy,
    NodeCategory,
    ShipViewer,
    ShipParticleEventOption,
    ShipParticleMode,
    ShipParticleStats,
    WgEnvironmentInfo,
  } from '$lib/ship';
  import {
    DEFAULT_BLOOM_PARAMS,
    ARMOR_THICKNESS_STOPS,
    hitboxStyleFor,
    NODE_CATEGORIES,
    NODE_CATEGORY_LABEL,
    NODE_CATEGORY_COLOR,
  } from '$lib/ship';
  import type { CamoDiagnostics } from '$lib/ship/textures';
  import { loadState, patchState, patchNestedState, type PanelSection } from '$lib/store';
  import { rowCls, labelCls, inputBoxCls } from '$lib/ui/controls';
  import { listEnvironments } from '$lib/three/env_ibl';

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
    /** Notify parent inspector surfaces when the WG sky/weather changes. */
    onEnvironmentChange?: (info: WgEnvironmentInfo | null) => void;
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
    onEnvironmentChange,
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
  // Armor + hitbox overlays. Per-ship inspection state (reset on swap, not
  // persisted). `hideHull` is auto-managed on the none↔some-overlay edge but
  // stays user-overridable via its checkbox.
  let armorView = $state(false);
  let hitboxView = $state(false);
  let hideHull = $state(false);
  let hasArmor = $state(false);
  let hasHitbox = $state(false);
  let hitboxCats = $state<Array<{ label: string; hex: string }>>([]);
  // Module damage: destructible mounts + their live/dead state.
  // Reset per ship; toggles drive viewer.setMountDestroyed (the dead-mesh swap).
  type ModuleRow = {
    instanceId: string;
    assetId: string;
    hpName: string | null;
    section: string;
    dead: boolean;
  };
  let hasModules = $state(false);
  let modules = $state<ModuleRow[]>([]);
  const modulesBySection = $derived.by(() => {
    const by = new Map<string, ModuleRow[]>();
    for (const m of modules) {
      let arr = by.get(m.section);
      if (!arr) {
        arr = [];
        by.set(m.section, arr);
      }
      arr.push(m);
    }
    return [...by.entries()].map(([section, rows]) => ({
      section,
      rows,
      deadCount: rows.filter((r) => r.dead).length,
    }));
  });
  // WG bones & VFX-points overlay. Per-ship inspection state (reset on swap,
  // not persisted) — mirrors the armor/aim sections.
  let hasNodes = $state(false);
  let nodesView = $state(false);
  let nodeCats = $state<Record<NodeCategory, boolean>>(
    Object.fromEntries(NODE_CATEGORIES.map((c) => [c, false])) as Record<NodeCategory, boolean>,
  );
  let nodeCounts = $state<Record<NodeCategory, number>>(
    Object.fromEntries(NODE_CATEGORIES.map((c) => [c, 0])) as Record<NodeCategory, number>,
  );
  let hasParticles = $state(false);
  let particlesView = $state(false);
  let particleMode = $state<ShipParticleMode>('ambient');
  let particlesLoading = $state(false);
  let particleStats = $state<Readonly<ShipParticleStats> | null>(null);
  let particleEvents = $state<readonly ShipParticleEventOption[]>([]);
  let particleLoops = $state<ReadonlySet<string>>(new Set());
  let waterEnabled = $state(false);
  const PARTICLE_MODES: Array<{ value: ShipParticleMode; label: string }> = [
    { value: 'ambient', label: 'Ambient' },
    { value: 'all', label: 'All' },
  ];
  // Color swatch CSS for the legend (hex int -> #rrggbb).
  function catSwatch(cat: NodeCategory): string {
    return '#' + NODE_CATEGORY_COLOR[cat].toString(16).padStart(6, '0');
  }
  let seamStates = $state<Record<SeamKey, SeamState>>({
    'Bow-MidFront': 'Intact',
    'MidFront-MidBack': 'Intact',
    'MidBack-Stern': 'Intact',
  });
  let showTextures = $state(false);
  let aoMaps = $state(true);
  let mrMaps = $state(false);
  let normalScale = $state(2.0);
  let bloomEnabled = $state(false);
  let bloomStrength = $state(DEFAULT_BLOOM_PARAMS.strength);
  let bloomRadius = $state(DEFAULT_BLOOM_PARAMS.radius);
  let bloomThreshold = $state(DEFAULT_BLOOM_PARAMS.threshold);
  // Aim controls — global yaw/pitch driving every turret with a rig
  // (gun/main + gun/secondary mounts; AA mounts have no rig and are
  // skipped silently). Stored as degrees in the UI, converted to
  // radians when handed to the rig manager.
  let aimYaw = $state(0);
  let aimPitch = $state(0);
  let rigCount = $state(0);
  // Per-mount yaw/elev clamp is automatic (the rig reads sidecar arcs); this
  // toggles the firing-arc fan overlay (green = can fire, red = no-fire dead
  // zone). `arcCount` = rigs that actually carry traverse limits.
  let aimArcs = $state(false);
  let arcCount = $state(0);

  // Panel open/close — UI-only; tracked separately so toggling a section
  // doesn't trigger the larger $effect that re-reads viewer state.
  let panelOpen = $state(loadState().panelOpen);

  // Camo-debug snapshot. Refreshed on demand (panel-open toggle, manual
  // "Refresh" button) since skin changes don't bump `revision`. Periodic
  // poll while the panel is open is intentional — cheap O(entries) walk.
  let camoDiag = $state<CamoDiagnostics | null>(null);
  let camoDiagTimer: ReturnType<typeof setInterval> | null = null;
  function refreshCamoDiag() {
    try {
      camoDiag = viewer.getCamoDiagnostics();
    } catch {
      camoDiag = null;
    }
  }

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
      viewer.setNormalScale(persisted.normalScale);
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
      normalScale = viewer.getNormalScale();
      bloomEnabled = viewer.getBloomEnabled();
      const bp = viewer.getBloomParams();
      bloomStrength = bp.strength;
      bloomRadius = bp.radius;
      bloomThreshold = bp.threshold;
      // Aim resets per ship — no persistence (per-ship inspector state).
      // Read rig count so the panel can hide aim controls for fleets
      // where no accessory shipped with a bone tree.
      aimYaw = 0;
      aimPitch = 0;
      rigCount = viewer.getTurretRigManager().size();
      // Firing-arc visibility persists across ship swaps (held on the rig
      // manager); re-read it so the checkbox reflects the live state.
      aimArcs = viewer.getTurretRigManager().isFiringArcsVisible();
      arcCount = viewer.getTurretRigManager().countWithLimits();
      onShowTexturesChange?.(newShowTextures);
      onEnvironmentChange?.(viewer.getWgEnvironment());
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

      // Armor + hitbox overlays reset per ship. Read availability + build the
      // hitbox legend (only categories actually present on this ship).
      hasArmor = viewer.hasArmorData();
      hasHitbox = viewer.hasHitboxData();
      armorView = viewer.getArmorViewEnabled();
      hitboxView = viewer.getHitboxViewEnabled();
      hideHull = false;
      const boxes = viewer.getSidecar()?.hitbox?.boxes ?? {};
      const catMap = new Map<string, string>();
      for (const b of Object.values(boxes)) {
        const s = hitboxStyleFor(b);
        catMap.set(s.label, s.hex);
      }
      hitboxCats = [...catMap].map(([label, hex]) => ({ label, hex }));

      // Module damage: destructible mounts (turret/secondary/AA with a
      // glb_dead or standalone _dead model). Reset per ship.
      modules = viewer.getDestructibleMounts();
      hasModules = modules.length > 0;

      // Bones & VFX overlay state (per-ship; not persisted). Re-read the
      // overlay's current visibility + per-category state + counts.
      hasNodes = viewer.hasNodeData();
      nodesView = viewer.getNodesViewEnabled();
      nodeCounts = viewer.getNodeCounts();
      const nc = {} as Record<NodeCategory, boolean>;
      for (const c of NODE_CATEGORIES) nc[c] = viewer.getNodeCategoryVisible(c);
      nodeCats = nc;
      hasParticles = viewer.hasShipParticleData();
      particlesView = viewer.getShipParticlesVisible();
      particleMode = viewer.getShipParticleMode();
      particleStats = viewer.getShipParticleStats();
      waterEnabled = viewer.isShipWaterEnabled();
      refreshParticleEvents();

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

  // ── Environment IBL selector ────────────────────────────────────────────
  // WG sky-cube IBL (PMREM) + keyed exposure, driven by the environment
  // library. The viewer auto-applies a default on construction; this panel
  // reflects + controls it. Empty list (library not built) hides the section.
  let envList = $state<{ space: string; weathers: string[] }[]>([]);
  let envSpace = $state(''); // '' = Procedural (studio)
  let envWeather = $state('');
  let envPanelOpen = $state(true);

  onMount(async () => {
    envList = await listEnvironments();
    if (envList.length === 0) return;
    const cur = viewer.getWgEnvironment();
    if (cur) {
      envSpace = cur.space;
      envWeather = cur.weather;
    } else {
      // Auto-apply may not have resolved yet — mirror its default choice.
      const def = envList.find((e) => e.space === '14_Atlantic') ?? envList[0];
      envSpace = def.space;
      envWeather = def.weathers.includes('Default') ? 'Default' : (def.weathers[0] ?? '');
    }
  });

  function envWeathersFor(space: string): string[] {
    return envList.find((e) => e.space === space)?.weathers ?? [];
  }
  async function onEnvSpace(v: string) {
    envSpace = v;
    if (v === '') {
      viewer.clearWgEnvironment();
      envWeather = '';
      onEnvironmentChange?.(null);
      return;
    }
    const weathers = envWeathersFor(v);
    envWeather = weathers.includes('Default') ? 'Default' : (weathers[0] ?? '');
    await viewer.applyWgEnvironment({ space: v, weather: envWeather });
    onEnvironmentChange?.(viewer.getWgEnvironment());
  }
  async function onEnvWeather(v: string) {
    envWeather = v;
    if (envSpace) {
      await viewer.applyWgEnvironment({ space: envSpace, weather: v });
      onEnvironmentChange?.(viewer.getWgEnvironment());
    }
  }

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
  // Auto-hide the ship's visual meshes (hull + turrets/accessories) on the
  // none→some-overlay edge so the armor shells (hull + per-mount turret armor)
  // read clearly, and restore them when the last overlay turns off. Leaves a
  // manual `hideHull` override untouched in between.
  function reconcileHull(wasAny: boolean) {
    const isAny = armorView || hitboxView;
    if (!wasAny && isAny) {
      hideHull = true;
      viewer.setArmorOnly(true);
    } else if (wasAny && !isAny) {
      hideHull = false;
      viewer.setArmorOnly(false);
    }
  }
  function toggleArmorView(v: boolean) {
    const wasAny = armorView || hitboxView;
    armorView = v;
    viewer.setArmorView(v);
    reconcileHull(wasAny);
  }
  function toggleHitboxView(v: boolean) {
    const wasAny = armorView || hitboxView;
    hitboxView = v;
    viewer.setHitboxView(v);
    reconcileHull(wasAny);
  }
  function toggleHideHull(v: boolean) {
    hideHull = v;
    viewer.setArmorOnly(v);
  }
  function toggleNodesView(v: boolean) {
    nodesView = v;
    viewer.setNodesView(v);
  }
  function toggleNodeCat(cat: NodeCategory, v: boolean) {
    nodeCats[cat] = v;
    nodeCats = { ...nodeCats };
    viewer.setNodeCategoryVisible(cat, v);
  }
  function refreshParticleEvents() {
    particleEvents = viewer.getShipParticleEventOptions();
    particleLoops = new Set(viewer.getShipParticleEventLoops());
  }
  async function toggleParticlesView(v: boolean) {
    particlesView = v;
    particlesLoading = true;
    try {
      particleStats = await viewer.setShipParticlesVisible(v);
      waterEnabled = viewer.isShipWaterEnabled();
      refreshParticleEvents();
    } catch (err) {
      particlesView = viewer.getShipParticlesVisible();
      const msg = err instanceof Error ? err.message : String(err);
      toast.error('Particle layer failed', { description: msg, duration: 8000 });
    } finally {
      particlesLoading = false;
    }
  }
  function toggleWater(v: boolean) {
    waterEnabled = v;
    viewer.setShipWaterEnabled(v);
  }
  async function setParticleMode(mode: ShipParticleMode) {
    particleMode = mode;
    particlesLoading = true;
    try {
      particleStats = await viewer.setShipParticleMode(mode);
      refreshParticleEvents();
    } catch (err) {
      particleMode = viewer.getShipParticleMode();
      const msg = err instanceof Error ? err.message : String(err);
      toast.error('Particle mode failed', { description: msg, duration: 8000 });
    } finally {
      particlesLoading = false;
    }
  }
  async function toggleEventLoop(key: string, on: boolean) {
    const next = new Set(particleLoops);
    if (on) next.add(key);
    else next.delete(key);
    particleLoops = next;
    particlesLoading = true;
    try {
      particleStats = await viewer.setShipParticleEventLoop(key, on);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error('Particle loop failed', { description: msg, duration: 8000 });
    } finally {
      particleLoops = new Set(viewer.getShipParticleEventLoops());
      particlesLoading = false;
    }
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

  // ── Module damage — per-mount dead-mesh swap ────────────────
  async function setModuleDead(instanceId: string, dead: boolean) {
    await viewer.setMountDestroyed(instanceId, dead);
    modules = viewer.getDestructibleMounts();
  }
  async function setSectionDead(section: string, dead: boolean) {
    for (const m of modules) {
      if (m.section === section && m.dead !== dead) {
        await viewer.setMountDestroyed(m.instanceId, dead);
      }
    }
    modules = viewer.getDestructibleMounts();
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
  function setNormalScale(v: number) {
    normalScale = v;
    viewer.setNormalScale(v);
    patchState({ normalScale: v });
  }
  function setAimYaw(deg: number) {
    aimYaw = deg;
    viewer.getTurretRigManager().setGlobalAim((deg * Math.PI) / 180, (aimPitch * Math.PI) / 180);
  }
  function setAimPitch(deg: number) {
    aimPitch = deg;
    viewer.getTurretRigManager().setGlobalAim((aimYaw * Math.PI) / 180, (deg * Math.PI) / 180);
  }
  function resetAim() {
    aimYaw = 0;
    aimPitch = 0;
    viewer.getTurretRigManager().reset();
  }
  function toggleAimArcs(v: boolean) {
    aimArcs = v;
    viewer.getTurretRigManager().setFiringArcsVisible(v);
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
    if (key === 'camo-debug') {
      if (open) {
        refreshCamoDiag();
        // Skin changes don't bump `revision`, so poll while the panel is
        // open. 1s is plenty for a debug surface and the walk is O(entries).
        camoDiagTimer ??= setInterval(refreshCamoDiag, 1000);
      } else if (camoDiagTimer) {
        clearInterval(camoDiagTimer);
        camoDiagTimer = null;
      }
    }
  }

  // Number formatter for the small camo-debug tables.
  function fmtNum(n: number | undefined): string {
    return n === undefined ? '—' : String(n);
  }
  function fmtPct(part: number, total: number): string {
    if (!total) return '—';
    return `${part}/${total}`;
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

<section
  class="bg-card border-border flex w-[280px] flex-none flex-col gap-0 overflow-y-auto border-l"
>
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

  {#if envList.length > 0}
    <details bind:open={envPanelOpen} class="group {detailsCls}">
      <summary class={summaryCls}>Environment</summary>
      <div class={bodyCls}>
        <label class={labelCls}>
          Sky / lighting
          <select
            value={envSpace}
            onchange={(e) => onEnvSpace(e.currentTarget.value)}
            class={inputBoxCls}
          >
            <option value="">Procedural (studio)</option>
            {#each envList as e (e.space)}
              <option value={e.space}>{e.space}</option>
            {/each}
          </select>
        </label>
        {#if envSpace}
          <label class={labelCls}>
            Weather
            <select
              value={envWeather}
              onchange={(e) => onEnvWeather(e.currentTarget.value)}
              class={inputBoxCls}
            >
              {#each envWeathersFor(envSpace) as w (w)}
                <option value={w}>{w}</option>
              {/each}
            </select>
          </label>
        {/if}
        <p class="text-muted-foreground text-[10px] leading-snug">
          WG sky-cube IBL (PMREM) with keyed GT exposure from the space's HDR settings. “Procedural”
          uses a neutral studio environment.
        </p>
      </div>
    </details>
  {/if}

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

  {#if hasArmor || hasHitbox}
    <details
      open={panelOpen['armor-hitbox']}
      ontoggle={(e) => togglePanel('armor-hitbox', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Armor &amp; hitbox</summary>
      <div class={bodyCls}>
        {#if hasArmor}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={armorView}
              onchange={(e) => toggleArmorView(e.currentTarget.checked)}
            />
            Armor (thickness)
          </label>
          {#if armorView}
            <div class="flex flex-wrap gap-x-2 gap-y-0.5 pl-5">
              {#each ARMOR_THICKNESS_STOPS as s (s.mm)}
                <span class="text-muted-foreground flex items-center gap-1 text-[9px] tabular-nums">
                  <span class="inline-block size-2.5 rounded-sm" style="background:{s.hex}"></span>
                  {s.mm}
                </span>
              {/each}
              <span class="text-muted-foreground/70 text-[9px]">mm</span>
            </div>
            <p class="text-muted-foreground/70 pl-5 text-[9px] leading-snug">
              Hover a plate in the 3D view for its thickness, zone &amp; layers.
            </p>
          {/if}
        {/if}

        {#if hasHitbox}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={hitboxView}
              onchange={(e) => toggleHitboxView(e.currentTarget.checked)}
            />
            Hitboxes (damage modules)
          </label>
          {#if hitboxView && hitboxCats.length > 0}
            <div class="flex flex-wrap gap-x-2.5 gap-y-0.5 pl-5">
              {#each hitboxCats as c (c.label)}
                <span class="flex items-center gap-1 text-[10px]">
                  <span class="inline-block size-2.5 rounded-sm" style="background:{c.hex}"></span>
                  {c.label}
                </span>
              {/each}
            </div>
          {/if}
        {/if}

        {#if armorView || hitboxView}
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={hideHull}
              onchange={(e) => toggleHideHull(e.currentTarget.checked)}
            />
            Hide ship (armor only)
          </label>
        {/if}
      </div>
    </details>
  {/if}

  {#if hasModules}
    <details
      open={panelOpen['module-damage'] ?? true}
      ontoggle={(e) => togglePanel('module-damage', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Module damage</summary>
      <div class={bodyCls}>
        <p class="text-muted-foreground/70 text-[9px] leading-snug">
          Swaps a mount to its destroyed (<code>glb_dead</code>) model — a destroyed-state reference,
          visual only. Click a mount to toggle.
        </p>
        {#each modulesBySection as grp (grp.section)}
          <div class="mt-1.5">
            <div class="flex items-center justify-between gap-2">
              <span class="text-[11px] font-medium">
                {grp.section}
                <span class="text-muted-foreground">({grp.deadCount}/{grp.rows.length})</span>
              </span>
              <span class="flex gap-1">
                <Button size="sm" variant="outline" onclick={() => setSectionDead(grp.section, true)}>
                  Destroy all
                </Button>
                <Button size="sm" variant="outline" onclick={() => setSectionDead(grp.section, false)}>
                  Restore
                </Button>
              </span>
            </div>
            {#if grp.rows.length <= 16}
              <div class="mt-1 flex flex-wrap gap-1">
                {#each grp.rows as m (m.instanceId)}
                  <button
                    type="button"
                    title={m.assetId}
                    onclick={() => setModuleDead(m.instanceId, !m.dead)}
                    class="rounded-sm border px-1.5 py-0.5 text-[10px] {m.dead
                      ? 'border-red-500 bg-red-500/20 text-red-300'
                      : 'border-border text-muted-foreground hover:bg-muted'}"
                  >
                    {m.hpName ?? m.instanceId}
                  </button>
                {/each}
              </div>
            {:else}
              <p class="text-muted-foreground/70 mt-1 text-[9px]">
                {grp.rows.length} mounts — use Destroy all / Restore.
              </p>
            {/if}
          </div>
        {/each}
      </div>
    </details>
  {/if}

  {#if hasNodes}
    <details
      open={panelOpen['bones-vfx']}
      ontoggle={(e) => togglePanel('bones-vfx', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Bones &amp; VFX</summary>
      <div class={bodyCls}>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={nodesView}
            onchange={(e) => toggleNodesView(e.currentTarget.checked)}
          />
          Show WG bones &amp; VFX points
        </label>
        <p class="text-muted-foreground text-[10px] leading-snug">
          Hull EP_ effect points (magenta) come from the sidecar; accessory bones, hardpoints +
          muzzle anchors are read live from the model. Hover a marker for its name; pin from the
          Nodes tab.
        </p>
        <div class="flex flex-col gap-1 {!nodesView ? 'opacity-55' : ''}">
          {#each NODE_CATEGORIES as cat (cat)}
            {#if nodeCounts[cat] > 0}
              <label class={rowCls}>
                <input
                  type="checkbox"
                  checked={nodeCats[cat]}
                  disabled={!nodesView}
                  onchange={(e) => toggleNodeCat(cat, e.currentTarget.checked)}
                />
                <span class="inline-block size-2.5 rounded-full" style="background:{catSwatch(cat)}"
                ></span>
                {NODE_CATEGORY_LABEL[cat]}
                <span class="text-muted-foreground tabular-nums text-[10px]">
                  ({nodeCounts[cat]})
                </span>
              </label>
            {/if}
          {/each}
        </div>
      </div>
    </details>
  {/if}

  {#if hasParticles}
    <details
      open={panelOpen.particles}
      ontoggle={(e) => togglePanel('particles', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Particles</summary>
      <div class={bodyCls}>
        <label class={rowCls}>
          <input
            type="checkbox"
            checked={particlesView}
            disabled={particlesLoading}
            onchange={(e) => void toggleParticlesView(e.currentTarget.checked)}
          />
          Ship particles
          {#if particlesLoading}
            <span class="text-muted-foreground/70 text-[10px]">loading</span>
          {/if}
        </label>
        <div class="ml-5 flex w-fit overflow-hidden rounded border border-border text-[10px]">
          {#each PARTICLE_MODES as mode}
            <button
              type="button"
              class="px-2 py-0.5 transition-colors disabled:opacity-50 {particleMode === mode.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-background text-muted-foreground hover:bg-muted hover:text-foreground'}"
              aria-pressed={particleMode === mode.value}
              disabled={particlesLoading}
              onclick={() => void setParticleMode(mode.value)}
            >
              {mode.label}
            </button>
          {/each}
        </div>
        {#if particlesView}
          <label class={`${rowCls} ml-5`}>
            <input
              type="checkbox"
              checked={waterEnabled}
              disabled={particlesLoading}
              onchange={(e) => toggleWater(e.currentTarget.checked)}
            />
            Water surface
          </label>
        {/if}
        {#if particlesView && particleEvents.length > 0}
          <div class="ml-5 flex flex-col gap-0.5">
            <span class="text-muted-foreground text-[10px]">Loop event particles</span>
            {#each particleEvents as event}
              <label class="flex items-center gap-1.5 text-[10px]" title={event.key}>
                <input
                  type="checkbox"
                  checked={particleMode === 'all' || particleLoops.has(event.key)}
                  disabled={particlesLoading || particleMode === 'all'}
                  onchange={(e) => void toggleEventLoop(event.key, e.currentTarget.checked)}
                />
                <span class="truncate">{event.label}</span>
                <span class="text-muted-foreground/60 tabular-nums">({event.handles})</span>
              </label>
            {/each}
          </div>
        {/if}
        {#if particleStats}
          <div class="grid grid-cols-2 gap-x-3 gap-y-0.5 pl-5 text-[10px]">
            <span class="text-muted-foreground">active</span>
            <span class="tabular-nums">{particleStats.activeAttachments}</span>
            <span class="text-muted-foreground">ambient</span>
            <span class="tabular-nums">{particleStats.ambientAttachments}</span>
            <span class="text-muted-foreground">events</span>
            <span class="tabular-nums">{particleStats.eventAttachments}</span>
            <span class="text-muted-foreground">rows</span>
            <span class="tabular-nums">{particleStats.renderableAttachments}</span>
            <span class="text-muted-foreground">anchors</span>
            <span class="tabular-nums">{particleStats.anchorInstances}</span>
            <span class="text-muted-foreground">records</span>
            <span class="tabular-nums"
              >{particleStats.recordsLoaded}/{particleStats.uniquePaths}</span
            >
            <span class="text-muted-foreground">systems</span>
            <span class="tabular-nums">{particleStats.systems}</span>
            {#if particleStats.unanchoredAttachments > 0}
              <span class="text-muted-foreground">unanchored</span>
              <span class="tabular-nums">{particleStats.unanchoredAttachments}</span>
            {/if}
            {#if particleStats.missingRecords > 0}
              <span class="text-muted-foreground">missing</span>
              <span class="text-destructive tabular-nums">{particleStats.missingRecords}</span>
            {/if}
          </div>
        {/if}
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

  {#if rigCount > 0 || arcCount > 0}
    <details
      open={panelOpen.aim}
      ontoggle={(e) => togglePanel('aim', e.currentTarget.open)}
      class="group {detailsCls}"
    >
      <summary class={summaryCls}>Aim{rigCount > 0 ? ` (${rigCount} rigged)` : ''}</summary>
      <div class={bodyCls}>
        {#if rigCount > 0}
          <label class={labelCls}>
            <span class="flex items-center justify-between">
              Yaw
              <span class="text-muted-foreground tabular-nums text-[10px]">
                {aimYaw.toFixed(0)}°
              </span>
            </span>
            <input
              type="range"
              min="-180"
              max="180"
              step="1"
              value={aimYaw}
              oninput={(e) => setAimYaw(parseFloat(e.currentTarget.value))}
            />
          </label>
          <label class={labelCls}>
            <span class="flex items-center justify-between">
              Pitch (elevation)
              <span class="text-muted-foreground tabular-nums text-[10px]">
                {aimPitch.toFixed(0)}°
              </span>
            </span>
            <input
              type="range"
              min="-15"
              max="90"
              step="1"
              value={aimPitch}
              oninput={(e) => setAimPitch(parseFloat(e.currentTarget.value))}
            />
          </label>
        {/if}
        {#if arcCount > 0}
          <p class="text-muted-foreground text-[10px] leading-snug">
            {rigCount > 0 ? 'Aim is clamped per mount to its GameParams arc. ' : ''}{arcCount} mount{arcCount ===
            1
              ? ''
              : 's'} carry a firing arc (incl. static torpedo tubes).
          </p>
          <label class={rowCls}>
            <input
              type="checkbox"
              checked={aimArcs}
              onchange={(e) => toggleAimArcs(e.currentTarget.checked)}
            />
            Show firing arcs
            <span class="inline-block size-2 rounded-sm" style="background:#40e659" title="can fire"
            ></span>
            <span
              class="inline-block size-2 rounded-sm"
              style="background:#f24033"
              title="no-fire dead zone"
            ></span>
          </label>
        {/if}
        {#if rigCount > 0}
          <Button variant="outline" size="xs" class="mt-1.5 w-fit" onclick={resetAim}>
            Reset aim
          </Button>
        {/if}
      </div>
    </details>
  {/if}

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
      <label class="{labelCls} {!showTextures ? 'opacity-55' : ''}">
        <span class="flex items-center justify-between">
          Normal-map intensity
          <span class="text-muted-foreground tabular-nums text-[10px]">
            {normalScale.toFixed(2)}×
          </span>
        </span>
        <input
          type="range"
          min="0"
          max="4"
          step="0.1"
          value={normalScale}
          disabled={!showTextures}
          oninput={(e) => setNormalScale(parseFloat(e.currentTarget.value))}
        />
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

  <details
    open={panelOpen['camo-debug']}
    ontoggle={(e) => togglePanel('camo-debug', e.currentTarget.open)}
    class="group {detailsCls}"
  >
    <summary class={summaryCls}>Camo debug</summary>
    <div class={bodyCls}>
      {#if !camoDiag}
        <span class="text-muted-foreground text-[11px]"
          >No data — refresh once textures are on.</span
        >
        <Button variant="outline" size="xs" class="w-fit" onclick={refreshCamoDiag}>Refresh</Button>
      {:else}
        <div class="flex items-center justify-between">
          <span class="text-muted-foreground text-[10px] uppercase tracking-wide">Active skin</span>
          <Button variant="outline" size="xs" class="h-5 px-2 text-[10px]" onclick={refreshCamoDiag}
            >Refresh</Button
          >
        </div>
        <div class="font-mono text-[11px] leading-snug">
          <div>
            <span class="text-muted-foreground">id:</span>
            {camoDiag.activeSkinId ?? '(none)'}
          </div>
          <div><span class="text-muted-foreground">scheme:</span> {camoDiag.schemeKey}</div>
          <div class="flex items-center gap-1">
            <span class="text-muted-foreground">palette:</span>
            {#if camoDiag.paletteColors}
              <div class="flex gap-0.5">
                {#each camoDiag.paletteColors as c, i (i)}
                  <span
                    class="border-border inline-block size-3 border"
                    style:background-color={`rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${c[3]})`}
                    title={`#${[c[0], c[1], c[2]]
                      .map((v) =>
                        Math.round(v * 255)
                          .toString(16)
                          .padStart(2, '0'),
                      )
                      .join('')} a=${c[3].toFixed(2)}`}
                  ></span>
                {/each}
              </div>
            {:else}
              <span>—</span>
            {/if}
          </div>
        </div>

        <div class="border-border mt-1 border-t pt-1.5">
          <div class="text-muted-foreground mb-0.5 text-[10px] uppercase tracking-wide">
            Entry stats
          </div>
          <div class="font-mono text-[11px] leading-snug grid grid-cols-2 gap-x-2">
            <span class="text-muted-foreground">total:</span><span
              >{fmtNum(camoDiag.entryStats.total)}</span
            >
            <span class="text-muted-foreground">hull:</span><span
              >{fmtNum(camoDiag.entryStats.hullEntries)}</span
            >
            <span class="text-muted-foreground">accessory:</span><span
              >{fmtNum(camoDiag.entryStats.accessoryEntries)}</span
            >
            <span class="text-muted-foreground">camo on:</span><span
              >{fmtNum(camoDiag.entryStats.camoEnabled)}</span
            >
            <span class="text-muted-foreground">mat_albedo on:</span><span
              >{fmtNum(camoDiag.entryStats.matAlbedoEnabled)}</span
            >
            <span class="text-muted-foreground">unpainted:</span><span
              >{fmtNum(camoDiag.entryStats.bothDisabled)}</span
            >
            <span class="text-muted-foreground">transparent:</span><span
              >{fmtNum(camoDiag.entryStats.noCamoEntries)}</span
            >
          </div>
        </div>

        {#if Object.keys(camoDiag.categories).length > 0}
          <div class="border-border mt-1 border-t pt-1.5">
            <div class="text-muted-foreground mb-0.5 text-[10px] uppercase tracking-wide">
              Skin categories
            </div>
            <table class="font-mono text-[10px] w-full">
              <thead>
                <tr class="text-muted-foreground">
                  <th class="text-left font-normal">cat</th>
                  <th class="text-center font-normal" title="categories[cat].mask">msk</th>
                  <th class="text-center font-normal" title="categories[cat].mgn (Path B)">mgn</th>
                  <th class="text-center font-normal" title="mat_textures[cat].albedo">alb</th>
                  <th
                    class="text-right font-normal"
                    title="entries with camoEnable=1 / total in this category">camo</th
                  >
                </tr>
              </thead>
              <tbody>
                {#each Object.keys(camoDiag.categories).sort() as cat (cat)}
                  {@const c = camoDiag.categories[cat]}
                  {@const p = camoDiag.perCategory[cat]}
                  {@const isHull = cat === 'tile' || cat === 'deckhouse' || cat === 'bulge'}
                  <tr class={isHull ? 'text-amber-400' : ''}>
                    <td>{cat}</td>
                    <td class="text-center">{c.hasMask ? '✓' : '·'}</td>
                    <td class="text-center">{c.hasMgn ? '✓' : '·'}</td>
                    <td class="text-center">{c.hasMatAlbedo ? '✓' : '·'}</td>
                    <td class="text-right">{p ? fmtPct(p.camoOn + p.matOn, p.total) : '—'}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <div class="text-muted-foreground mt-1 text-[9px]">
              Hull-side cats in amber. msk = Path A mask, mgn = Path B MGN, alb = mat_albedo atlas.
            </div>
          </div>

          {@const catSet = camoDiag.categories}
          {@const perCat = camoDiag.perCategory}
          {@const unmatched = Object.keys(perCat)
            .filter((k) => !(k in catSet))
            .sort()}
          {#if unmatched.length > 0}
            <div class="border-border mt-1 border-t pt-1.5">
              <div class="text-muted-foreground mb-0.5 text-[10px] uppercase tracking-wide">
                Entries unmatched
              </div>
              <table class="font-mono text-[10px] w-full">
                <thead>
                  <tr class="text-muted-foreground">
                    <th class="text-left font-normal">cat</th>
                    <th class="text-right font-normal">painted</th>
                  </tr>
                </thead>
                <tbody>
                  {#each unmatched as cat (cat)}
                    {@const p = perCat[cat]}
                    <tr class={p.camoOn + p.matOn === 0 ? 'text-red-400' : ''}>
                      <td>{cat}</td>
                      <td class="text-right">{fmtPct(p.camoOn + p.matOn, p.total)}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
              <div class="text-muted-foreground mt-1 text-[9px]">
                Entry categories without a skin-category binding. Red = no paint applied.
              </div>
            </div>
          {/if}
        {/if}

        {#if camoDiag.noCamoKeys.length > 0}
          <details class="border-border group/inner mt-1 border-t pt-1.5">
            <summary
              class="text-muted-foreground hover:text-foreground mb-0.5 cursor-pointer select-none text-[10px] uppercase tracking-wide [&::-webkit-details-marker]:hidden before:content-[''] before:inline-block before:size-0 before:border-y-[3px] before:border-y-transparent before:border-l-[4px] before:border-l-current before:mr-1 before:transition-transform before:translate-y-[-1px] group-open/inner:before:rotate-90"
            >
              No-camo keys ({camoDiag.noCamoKeys.length})
            </summary>
            <div class="max-h-64 overflow-y-auto pt-0.5 font-mono text-[10px] leading-snug">
              {#each camoDiag.noCamoKeys as key (key)}
                <div class="truncate" title={key}>{key}</div>
              {/each}
            </div>
            <div class="text-muted-foreground mt-1 text-[9px]">
              Materials with sidecar <code>shader_intent: "transparent"</code> — camo override skipped.
            </div>
          </details>
        {/if}
      {/if}
    </div>
  </details>
</section>
