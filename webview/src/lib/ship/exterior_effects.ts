import {
  WEAPON_PARTICLE_SOURCES,
  type ParticleAttachment,
  type SidecarDoc,
} from '../types/sidecar';

/** The producer resolves native hull replacement and theme path substitution.
 * Never concatenate: that would leave vanilla emitters under themed effects.
 *
 * `hullName` is the hull on screen. When the producer wrote that hull's
 * weapon-bound rows (`effects.by_hull`, or the exterior payload's own
 * `by_hull` when the exterior substitutes weapon effects), they replace the
 * list's weapon-bound rows; hull, AA-aura and map rows are kept. */
export function exteriorParticleAttachments(
  sidecar: SidecarDoc | null | undefined,
  exteriorId: string,
  hullName?: string | null,
): ParticleAttachment[] {
  const exteriorEffects = sidecar?.exteriors?.find((e) => e.exterior_id === exteriorId)?.effects;
  const rows = exteriorEffects?.attachments ?? sidecar?.effects?.attachments ?? [];
  if (!hullName) return rows;
  const hullRows =
    exteriorEffects?.by_hull?.[hullName]?.attachments ??
    sidecar?.effects?.by_hull?.[hullName]?.attachments;
  if (!hullRows) return rows;
  const kept = rows.filter((r) => !WEAPON_PARTICLE_SOURCES.has(r.source ?? 'hull'));
  return [...kept, ...hullRows];
}
