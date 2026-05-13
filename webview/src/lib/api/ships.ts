// `/api/ships` client: ship summary list for the ship picker.

import { fetchJson } from './client';
import type { ShipSummary } from '$lib/types';

interface ShipsResponse {
  ships: ShipSummary[];
}

export async function fetchShips(): Promise<ShipSummary[]> {
  const res = await fetchJson<ShipsResponse>('/api/ships');
  return res.ships;
}
