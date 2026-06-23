<script lang="ts">
  // Tabbed read-only inspector that lives under the ShipViewer.
  //
  // Tabs:
  //   - Overview    : display_name + nation/tier + load timing + section counts
  //   - Placements  : per-section rendered/requested totals + miscFilter drops
  //   - Unresolved  : asset_ids referenced by the ship that the library couldn't resolve
  //   - Hull        : per-hull-group mesh + triangle counts
  //   - Skins       : full list with click-to-activate (hidden when ≤ 1 skin)
  //   - Damage      : seam state matrix snapshot
  //   - Pick        : currently-picked mesh details (hidden when nothing picked)
  //
  // Resize/collapse mechanics mirror DetailBottomPanel exactly: drag the
  // handle at the top to grow/shrink, persisted to localStorage; below
  // ~36px snaps to a slim tab-strip. Tab choice is persisted; auto-
  // switching to Unresolved (via header pill) or Pick (when user clicks a
  // mesh) does NOT overwrite the persisted choice.
  //
  // External control: parent calls `selectTab(tab)` via the `bindHandle`
  // callback to trigger a tab switch from outside (e.g. the header's
  // "N unresolved" pill).

  import { onMount, untrack } from 'svelte';
  import ExternalLink from '@lucide/svelte/icons/external-link';
  import { Button } from '$lib/components/ui/button';
  import { navigate } from '$lib/router';
  import { tabBtnBase } from '$lib/ui/controls';
  import { SHIP_SECTIONS, SEAMS } from '$lib/types';
  import type {
    ExteriorRecord,
    LibraryIndex,
    SeamKey,
    SeamState,
    ShipSectionKey,
    ShipSummary,
    SidecarDoc,
    Skin,
  } from '$lib/types';
  import type {
    NodeCategory,
    NodeEntry,
    PickResult,
    ShipLoadStats,
    ShipViewer,
    WgEnvironmentInfo,
  } from '$lib/ship';
  import {
    thicknessToColorHex,
    hitboxStyleFor,
    NODE_CATEGORY_LABEL,
    NODE_CATEGORY_COLOR,
  } from '$lib/ship';
  import DdsTexturePreview from './DdsTexturePreview.svelte';

  export type ShipBottomTab =
    | 'overview'
    | 'placements'
    | 'unresolved'
    | 'hull'
    | 'armor'
    | 'skins'
    | 'exteriors'
    | 'damage'
    | 'textures'
    | 'nodes'
    | 'particles'
    | 'pick';

  // PBR-read slot order for the Textures tab, matching the AssetDetail
  // bottom panel.
  const TEXTURE_SLOT_ORDER = [
    'baseColor',
    'normal',
    'metallicRoughness',
    'occlusion',
    'emissive',
    'camoMask',
  ] as const;

  // Scheme ordering — `main` first, `dead` next, then camo variants
  // alphabetised. Anything unknown trails after.
  function schemeWeight(key: string): number {
    if (key === 'main') return 0;
    if (key === 'dead') return 1;
    if (key.startsWith('camo_')) return 2;
    if (key.startsWith('dead_camo_')) return 3;
    return 4;
  }

  export interface ShipBottomPanelHandle {
    selectTab: (tab: ShipBottomTab) => void;
  }

  interface Props {
    ship: ShipSummary;
    viewer: ShipViewer | null;
    loadStats: ShipLoadStats | null;
    library: LibraryIndex | null;
    /** Bumped by the parent whenever viewer-side mirror state may have
     *  changed (ship load, skin pick, etc.) — drives a re-read of the
     *  hull stats so the Hull tab reflects the current ship. */
    revision: number;
    /** Active WG environment snapshot from the viewer, or null for procedural studio lighting. */
    envInfo: WgEnvironmentInfo | null;
    skins: readonly Skin[];
    activeSkin: string | null;
    /** Async skin-activate handler (lives in the parent so the side
     *  panel + bottom panel share one toast id). */
    onPickSkin: (skinId: string) => void;
    /** Mesh-swap permoflage selector (ship-exterior unification). Index 0
     *  is always the vanilla `default`; hidden when that's all there is. */
    exteriors: readonly ExteriorRecord[];
    activeExteriorId: string | null;
    onPickExterior: (exteriorId: string) => void;
    /** Snapshot of seam states. Updated on `revision` bump. */
    seamStates: Readonly<Record<SeamKey, SeamState>>;
    /** Currently-picked mesh (null = nothing picked). Drives the Pick
     *  tab's content + auto-switching. */
    selectedPick: PickResult | null;
    /** Clear the pick selection (called by the Pick tab's close button
     *  + Esc key handler in the parent). */
    onClosePick: () => void;
    /** Expose a `selectTab` method so the parent (and the header's
     *  "N unresolved" pill) can drive the active tab from outside. */
    bindHandle?: (h: ShipBottomPanelHandle) => void;
  }

  const {
    ship,
    viewer,
    loadStats,
    library,
    revision,
    envInfo,
    skins,
    activeSkin,
    onPickSkin,
    exteriors,
    activeExteriorId,
    onPickExterior,
    seamStates,
    selectedPick,
    onClosePick,
    bindHandle,
  }: Props = $props();

  const HEIGHT_KEY = 'wows-webview.ship-bottom-panel.height';
  const TAB_KEY = 'wows-webview.ship-bottom-panel.tab';
  const DEFAULT_HEIGHT = 240;
  const COLLAPSED_HEIGHT = 36;
  const COLLAPSE_THRESHOLD = 60;
  const MIN_EXPANDED = 120;
  const MAX_HEIGHT_FRAC = 0.7;

  let height = $state<number>(DEFAULT_HEIGHT);
  let activeTab = $state<ShipBottomTab>('overview');
  let dragging = $state(false);

  // Persistable tabs — the auto-switched 'pick' tab doesn't get written
  // to localStorage so closing the inspector falls back to the user's
  // last *deliberate* choice.
  const PERSISTABLE: ShipBottomTab[] = [
    'overview',
    'placements',
    'unresolved',
    'hull',
    'armor',
    'skins',
    'exteriors',
    'damage',
    'textures',
    'nodes',
    'particles',
  ];

  onMount(() => {
    try {
      const stored = localStorage.getItem(HEIGHT_KEY);
      if (stored !== null) {
        const n = Number(stored);
        if (Number.isFinite(n) && n >= COLLAPSED_HEIGHT) height = n;
      }
      const stored_t = localStorage.getItem(TAB_KEY);
      // Pre-2026-05-16 sidecars persisted an `effects` tab here that never
      // existed in the bottom panel — quietly redirect it to `overview`.
      // (`particles` is a real bottom-panel tab again: the per-ship list of
      // attached particle ids — see the Particles tab below.)
      let t = stored_t as ShipBottomTab | null;
      if (stored_t === 'effects') t = 'overview';
      if (t && PERSISTABLE.includes(t)) activeTab = t;
    } catch {
      /* localStorage may be unavailable */
    }
    bindHandle?.({ selectTab });
  });

  function selectTab(t: ShipBottomTab) {
    activeTab = t;
  }

  // Auto-switch to Pick when something is picked. Restore the
  // persisted tab when the pick is cleared (mirrors DetailBottomPanel's
  // rig-editor auto-switch behaviour).
  let prevSelectedPick: PickResult | null = null;
  $effect(() => {
    const pick = selectedPick;
    untrack(() => {
      if (pick && !prevSelectedPick) {
        activeTab = 'pick';
      } else if (!pick && prevSelectedPick && activeTab === 'pick') {
        let t: string | null = null;
        try {
          t = localStorage.getItem(TAB_KEY);
        } catch {
          t = null;
        }
        activeTab =
          t && PERSISTABLE.includes(t as ShipBottomTab) ? (t as ShipBottomTab) : 'overview';
      }
      prevSelectedPick = pick;
    });
  });

  function setTab(t: ShipBottomTab) {
    activeTab = t;
    if (PERSISTABLE.includes(t)) {
      try {
        localStorage.setItem(TAB_KEY, t);
      } catch {
        /* ignore */
      }
    }
  }

  function persistHeight(h: number) {
    try {
      localStorage.setItem(HEIGHT_KEY, String(h));
    } catch {
      /* ignore */
    }
  }

  let dragStartY = 0;
  let dragStartHeight = 0;

  function onPointerDown(ev: PointerEvent) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    dragging = true;
    dragStartY = ev.clientY;
    dragStartHeight = height;
    (ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId);
  }

  function onPointerMove(ev: PointerEvent) {
    if (!dragging) return;
    const dy = ev.clientY - dragStartY;
    let next = dragStartHeight - dy;
    const max = Math.floor(window.innerHeight * MAX_HEIGHT_FRAC);
    if (next > max) next = max;
    if (next < COLLAPSE_THRESHOLD) {
      next = COLLAPSED_HEIGHT;
    } else if (next < MIN_EXPANDED) {
      next = MIN_EXPANDED;
    }
    height = next;
  }

  function onPointerUp(ev: PointerEvent) {
    if (!dragging) return;
    dragging = false;
    try {
      (ev.currentTarget as HTMLElement).releasePointerCapture(ev.pointerId);
    } catch {
      /* ignore */
    }
    persistHeight(height);
  }

  function toggleCollapsed() {
    if (height <= COLLAPSED_HEIGHT + 4) {
      height = DEFAULT_HEIGHT;
    } else {
      height = COLLAPSED_HEIGHT;
    }
    persistHeight(height);
  }

  const collapsed = $derived(height <= COLLAPSED_HEIGHT + 4);

  // Hull stats are pulled from the viewer on every `revision` bump
  // (ship swap, skin change, etc.). Cheap: walks the hull tree once,
  // counts position-attribute / index buffer per Mesh.
  const hullGroupStats = $derived.by(() => {
    void revision;
    if (!viewer) return [];
    return viewer.getHullGroupStats();
  });

  // Unresolved asset list — sorted by descending count so the most-
  // referenced missing ids surface first. Each entry is `[asset_id, count]`.
  const unresolvedEntries = $derived.by(() => {
    if (!loadStats) return [];
    return Array.from(loadStats.unresolvedAssets.entries()).sort((a, b) => b[1] - a[1]);
  });

  // Section breakdown — pairs the ship-source `section_counts` (from
  // `<Ship>_accessories.json`) with whatever the viewer reports. The
  // ShipLoadStats doesn't currently break down rendered counts by
  // section, so we just show the source counts + the total rendered /
  // requested at the top. Future expansion: ShipViewer can emit
  // per-section rendered counts.
  const sectionRows = $derived.by(() => {
    return SHIP_SECTIONS.map((k) => ({
      key: k as ShipSectionKey,
      count: ship.section_counts[k],
    }));
  });

  const unresolvedCount = $derived(loadStats?.unresolvedAssets.size ?? 0);

  // Hull material textures grouped by material, then by scheme, slot-
  // ordered. The sidecar shape is:
  //   sidecar.materials[i].texture_sets[<scheme>][<slot>].dds_mips[]
  // — different from the asset-level `texture_sets` (which inlines
  // path arrays directly). Bumped by `revision` so a skin pick / ship
  // swap re-reads via getSidecar(). Empty when the viewer isn't loaded
  // or the sidecar fetch failed.
  interface MaterialSchemeView {
    scheme: string;
    slots: Array<{ slot: string; paths: string[] }>;
  }
  interface MaterialTextureView {
    materialId: string;
    displayName: string | null;
    schemes: MaterialSchemeView[];
  }
  const materialTextures: MaterialTextureView[] = $derived.by(() => {
    void revision;
    if (!viewer) return [];
    const sidecar = viewer.getSidecar() as SidecarDoc | null;
    if (!sidecar?.materials?.length) return [];
    const out: MaterialTextureView[] = [];
    for (const mat of sidecar.materials) {
      const matId = mat.material_id || '';
      if (!matId) continue;
      const ts = mat.texture_sets ?? {};
      const schemes: MaterialSchemeView[] = [];
      for (const [schemeKey, slotMap] of Object.entries(ts)) {
        if (!slotMap) continue;
        const slots: Array<{ slot: string; paths: string[] }> = [];
        // Conventional slots first, then any extras.
        for (const slot of TEXTURE_SLOT_ORDER) {
          const entry = slotMap[slot];
          const paths = entry?.dds_mips;
          if (Array.isArray(paths) && paths.length > 0) slots.push({ slot, paths });
        }
        for (const [slot, entry] of Object.entries(slotMap)) {
          if ((TEXTURE_SLOT_ORDER as readonly string[]).includes(slot)) continue;
          const paths = (entry as { dds_mips?: string[] } | undefined)?.dds_mips;
          if (Array.isArray(paths) && paths.length > 0) slots.push({ slot, paths });
        }
        if (slots.length > 0) schemes.push({ scheme: schemeKey, slots });
      }
      schemes.sort((a, b) => {
        const wa = schemeWeight(a.scheme);
        const wb = schemeWeight(b.scheme);
        if (wa !== wb) return wa - wb;
        return a.scheme.localeCompare(b.scheme);
      });
      if (schemes.length > 0) {
        out.push({
          materialId: matId,
          displayName: (mat as { display_name?: string }).display_name ?? null,
          schemes,
        });
      }
    }
    return out;
  });

  // Hull GLB's directory URL — the sidecar paths (e.g.
  // `textures_dds/ASB017_a.dd0`) resolve against this. Mirrors how
  // `TextureManager.bindHullMaterials` builds its base.
  const hullTexturesBaseUrl = $derived.by(() => {
    void revision;
    return viewer?.getHullBaseUrl() ?? '';
  });

  const hasHullTextures = $derived(materialTextures.length > 0);

  // ── Armor + hitbox tab data ──────────────────────────────────────────
  // Read straight off the sidecar (`armor` / `hitbox` sections). Bumped by
  // `revision` so a ship swap re-reads. See METADATA_SPEC §6 / §7.
  const armorSection = $derived.by(() => {
    void revision;
    return viewer?.getSidecar()?.armor ?? null;
  });
  const hitboxSection = $derived.by(() => {
    void revision;
    return viewer?.getSidecar()?.hitbox ?? null;
  });
  // Zones sorted thickest-first so the citadel / belt lead.
  const armorZoneRows = $derived.by(() =>
    Object.entries(armorSection?.zones ?? {})
      .map(([zone, z]) => ({ zone, ...z }))
      .sort((a, b) => b.max_thickness_mm - a.max_thickness_mm),
  );
  // Materials sorted by thickness desc; 0mm (no-armor) sentinels trail.
  const armorMaterialRows = $derived.by(() =>
    Object.entries(armorSection?.materials_table ?? {})
      .map(([id, m]) => ({ id, ...m }))
      .sort((a, b) => b.thickness_mm - a.thickness_mm || Number(a.id) - Number(b.id)),
  );
  const hitLocationRows = $derived.by(() =>
    Object.entries(hitboxSection?.hit_locations ?? {})
      .map(([section, h]) => ({ section, ...h }))
      .sort((a, b) => (b.max_hp ?? 0) - (a.max_hp ?? 0)),
  );
  const hitboxRegionRows = $derived.by(() =>
    Object.entries(hitboxSection?.regions ?? {})
      .map(([zone, r]) => ({ zone, ...r, style: hitboxStyleFor({ section: zone, hl_type: zone }) }))
      .sort((a, b) => b.box_count - a.box_count),
  );
  const hasArmorTab = $derived(
    armorZoneRows.length > 0 ||
      armorMaterialRows.length > 0 ||
      hitLocationRows.length > 0 ||
      hitboxRegionRows.length > 0,
  );

  // ── Nodes tab (WG bones & VFX points) ────────────────────────────────
  // Full node inventory read off the overlay (accessory bones / hardpoints
  // / gun-fire anchors from the live scene + hull EP_ points from the
  // sidecar). Bumped by `revision` so a ship swap re-reads.
  const nodeRows: readonly NodeEntry[] = $derived.by(() => {
    void revision;
    // List the bones / VFX / hardpoints / structural nodes — skip raw
    // geometry (`mesh`) nodes, which are numerous (thousands) and aren't
    // "bones". They remain available as overlay markers via the side
    // panel's "Mesh nodes" category toggle.
    return (viewer?.getNodeList() ?? []).filter((n) => n.category !== 'mesh');
  });
  const hasNodesTab = $derived(nodeRows.length > 0);
  let nodeFilter = $state('');
  let pinnedNode = $state<string | null>(null);
  const filteredNodes = $derived.by(() => {
    const needle = nodeFilter.trim().toLowerCase();
    if (!needle) return nodeRows;
    return nodeRows.filter(
      (n) =>
        n.name.toLowerCase().includes(needle) ||
        n.owner.toLowerCase().includes(needle) ||
        NODE_CATEGORY_LABEL[n.category].toLowerCase().includes(needle),
    );
  });
  function catSwatchHex(cat: NodeCategory): string {
    return '#' + NODE_CATEGORY_COLOR[cat].toString(16).padStart(6, '0');
  }
  function togglePinNode(name: string) {
    const next = pinnedNode === name ? null : name;
    pinnedNode = next;
    viewer?.pinNode(next);
  }
  function frameNode(name: string) {
    viewer?.frameOnNode(name);
  }
  function fmtCoord(n: number): string {
    return (n >= 0 ? '+' : '') + n.toFixed(2);
  }
  function fmtEnvScalar(n: number): string {
    if (!Number.isFinite(n)) return '—';
    if (Math.abs(n) > 0 && Math.abs(n) < 0.001) return n.toExponential(2);
    return n.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  }

  // ── Particles tab (effect attachments) ───────────────────────────────
  // Lists every particle effect this ship attaches, keyed by its VFS
  // `particle_path` (the `.xml` id). Read straight off the sidecar's
  // `effects.attachments`, so the list is available whether or not the
  // live particle layer is toggled on. Bumped by `revision` for ship
  // swaps. Each id deep-links to the standalone `#/particles/<path>`
  // inspector for lookup.
  interface ShipParticleRow {
    /** Full VFS id, e.g. `particles/vehicles/Fire_small.xml`. */
    path: string;
    /** Distinct source categories (hull / artillery / aa_aura / …). */
    sources: string[];
    /** Distinct effect groups (smoke / shotEffect / waketracefront / …). */
    groups: string[];
    /** Distinct anchor nodes (EP_*, HP_*) — fallback label when no group. */
    nodes: string[];
    /** Total attachment rows referencing this id. */
    count: number;
  }
  const shipParticleRows: ShipParticleRow[] = $derived.by(() => {
    void revision;
    const attachments = (viewer?.getSidecar() as SidecarDoc | null)?.effects?.attachments ?? [];
    const byPath = new Map<
      string,
      { sources: Set<string>; groups: Set<string>; nodes: Set<string>; count: number }
    >();
    for (const a of attachments) {
      const path = a.particle_path;
      if (!path) continue;
      let row = byPath.get(path);
      if (!row) {
        row = { sources: new Set(), groups: new Set(), nodes: new Set(), count: 0 };
        byPath.set(path, row);
      }
      row.count += 1;
      if (a.source) row.sources.add(a.source);
      if (a.group) row.groups.add(a.group);
      if (a.node) row.nodes.add(a.node);
    }
    return Array.from(byPath.entries())
      .map(([path, r]) => ({
        path,
        sources: [...r.sources].sort(),
        groups: [...r.groups].sort(),
        nodes: [...r.nodes].sort(),
        count: r.count,
      }))
      .sort((a, b) => a.path.localeCompare(b.path));
  });
  const hasParticlesTab = $derived(shipParticleRows.length > 0);

  /** Open this particle in the standalone `#/particles` inspector. The
   *  router preserves embedded slashes after the first segment, so the
   *  full VFS path passes through without encoding. */
  function openParticleInInspector(path: string) {
    navigate(`#/particles/${path}`);
  }

  const tabs: Array<{ id: ShipBottomTab; label: string; hide?: boolean; badge?: number }> =
    $derived([
      { id: 'overview', label: 'Overview' },
      { id: 'placements', label: 'Placements' },
      {
        id: 'unresolved',
        label: 'Unresolved',
        hide: unresolvedCount === 0,
        badge: unresolvedCount,
      },
      { id: 'hull', label: 'Hull' },
      { id: 'armor', label: 'Armor', hide: !hasArmorTab },
      { id: 'skins', label: 'Skins', hide: skins.length <= 1 },
      { id: 'exteriors', label: 'Exteriors', hide: exteriors.length <= 1 },
      { id: 'damage', label: 'Damage' },
      { id: 'textures', label: 'Textures', hide: !hasHullTextures },
      { id: 'nodes', label: 'Nodes', hide: !hasNodesTab },
      { id: 'particles', label: 'Particles', hide: !hasParticlesTab },
      { id: 'pick', label: 'Pick', hide: !selectedPick },
    ]);

  // The auto-switched tab may go stale if the underlying data changes
  // (e.g. selectedPick cleared while on Pick). Reconcile here.
  $effect(() => {
    const visible = new Set(tabs.filter((t) => !t.hide).map((t) => t.id));
    if (!visible.has(activeTab)) {
      activeTab = 'overview';
    }
  });

  // Exteriors grouped by peculiarity — `default` first, then alphabetical.
  const exteriorGroups = $derived.by(() => {
    const by = new Map<string, ExteriorRecord[]>();
    for (const e of exteriors) {
      const k = e.peculiarity || 'other';
      const list = by.get(k) ?? [];
      list.push(e);
      by.set(k, list);
    }
    return Array.from(by.entries())
      .map(([peculiarity, records]) => ({ peculiarity, records }))
      .sort((a, b) =>
        a.peculiarity === 'default'
          ? -1
          : b.peculiarity === 'default'
            ? 1
            : a.peculiarity.localeCompare(b.peculiarity),
      );
  });

  const pickInfo = $derived(selectedPick?.info ?? null);
  const pickLibEntry = $derived(
    pickInfo && library ? (library.assets[pickInfo.asset_id] ?? null) : null,
  );
  // Per-mount damage state for the picked mount (or, for an attached child,
  // its host mount). Matched by instance_id against the sidecar's typed mount
  // arrays. Drives the Pick-tab HP/crit/repair readout (module damage).
  const pickHitLocation = $derived.by(() => {
    void revision;
    const id = pickInfo?.instance_id ?? pickInfo?.attached_to_instance_id;
    if (!id) return null;
    const sc = viewer?.getSidecar() as SidecarDoc | null;
    if (!sc) return null;
    for (const arr of [sc.turrets, sc.secondaries, sc.antiair, sc.torpedoes]) {
      const m = arr?.find((x) => x.instance_id === id);
      if (m?.hit_location) return m.hit_location;
    }
    return null;
  });

  // Per-mount destroyed-state (Pick-tab toggle, module-damage
  // reference). Panel-local; the viewer owns the actual mesh swap
  // (setMountDestroyed). Reset on ship swap.
  let destroyedMounts = $state(new Set<string>());
  $effect(() => {
    void ship; // ship-identity change ⇒ clear (revision also bumps on skin/exterior)
    destroyedMounts = new Set();
  });
  async function toggleMountDestroyed() {
    const id = pickInfo?.instance_id ?? pickInfo?.attached_to_instance_id;
    if (!id || !viewer) return;
    const next = !destroyedMounts.has(id);
    const ok = await viewer.setMountDestroyed(id, next);
    if (!ok) return;
    const updated = new Set(destroyedMounts);
    if (next) updated.add(id);
    else updated.delete(id);
    destroyedMounts = updated;
  }

  function openPickInLibrary() {
    if (!pickInfo) return;
    navigate(`#/asset/${encodeURIComponent(pickInfo.asset_id)}`);
  }
