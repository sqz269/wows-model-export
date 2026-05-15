<script lang="ts">
  // Three-region header that sits above the ShipViewer:
  //   Left   — display_name + nation/tier/class chips + internal name (code font)
  //   Center — load summary (placements / hull meshes / load time) + status pills
  //   Right  — quick-access action cluster (Frame, Reset camera)
  //
  // Mirrors AssetDetail.svelte's header bar so the Library and Ships
  // pages share a layout vocabulary. Toggles (textures, color mode,
  // seam states, etc.) stay in ShipControls — the header surfaces
  // *read-only state pills* + the two universally-useful camera
  // shortcuts. Clicking the "N unresolved" pill jumps the bottom panel
  // to the Unresolved tab (via `onShowUnresolved`).
  //
  // Status pills colour scheme mirrors AssetDetail's winding pill:
  //   green = healthy (textures on, no unresolved)
  //   amber = needs attention (unresolved assets, miscFilter drops)
  //   muted = neutral info (textures off, skin name)

  import type { ShipLoadStats, ShipViewer } from '$lib/ship';
  import type { ShipSummary, Skin } from '$lib/types';

  interface Props {
    ship: ShipSummary;
    viewer: ShipViewer | null;
    loadStats: ShipLoadStats | null;
    /** Mirror of the textures toggle in ShipControls. Drives the pill
     *  colour; the toggle itself stays in the side panel. */
    showTextures: boolean;
    skins: readonly Skin[];
    activeSkin: string | null;
    /** Jump the bottom inspector to a specific tab. Used by the
     *  "N unresolved" pill so the user can drill into the IDs. */
    onShowUnresolved?: () => void;
  }

  const {
    ship,
    viewer,
    loadStats,
    showTextures,
    skins,
    activeSkin,
    onShowUnresolved,
  }: Props = $props();

  const activeSkinName = $derived.by(() => {
    if (!activeSkin) return null;
    const s = skins.find((x) => x.skin_id === activeSkin);
    return s?.display_name || s?.skin_id || activeSkin;
  });

  const unresolvedCount = $derived(loadStats?.unresolvedAssets.size ?? 0);
  const miscDropCount = $derived(loadStats?.attachmentsFilteredByMisc ?? 0);

  // Chip metadata for the left header region. Skips falsy/null fields
  // so a ship with no `tier` doesn't leave a "tier null" placeholder.
  const chips = $derived.by(() => {
    const out: string[] = [];
    if (ship.nation) out.push(ship.nation);
    if (ship.ship_class) out.push(ship.ship_class);
    if (ship.tier != null) out.push(`T${ship.tier}`);
    return out;
  });
</script>

<!--
  Header bar: three regions, left-to-right.
  Left  — display_name + nation/tier/class chips + internal name.
  Center— load summary + at-a-glance status pills.
  Right — camera action cluster (Frame / Reset).
-->
<header class="bg-card border-border flex flex-none items-center gap-4 border-b px-5 py-2.5">
  <div class="flex min-w-0 flex-1 flex-col">
    <div class="flex items-baseline gap-2 min-w-0">
      <h2 class="m-0 truncate text-sm font-semibold">{ship.display_name}</h2>
      {#if chips.length}
        <div class="text-muted-foreground flex items-center gap-1 text-[11px]">
          {#each chips as c, i (c)}
            {#if i > 0}<span class="opacity-50">·</span>{/if}
            <span>{c}</span>
          {/each}
        </div>
      {/if}
    </div>
    <div class="text-muted-foreground mt-0.5 truncate font-mono text-[11px]">
      {ship.name}
    </div>
  </div>

  <div class="flex flex-none flex-col items-center gap-1">
    <div class="text-[11px] tabular-nums text-foreground">
      {#if loadStats}
        {loadStats.placementsRendered}/{loadStats.placementsRequested} placements ·
        {loadStats.attachmentsRendered} attached ·
        {loadStats.hullMeshCount} hull meshes ·
        {(loadStats.loadMs / 1000).toFixed(1)}s
      {:else}
        <span class="text-muted-foreground">Loading…</span>
      {/if}
    </div>
    <div class="flex items-center gap-1.5">
      {#if activeSkinName}
        <span
          class="bg-muted text-muted-foreground rounded px-1.5 py-[1px] text-[10px]"
          title={`Active skin: ${activeSkin}`}
        >
          skin: <span class="text-foreground">{activeSkinName}</span>
        </span>
      {/if}
      <span
        class="rounded px-1.5 py-[1px] text-[10px] font-semibold {showTextures
          ? 'bg-emerald-950/60 text-emerald-300'
          : 'bg-muted text-muted-foreground'}"
        title={showTextures
          ? 'DDS textures decoded and applied'
          : 'Untextured — toggle "Show textures" in the side panel to decode'}
      >
        textures: {showTextures ? 'on' : 'off'}
      </span>
      {#if unresolvedCount > 0}
        <button
          type="button"
          onclick={() => onShowUnresolved?.()}
          class="cursor-pointer rounded bg-amber-950/60 px-1.5 py-[1px] text-[10px] font-semibold text-amber-300 hover:bg-amber-950/80"
          title={`${unresolvedCount} asset_id(s) referenced by the ship's accessories.json had no matching library entry. Click to see the list.`}
        >
          {unresolvedCount} unresolved
        </button>
      {/if}
      {#if miscDropCount > 0}
        <span
          class="bg-muted text-muted-foreground rounded px-1.5 py-[1px] text-[10px]"
          title={`${miscDropCount} attached child(ren) dropped by miscFilter whitelist`}
        >
          {miscDropCount} misc-dropped
        </span>
      {/if}
    </div>
  </div>

  <div class="flex flex-none items-center gap-1.5">
    <button
      type="button"
      disabled={!viewer}
      onclick={() => viewer?.frameOn(null)}
      title="Frame the camera on the whole ship. Shortcut: F"
      class="rounded border border-border bg-popover px-2 py-1 text-xs hover:bg-accent disabled:opacity-60"
    >
      Frame <span class="text-muted-foreground ml-0.5">F</span>
    </button>
    <button
      type="button"
      disabled={!viewer}
      onclick={() => viewer?.resetCamera()}
      title="Reset the camera to the scene default. Shortcut: R"
      class="rounded border border-border bg-popover px-2 py-1 text-xs hover:bg-accent disabled:opacity-60"
    >
      Reset cam <span class="text-muted-foreground ml-0.5">R</span>
    </button>
  </div>
</header>
