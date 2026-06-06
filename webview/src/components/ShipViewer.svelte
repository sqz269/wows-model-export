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
  import { ShipViewer, thicknessToColorHex } from '$lib/ship';
  import type { ArmorPickResult, PickResult, ShipLoadStats } from '$lib/ship';
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

  // Armor X-ray hover read-out. Populated by `pickArmorAt` on pointer move
  // while the armor view is enabled; rendered as a small tooltip following
  // the cursor (container-local px in `left`/`top`). Null = hide.
  let armorTip = $state<(ArmorPickResult & { left: number; top: number }) | null>(null);
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
    // Dev-console diagnostic surface. Use:
    //   __shipViewer__.getNormalDiagnostics()
    //   __shipViewer__.setNormalScale(3.5)
    (window as unknown as { __shipViewer__: ShipViewer }).__shipViewer__ = v;

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

    // Armor X-ray hover. pointermove fires fast, so coalesce raycasts to one
    // per animation frame. Suppressed while a button is held (camera orbit /
    // pan) and whenever the armor view is off (pickArmorAt short-circuits).
    let hoverX = 0;
    let hoverY = 0;
    let hoverPending = false;
    const resolveHover = () => {
      hoverPending = false;
      if (!viewer || !host || !viewer.getArmorViewEnabled()) {
        armorTip = null;
        return;
      }
      const hit = viewer.pickArmorAt(hoverX, hoverY);
      if (!hit) {
        armorTip = null;
        return;
      }
      const rect = host.getBoundingClientRect();
      // Offset from the cursor, clamped so the tooltip stays on-canvas.
      const left = Math.min(Math.max(hoverX - rect.left + 14, 4), rect.width - 196);
      const top = Math.min(Math.max(hoverY - rect.top + 16, 4), rect.height - 92);
      armorTip = { ...hit, left, top };
    };
    const handleMove = (e: PointerEvent) => {
      if (e.buttons !== 0 || !viewer || !viewer.getArmorViewEnabled()) {
        if (armorTip) armorTip = null;
        return;
      }
      hoverX = e.clientX;
      hoverY = e.clientY;
      if (hoverPending) return;
      hoverPending = true;
      requestAnimationFrame(resolveHover);
    };
    const handleLeave = () => {
      if (armorTip) armorTip = null;
    };
    canvas.addEventListener('pointermove', handleMove);
    canvas.addEventListener('pointerleave', handleLeave);

    return () => {
      canvas.removeEventListener('pointerdown', handleDown);
      canvas.removeEventListener('pointerup', handleUp);
      canvas.removeEventListener('pointermove', handleMove);
      canvas.removeEventListener('pointerleave', handleLeave);
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
>
  {#if armorTip}
    <!--
      Armor X-ray hover read-out. Floats over the canvas at the cursor;
      pointer-events-none so it never eats the orbit/pick gestures.
    -->
    <div
      class="pointer-events-none absolute z-20 rounded-md border border-border bg-popover/95 px-2.5 py-1.5 text-[11px] shadow-lg backdrop-blur-sm"
      style="left:{armorTip.left}px; top:{armorTip.top}px;"
    >
      <div class="flex items-center gap-1.5">
        <span
          class="inline-block size-3 flex-none rounded-[3px]"
          style="background:{thicknessToColorHex(armorTip.thicknessMm)}"
        ></span>
        <span class="text-foreground font-semibold tabular-nums">
          {armorTip.thicknessMm > 0 ? `${armorTip.thicknessMm} mm` : 'no armor'}
        </span>
        {#if armorTip.zoneLabel}
          <span class="text-muted-foreground">· {armorTip.zoneLabel}</span>
        {/if}
      </div>
      {#if armorTip.layers && armorTip.layers.length > 1}
        <div class="text-muted-foreground text-[10px] tabular-nums">
          layers: {armorTip.layers.join(' + ')} mm
        </div>
      {/if}
      {#if armorTip.source === 'mount'}
        <div class="text-muted-foreground text-[10px]">
          turret armor{armorTip.hp ? ` · ${armorTip.hp}` : ''}
        </div>
        {#if armorTip.owner}
          <div class="text-muted-foreground/70 max-w-[180px] truncate font-mono text-[9px]">
            {armorTip.owner}
          </div>
        {/if}
      {:else if armorTip.zones && armorTip.zones.length > 0}
        <div class="text-muted-foreground max-w-[180px] truncate text-[10px]">
          zones: {armorTip.zones.join(', ')}
        </div>
      {/if}
      <div class="text-muted-foreground/70 text-[9px] tabular-nums">mat #{armorTip.materialId}</div>
    </div>
  {/if}
</div>
