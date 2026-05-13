<script lang="ts">
  import { onMount } from 'svelte';
  import { readRoute, onRouteChange, navigate, type RouteState } from '$lib/router';
  import Library from '$routes/Library.svelte';
  import Ships from '$routes/Ships.svelte';
  import Extract from '$routes/Extract.svelte';

  let route = $state<RouteState>(readRoute());

  onMount(() => {
    return onRouteChange((s) => {
      route = s;
    });
  });

  const NAV: Array<{ page: RouteState['page']; href: string; label: string }> = [
    { page: 'library', href: '#/library', label: 'Library' },
    { page: 'ships', href: '#/ships', label: 'Ships' },
    { page: 'extract', href: '#/extract', label: 'Extract' },
  ];

  function go(e: MouseEvent, href: string) {
    e.preventDefault();
    navigate(href);
  }
</script>

<header class="topnav">
  <div class="brand">wows-model-export</div>
  <nav>
    {#each NAV as item (item.page)}
      <a href={item.href} class:active={route.page === item.page} onclick={(e) => go(e, item.href)}>
        {item.label}
      </a>
    {/each}
  </nav>
</header>

<main class="page-host">
  {#if route.page === 'library'}
    <Library param={route.param} />
  {:else if route.page === 'ships'}
    <Ships param={route.param} />
  {:else if route.page === 'extract'}
    <Extract param={route.param} />
  {/if}
</main>

<style>
  .topnav {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 16px;
    height: 38px;
    flex: 0 0 auto;
    background: var(--bg-side);
    border-bottom: 1px solid var(--border);
  }
  .brand {
    font-size: 12px;
    font-weight: 600;
    color: var(--fg-dim);
    letter-spacing: 0.04em;
  }
  nav {
    display: flex;
    gap: 4px;
  }
  nav a {
    display: inline-block;
    padding: 5px 12px;
    border-radius: 3px;
    font-size: 12px;
    color: var(--fg-dim);
  }
  nav a:hover {
    background: var(--bg-elev);
    color: var(--fg);
    text-decoration: none;
  }
  nav a.active {
    background: var(--accent-bg);
    color: var(--fg);
  }
  .page-host {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    overflow: hidden;
  }
</style>
