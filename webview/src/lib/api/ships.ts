// `/api/ships` client: ship summary list for the ship picker.
//
// Dedupes parallel callers via a module-level promise cache, matching
// the pattern in `./library.ts`. Only the Ships page consumes /api/ships
// today, but caching costs nothing and makes a future "ship list on
// dashboard" mount safe.

import { fetchJson } from './client';
import type { ShipSummary } from '$lib/types';

interface ShipsResponse {
  ships: ShipSummary[];
}

let cached: Promise<ShipSummary[]> | null = null;

export function fetchShips(): Promise<ShipSummary[]> {
  if (!cached) {
    cached = fetchJson<ShipsResponse>('/api/ships')
      .then((res) => res.ships)
      .catch((err) => {
        cached = null;
        throw err;
      });
  }
  return cached;
}

/** Reset the cache so a re-ingest is picked up on the next mount. */
export function invalidateShips(): void {
  cached = null;
}