</script>

<section class="bg-card border-border flex flex-none flex-col border-t" style="height: {height}px">
  <div
    role="separator"
    aria-orientation="horizontal"
    aria-label="Resize inspector"
    class="h-1.5 cursor-row-resize bg-border/40 hover:bg-border flex-none"
    class:bg-primary={dragging}
    class:hover:bg-primary={dragging}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    ondblclick={toggleCollapsed}
  ></div>

  <div class="flex flex-none items-center justify-between border-border border-b">
    <div role="tablist" class="flex">
      {#each tabs as t (t.id)}
        {#if !t.hide}
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === t.id}
            onclick={() => setTab(t.id)}
            class="{tabBtnBase} {activeTab === t.id
              ? 'border-primary text-foreground'
              : 'border-transparent text-muted-foreground hover:text-foreground'}"
          >
            {t.label}
            {#if t.badge != null && t.badge > 0}
              <span class="ml-1 rounded bg-amber-950/60 px-1 py-[1px] text-[9px] text-amber-300">
                {t.badge}
              </span>
            {/if}
          </button>
        {/if}
      {/each}
    </div>
    <button
      type="button"
      onclick={toggleCollapsed}
      title={collapsed ? 'Expand inspector' : 'Collapse inspector'}
      class="text-muted-foreground hover:text-foreground px-3 py-1 text-[11px]"
    >
      {collapsed ? '▲' : '▼'}
    </button>
  </div>

  {#if !collapsed}
    <div class="flex-1 min-h-0 overflow-y-auto px-5 py-3 text-xs">
      {#if activeTab === 'overview'}
        <dl
          class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:break-words"
        >
          <dt>display name</dt>
          <dd>{ship.display_name}</dd>
          <dt>internal name</dt>
          <dd><code class="font-mono text-[11px]">{ship.name}</code></dd>
          {#if ship.nation}
            <dt>nation</dt>
            <dd>{ship.nation}</dd>
          {/if}
          {#if ship.ship_class}
            <dt>class</dt>
            <dd>{ship.ship_class}</dd>
          {/if}
          {#if ship.tier != null}
            <dt>tier</dt>
            <dd>{ship.tier}</dd>
          {/if}
          <dt>hull glb</dt>
          <dd><code class="font-mono text-[11px]">{ship.hull_glb}</code></dd>
          <dt>environment</dt>
          <dd>
            {#if envInfo}
              <code class="font-mono text-[11px]">{envInfo.space}</code>
              <span class="text-muted-foreground"> / </span>{envInfo.weather}
            {:else}
              Procedural (studio)
            {/if}
          </dd>
          {#if envInfo}
            <dt>wetness</dt>
            <dd class="tabular-nums">
              overall {fmtEnvScalar(envInfo.wetness.overallWetness)}
              <span class="text-muted-foreground"> · </span>puddles {fmtEnvScalar(
                envInfo.wetness.puddlesIntensity,
              )}
              <span class="text-muted-foreground"> · </span>ripples {fmtEnvScalar(
                envInfo.wetness.ripplesIntensity,
              )}
            </dd>
            <dt>env avg lum</dt>
            <dd class="tabular-nums">{fmtEnvScalar(envInfo.avgLum)}</dd>
          {/if}
          {#if loadStats}
            <dt>load time</dt>
            <dd class="tabular-nums">{(loadStats.loadMs / 1000).toFixed(2)}s</dd>
            <dt>hull meshes</dt>
            <dd class="tabular-nums">{loadStats.hullMeshCount}</dd>
            <dt>placements</dt>
            <dd class="tabular-nums">
              {loadStats.placementsRendered} / {loadStats.placementsRequested} rendered
            </dd>
            <dt>attached children</dt>
            <dd class="tabular-nums">
              {loadStats.attachmentsRendered} rendered{#if loadStats.attachmentsFilteredByMisc > 0},
                {loadStats.attachmentsFilteredByMisc} dropped by miscFilter
              {/if}
            </dd>
            <dt>skins</dt>
            <dd class="tabular-nums">{loadStats.skinCount}</dd>
          {/if}
        </dl>
      {:else if activeTab === 'placements'}
        <div class="flex flex-col gap-3">
          {#if loadStats}
            <div class="text-muted-foreground text-[11px]">
              Totals: <span class="text-foreground tabular-nums"
                >{loadStats.placementsRendered} / {loadStats.placementsRequested}</span
              >
              placements rendered ·
              <span class="text-foreground tabular-nums">{loadStats.attachmentsRendered}</span>
              attached children
              {#if loadStats.attachmentsFilteredByMisc > 0}
                · <span class="text-amber-300 tabular-nums"
                  >{loadStats.attachmentsFilteredByMisc}</span
                >
                miscFilter-dropped
              {/if}
            </div>
          {/if}
          <table
            class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">section</th>
                <th class="text-right">placements</th>
              </tr>
            </thead>
            <tbody>
              {#each sectionRows as row (row.key)}
                <tr>
                  <td class="text-muted-foreground">{row.key}</td>
                  <td class="text-right">{row.count}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <div class="text-muted-foreground text-[10px] leading-tight max-w-[60ch]">
            Counts come from the ship's source <code class="font-mono text-[10px]"
              >accessories.json</code
            > — the per-section breakdown of rendered placements isn't currently surfaced through ShipLoadStats.
          </div>
        </div>
      {:else if activeTab === 'unresolved'}
        {#if unresolvedEntries.length === 0}
          <div class="text-muted-foreground">
            All asset_ids referenced by this ship resolved to library entries.
          </div>
        {:else}
          <div class="flex flex-col gap-2">
            <div class="text-muted-foreground text-[11px]">
              {unresolvedEntries.length} asset_id(s) referenced by this ship had no matching entry in
              the accessory library. Most often this means the asset wasn't extracted yet — re-run
              <code class="font-mono text-[11px]">wows-build-accessory-library</code> after extracting
              the missing source.
            </div>
            <table
              class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
            >
              <thead>
                <tr>
                  <th class="text-left">asset_id</th>
                  <th class="text-right">placements</th>
                </tr>
              </thead>
              <tbody>
                {#each unresolvedEntries as [id, count] (id)}
                  <tr>
                    <td>
                      <button
                        type="button"
                        class="font-mono text-[11px] hover:underline"
                        title="Copy asset_id to clipboard"
                        onclick={() => navigator.clipboard?.writeText(id)}
                      >
                        {id}
                      </button>
                    </td>
                    <td class="text-right">{count}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      {:else if activeTab === 'hull'}
        {#if hullGroupStats.length === 0}
          <div class="text-muted-foreground">No hull groups classified.</div>
        {:else}
          <table
            class="w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">group</th>
                <th class="text-right">meshes</th>
                <th class="text-right">triangles</th>
              </tr>
            </thead>
            <tbody>
              {#each hullGroupStats as g (g.name)}
                <tr>
                  <td class="text-muted-foreground">{g.name}</td>
                  <td class="text-right">{g.meshes}</td>
                  <td class="text-right">{g.triangles.toLocaleString()}</td>
                </tr>
              {/each}
            </tbody>
            <tfoot>
              <tr class="border-border [&_td]:border-t [&_td]:text-muted-foreground">
                <td>total</td>
                <td class="text-right">
                  {hullGroupStats.reduce((a, g) => a + g.meshes, 0)}
                </td>
                <td class="text-right">
                  {hullGroupStats.reduce((a, g) => a + g.triangles, 0).toLocaleString()}
                </td>
              </tr>
            </tfoot>
          </table>
        {/if}
      {:else if activeTab === 'armor'}
        {@const tableCls =
          'w-fit text-[11px] tabular-nums [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-5 [&_td]:pr-5 [&_th]:py-0.5 [&_td]:py-0.5'}
        {@const hdrCls = 'text-muted-foreground mb-1 text-[10px] uppercase tracking-wider'}
        <div class="flex flex-wrap gap-x-10 gap-y-4">
          {#if armorZoneRows.length > 0}
            <div>
              <div class={hdrCls}>Armor zones</div>
              <table class={tableCls}>
                <thead>
                  <tr>
                    <th class="text-left">zone</th>
                    <th class="text-right">default</th>
                    <th class="text-right">max</th>
                    <th class="text-right">plates</th>
                  </tr>
                </thead>
                <tbody>
                  {#each armorZoneRows as z (z.zone)}
                    <tr>
                      <td class="text-foreground">{z.zone}</td>
                      <td class="text-right">
                        <span
                          class="mr-1 inline-block size-2 rounded-[2px] align-middle"
                          style="background:{thicknessToColorHex(z.default_thickness_mm)}"
                        ></span>{z.default_thickness_mm}
                      </td>
                      <td class="text-right">{z.max_thickness_mm}</td>
                      <td class="text-muted-foreground text-right">{z.plate_count}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
              <div class="text-muted-foreground/70 mt-1 text-[9px]">thickness in mm</div>
            </div>
          {/if}

          {#if hitLocationRows.length > 0}
            <div>
              <div class={hdrCls}>Module HP</div>
              <table class={tableCls}>
                <thead>
                  <tr>
                    <th class="text-left">section</th>
                    <th class="text-left">type</th>
                    <th class="text-right">max HP</th>
                    <th class="text-right">regen</th>
                    <th class="text-right">repair</th>
                  </tr>
                </thead>
                <tbody>
                  {#each hitLocationRows as h (h.section)}
                    {@const style = hitboxStyleFor({ section: h.section, hl_type: h.hl_type })}
                    <tr>
                      <td class="text-foreground">
                        <span
                          class="mr-1 inline-block size-2 rounded-[2px] align-middle"
                          style="background:{style.hex}"
                        ></span>{h.section}
                      </td>
                      <td class="text-muted-foreground">{h.hl_type.replace('_hitlocation', '')}</td>
                      <td class="text-right"
                        >{h.max_hp != null ? h.max_hp.toLocaleString() : '—'}</td
                      >
                      <td class="text-muted-foreground text-right">
                        {h.regen_part != null ? `${Math.round(h.regen_part * 100)}%` : '—'}
                      </td>
                      <td class="text-muted-foreground text-right">
                        {h.broken_repair_s != null ? `${h.broken_repair_s}s` : '—'}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
              <div class="text-muted-foreground/70 mt-1 text-[9px]">
                regen = HP restored per repair tick · repair = full-section restore time
              </div>
            </div>
          {/if}

          {#if hitboxRegionRows.length > 0}
            <div>
              <div class={hdrCls}>Hitbox regions</div>
              <table class={tableCls}>
                <thead>
                  <tr>
                    <th class="text-left">zone</th>
                    <th class="text-right">boxes</th>
                  </tr>
                </thead>
                <tbody>
                  {#each hitboxRegionRows as r (r.zone)}
                    <tr>
                      <td class="text-foreground">
                        <span
                          class="mr-1 inline-block size-2 rounded-[2px] align-middle"
                          style="background:{r.style.hex}"
                        ></span>{r.zone}
                      </td>
                      <td class="text-muted-foreground text-right">{r.box_count}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}

          {#if armorMaterialRows.length > 0}
            <div class="min-w-0">
              <div class={hdrCls}>Armor materials ({armorMaterialRows.length})</div>
              <div class="max-h-44 overflow-y-auto pr-1">
                <table class={tableCls}>
                  <thead>
                    <tr>
                      <th class="text-left">id</th>
                      <th class="text-right">mm</th>
                      <th class="text-left">layers</th>
                      <th class="text-left">zones</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each armorMaterialRows as m (m.id)}
                      <tr class={m.hidden ? 'text-muted-foreground/60' : ''}>
                        <td class="font-mono">{m.id}</td>
                        <td class="text-right">
                          <span
                            class="mr-1 inline-block size-2 rounded-[2px] align-middle"
                            style="background:{thicknessToColorHex(m.thickness_mm)}"
                          ></span>{m.thickness_mm}
                        </td>
                        <td class="text-muted-foreground">{m.layers.join(' + ') || '—'}</td>
                        <td class="text-muted-foreground">{m.zones.join(', ') || '—'}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
              <div class="text-muted-foreground/70 mt-1 text-[9px]">
                dimmed rows = hidden (internal decks / bulkheads); layers = outer→inner mm
              </div>
            </div>
          {/if}
        </div>
      {:else if activeTab === 'skins'}
        {#if skins.length === 0}
          <div class="text-muted-foreground">No skins available.</div>
        {:else}
          <div class="flex flex-col gap-1">
            {#each skins as skin (skin.skin_id)}
              <button
                type="button"
                onclick={() => onPickSkin(skin.skin_id)}
                class="flex items-center gap-2 rounded border px-2 py-1 text-left text-[11px] {activeSkin ===
                skin.skin_id
                  ? 'border-primary bg-primary/10'
                  : 'border-border bg-popover hover:bg-accent'}"
                title={skin.skin_id}
              >
                <span
                  class="inline-flex size-3 flex-none items-center justify-center rounded-full border {activeSkin ===
                  skin.skin_id
                    ? 'border-primary bg-primary'
                    : 'border-border'}"
                ></span>
                <span class="min-w-0 flex-1">
                  <span class="block truncate font-medium text-foreground">
                    {skin.display_name || skin.skin_id}
                  </span>
                  <span class="text-muted-foreground block truncate font-mono text-[10px]">
                    {skin.skin_id}
                  </span>
                </span>
                <span class="text-muted-foreground flex-none text-[10px] uppercase tracking-wider">
                  {skin.scheme_key}
                </span>
              </button>
            {/each}
          </div>
        {/if}
      {:else if activeTab === 'exteriors'}
        <div class="flex flex-col gap-2">
          <div class="text-muted-foreground max-w-[64ch] text-[11px]">
            Mesh-swap permoflages (WG <code class="font-mono text-[10px]">Exterior</code> records).
            Selecting one swaps the affected mounts to the variant models and applies the matching
            camo. Entries marked <span class="text-amber-500">hull differs</span> also swap the hull:
            when the HullDelta export exists the ship reloads on the variant hull and mounts re-anchor
            to its HP nodes (WG hides unused base accessories by parking their nodes inside the hull);
            otherwise mounts render on the base hull until the ship is re-extracted with exterior hulls
            on.
          </div>
          {#each exteriorGroups as group (group.peculiarity)}
            <div>
              <div
                class="text-muted-foreground mb-1 text-[10px] font-medium uppercase tracking-wider"
              >
                {group.peculiarity}
              </div>
              <div class="flex flex-col gap-1">
                {#each group.records as ext (ext.exterior_id)}
                  <button
                    type="button"
                    onclick={() => onPickExterior(ext.exterior_id)}
                    class="flex items-center gap-2 rounded border px-2 py-1 text-left text-[11px] {activeExteriorId ===
                    ext.exterior_id
                      ? 'border-primary bg-primary/10'
                      : 'border-border bg-popover hover:bg-accent'}"
                    title={ext.exterior_id}
                  >
                    <span
                      class="inline-flex size-3 flex-none items-center justify-center rounded-full border {activeExteriorId ===
                      ext.exterior_id
                        ? 'border-primary bg-primary'
                        : 'border-border'}"
                    ></span>
                    <span class="min-w-0 flex-1">
                      <span class="block truncate font-medium text-foreground">
                        {ext.display_name || ext.exterior_id}
                      </span>
                      <span class="text-muted-foreground block truncate font-mono text-[10px]">
                        {ext.exterior_id}
                      </span>
                    </span>
                    {#if ext.is_native && ext.exterior_id !== 'default'}
                      <span
                        class="flex-none rounded bg-primary/15 px-1 text-[9px] uppercase tracking-wider text-primary"
                      >
                        native
                      </span>
                    {/if}
                    {#if (ext.mounts?.length ?? 0) > 0}
                      <span class="text-muted-foreground flex-none text-[10px]">
                        {ext.mounts?.length} mounts
                      </span>
                    {/if}
                    {#if ext.wg_asset_id}
                      <span
                        class="flex-none rounded bg-amber-500/15 px-1 text-[9px] uppercase tracking-wider text-amber-500"
                        title="This exterior also swaps the hull model in game; the variant hull GLB isn't extracted into the unified folder yet."
                      >
                        hull differs
                      </span>
                    {/if}
                  </button>
                {/each}
              </div>
            </div>
          {/each}
        </div>
      {:else if activeTab === 'damage'}
        <div class="flex flex-col gap-2">
          <div class="text-muted-foreground text-[11px] max-w-[60ch]">
            Per-seam damage state. Toggling a seam in the side panel cascades hull patches + cracks
            via <code class="font-mono text-[11px]">damage_cascade</code>; this tab shows the
            current snapshot.
          </div>
          <table
            class="w-fit text-[11px] [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
          >
            <thead>
              <tr>
                <th class="text-left">seam</th>
                <th class="text-left">state</th>
              </tr>
            </thead>
            <tbody>
              {#each SEAMS as seam (seam)}
                <tr>
                  <td class="text-muted-foreground">{seam}</td>
                  <td
                    class:text-emerald-400={seamStates[seam] === 'Intact'}
                    class:text-rose-400={seamStates[seam] === 'Broken'}
                  >
                    {seamStates[seam]}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else if activeTab === 'textures'}
        {#if materialTextures.length === 0}
          <div class="text-muted-foreground">
            no hull <code class="font-mono text-[11px]">materials[*].texture_sets</code>
            on this ship — sidecar fetch failed or sidecar pre-dates the materials autofill
          </div>
        {:else}
          <!--
            Per-material rows, each grouping its schemes (main → dead →
            camo_*). Material-level layout (vs. flat slot grid like the
            Library page's accessory previews) because hull sidecars
            carry many materials with the same scheme keys — flattening
            would hide which material owns which texture.
          -->
          <div class="flex flex-col gap-4">
            {#each materialTextures as mat (mat.materialId)}
              <div class="flex flex-col gap-2 border-l-2 border-border/60 pl-3">
                <div class="flex items-baseline gap-2">
                  <span class="text-foreground font-mono text-[11px] font-semibold">
                    {mat.materialId}
                  </span>
                  {#if mat.displayName && mat.displayName !== mat.materialId}
                    <span class="text-muted-foreground text-[10px]">
                      {mat.displayName}
                    </span>
                  {/if}
                  <span class="text-muted-foreground/70 ml-auto text-[10px]">
                    {mat.schemes.length} scheme{mat.schemes.length === 1 ? '' : 's'}
                  </span>
                </div>
                {#each mat.schemes as scheme (scheme.scheme)}
                  <div class="flex flex-col gap-1">
                    <div class="text-muted-foreground text-[10px] uppercase tracking-wider">
                      {scheme.scheme}
                      <span class="text-muted-foreground/70 ml-1 normal-case">
                        · {scheme.slots.length} slot{scheme.slots.length === 1 ? '' : 's'}
                      </span>
                    </div>
                    <div class="flex flex-wrap gap-2">
                      {#each scheme.slots as s (s.slot)}
                        <DdsTexturePreview
                          paths={s.paths}
                          baseUrl={hullTexturesBaseUrl}
                          slot={s.slot}
                        />
                      {/each}
                    </div>
                  </div>
                {/each}
              </div>
            {/each}
          </div>
        {/if}
      {:else if activeTab === 'nodes'}
        <div class="flex flex-col gap-2">
          <div class="flex items-center gap-3 text-[11px]">
            <span class="text-muted-foreground tabular-nums">
              {filteredNodes.length}/{nodeRows.length} node{nodeRows.length === 1 ? '' : 's'}
            </span>
            <input
              type="text"
              placeholder="filter by name / owner / type (e.g. EP_Fire, HP_gunFire, Rotate)"
              bind:value={nodeFilter}
              class="h-6 flex-1 max-w-[340px] rounded border border-border bg-popover px-1.5 text-[11px] text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring"
            />
            <span class="text-muted-foreground/70 text-[10px]">
              Enable “Bones &amp; VFX” in the side panel to see markers · pin to label in 3D
            </span>
          </div>
          <div class="flex flex-col">
            <div
              class="grid grid-cols-[1.6rem_minmax(8rem,1.4fr)_minmax(6rem,1fr)_minmax(9rem,1fr)_auto] gap-x-2 border-b border-border/60 pb-1 text-[10px] uppercase tracking-wider text-muted-foreground"
            >
              <span></span>
              <span>node</span>
              <span>owner</span>
              <span>position (m)</span>
              <span></span>
            </div>
            {#each filteredNodes as n, i (i)}
              {@const isPinned = pinnedNode === n.name}
              <div
                class="grid grid-cols-[1.6rem_minmax(8rem,1.4fr)_minmax(6rem,1fr)_minmax(9rem,1fr)_auto] items-center gap-x-2 border-b border-border/30 py-0.5 text-[11px]"
                class:bg-accent={isPinned}
              >
                <span
                  class="inline-block size-2.5 rounded-full justify-self-center"
                  style="background:{catSwatchHex(n.category)}"
                  title={NODE_CATEGORY_LABEL[n.category]}
                ></span>
                <code class="font-mono text-foreground truncate" title={n.name}>{n.name}</code>
                <span class="text-muted-foreground truncate" title={n.owner}>
                  {n.owner}{n.effectGroup ? ` · ${n.effectGroup}` : ''}
                </span>
                <span class="text-muted-foreground tabular-nums text-[10px]">
                  {fmtCoord(n.position.x)}, {fmtCoord(n.position.y)}, {fmtCoord(n.position.z)}
                </span>
                <div class="flex gap-1 justify-self-end">
                  <button
                    type="button"
                    onclick={() => togglePinNode(n.name)}
                    class="rounded border border-border bg-popover hover:bg-accent px-1.5 py-0.5 text-[10px]"
                    class:border-primary={isPinned}
                    title="Pin a labelled marker at this node"
                  >
                    {isPinned ? '● pinned' : '○ pin'}
                  </button>
                  <button
                    type="button"
                    onclick={() => frameNode(n.name)}
                    class="rounded border border-border bg-popover hover:bg-accent px-1.5 py-0.5 text-[10px]"
                    title="Move the camera to this node"
                  >
                    frame
                  </button>
                </div>
              </div>
            {/each}
          </div>
        </div>
      {:else if activeTab === 'particles'}
        {#if shipParticleRows.length === 0}
          <div class="text-muted-foreground">
            This ship attaches no particle effects — no
            <code class="font-mono text-[11px]">effects.attachments</code> in the sidecar.
          </div>
        {:else}
          <div class="flex flex-col gap-2">
            <div class="text-muted-foreground max-w-[72ch] text-[11px]">
              Particle effects attached to this ship, by their VFS id (the
              <code class="font-mono text-[10px]">.xml</code> path). Click an id to open it in the
              <button
                type="button"
                class="text-primary hover:underline"
                onclick={() => navigate('#/particles')}
              >
                Particles inspector
              </button>, or copy it to look up there.
            </div>
            <table
              class="text-[11px] [&_th]:text-muted-foreground [&_th]:font-normal [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-[10px] [&_th]:pr-6 [&_td]:pr-6 [&_th]:py-0.5 [&_td]:py-0.5"
            >
              <thead>
                <tr>
                  <th class="text-left">particle id</th>
                  <th class="text-left">source</th>
                  <th class="text-left">attached at</th>
                  <th class="text-right">count</th>
                </tr>
              </thead>
              <tbody>
                {#each shipParticleRows as row (row.path)}
                  <tr>
                    <td>
                      <button
                        type="button"
                        class="text-primary font-mono text-[11px] hover:underline"
                        title={`Open ${row.path} in the Particles inspector`}
                        onclick={() => openParticleInInspector(row.path)}
                      >
                        {row.path}
                      </button>
                      <button
                        type="button"
                        class="text-muted-foreground hover:text-foreground ml-1.5 align-middle text-[10px]"
                        title="Copy full VFS id to clipboard"
                        onclick={() => navigator.clipboard?.writeText(row.path)}
                      >
                        copy
                      </button>
                    </td>
                    <td class="text-muted-foreground">{row.sources.join(', ') || '—'}</td>
                    <td class="text-muted-foreground">
                      {(row.groups.length ? row.groups : row.nodes).join(', ') || '—'}
                    </td>
                    <td class="text-right tabular-nums">{row.count}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <div class="text-muted-foreground/70 text-[10px]">
              {shipParticleRows.length} unique particle{shipParticleRows.length === 1 ? '' : 's'} ·
              {shipParticleRows.reduce((a, r) => a + r.count, 0)} attachment{shipParticleRows.reduce(
                (a, r) => a + r.count,
                0,
              ) === 1
                ? ''
                : 's'}
            </div>
          </div>
        {/if}
      {:else if activeTab === 'pick'}
        {#if pickInfo}
          <div class="flex flex-col gap-2">
            <header class="border-border flex items-center gap-2 border-b pb-1.5">
              <button
                type="button"
                onclick={openPickInLibrary}
                title="Open this asset in the Library"
                class="text-primary inline-flex min-w-0 flex-1 items-center gap-1.5 bg-transparent p-0 text-left font-mono text-xs font-medium hover:underline"
              >
                <span class="overflow-hidden text-ellipsis whitespace-nowrap">
                  {pickInfo.asset_id}
                </span>
                <ExternalLink class="size-3 shrink-0" />
              </button>
              <Button
                variant="ghost"
                size="icon-xs"
                onclick={onClosePick}
                aria-label="Clear selection"
                class="size-[18px]"
              >
                ×
              </Button>
            </header>
            <dl
              class="m-0 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1 [&_dt]:text-muted-foreground [&_dd]:m-0 [&_dd]:overflow-hidden [&_dd]:text-ellipsis [&_dd]:whitespace-nowrap"
            >
              {#if pickInfo.section}
                <dt>section</dt>
                <dd>{pickInfo.section}</dd>
              {/if}
              {#if pickInfo.parent_section}
                <dt>hull anchor</dt>
                <dd>{pickInfo.parent_section}</dd>
              {/if}
              {#if pickInfo.parent_mesh}
                <dt>parent mesh</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.parent_mesh}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.instance_id}
                <dt>instance</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.instance_id}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.attached_to_instance_id}
                <dt>attached to</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.attached_to_instance_id}
                  </code>
                </dd>
              {/if}
              {#if pickInfo.attached_placement_id}
                <dt>placement</dt>
                <dd>
                  <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
                    {pickInfo.attached_placement_id}
                  </code>
                </dd>
              {/if}
              {#if pickLibEntry}
                <dt>scope</dt>
                <dd>
                  {pickLibEntry.scope}/{pickLibEntry.category}{pickLibEntry.subcategory
                    ? `/${pickLibEntry.subcategory}`
                    : ''}
                </dd>
                <dt>used by</dt>
                <dd>
                  {pickLibEntry.used_by_ships.length} ship{pickLibEntry.used_by_ships.length === 1
                    ? ''
                    : 's'}
                </dd>
              {:else}
                <dt>library</dt>
                <dd class="text-amber-300">unresolved</dd>
              {/if}
              {#if pickHitLocation}
                <dt>module HP</dt>
                <dd>
                  {pickHitLocation.max_hp ?? '—'}
                  <span class="text-muted-foreground">
                    ({pickHitLocation.can_be_destroyed ? 'destructible' : 'indestructible'})
                  </span>
                </dd>
                {#if pickHitLocation.crit_prob && (pickHitLocation.crit_prob[0] > 0 || pickHitLocation.crit_prob[1] > 0)}
                  <dt>crit chance</dt>
                  <dd>
                    {(pickHitLocation.crit_prob[0] * 100).toFixed(0)}–{(
                      pickHitLocation.crit_prob[1] * 100
                    ).toFixed(0)}%
                  </dd>
                {/if}
                {#if pickHitLocation.auto_repair_s != null}
                  <dt>repair</dt>
                  <dd>
                    {pickHitLocation.auto_repair_s}s crit{pickHitLocation.broken_repair_s != null
                      ? ` / ${pickHitLocation.broken_repair_s}s broken`
                      : ''}
                  </dd>
                {/if}
              {/if}
            </dl>
            {#if pickInfo.instance_id && pickLibEntry?.glb_dead}
              <div class="mt-3 flex items-center gap-2">
                <Button
                  variant={destroyedMounts.has(pickInfo.instance_id) ? 'destructive' : 'outline'}
                  size="sm"
                  onclick={toggleMountDestroyed}
                >
                  {destroyedMounts.has(pickInfo.instance_id) ? 'Restore mount' : 'Destroy mount'}
                </Button>
                <span class="text-muted-foreground text-[11px]">
                  swaps to the destroyed (glb_dead) model
                </span>
              </div>
            {/if}
          </div>
        {:else}
          <div class="text-muted-foreground">
            Click a mesh in the viewer to inspect it. Press <kbd>Esc</kbd> to clear.
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</section>
