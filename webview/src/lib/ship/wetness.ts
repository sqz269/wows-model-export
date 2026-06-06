// Per-clone rain-wetness uniform set (engine "path 1" global weather wetness:
// `overallWetness` + `wetnessColor`, reference/engine/wg_render_ship_water.md
// §5). Layer 1 of the wetness consumer — a whole-ship tint + gloss drop driven
// by the active WG weather. Injected into the hull/accessory material program
// by `attachCamoChunk` (there is ONE `onBeforeCompile` slot per material, so
// wetness rides the camo chunk) but kept as a SEPARATE uniform object stored at
// `mat.userData.wetnessUniforms`, so a camo/skin swap never clobbers it.
//
// Layers 2 (waterline band) + 3 (deck puddles, §5b.2) extend this set later.

import * as THREE from 'three';

export interface WetnessUniforms {
  /** Per-weather `overallWetness`: 0 = dry .. ~0.35–0.5 (Storm). Master scale. */
  wetOverall: { value: number };
  /** Linear RGB the albedo lerps toward (per-weather `wetnessColor`). */
  wetColor: { value: THREE.Color };
  /** Roughness multiplier at full wet (lower = glossier). Tunable default. */
  wetRoughDrop: { value: number };
}

/** Neutral dark blue-grey — the fallback wet tint when a weather authors no
 *  `wetnessColor` (never tint toward the hull albedo). */
const DEFAULT_WET_COLOR = (): THREE.Color => new THREE.Color(0.05, 0.06, 0.08);

export function makeWetnessUniforms(): WetnessUniforms {
  return {
    wetOverall: { value: 0.0 }, // dry until a WG weather is applied
    wetColor: { value: DEFAULT_WET_COLOR() },
    wetRoughDrop: { value: 0.6 },
  };
}

/** Pull the wetness uniform set off a material (or array of materials).
 *  Empty when the material never went through `attachCamoChunk` (e.g. an
 *  untextured raw GLB material). */
export function wetnessUniformsOf(mat: THREE.Material | THREE.Material[]): WetnessUniforms[] {
  const list: WetnessUniforms[] = [];
  const collect = (m: THREE.Material) => {
    const data = m.userData as { wetnessUniforms?: WetnessUniforms } | undefined;
    if (data?.wetnessUniforms) list.push(data.wetnessUniforms);
  };
  if (Array.isArray(mat)) mat.forEach(collect);
  else collect(mat);
  return list;
}
