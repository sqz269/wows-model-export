<script lang="ts">
  // Projectiles route. Mirrors Library.svelte's structure (left list +
  // right detail) but joins two artifacts:
  //
  //   libraries/projectiles/index.json           (geometry)
  //   libraries/projectiles/ammo_profiles.json   (per-ammo ballistics)
  //
  // Two list modes:
  //
  //   mesh  → rows are unique projectile asset_ids (~dozens). Detail
  //           pane shows the GLB + a "this mesh is used by …" hint
  //           (the join's reverse direction).
  //   ammo  → rows are ammo_ids (~thousands). Detail pane shows the
  //           linked mesh (when present) + the per-ammo stats card.
  //
  // The 3D viewer is the existing AccessoryViewer with `lib={null}` —
  // for v1 we skip DDS texture binding (the texture pipeline's URL
  // prefix is hardcoded to libraries/accessories/; a refactor to make
  // it configurable is parked for later). Embedded GLB materials still
  // render so the user can see geometry.

  import { onMount } from 'svelte';
  import { navigate } from '$lib/router';
  import { navState, settingsHref } from '$lib/nav_state.svelte';
  import {
    fetchProjectiles,
    invalidateProjectiles,
  } from '$lib/api';
  import type {
    AmmoProfile,
    ProjectileFilterState,
    ProjectileListMode,
    ProjectileMesh,
    ProjectilesPayload,
  } from '$lib/types/projectiles';
  import type { LibraryAsset } from '$lib/types';
  import type {
    AccessoryViewer as AccessoryViewerClass,
    LibraryContext,
    LoadResult,
  } from '$lib/accessory';

  import AccessoryViewer from '$components/AccessoryViewer.svelte';
  import ProjectileList from '$components/ProjectileList.svelte';
  import ProjectileStatsCard from '$components/ProjectileStatsCard.svelte';
  import ProjectileAmmoUsageList from '$components/ProjectileAmmoUsageList.svelte';

  interface Props {
    /** URL fragment after `#/projectile/` — either an asset_id (mesh
     *  mode) or an ammo_id (ammo mode). Mode is inferred from which
     *  index the id resolves into. */
    selectedId: string | null;
    active: boolean;
  }
  const { selectedId, active: _active }: Props = $props();

  let payload = $state<ProjectilesPayload | null>(null);
  let loadError = $state<string | null>(null);
  let loadHint = $state<string | null>(null);

  let mode = $state<ProjectileListMode>('mesh');
  let filter = $state<ProjectileFilterState>({
    text: '',
    nations: { include: new Set(), exclude: new Set() },
    categories: { include: new Set(), exclude: new Set() },
    ammoTypes: { include: new Set(), exclude: new Set() },
    species: { include: new Set(), exclude: new Set() },
  });

  // Current selection. In mesh mode this is an asset_id; in ammo mode
  // it's an ammo_id. The URL fragment hands us a value; we figure out
  // which mode it belongs to based on which index has the key.
  let selection = $state<string | null>(null);

  // LOD filter state. Defaults to 0 so the user lands on a clean
  // single-resolution render — projectile GLBs ship with 2-4 LODs per
  // mesh and rendering all of them stacked produces visible z-fighting.
  // Sticky across asset switches; clamped to "All" when the new asset
  // doesn't have the previously-selected LOD level (mirrors the
  // accessory page's clamp behaviour).
  let lodFilter = $state<number | null>(0);
  let viewer = $state<AccessoryViewerClass | null>(null);
  let loadResult = $state<LoadResult | null>(null);

  /** Distinct LOD levels present on the currently-loaded mesh, ascending.
   *  Drives the LOD dropdown's options + the clamp guard. */
  const availableLods = $derived.by<number[]>(() => {
    if (!loadResult) return [];
    const set = new Set<number>();
    for (const m of loadResult.meshes) set.add(m.lod);
    return Array.from(set).sort((a, b) => a - b);
  });

  // Clamp the sticky filter once a new asset's meshes arrive — if the
  // chosen level isn't present, fall back to "All" so the viewport
  // never goes empty on a load.
  $effect(() => {
    if (!loadResult) return;
    if (lodFilter !== null && !availableLods.includes(lodFilter)) {
      lodFilter = null;
      viewer?.setLodFilter(null);
    }
  });

  // Apply the current filter every time the viewer or filter changes.
  // Without this the initial-load default never reaches the viewer
  // before the user has interacted with the dropdown.
  $effect(() => {
    if (!viewer) return;
    viewer.setLodFilter(lodFilter);
  });

  function setLod(v: number | null) {
    lodFilter = v;
    viewer?.setLodFilter(v);
  }

  onMount(async () => {
    try {
      payload = await fetchProjectiles();
    } catch (err) {
      // Try to surface the structured 503 body when the artifacts
      // are missing (the SettingsHref pointer in the UI guides the
      // user to the Build button).
      const raw = err instanceof Error ? err.message : String(err);
      loadError = raw;
      const body = err && typeof err === 'object' && 'body' in err
        ? (err as { body: unknown }).body
        : null;
      if (body && typeof body === 'object' && 'hint' in body) {
        loadHint = String((body as { hint: unknown }).hint);
      }
    }
  });

  // Adopt the URL selection into local state. The mode comes from
  // wherever the id resolves.
  $effect(() => {
    if (!selectedId || !payload) return;
    if (payload.index.assets[selectedId]) {
      selection = selectedId;
      if (mode !== 'mesh') mode = 'mesh';
    } else if (payload.ammo_profiles.profiles[selectedId]) {
      selection = selectedId;
      if (mode !== 'ammo') mode = 'ammo';
    } else {
      // Unknown id — leave selection cleared so the empty state shows.
      selection = null;
    }
  });

  function onSelect(id: string) {
    navigate(`#/projectile/${encodeURIComponent(id)}`);
  }

  // Mirror to cross-route nav memory so the topnav "Projectiles" link
  // lands back here with the right entry open after a detour.
  $effect(() => {
    if (selection) navState.lastProjectileId = selection;
  });

  function onModeChange(next: ProjectileListMode) {
    // Switching modes clears the selection because the id space differs.
    if (next === mode) return;
    mode = next;
    selection = null;
    navigate('#/projectiles');
  }

  // ── Derived viewer + stats inputs ───────────────────────────────────
  // Dependency order (top → bottom): activeMesh feeds the viewer inputs
  // (glbUrl / libContext) + the reverse lookup (usedByAmmo); the reverse
  // lookup feeds the per-mesh ammo sub-selection; that feeds activeAmmo
  // (mesh mode). activeAmmo also serves ammo mode directly off `selection`.

  /** The currently-selected mesh — directly in mesh mode, or via the
   *  ammo profile's asset_id in ammo mode. Null when there's no mesh
   *  (pure-VFX ammo). */
  const activeMesh = $derived.by<{
    id: string | null;
    mesh: ProjectileMesh | null;
  }>(() => {
    if (!payload || !selection) return { id: null, mesh: null };
    if (mode === 'mesh') {
      const m = payload.index.assets[selection];
      return m ? { id: selection, mesh: m } : { id: null, mesh: null };
    }
    const p = payload.ammo_profiles.profiles[selection];
    if (!p?.asset_id) return { id: null, mesh: null };
    const m = payload.index.assets[p.asset_id];
    return m ? { id: p.asset_id, mesh: m } : { id: p.asset_id, mesh: null };
  });

  /** Workspace-relative GLB URL for the viewer. */
  const glbUrl = $derived.by<string | null>(() => {
    const m = activeMesh.mesh;
    if (!m) return null;
    // The /repo static server normalises slashes; encode each segment.
    const parts = m.glb.split(/[\\/]/).map(encodeURIComponent);
    return `/repo/libraries/projectiles/${parts.join('/')}`;
  });

  /** LibraryContext to hand the AccessoryViewer so the texture pipeline
   *  binds DDS files via /repo/libraries/projectiles/... instead of
   *  the default accessory path.
   *
   *  ProjectileMesh shares the load-bearing fields with LibraryAsset
   *  (`glb`, `texture_sets`, `materials`, `nation`/`category`) but not
   *  the full schema (no `scope`, `used_by_ships`, etc.). We synthesise
   *  a LibraryAsset-shaped object so the type system is happy and the
   *  downstream texture manager + accessory-mesh registration code
   *  reads the same fields it always has. */
  const libContext = $derived.by<LibraryContext | null>(() => {
    const id = activeMesh.id;
    const mesh = activeMesh.mesh;
    if (!id || !mesh) return null;
    const synth: LibraryAsset = {
      scope:           mesh.category,
      category:        mesh.category,
      subcategory:     null,
      species:         null,
      glb:             mesh.glb,
      textures_dds:    mesh.textures_dds ?? null,
      glb_bytes:       mesh.glb_bytes,
      built_at:        mesh.built_at ?? null,
      used_by_ships:   [],
      texture_sets:    mesh.texture_sets,
      materials:       mesh.materials,
    };
    return {
      assetId:     id,
      asset:       synth,
      variant:     'main',
      libraryRoot: 'projectiles',
    };
  });

  /** Reverse-lookup: ammo ids that use the active mesh, ascending.
   *  Drives both the "used by N" header hint and the clickable usage
   *  list in mesh mode. Empty in ammo mode (the gate skips the
   *  full-corpus scan when it isn't needed). */
  const usedByAmmo = $derived.by<string[]>(() => {
    if (!payload || !activeMesh.id || mode !== 'mesh') return [];
    const out: string[] = [];
    for (const [aid, p] of Object.entries(payload.ammo_profiles.profiles)) {
      if (p.asset_id === activeMesh.id) out.push(aid);
    }
    return out.sort();
  });

  /** Which ammo profile (out of `usedByAmmo`) the user is inspecting in
   *  mesh mode. Ephemeral — NOT URL-encoded, since the URL holds the
   *  mesh and this is a within-mesh inspection. */
  let meshAmmoSelection = $state<string | null>(null);

  /** The effective inspected ammo for the active mesh. Honors the user's
   *  click while it's still valid; otherwise falls back to the first
   *  usage entry. The fallback covers a mesh switch (the previous
   *  selection belongs to a different mesh — ammo→mesh is many-to-one,
   *  so a stale id never appears in another mesh's usage list). */
  const meshAmmoEffective = $derived.by<string | null>(() => {
    if (usedByAmmo.length === 0) return null;
    if (meshAmmoSelection && usedByAmmo.includes(meshAmmoSelection)) {
      return meshAmmoSelection;
    }
    return usedByAmmo[0];
  });

  /** The ammo profile feeding the stats card. In ammo mode it's the URL
   *  selection; in mesh mode it's the user-picked (or first) profile that
   *  uses the active mesh. */
  const activeAmmo = $derived.by<{
    id: string | null;
    profile: AmmoProfile | null;
  }>(() => {
    if (!payload || !selection) return { id: null, profile: null };
    if (mode === 'ammo') {
      const p = payload.ammo_profiles.profiles[selection];
      return p ? { id: selection, profile: p } : { id: null, profile: null };
    }
    // mesh mode — whichever associated ammo the user picked (or first).
    const id = meshAmmoEffective;
    if (!id) return { id: null, profile: null };
    const p = payload.ammo_profiles.profiles[id];
    return p ? { id, profile: p } : { id: null, profile: null };
  });

  function retryLoad() {
    loadError = null;
    loadHint = null;
    invalidateProjectiles();
    void (async () => {
      try {
        payload = await fetchProjectiles();
      } catch (err) {
        loadError = err instanceof Error ? err.message : String(err);
      }
    })();
  }
