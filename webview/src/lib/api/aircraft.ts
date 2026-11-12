// `/api/aircraft` client: the aircraft library index
// (`libraries/aircraft/index.json`). Module-level promise dedupe, like
// `fetchProjectiles`.

import { fetchJson } from './client';
import type { AircraftPayload, AnimationDocument } from '$lib/types/aircraft';

let cached: Promise<AircraftPayload> | null = null;

/** Fetch the aircraft library index. Throws on 503 (index not built); the
 *  error's `.body` carries the `hint`. */
export function fetchAircraft(): Promise<AircraftPayload> {
  if (!cached) {
    cached = fetchJson<AircraftPayload>('/api/aircraft').catch((err) => {
      cached = null;
      throw err;
    });
  }
  return cached;
}

/** Drop the cached index so the next read picks up a rebuilt library. */
export function invalidateAircraft(): void {
  cached = null;
}

/** Fetch an airframe's animation document (`animation.file`, relative to
 *  the library root), served from the workspace like the GLB. */
export function fetchAircraftAnimation(file: string): Promise<AnimationDocument> {
  const url = `/repo/libraries/aircraft/${file.split(/[\\/]/).map(encodeURIComponent).join('/')}`;
  return fetchJson<AnimationDocument>(url);
}
