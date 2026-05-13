// Apply the current visibility state across the hull AND every
// placement bound to a hull mesh. Pure data → boolean predicates from
// `visibility.ts` plus an LOD / damage-variant override; this module
// just walks the tracked maps and assigns `.visible`.

import type * as THREE from 'three';
import { resolveMeshVisibility } from './visibility';
import type { SeamKey, SeamState } from '$lib/types';

export interface CascadeInputs {
  /** Short-name → hull-renderer list. */
  hullRenderersByMesh: Map<string, THREE.Mesh[]>;
  /** parent_mesh → placement nodes. Hides cascade onto these. */
  placementsByMesh: Map<string, THREE.Object3D[]>;
  /** Non-LOD0 meshes — hidden under `lod0`. */
  hullLowLodMeshes: THREE.Object3D[];
  placementLowLodMeshes: THREE.Object3D[];
  /** Cracks + patches — hidden unless the diagnostic toggle is on. */
  hullDamageMeshes: THREE.Object3D[];

  seamStates: Record<SeamKey, SeamState>;
  lodPolicy: 'lod0' | 'all';
  damageVariantsVisible: boolean;
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

  // LOD override: hide non-LOD0 if policy says so.
  for (const m of inp.hullLowLodMeshes) {
    if (inp.lodPolicy === 'lod0') m.visible = false;
  }
  for (const m of inp.placementLowLodMeshes) {
    if (inp.lodPolicy === 'lod0') m.visible = false;
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
  isLowLod = false,
  isDamageVariant = false,
): boolean {
  if (!resolveMeshVisibility(meshName, inp.seamStates)) return false;
  if (isLowLod && inp.lodPolicy === 'lod0') return false;
  if (isDamageVariant && !inp.damageVariantsVisible) return false;
  return true;
}
