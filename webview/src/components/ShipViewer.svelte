<script lang="ts">
  // Thin Svelte wrapper around lib/ship/ShipViewer. The Three.js layer
  // never imports Svelte directly — this component owns the lifecycle.
  //
  // Props:
  //   ship    — selected ship summary (changing it triggers a reload).
  //   library — accessory library index (resolved once at page mount).
  //   bindHandle — optional callback that receives the live viewer
  //                handle so the page can drive control toggles.
  //   onPick — fires for every canvas click; receives the picked
  //            accessory record or null if the click hit empty space /
  //            hull-only / helpers. Page-level state (mesh inspector
  //            overlay, F-frame target) lives in the consumer.

  import { onMount, untrack } from 'svelte';
  import { ShipViewer } from '$lib/ship';
  import type { PickResult, ShipLoadStats } from '$lib/ship';
  import type { LibraryIndex, ShipSummary } from '$lib/types';

  interface Props {
    ship: ShipSummary;
    library: LibraryIndex;
    bindHandle?: (v: ShipViewer | null) => void;
    onProgress?: (msg: string) => void;
    onLoaded?: (stats: ShipLoadStats) => void;
    onError?: (err: unknown) => void;
    /** Fires for every canvas click. `clientX`/`clientY` are document
     *  coords (event.clientX/Y) — consumers convert to local-container
     *  px if they need to position an overlay relative to the viewer. */
    onPick?: (pick: PickResult | null, clientX: number, clientY: number) => void;
  }

  const { ship, library, bindHandle, onProgress, onLoaded, onError, onPick }: Props = $props();

  let host: HTMLDivElement | null = $state(null);
  let viewer: ShipViewer | null = null;
  let loadToken = 0;
  // Distinguish click-to-pick from camera drag. OrbitControls swallows
  // pointer drags; we still want a click to pick. Track pointerdown
  // position + suppress pick if the pointer moved past a small threshold
  // (drag, not click).
  let downX = 0;
  let downY = 0;
  let downButton = -1;

  onMount(() => {
    if (!host) return;
    const v = new ShipViewer(host);
    viewer = v;
    bindHandle?.(v);

    const canvas = v.getCanvas();
    const DRAG_THRESHOLD_PX = 4;

    const handleDown = (e: PointerEvent) => {
      downX = e.clientX;
      downY = e.clientY;
      downButton = e.button;
    };
    const handleUp = (e: PointerEvent) => {
      // Left button only; orbit-controls uses middle/right for camera.
      if (downButton !== 0 || e.button !== 0) return;
      const dx = e.clientX - downX;
      const dy = e.clientY - downY;
      if (Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) return;
      if (!viewer) return;
      const pick = viewer.pickAt(e.clientX, e.clientY);
      onPick?.(pick, e.clientX, e.clientY);
    };
    canvas.addEventListener('pointerdown', handleDown);
    canvas.addEventListener('pointerup', handleUp);

    return () => {
      canvas.removeEventListener('pointerdown', handleDown);
      canvas.removeEventListener('pointerup', handleUp);
      bindHandle?.(null);
      viewer = null;
      void v.dispose();
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

<!--
  Tailwind arbitrary child selectors style the Three.js-injected
  <canvas> (we can't add classes to it directly since it's created
  inside `ShipViewer` and appended to this host).
-->
<div
  bind:this={host}
  class="relative flex-1 min-w-0 min-h-0 [&_canvas]:block [&_canvas]:w-full [&_canvas]:h-full"
></div>
