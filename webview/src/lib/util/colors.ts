// Color helpers used by the section-color palette and patch-anchored
// material assignment in the ship viewer.

/**
 * Lighten a packed RGB value toward white by `ratio`. Used for
 * patch-anchored placements so they pop against intact-mesh placements'
 * deeper section colors. Pairs with an emissive boost (see the ship
 * viewer's hullSectionPatchMaterials) to make patches visibly glow
 * regardless of lighting / camera angle.
 */
export function lightenTowardWhite(rgb: number, ratio = 0.55): number {
  const r = (rgb >> 16) & 0xff;
  const g = (rgb >> 8) & 0xff;
  const b = rgb & 0xff;
  const mix = (c: number) => Math.round(c * (1 - ratio) + 255 * ratio);
  return (mix(r) << 16) | (mix(g) << 8) | mix(b);
}
