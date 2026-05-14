<script lang="ts">
  // Thin wrapper around lib/accessory/AccessoryViewer. The class manages
  // the Three.js scene + loader + texture pipeline; this component owns
  // the lifecycle.
  //
  // Props:
  //   url        — workspace-relative GLB to load (changing reloads).
  //   lib        — optional library asset context. When supplied, the
  //                viewer wires DDS textures via the shared
  //                TextureManager so the library page renders like the
  //                in-context ship page does.
  //   bindHandle — caller receives the live viewer handle for controls.

  import { onMount, untrack } from 'svelte';
  import { AccessoryViewer } from '$lib/accessory';
  import type { LibraryContext, LoadResult } from '$lib/accessory';

  interface Props {
    url: string | null;
    lib?: LibraryContext | null;
    bindHandle?: (v: AccessoryViewer | null) => void;
    onLoaded?: (res: LoadResult) => void;
    onError?: (err: unknown) => void;
  }

  const { url, lib, bindHandle, onLoaded, onError }: Props = $props();

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

  // Reload when url OR lib changes. lib carries the texture pipeline
  // context — switching between intact/dead variants of the same asset
  // changes url but keeps lib stable; switching assets changes both.
  $effect(() => {
    const target = url;
    const ctx = lib;
    untrack(() => {
      if (!viewer || !target) return;
      const token = ++loadToken;
      viewer
        .loadGlb(target, ctx ?? null)
        .then((res) => {
          if (token === loadToken) onLoaded?.(res);
        })
        .catch((err) => {
          if (token === loadToken) onError?.(err);
        });
    });
  });
</script>

<!--
  Tailwind arbitrary-child selectors size the Three.js-injected <canvas>
  (we can't put classes on it directly since it's created inside the
  AccessoryViewer class).
-->
<div
  bind:this={host}
  class="relative flex-1 min-w-0 min-h-0 [&_canvas]:block [&_canvas]:w-full [&_canvas]:h-full"
></div>
