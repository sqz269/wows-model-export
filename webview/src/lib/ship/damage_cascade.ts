// Apply the current visibility state across the hull AND every
// placement bound to a hull mesh. Pure data → boolean predicates from
// `visibility.ts` plus an LOD / damage-variant override; this module
// just walks the tracked maps and assigns `.visible`.

import type * as THREE from 'three';
import { resolveMeshVisibility } from './visibility';
import type { LodPolicy } from './placement';
import type { SeamKey, SeamState } from '$lib/types';

export interface CascadeInputs {
  /** Short-name → hull-renderer list. */
  hullRenderersByMesh: Map<string, THREE.Mesh[]>;
  /** parent_mesh → placement nodes. Hides cascade onto these. */
  placementsByMesh: Map<string, THREE.Object3D[]>;
  /** Hull meshes bucketed by LOD level (0 = high-detail, 1+ = coarser). */
  hullMeshesByLodLevel: Map<number, THREE.Object3D[]>;
  /** Placement meshes bucketed by LOD level. */
  placementMeshesByLodLevel: Map<number, THREE.Object3D[]>;
  /** Cracks + patches — hidden unless the diagnostic toggle is on. */
  hullDamageMeshes: THREE.Object3D[];

  seamStates: Record<SeamKey, SeamState>;
  lodPolicy: LodPolicy;
  damageVariantsVisible: boolean;
}

/** Parse a `'lod${N}'` policy string into its numeric level, or `null`
 *  for `'all'`. Used by the cascade + meshIsVisibleNow predicates. */
export function policyLodLevel(p: LodPolicy): number | null {
  if (p === 'all') return null;
  const n = parseInt(p.slice(3), 10);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Apply the current visibility state. Idempotent — call after any seam /
 * LOD / damage-variant toggle.
 */
export function applyAllStates(inp: CascadeInputs): void {
  // Per-mesh visibility from the damage-state resolver.
  for (const [short, renderers] of inp.hullRenderersByMesh) {
    const visible = resolveMeshVisibility(short, inp.seamStates);
    for (const r of renderers) r.visible = visible;
  }

  // LOD override. `'all'` is a no-op; `'lodN'` hides every mesh whose
  // level isn't N (including level-0 meshes when N >= 1, so the user
  // can inspect a coarser LOD's geometry in isolation).
  const target = policyLodLevel(inp.lodPolicy);
  if (target !== null) {
    for (const [level, meshes] of inp.hullMeshesByLodLevel) {
      if (level !== target) {
        for (const m of meshes) m.visible = false;
      }
    }
    for (const [level, meshes] of inp.placementMeshesByLodLevel) {
      if (level !== target) {
        for (const m of meshes) m.visible = false;
      }
    }
  }

  // Damage-variant override: by default the resolver hides cracks (and
  // shows intact patches); the diagnostic toggle forces both visible.
  if (inp.damageVariantsVisible) {
    for (const m of inp.hullDamageMeshes) m.visible = true;
  }

  // Cascade hull visibility into placements: a placement bound to a
  // hidden hull mesh hides with it. Builds the inverse map at each call
  // (cheap; the set of distinct parent_meshes is small per ship).
  for (const [parentMesh, nodes] of inp.placementsByMesh) {
    const renderers = inp.hullRenderersByMesh.get(parentMesh);
    const parentVisible = renderers ? renderers.some((r) => r.visible) : true; // unknown parent mesh → keep visible
    for (const n of nodes) n.visible = parentVisible;
  }
}

/** True iff the named mesh would currently render. */
export function meshIsVisibleNow(
  meshName: string,
  inp: Pick<CascadeInputs, 'seamStates' | 'lodPolicy' | 'damageVariantsVisible'>,
  meshLodLevel = 0,
  isDamageVariant = false,
): boolean {
  if (!resolveMeshVisibility(meshName, inp.seamStates)) return false;
  const target = policyLodLevel(inp.lodPolicy);
  if (target !== null && meshLodLevel !== target) return false;
  if (isDamageVariant && !inp.damageVariantsVisible) return false;
  return true;
}
