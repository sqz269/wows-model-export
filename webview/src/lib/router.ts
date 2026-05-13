// Hash-driven router state. Tiny — the webview has 3 routes and no
// nested navigation. A real router (svelte-spa-router, tinro, …) would
// be overkill; raw `hashchange` works fine.
//
// API: a Svelte 5 store ($state-backed) that components can subscribe to.

interface RouteState {
  page: 'library' | 'ships' | 'extract';
  /** The full hash (without leading `#/`), e.g. `ship/Iowa`. */
  path: string;
  /** First path segment after the page, e.g. `Iowa` for `#/ship/Iowa`. */
  param: string | null;
}

function parseHash(hash: string): RouteState {
  const path = hash.replace(/^#\/?/, '');
  const [head, rest] = path.split(/\/(.+)/, 2);

  if (head === 'extract') {
    return { page: 'extract', path, param: rest ?? null };
  }
  if (head === 'ships' || head === 'ship') {
    return { page: 'ships', path, param: rest ?? null };
  }
  // Default + `library` + `asset/...` all route to the library page.
  return { page: 'library', path, param: rest ?? null };
}

// Svelte 5 runes only work inside .svelte files / .svelte.ts files;
// for a plain module-level store, expose a subscribable factory the
// root component owns. The store impl is intentionally light — Svelte
// 5's `$state` will be used in App.svelte to bind to this directly.

export function readRoute(): RouteState {
  return parseHash(window.location.hash);
}

export function onRouteChange(listener: (s: RouteState) => void): () => void {
  const fire = () => listener(parseHash(window.location.hash));
  window.addEventListener('hashchange', fire);
  return () => window.removeEventListener('hashchange', fire);
}

export function navigate(hash: string): void {
  // Always include the leading `#/`, regardless of what the caller passed.
  const norm = hash.startsWith('#') ? hash : '#' + (hash.startsWith('/') ? hash : '/' + hash);
  window.location.hash = norm;
}

export type { RouteState };
