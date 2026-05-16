<script lang="ts">
  // Single DDS texture thumbnail. Fetches + decodes the mip-chain via
  // the shared off-screen renderer, then shows the result as a PNG
  // `<img>`. Falls through gracefully on placeholder textures
  // (`default_ao.dds`), unsupported formats, or decode failures.
  //
  // Re-decodes whenever `paths` changes (asset swap → new texture set).
  // Owns its blob URL — revokes on unmount and on path change so
  // long browsing sessions don't leak.

  import { decodeDdsTextureSetSlot, type DdsPreviewResult } from '$lib/dds/preview';
  import { DDS_SLOT_SRGB } from '$lib/dds';

  interface Props {
    /** Workspace-relative mip-chain paths from `LibraryAsset.texture_sets`. */
    paths: string[] | undefined;
    /** Base URL for resolving relative paths — usually the asset's
     *  `libraries/accessories/<rel>/` repo URL. */
    baseUrl: string;
    /** PBR slot name; controls sRGB and is displayed as the label. */
    slot: string;
    /** Tile size in CSS pixels. Default 96. */
    size?: number;
  }

  const { paths, baseUrl, slot, size = 96 }: Props = $props();

  let preview = $state<DdsPreviewResult | null>(null);
  let loading = $state(true);
  let failed = $state(false);

  /** Pick the highest-level mip filename for the path label / copy. */
  const labelPath = $derived.by(() => {
    const list = paths;
    if (!list || list.length === 0) return null;
    // Prefer the `.dds` (full chain) entry; fall back to whatever's there.
    const full = list.find((u) => u.toLowerCase().endsWith('.dds'));
    return full ?? list[0];
  });

  const filename = $derived.by(() => {
    if (!labelPath) return null;
    return labelPath.replace(/\\/g, '/').split('/').pop() ?? labelPath;
  });

  // Re-decode whenever the inputs change. svelte 5 reuses keyed
  // components across `{#each}` updates so an asset swap reaches us
  // as a prop change, not a remount — `$effect` covers both lifecycle
  // events. The cleanup revokes the blob URL before the next decode
  // (and on unmount), so a long browsing session can't leak.
  $effect(() => {
    const slotPaths = paths;
    const slotBase = baseUrl;
    const slotName = slot;
    let cancelled = false;
    let assignedBlob: string | null = null;
    preview = null;
    failed = false;
    loading = true;
    void (async () => {
      try {
        // glTF spec sRGB flags via `DDS_SLOT_SRGB`; unknown slots default
        // to linear (matches the texture pipeline elsewhere).
        const sRGB = DDS_SLOT_SRGB[slotName] ?? false;
        const res = await decodeDdsTextureSetSlot(slotPaths, slotBase, sRGB);
        if (cancelled) {
          if (res) URL.revokeObjectURL(res.blobUrl);
          return;
        }
        if (!res) {
          failed = true;
        } else {
          preview = res;
          assignedBlob = res.blobUrl;
        }
      } catch (err) {
        console.warn(`[dds-preview] ${slotName}:`, err);
        if (!cancelled) failed = true;
      } finally {
        if (!cancelled) loading = false;
      }
    })();
    return () => {
      cancelled = true;
      if (assignedBlob) URL.revokeObjectURL(assignedBlob);
    };
  });

  function copyPath() {
    if (!labelPath) return;
    void navigator.clipboard?.writeText(labelPath);
  }
</script>

<!--
  Tile layout: fixed-size thumbnail on top, then slot label + filename +
  dimensions. The thumbnail uses a CSS checkerboard so transparent /
  alpha-only textures (camoMask, occlusion R-channel) are still readable.
-->
<div
  class="border-border bg-popover/40 flex flex-none flex-col gap-1 rounded border p-1.5"
  style="width: {size + 12}px"
>
  <button
    type="button"
    onclick={copyPath}
    disabled={!labelPath}
    title={labelPath
      ? `${slot}: ${labelPath}\n(click to copy path)`
      : `no ${slot}`}
    class="dds-preview-tile relative flex flex-none items-center justify-center overflow-hidden rounded border border-border bg-checker disabled:cursor-default"
    style="width: {size}px; height: {size}px"
  >
    {#if preview}
      <img
        src={preview.blobUrl}
        alt={slot}
        class="block size-full object-contain"
        draggable="false"
      />
    {:else if loading}
      <span class="text-muted-foreground text-[10px]">loading…</span>
    {:else if failed}
      <span class="text-amber-300 text-[10px]">no preview</span>
    {:else}
      <span class="text-muted-foreground text-[10px]">—</span>
    {/if}
  </button>
  <div class="flex flex-col gap-0">
    <span class="text-foreground truncate text-[10px] font-semibold">{slot}</span>
    {#if preview}
      <span class="text-muted-foreground tabular-nums text-[9px]">
        {preview.width}×{preview.height}
      </span>
    {/if}
    {#if filename}
      <span
        class="text-muted-foreground overflow-hidden text-ellipsis whitespace-nowrap font-mono text-[9px]"
        title={filename}
      >
        {filename}
      </span>
    {/if}
  </div>
</div>

<style>
  /* Tiny checkerboard so transparent / low-alpha textures stay legible.
     Two-tile gradient at 8px gives a tight grid that doesn't clash with
     the surrounding rounded border. */
  :global(.bg-checker) {
    background-image:
      linear-gradient(45deg, #1f2937 25%, transparent 25%),
      linear-gradient(-45deg, #1f2937 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #1f2937 75%),
      linear-gradient(-45deg, transparent 75%, #1f2937 75%);
    background-size: 8px 8px;
    background-position:
      0 0,
      0 4px,
      4px -4px,
      -4px 0;
    background-color: #111827;
  }
</style>
