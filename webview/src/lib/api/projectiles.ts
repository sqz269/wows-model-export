// `/api/projectiles` client. One round trip pulls both the geometry
// index AND the ammo profiles JSON (~few MB combined when fully built)
// since the Projectiles page joins them client-side at render time.
//
// Caching follows the accessory `fetchLibrary` pattern: module-level
// promise dedupe so concurrent mount calls (route switches, etc.) only
// hit the backend once. After a successful bootstrap build, call
// `invalidateProjectiles()` so the next read picks up the fresh files.

import { fetchJson } from './client';
import type {
  ProjectilesMissingPayload,
  ProjectilesPayload,
} from '$lib/types/projectiles';

let cached: Promise<ProjectilesPayload> | null = null;

/**
 * Fetch the merged projectile + ammo payload. Resolves with the
 * "ok: true" shape on success; throws an Error carrying the
 * "ok: false" missing-files payload on 503.
 *
 * The thrown Error's `.body` field carries the typed
 * :class:`ProjectilesMissingPayload` for callers that want to render
 * the empty-state hint.
 */
export function fetchProjectiles(): Promise<ProjectilesPayload> {
  if (!cached) {
    cached = fetchJson<ProjectilesPayload>('/api/projectiles').catch(
      (err) => {
        // Don't pin a failed result — a "build is in flight" 503 should
        // not block a fresh read after the build finishes.
        cached = null;
        throw err;
      },
    );
  }
  return cached;
}

/** Drop the in-memory payload. Call after the bootstrap "projectiles"
 *  build finishes so the next page mount re-fetches from disk. */
export function invalidateProjectiles(): void {
  cached = null;
}

// Re-export the missing-payload type for consumers that need to
// render the empty state.
export type { ProjectilesMissingPayload };
