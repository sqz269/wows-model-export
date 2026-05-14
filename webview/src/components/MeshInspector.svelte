<script lang="ts">
  // Floating card surfaced when the user clicks a mesh in the ship
  // viewer. Renders `userData` resolved by ShipViewer.pickAt — the
  // accessory's asset_id, hull anchorage, attached-bundle info — plus
  // a link into the Library page for the matching asset.
  //
  // Positioning: the parent passes container-local px (x/y). It hosts
  // the card inside `.main` (which is `position: relative`), so we lay
  // out with `position: absolute` at the supplied coordinates.

  import ExternalLink from '@lucide/svelte/icons/external-link';
  import { Button } from '$lib/components/ui/button';
  import { Kbd } from '$lib/components/ui/kbd';
  import { navigate } from '$lib/router';
  import type { PickedAssetInfo } from '$lib/ship';
  import type { LibraryIndex } from '$lib/types';

  interface Props {
    info: PickedAssetInfo;
    /** Click coordinates in the host container's local px. */
    x: number;
    y: number;
    library: LibraryIndex | null;
    onClose: () => void;
  }

  const { info, x, y, library, onClose }: Props = $props();

  const libEntry = $derived(library?.assets[info.asset_id] ?? null);

  function openInLibrary() {
    navigate(`#/asset/${encodeURIComponent(info.asset_id)}`);
  }
</script>

<div
  role="dialog"
  aria-label="Mesh inspector"
  style="left: {x}px; top: {y}px;"
  class="bg-card border-border pointer-events-auto absolute z-20 min-w-[220px] max-w-[320px] rounded-md border p-2.5 text-xs text-foreground shadow-[0_12px_30px_rgba(0,0,0,0.55)]"
>
  <header class="border-border mb-1.5 flex items-center gap-1.5 border-b pb-1">
    <button
      type="button"
      onclick={openInLibrary}
      title="Open in Library"
      class="text-primary inline-flex flex-1 min-w-0 items-center gap-1.5 bg-transparent p-0 text-left font-mono text-xs font-medium hover:underline"
    >
      <span class="overflow-hidden text-ellipsis whitespace-nowrap">{info.asset_id}</span>
      <ExternalLink class="size-3 shrink-0" />
    </button>
    <Button
      variant="ghost"
      size="icon-xs"
      onclick={onClose}
      aria-label="Close inspector"
      class="size-[18px]"
    >
      ×
    </Button>
  </header>

  <dl class="grid grid-cols-[auto_1fr] items-center gap-x-2.5 gap-y-1 text-[11px]">
    {#if info.section}
      <dt class="text-muted-foreground lowercase">section</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">{info.section}</dd>
    {/if}
    {#if info.parent_section}
      <dt class="text-muted-foreground lowercase">hull anchor</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">{info.parent_section}</dd>
    {/if}
    {#if info.parent_mesh}
      <dt class="text-muted-foreground lowercase">parent mesh</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">
        <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">{info.parent_mesh}</code>
      </dd>
    {/if}
    {#if info.instance_id}
      <dt class="text-muted-foreground lowercase">instance</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">
        <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">{info.instance_id}</code>
      </dd>
    {/if}
    {#if info.attached_to_instance_id}
      <dt class="text-muted-foreground lowercase">attached to</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">
        <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
          {info.attached_to_instance_id}
        </code>
      </dd>
    {/if}
    {#if info.attached_placement_id}
      <dt class="text-muted-foreground lowercase">placement</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">
        <code class="bg-popover rounded-sm px-1 font-mono text-[11px]">
          {info.attached_placement_id}
        </code>
      </dd>
    {/if}
    {#if libEntry}
      <dt class="text-muted-foreground lowercase">scope</dt>
      <dd class="m-0 overflow-hidden text-ellipsis whitespace-nowrap">
        {libEntry.scope}/{libEntry.category}{libEntry.subcategory
          ? `/${libEntry.subcategory}`
          : ''}
      </dd>
      <dt class="text-muted-foreground lowercase">used by</dt>
      <dd class="m-0">
        {libEntry.used_by_ships.length} ship{libEntry.used_by_ships.length === 1 ? '' : 's'}
      </dd>
    {/if}
  </dl>

  <footer class="border-border text-muted-foreground mt-1.5 flex items-center gap-1 border-t pt-1 text-[10px]">
    <Kbd>F</Kbd>
    <span>frame</span>
    <span class="mx-1">·</span>
    <Kbd>Esc</Kbd>
    <span>close</span>
  </footer>
</div>
