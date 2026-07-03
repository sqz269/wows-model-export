// Scene-global main-bucket draw compositor — the webview twin of the native
// pass-context list 0 (RE 2026-07-03, WoWS build 12506899):
//
//   fx_ParticleSystem_cookDrawRecords (@0x14071c820) appends EVERY main-bucket
//   system's 0x50-stride DrawRecs — across ALL effects — into ONE shared list,
//   each record keyed at rec+0x00 with its camera-view depth (positive =
//   farther; camera-attached coordinateStyle-1 systems get the −1000
//   sentinel; blend ∈ 0x2e8 && sortType < 2 systems get their keys
//   relinearised min→max over append order = emission-pinned).
//   fx_ParticleSystem_sortDrawLists (@0x14071d790) radix-sorts the list
//   ascending and copies it back REVERSED → strict back-to-front.
//   fx_Sprite_buildDrawRecords fills one instance buffer in that order and
//   fx_Sprite_coalesceBatches splits it into contiguous same-technique
//   batches — one DrawIndexedInstanced per batch.
//
// three.js can't switch materials mid-draw, so each contiguous same-system
// run of the sorted list becomes one pooled run mesh (SystemRenderer.packRun)
// with the system's material and a fractional renderOrder inside the main
// tier — three.js then submits the runs in exactly the native batch order.
// Per-system meshes remain for the non-main tiers (water/underwater/shimmer,
// see BLEND_BUCKET_RENDER_ORDER); underwater (native list 3) is also
// globally sorted natively but keeps the per-system approximation for now.
import type * as THREE from 'three';
import type { BucketDrawRecord, SystemRenderer } from './system-renderer';

/** Compose one frame of the main bucket: gather every composed system's
 *  records, apply the native global sort, split into runs, and hand each run
 *  to its owning system. Call once per ParticleScene tick, after every
 *  system has ticked. Systems must all be `composedMainBucket` members. */
export function composeMainBucket(
  systems: readonly SystemRenderer[],
  camera: THREE.Camera | null,
): void {
  if (camera) camera.updateMatrixWorld(true);
  const records: BucketDrawRecord[] = [];
  for (const s of systems) s.appendDrawRecords(records, camera);
  // Native: stable radix sort ascending on the float key, then a reversed
  // copy-back → descending = back-to-front. Array.prototype.sort is stable
  // (ES2019), so sort-then-reverse reproduces the native tie behavior.
  records.sort((a, b) => a.key - b.key);
  records.reverse();
  for (const s of systems) s.beginRuns();
  let runIdx = 0;
  let i = 0;
  while (i < records.length) {
    const sys = records[i].sys;
    let j = i + 1;
    while (j < records.length && records[j].sys === sys) j++;
    sys.packRun(records, i, j - i, runIdx++);
    i = j;
  }
  for (const s of systems) s.endRuns();
}
