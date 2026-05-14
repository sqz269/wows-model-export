// Hull section / seam / damage-state types.
//
// Independent of any Three.js scene state — these are pure data
// describing WG's per-ship axial split. The four-piece axial split
// (Bow / MidFront / MidBack / Stern) is shared with consumer-side
// hull-damage resolvers; see `docs/contracts/damage-state.md` (TODO)
// for the cross-language contract.

export type HullSectionKey = 'Bow' | 'MidFront' | 'MidBack' | 'Stern' | 'Full';

export const HULL_SECTIONS: HullSectionKey[] = ['Bow', 'MidFront', 'MidBack', 'Stern', 'Full'];

// Per-section damage state. Reserved for future per-section damaged-look
// variants; not currently used by the visibility resolver (the live
// mechanism is per-seam).
export type SectionState = 'Intact' | 'SeamExposed' | 'Destroyed';

// Seam between two adjacent hull sections. WG's per-seam damage assets
// describe one patch (bridge) and two cracks (broken edges, one per side)
// per seam; the seam's state drives which is currently visible.
export type SeamKey = 'Bow-MidFront' | 'MidFront-MidBack' | 'MidBack-Stern';

export const SEAMS: SeamKey[] = ['Bow-MidFront', 'MidFront-MidBack', 'MidBack-Stern'];

export const SEAM_SECTIONS: Record<SeamKey, [HullSectionKey, HullSectionKey]> = {
  'Bow-MidFront': ['Bow', 'MidFront'],
  'MidFront-MidBack': ['MidFront', 'MidBack'],
  'MidBack-Stern': ['MidBack', 'Stern'],
};

// Per-seam state. Intact = bridge (patch) shows, both cracks hidden.
// Broken = bridge hidden, both cracks shown.
export type SeamState = 'Intact' | 'Broken';

/**
 * Look up the seam connecting two sections, in either order. Returns null
 * for non-adjacent pairs (e.g. Bow + Stern) — those don't share a seam,
 * so any patch/crack referencing them is malformed.
 */
export function seamFor(a: HullSectionKey, b: HullSectionKey): SeamKey | null {
  for (const s of SEAMS) {
    const [x, y] = SEAM_SECTIONS[s];
    if ((x === a && y === b) || (x === b && y === a)) return s;
  }
  return null;
}
