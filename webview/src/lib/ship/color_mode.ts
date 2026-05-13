// Color-by-category / color-by-hull-section palettes + the material
// instances they back. Materials are built once per viewer (so toggling
// modes is a per-mesh reassignment, no allocation) and disposed when the
// viewer disposes.

import * as THREE from 'three';
import { HULL_SECTIONS, SHIP_SECTIONS } from '$lib/types';
import type { HullSectionKey, ShipSectionKey } from '$lib/types';
import { lightenTowardWhite } from '$lib/util/colors';

export type ColorMode = 'off' | 'category' | 'hullSection';

export const SECTION_LABELS: Record<ShipSectionKey, string> = {
  turrets: 'turrets',
  secondaries: 'secondaries',
  antiair: 'AA',
  torpedoes: 'torpedoes',
  accessories: 'accessories',
};

// Color-by-category material colors. Hull stays default-grey; accessories
// get muted slate so gameplay mounts read as focal points.
export const SECTION_COLORS: Record<ShipSectionKey, number> = {
  turrets: 0xd9554f,
  secondaries: 0xe69147,
  antiair: 0xe6c947,
  torpedoes: 0x47b8e6,
  accessories: 0x7e8597,
};

// Color-by-hull-section material colors. Picked for clear bow→stern
// gradient + accessibility-friendly contrast against the dark scene
// background. Used by the color mode AND by damage-state controls (each
// section's swatch matches its renderer color).
export const HULL_SECTION_COLORS: Record<HullSectionKey, number> = {
  Bow: 0x4a90e2,
  MidFront: 0x65c466,
  MidBack: 0xe6a23c,
  Stern: 0xe35d6a,
  Full: 0x999999,
};

export const HULL_SECTION_PATCH_COLORS: Record<HullSectionKey, number> = {
  Bow: lightenTowardWhite(HULL_SECTION_COLORS.Bow),
  MidFront: lightenTowardWhite(HULL_SECTION_COLORS.MidFront),
  MidBack: lightenTowardWhite(HULL_SECTION_COLORS.MidBack),
  Stern: lightenTowardWhite(HULL_SECTION_COLORS.Stern),
  Full: lightenTowardWhite(HULL_SECTION_COLORS.Full),
};

export const HULL_SECTION_NULL_COLOR = 0x404040;

/**
 * Per-placement color-mode swap targets. Built once per placement in
 * `tagAndIndexInstance`; switching color modes is a reassignment off
 * this record, no allocation.
 */
export interface PlacementColorEntry {
  mesh: THREE.Mesh;
  originalMaterial: THREE.Material | THREE.Material[];
  categoryMaterial: THREE.MeshStandardMaterial;
  hullSectionMaterial: THREE.MeshStandardMaterial;
}

/** Bundle of pre-built per-section / per-hull-section materials. */
export interface ColorMaterials {
  category: Record<ShipSectionKey, THREE.MeshStandardMaterial>;
  hullSection: Record<HullSectionKey, THREE.MeshStandardMaterial>;
  hullSectionPatch: Record<HullSectionKey, THREE.MeshStandardMaterial>;
  hullSectionNull: THREE.MeshStandardMaterial;
}

export function createColorMaterials(): ColorMaterials {
  const category = Object.fromEntries(
    SHIP_SECTIONS.map((s) => [
      s,
      new THREE.MeshStandardMaterial({
        color: SECTION_COLORS[s],
        roughness: 0.7,
        metalness: 0.0,
      }),
    ]),
  ) as Record<ShipSectionKey, THREE.MeshStandardMaterial>;

  const hullSection = Object.fromEntries(
    HULL_SECTIONS.map((s) => [
      s,
      new THREE.MeshStandardMaterial({
        color: HULL_SECTION_COLORS[s],
        roughness: 0.6,
        metalness: 0.0,
      }),
    ]),
  ) as Record<HullSectionKey, THREE.MeshStandardMaterial>;

  // Patch materials use a strong emissive in the section's saturated color
  // so the model self-illuminates regardless of lighting / camera angle.
  // The base color is the lightened (toward-white) variant; the combination
  // reads as a "glowing pastel" against the matte intact-mesh placements.
  // Roughness is dropped so the surface picks up specular highlights too.
  const hullSectionPatch = Object.fromEntries(
    HULL_SECTIONS.map((s) => [
      s,
      new THREE.MeshStandardMaterial({
        color: HULL_SECTION_PATCH_COLORS[s],
        emissive: HULL_SECTION_COLORS[s],
        emissiveIntensity: 0.85,
        roughness: 0.25,
        metalness: 0.0,
      }),
    ]),
  ) as Record<HullSectionKey, THREE.MeshStandardMaterial>;

  const hullSectionNull = new THREE.MeshStandardMaterial({
    color: HULL_SECTION_NULL_COLOR,
    roughness: 0.6,
    metalness: 0.0,
  });

  return { category, hullSection, hullSectionPatch, hullSectionNull };
}

export function disposeColorMaterials(m: ColorMaterials): void {
  for (const k of SHIP_SECTIONS) m.category[k].dispose();
  for (const k of HULL_SECTIONS) {
    m.hullSection[k].dispose();
    m.hullSectionPatch[k].dispose();
  }
  m.hullSectionNull.dispose();
}

export function applyColorMode(entries: PlacementColorEntry[], mode: ColorMode): void {
  for (const e of entries) {
    if (mode === 'off') e.mesh.material = e.originalMaterial;
    else if (mode === 'category') e.mesh.material = e.categoryMaterial;
    else e.mesh.material = e.hullSectionMaterial;
  }
}
