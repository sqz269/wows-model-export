// Cross-route navigation memory.
//
// The router's URL hash carries exactly one selection at a time
// (`#/ship/<name>` OR `#/asset/<id>` OR `#/library` etc.). When the
// user clicks a tab in the topnav, the hash for the OTHER tab is
// lost — the Ships route gets `param = null` while the user is on
// Library, and (without help) its `activeShip` resolves to null too,
// unmounting the ShipViewer + losing every piece of non-persisted
// state (camera, mesh inspector pin, seam toggles, active skin).
//
// This module is the bridge. Each route records the last non-null
// selection here; the topnav reads it to build smart hrefs so a click
// on "Ships" routes to `#/ship/<lastShip>` instead of bare `#/ships`,
// and each route's `activeX` derivation uses internal state that adopts
// `param` when non-null but stays sticky when it goes away — so the
// viewer stays mounted across tab switches.
//
// In-memory only. Persisting last-selection across page reloads is a
// separate decision (would surprise users returning hours later) — if
// we want it, the store.ts schema can absorb these two fields.

class NavState {
  lastShipName = $state<string | null>(null);
  lastAssetId = $state<string | null>(null);
}

export const navState = new NavState();

/** Topnav href for the Ships tab. Falls back to `#/ships` when no
 *  ship has been selected this session. */
export function shipsHref(): string {
  return navState.lastShipName
    ? `#/ship/${encodeURIComponent(navState.lastShipName)}`
    : '#/ships';
}

/** Topnav href for the Library tab. Falls back to `#/library` when
 *  no asset has been opened this session. */
export function libraryHref(): string {
  return navState.lastAssetId
    ? `#/asset/${encodeURIComponent(navState.lastAssetId)}`
    : '#/library';
}

/** Topnav href for the Extract tab. Reserved for future param-carrying
 *  state (selected vehicle / variant); for now there's no inner state
 *  to remember. */
export function extractHref(): string {
  return '#/extract';
}