</script>

<div class="flex flex-1 min-w-0 h-full">
  {#if loadError}
    <div
      class="text-destructive flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center"
    >
      <strong>Failed to load projectiles:</strong>
      <code class="mx-1 break-all max-w-[60ch]">{loadError}</code>
      {#if loadHint}
        <p class="text-muted-foreground m-0 max-w-[60ch] text-xs">
          {loadHint}
        </p>
      {/if}
      <p class="text-muted-foreground m-0 max-w-[60ch] text-xs">
        Open
        <a
          href={settingsHref()}
          onclick={(e) => {
            e.preventDefault();
            navigate(settingsHref());
          }}
          class="text-foreground underline hover:no-underline"
        >Settings → Workspace artifacts</a>
        and click <em>Build</em> next to “Projectile library + ammo profiles”.
      </p>
      <button
        type="button"
        onclick={retryLoad}
        class="border border-border hover:bg-popover/60 rounded px-3 py-1.5 text-xs"
      >
        Retry
      </button>
    </div>
  {:else if !payload}
    <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center">
      Loading projectiles…
    </div>
  {:else}
    <ProjectileList
      index={payload.index}
      ammo={payload.ammo_profiles}
      {mode}
      activeId={selection}
      {filter}
      {onModeChange}
      onFilterChange={(next) => (filter = next)}
      {onSelect}
    />

    <section class="flex flex-1 min-w-0 flex-col min-h-0">
      {#if selection && (activeMesh.mesh || activeAmmo.profile)}
        <!-- Header row: id + counts + meta + LOD picker -->
        <header class="border-border bg-card flex flex-none items-center justify-between gap-3 border-b px-4 py-2">
          <div class="flex items-baseline gap-2 min-w-0">
            <code class="text-foreground font-mono text-xs break-all">{selection}</code>
            {#if mode === 'mesh' && activeMesh.mesh}
              <span class="text-muted-foreground text-[11px]">
                {activeMesh.mesh.nation} · {activeMesh.mesh.category}
              </span>
            {:else if mode === 'ammo' && activeAmmo.profile}
              <span class="text-muted-foreground text-[11px]">
                {activeAmmo.profile.ammo_type} · {activeAmmo.profile.species}
              </span>
            {/if}
          </div>
          <div class="flex items-center gap-3 flex-shrink-0">
            {#if availableLods.length > 1}
              <label class="text-muted-foreground flex items-center gap-1.5 text-[11px]">
                LOD
                <select
                  value={lodFilter === null ? 'all' : String(lodFilter)}
                  onchange={(e) => {
                    const v = e.currentTarget.value;
                    setLod(v === 'all' ? null : Number(v));
                  }}
                  class="h-6 rounded border border-border bg-popover px-1.5 text-[11px] text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30"
                >
                  <option value="all">All ({availableLods.length})</option>
                  {#each availableLods as lod (lod)}
                    <option value={String(lod)}>LOD {lod}</option>
                  {/each}
                </select>
              </label>
            {/if}
            {#if mode === 'mesh' && usedByAmmo.length > 0}
              <span class="text-muted-foreground text-[11px]">
                used by {usedByAmmo.length} ammo profile{usedByAmmo.length === 1 ? '' : 's'}
              </span>
            {/if}
          </div>
        </header>

        <div class="flex flex-1 min-h-0">
          <!-- 3D viewer (flex-grow) -->
          <div class="flex flex-1 min-w-0 min-h-0">
            {#if glbUrl}
              <AccessoryViewer
                url={glbUrl}
                lib={libContext}
                bindHandle={(v) => (viewer = v)}
                onLoaded={(res) => (loadResult = res)}
              />
            {:else if mode === 'ammo' && activeAmmo.profile && !activeMesh.mesh}
              <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center text-xs">
                {#if activeAmmo.profile.asset_id}
                  Mesh <code>{activeAmmo.profile.asset_id}</code> not in projectile index.
                {:else}
                  This ammo entry has no mesh (pure VFX — laser / plane tracer / wave).
                {/if}
              </div>
            {:else}
              <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center text-xs">
                Geometry unavailable.
              </div>
            {/if}
          </div>

          {#if mode === 'mesh'}
            <!-- Reverse lookup: usage list (top) + stats for the picked
                 ammo (bottom). The container owns the left border so the
                 two stacked panels read as one sidebar. -->
            <div class="border-border flex w-80 flex-shrink-0 flex-col min-h-0 border-l">
              <div class="flex min-h-0 flex-col" style="flex: 1 1 45%;">
                {#key activeMesh.id}
                  <ProjectileAmmoUsageList
                    ammoIds={usedByAmmo}
                    profiles={payload.ammo_profiles.profiles}
                    selectedId={activeAmmo.id}
                    onSelect={(id) => (meshAmmoSelection = id)}
                  />
                {/key}
              </div>
              <div class="border-border min-h-0 flex-1 overflow-y-auto border-t">
                <ProjectileStatsCard
                  ammoId={activeAmmo.id}
                  profile={activeAmmo.profile}
                  fallbackTitle={activeMesh.id ?? undefined}
                />
              </div>
            </div>
          {:else}
            <!-- Ammo mode: single stats card. -->
            <div class="border-border w-72 flex-shrink-0 overflow-y-auto border-l">
              <ProjectileStatsCard
                ammoId={activeAmmo.id}
                profile={activeAmmo.profile}
              />
            </div>
          {/if}
        </div>
      {:else}
        <div class="text-muted-foreground flex flex-1 items-center justify-center p-6 text-center text-xs">
          {mode === 'mesh'
            ? 'Select a projectile mesh from the list.'
            : 'Select an ammo profile from the list.'}
        </div>
      {/if}
    </section>
  {/if}
</div>
