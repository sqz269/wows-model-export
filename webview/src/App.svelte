<script lang="ts">
  import { onMount } from 'svelte';
  import HelpCircle from '@lucide/svelte/icons/circle-help';
  import { Toaster } from '$lib/components/ui/sonner';
  import { Button } from '$lib/components/ui/button';
  import { readRoute, onRouteChange, navigate, type RouteState } from '$lib/router';
  import {
    consumersHref,
    extractHref,
    gameParamsHref,
    libraryHref,
    projectilesHref,
    particlesHref,
    settingsHref,
    shipsHref,
  } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import Library from '$routes/Library.svelte';
  import Ships from '$routes/Ships.svelte';
  import Particles from '$routes/Particles.svelte';
  import Extract from '$routes/Extract.svelte';
  import Settings from '$routes/Settings.svelte';
  import GameParams from '$routes/GameParams.svelte';
  import Consumers from '$routes/Consumers.svelte';
  import Projectiles from '$routes/Projectiles.svelte';
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
    // user navigates away. Route-switch keys read the *Href() helpers
    // so jumping to Ships / Library with `2` / `1` lands on the last
    // selection (matching the topnav links).
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
          navigate(extractHref());
          e.preventDefault();
          return;
        case '2':
          navigate(shipsHref());
          e.preventDefault();
          return;
        case '3':
          navigate(libraryHref());
          e.preventDefault();
          return;
        case '4':
          navigate(projectilesHref());
          e.preventDefault();
          return;
        case '5':
          navigate(particlesHref());
          e.preventDefault();
          return;
        case '6':
          navigate(gameParamsHref());
          e.preventDefault();
          return;
        case '7':
          navigate(consumersHref());
          e.preventDefault();
          return;
        case '8':
          navigate(settingsHref());
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

  // `href` reads the navState helpers so the topnav routes back to the
  // most recent selection within each tab (e.g. clicking Ships from
  // Library lands on `#/ship/<lastShip>`, not bare `#/ships`). `$derived`
  // because the helpers read $state from nav_state.svelte.
  const NAV = $derived<
    Array<{ page: RouteState['page']; href: string; label: string; keyHint: string }>
  >([
    { page: 'extract', href: extractHref(), label: 'Extract', keyHint: '1' },
    { page: 'ships', href: shipsHref(), label: 'Ships', keyHint: '2' },
    { page: 'library', href: libraryHref(), label: 'Library', keyHint: '3' },
    { page: 'projectiles', href: projectilesHref(), label: 'Projectiles', keyHint: '4' },
    { page: 'particles', href: particlesHref(), label: 'Particles', keyHint: '5' },
    { page: 'gameparams', href: gameParamsHref(), label: 'GameParams', keyHint: '6' },
    { page: 'consumers', href: consumersHref(), label: 'Consumers', keyHint: '7' },
    { page: 'settings', href: settingsHref(), label: 'Settings', keyHint: '8' },
  ]);

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
<!--
  Each route receives its OWN typed slice of the URL. RouteState is a
  discriminated union (see lib/router.ts); the ternary narrows the
  union per branch so `route.assetId` only typechecks inside the
  library arm. Components see a domain-typed prop (`assetId` /
  `shipName` / `vehicleId`), so an asset_id can never leak into the
  ship route — the type system enforces what was previously a
  hand-written boundary check.
-->
<main class="relative flex flex-1 min-h-0 overflow-hidden">
  <div class={route.page === 'library' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Library
      assetId={route.page === 'library' ? route.assetId : null}
      active={route.page === 'library'}
    />
  </div>
  <div class={route.page === 'ships' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Ships
      shipName={route.page === 'ships' ? route.shipName : null}
      active={route.page === 'ships'}
    />
  </div>
  <div class={route.page === 'particles' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Particles
      particlePath={route.page === 'particles' ? route.particlePath : null}
      active={route.page === 'particles'}
    />
  </div>
  <div class={route.page === 'extract' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Extract
      vehicleId={route.page === 'extract' ? route.vehicleId : null}
      active={route.page === 'extract'}
    />
  </div>
  <div class={route.page === 'gameparams' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <GameParams
      entityId={route.page === 'gameparams' ? route.entityId : null}
      active={route.page === 'gameparams'}
    />
  </div>
  <div class={route.page === 'consumers' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Consumers active={route.page === 'consumers'} />
  </div>
  <div class={route.page === 'projectiles' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Projectiles
      selectedId={route.page === 'projectiles' ? route.projectileId : null}
      active={route.page === 'projectiles'}
    />
  </div>
  <div class={route.page === 'settings' ? 'flex flex-1 min-w-0' : 'hidden'}>
    <Settings />
  </div>
</main>

<HelpDialog open={helpOpen} onOpenChange={(v) => (helpOpen = v)} />

<Toaster position="bottom-right" />
