// Hash-driven router state. Tiny — the webview has 3 routes and no
// nested navigation. A real router (svelte-spa-router, tinro, …) would
// be overkill; raw `hashchange` works fine.
//
// RouteState is a discriminated union, not a flat record with a bare
// `param: string | null`. The hash carries different semantics per
// page — `#/asset/X` is an assetId, `#/ship/Y` is a shipName — and
// modelling that in the type system makes cross-route pollution a
// compile error. App.svelte routes each variant's payload to the
// matching child component; if a fourth route lands tomorrow, the
// destructure at the boundary forces an explicit decision.
//
// API: callers `readRoute()` for the current state, `onRouteChange()`
// to subscribe to hash changes, `navigate(href)` to push a new hash.

export type LibraryRoute = {
  page: 'library';
  /** The full hash (without leading `#/`), e.g. `asset/AGM034_16in50_Mk7`. */
  path: string;
  /** Selected asset_id from `#/asset/<id>`. Null on bare `#/library`. */
  assetId: string | null;
};

export type ShipsRoute = {
  page: 'ships';
  path: string;
  /** Selected ship name from `#/ship/<name>`. Null on bare `#/ships`. */
  shipName: string | null;
};

export type ExtractRoute = {
  page: 'extract';
  path: string;
  /** Selected vehicle id from `#/extract/<id>`. Null on bare `#/extract`.
   *  Reserved for the eventual Vehicle picker port. */
  vehicleId: string | null;
};

export type SettingsRoute = {
  page: 'settings';
  path: string;
};

export type GameParamsRoute = {
  page: 'gameparams';
  path: string;
  /** Selected GameParams entity id from `#/gameparams/<id>`. Null on
   *  bare `#/gameparams`. */
  entityId: string | null;
};

export type ConsumersRoute = {
  page: 'consumers';
  path: string;
};

export type RouteState =
  | LibraryRoute
  | ShipsRoute
  | ExtractRoute
  | SettingsRoute
  | GameParamsRoute
  | ConsumersRoute;

function parseHash(hash: string): RouteState {
  const path = hash.replace(/^#\/?/, '');
  const [head, rest] = path.split(/\/(.+)/, 2);

  if (head === 'extract') {
    return { page: 'extract', path, vehicleId: rest ?? null };
  }
  if (head === 'ships' || head === 'ship') {
    return { page: 'ships', path, shipName: rest ?? null };
  }
  if (head === 'settings') {
    return { page: 'settings', path };
  }
  if (head === 'gameparams') {
    return { page: 'gameparams', path, entityId: rest ?? null };
  }
  if (head === 'consumers') {
    return { page: 'consumers', path };
  }
  // Default + `library` + `asset/...` all route to the library page.
  return { page: 'library', path, assetId: rest ?? null };
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
