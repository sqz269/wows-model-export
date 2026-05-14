// In-viewer winding flip — rebuilds the geometry's index buffer with
// each triangle reversed. Cached on the geometry as `__origIndex` so
// the toggle is reversible without re-loading the GLB.
//
// Distinct from `/api/flip-winding` which rewrites the GLB on disk:
// this is the live "preview the flipped winding" toggle; the persist
// path uses the API.

import * as THREE from 'three';

interface IndexCache {
  __origIndex?: THREE.BufferAttribute;
}

/** Reverse triangle winding in place. Idempotent — subsequent calls
 *  no-op (already-flipped index stays flipped). */
export function flipWindingIndex(mesh: THREE.Mesh): void {
  const g = mesh.geometry;
  if (!g) return;
  const idx = g.index;
  if (!idx) return;
  const cache = g as unknown as IndexCache;
  if (!cache.__origIndex) cache.__origIndex = idx;
  const arr = idx.array as ArrayLike<number>;
  const flipped = new Uint32Array(arr.length);
  for (let i = 0; i < arr.length; i += 3) {
    flipped[i] = arr[i];
    flipped[i + 1] = arr[i + 2];
    flipped[i + 2] = arr[i + 1];
  }
  g.setIndex(new THREE.BufferAttribute(flipped, 1));
}

/** Restore the original index buffer, if one was cached. */
export function resetWindingIndex(mesh: THREE.Mesh): void {
  const g = mesh.geometry;
  if (!g) return;
  const cache = g as unknown as IndexCache;
  if (cache.__origIndex) g.setIndex(cache.__origIndex);
}
