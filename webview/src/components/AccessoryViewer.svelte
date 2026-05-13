<script lang="ts">
  // Thin wrapper around lib/accessory/AccessoryViewer. The class manages
  // the Three.js scene + loader; this component owns the lifecycle.
  //
  // Props:
  //   url        — workspace-relative GLB to load (changing reloads).
  //   bindHandle — caller receives the live viewer handle for controls.
  //
  // The `url` prop is the load trigger — set it to `null` to teardown,
  // change it to swap models without recreating the viewer.

  import { onMount, untrack } from 'svelte';
  import { AccessoryViewer } from '$lib/accessory';
  import type { LoadResult } from '$lib/accessory';

  interface Props {
    url: string | null;
    bindHandle?: (v: AccessoryViewer | null) => void;
    onLoaded?: (res: LoadResult) => void;
    onError?: (err: unknown) => void;
  }

  const { url, bindHandle, onLoaded, onError }: Props = $props();

  let host: HTMLDivElement | null = $state(null);
  let viewer: AccessoryViewer | null = null;
  let loadToken = 0;

  onMount(() => {
    if (!host) return;
    viewer = new AccessoryViewer(host);
    bindHandle?.(viewer);
    return () => {
      bindHandle?.(null);
      const v = viewer;
      viewer = null;
      v?.dispose();
    };
  });

  $effect(() => {
    const target = url;
    untrack(() => {
      if (!viewer || !target) return;
      const token = ++loadToken;
      viewer
        .loadGlb(target)
        .then((res) => {
          if (token === loadToken) onLoaded?.(res);
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
