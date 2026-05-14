<script lang="ts">
  import { onMount } from 'svelte';
  import HelpCircle from '@lucide/svelte/icons/circle-help';
  import { Toaster } from '$lib/components/ui/sonner';
  import { Button } from '$lib/components/ui/button';
  import { readRoute, onRouteChange, navigate, type RouteState } from '$lib/router';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import Library from '$routes/Library.svelte';
  import Ships from '$routes/Ships.svelte';
  import Extract from '$routes/Extract.svelte';
  import HelpDialog from '$components/HelpDialog.svelte';

  let route = $state<RouteState>(readRoute());
  let helpOpen = $state(false);

  onMount(() => {
    const stopRoute = onRouteChange((s) => {
      route = s;
    });

    // Global shortcuts: page-agnostic actions only (route switching, help
    // dialog). Page-local shortcuts (ship search focus, camera reset)
    // live inside the page components so they unbind cleanly when the
    // user navigates away.
    const onKey = (e: KeyboardEvent) => {
      if (hasModifier(e)) return;
      if (isTypingContext(e)) return;
      switch (e.key) {
        case '?':
          helpOpen = !helpOpen;
          e.preventDefault();
          return;
        case 'Escape':
          if (helpOpen) {
            helpOpen = false;
            e.preventDefault();
          }
          return;
        case '1':
          navigate('#/library');
          e.preventDefault();
          return;
        case '2':
          navigate('#/ships');
          e.preventDefault();
          return;
        case '3':
          navigate('#/extract');
          e.preventDefault();
          return;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      stopRoute();
      window.removeEventListener('keydown', onKey);
    };
  });

  const NAV: Array<{ page: RouteState['page']; href: string; label: string; keyHint: string }> = [
    { page: 'library', href: '#/library', label: 'Library', keyHint: '1' },
    { page: 'ships', href: '#/ships', label: 'Ships', keyHint: '2' },
    { page: 'extract', href: '#/extract', label: 'Extract', keyHint: '3' },
  ];

  function go(e: MouseEvent, href: string) {
    e.preventDefault();
    navigate(href);
  }
</script>

<header
  class="bg-card border-border flex h-[38px] flex-none items-center gap-4 border-b px-4"
>
  <div
    class="text-muted-foreground text-xs font-semibold tracking-wider"
  >
    wows-model-export
  </div>
  <nav class="flex gap-1">
    {#each NAV as item (item.page)}
      <a
        href={item.href}
        onclick={(e) => go(e, item.href)}
        title={`Switch to ${item.label} (${item.keyHint})`}
        class="rounded px-3 py-1 text-xs text-muted-foreground hover:bg-popover hover:text-foreground hover:no-underline {route.page ===
        item.page
          ? 'bg-accent text-foreground'
          : ''}"
      >
        {item.label}
      </a>
    {/each}
  </nav>
  <Button
    variant="ghost"
    size="icon-sm"
    class="ml-auto"
    onclick={() => (helpOpen = true)}
    title="Keyboard shortcuts (?)"
    aria-label="Open keyboard shortcuts help"
  >
    <HelpCircle class="size-3.5" />
  </Button>
</header>

<!--
  Keep every route mounted; toggle visibility via `hidden`. Mount-once
  semantics mean a tab switch preserves filter state, scroll position,
  3D viewer cameras, and texture-decode caches that would otherwise be
  thrown away on unmount. Inactive routes get `display: none` so their
  ResizeObservers settle to 0×0 and the renderer skips real work.

  Each route receives `active` so its page-local keydown listener
  (Ships' `/`/R/F/Esc; Library's future `/` for asset search) only
  fires when the user is actually looking at that page.
-->
<main class="relative flex flex-1 min-h-0 overflow-hidden">
  <div class={route.page === 'library' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Library param={route.param} active={route.page === 'library'} />
  </div>
  <div class={route.page === 'ships' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Ships param={route.param} active={route.page === 'ships'} />
  </div>
  <div class={route.page === 'extract' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Extract param={route.param} active={route.page === 'extract'} />
  </div>
</main>

<HelpDialog open={helpOpen} onOpenChange={(v) => (helpOpen = v)} />

<Toaster position="bottom-right" />
