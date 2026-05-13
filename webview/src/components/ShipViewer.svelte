<script lang="ts">
  // Thin Svelte wrapper around lib/ship/ShipViewer. The Three.js layer
  // never imports Svelte directly — this component owns the lifecycle.
  //
  // Props:
  //   ship    — selected ship summary (changing it triggers a reload).
  //   library — accessory library index (resolved once at page mount).
  //   bindHandle — optional callback that receives the live viewer
  //                handle so the page can drive control toggles.
  //
  // Slots: none. The viewer mounts a `<canvas>` inside its container.

  import { onMount, untrack } from 'svelte';
  import { ShipViewer } from '$lib/ship';
  import type { ShipLoadStats } from '$lib/ship';
  import type { LibraryIndex, ShipSummary } from '$lib/types';

  interface Props {
    ship: ShipSummary;
    library: LibraryIndex;
    bindHandle?: (v: ShipViewer | null) => void;
    onProgress?: (msg: string) => void;
    onLoaded?: (stats: ShipLoadStats) => void;
    onError?: (err: unknown) => void;
  }

  const { ship, library, bindHandle, onProgress, onLoaded, onError }: Props = $props();

  let host: HTMLDivElement | null = $state(null);
  let viewer: ShipViewer | null = null;
  let loadToken = 0;

  onMount(() => {
    if (!host) return;
    viewer = new ShipViewer(host);
    bindHandle?.(viewer);

    return () => {
      bindHandle?.(null);
      const v = viewer;
      viewer = null;
      void v?.dispose();
    };
  });

  // React to ship / library changes. Use untrack on the viewer ref so
  // we don't take a dependency on our own internal handle.
  $effect(() => {
    const s = ship;
    const lib = library;
    void s;
    void lib;
    untrack(() => {
      if (!viewer) return;
      const token = ++loadToken;
      viewer
        .loadShip(s, lib, (msg) => {
          if (token === loadToken) onProgress?.(msg);
        })
        .then((stats) => {
          if (token === loadToken) onLoaded?.(stats);
        })
        .catch((err) => {
          if (token === loadToken) onError?.(err);
        });
    });
  });
</script>

<div class="viewer-host" bind:this={host}></div>

<style>
  .viewer-host {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    position: relative;
  }
  .viewer-host :global(canvas) {
    display: block;
    width: 100%;
    height: 100%;
  }
</style>
