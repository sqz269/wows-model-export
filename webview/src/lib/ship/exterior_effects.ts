import type { ParticleAttachment, SidecarDoc } from '../types/sidecar';

/** The producer resolves native hull replacement and theme path substitution.
 * Never concatenate: that would leave vanilla emitters under themed effects. */
export function exteriorParticleAttachments(
  sidecar: SidecarDoc | null | undefined,
  exteriorId: string,
): ParticleAttachment[] {
  return sidecar?.exteriors?.find((e) => e.exterior_id === exteriorId)
    ?.effects?.attachments ?? sidecar?.effects?.attachments ?? [];
}
