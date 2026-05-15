// Hull-mesh visibility — pure functions, no Three.js dependency.
//
// SHARED CONTRACT with the consumer-side hull-damage resolver. The
// webview's role is to be an independent verification view: identical
// inputs MUST produce identical visibility output across consumers. See
// `docs/contracts/damage-state.md` (TODO) for the full truth table.
//
// Tested by use rather than unit tests — load any ship, toggle a seam,
// observe the right meshes hide. Future: when the schema is stable, lift
// the resolver into a contracts package that every consumer shares.

import { HULL_SECTIONS, seamFor } from '$lib/types';
import type { HullSectionKey, SeamKey, SeamState } from '$lib/types';

// Damage-variant token regexes — match the conventions encoded by WG's
// hull mesh names.
//
// LOD naming has two flavours observed in three.js post-load:
//   `<base>_lod1Shape`, `_lod2Shape`, `_lod3Shape`, `_lod4Shape`
//   `<base>_lodShape1`, `_lodShape2`, `_lodShape3`
// Both indicate non-LOD0 and should hide under `lodPolicy === 'lod0'`.
export const LOD_RE = /_lod(?:[1-9]|Shape[1-9])/i;
export const PATCH_RE = /_patch_/i;
export const CRACK_RE = /_crack_/i;

/** Extract the LOD level number from a mesh name. Returns 0 for meshes
 *  without a `_lodN` / `_lodShapeN` suffix (the default high-detail
 *  mesh), or the parsed integer otherwise. Matches both naming flavours
 *  documented above. */
const LOD_LEVEL_RE = /_lod(?:Shape)?([1-9][0-9]*)/i;
export function lodLevelOfName(name: string): number {
  if (!name) return 0;
  const m = LOD_LEVEL_RE.exec(name);
  return m ? parseInt(m[1], 10) : 0;
}

/** Hidden by default — debug / collision overlays the user can opt into. */
export const HULL_HIDDEN_GROUPS = new Set(['Armor', 'Hitboxes']);

/**
 * Strip the parent-group prefix that the GLB import glues onto every
 * mesh name. Two flavours observed in the wild:
 *   gltFast:           `<Group>__<Mesh>`     (double-underscore)
 *   plain GLTFLoader:  `<Group> / <Mesh>`    (space-slash-space)
 * Try both. WG mesh names never contain `__` themselves, so the split
 * is unambiguous when it succeeds. Mirrors the consumer-side
 * `ShortMeshName` — keep them in sync if a third format appears.
 */
export function shortMeshName(raw: string): string {
  if (!raw) return raw;
  const dd = raw.lastIndexOf('__');
  if (dd >= 0) return raw.substring(dd + 2);
  const slash = raw.lastIndexOf(' / ');
  return slash >= 0 ? raw.substring(slash + 3) : raw;
}

/**
 * Identify a hull mesh's owning section by leading-prefix match against
 * `<Section>Shape` or `<Section>_*`. Returns null for non-section nodes
 * (group containers, debug overlays).
 */
export function sectionOfHullMesh(meshName: string): HullSectionKey | null {
  for (const s of HULL_SECTIONS) {
    if (
      meshName === `${s}Shape` ||
      meshName.startsWith(`${s}_`) ||
      meshName.startsWith(`${s}Shape`)
    ) {
      return s;
    }
  }
  return null;
}

/**
 * Decide whether a hull mesh should be rendered given its name + the
 * current per-seam damage state.
 *
 * One-line summary of the rules (full truth table in the contract doc):
 *   intact body / deckhouse / wire  → always visible
 *   X_patch_Y (bridge)              → visible iff seam(X, Y) == Intact
 *   X_crack_Y (broken edge)         → visible iff seam(X, Y) == Broken
 */
export function resolveMeshVisibility(
  meshName: string,
  seamStates: Record<SeamKey, SeamState>,
): boolean {
  const owning = sectionOfHullMesh(meshName);
  if (!owning) return true;

  const patchIdx = meshName.indexOf('_patch_');
  const crackIdx = meshName.indexOf('_crack_');
  const isPatch = patchIdx >= 0;
  const isCrack = crackIdx >= 0;

  if (isPatch || isCrack) {
    const start = (isPatch ? patchIdx : crackIdx) + '_patch_'.length;
    let end = meshName.indexOf('_', start);
    const shapeIdx = meshName.indexOf('Shape', start);
    if (shapeIdx >= 0 && (end < 0 || shapeIdx < end)) end = shapeIdx;
    if (end < 0) end = meshName.length;
    const adj = meshName.substring(start, end) as HullSectionKey;
    if (!HULL_SECTIONS.includes(adj)) return true;
    const seam = seamFor(owning, adj);
    if (!seam) return true;
    if (isPatch) return seamStates[seam] === 'Intact';
    return seamStates[seam] === 'Broken';
  }

  // Plain intact / deckhouse / wire / hide variants — always visible.
  // Section bodies are unaffected by seam damage; only the seams break.
  return true;
}

/** All-Intact seam states — the default state for a freshly loaded ship. */
export function defaultSeamStates(): Record<SeamKey, SeamState> {
  return {
    'Bow-MidFront': 'Intact',
    'MidFront-MidBack': 'Intact',
    'MidBack-Stern': 'Intact',
  };
}
