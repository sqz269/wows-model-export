// Camo-category classifiers — pure functions, no DOM / no Three.js.
//
// Hull-side categories the per-stem cascade handles via §1ter Layer 1
// (per-ship `_camo_NN.dds`). The `Skin.categories` mechanism only fills
// the accessory-side gap; hull stems always classify into one of these
// and skip `Skin.categories`.

export const HULL_CATEGORIES = new Set(['tile', 'deckhouse', 'bulge']);

/**
 * Classify an MFM stem / asset_id / mesh name into a camouflage part
 * category. Direct port of `wows_model_export.resolve.camo::classify_part_category`
 * (which itself ports the toolkit's `camouflage.rs:59`).
 *
 * Categories returned (lowercase):
 *   tile (= hull) | deckhouse | bulge | gun | director | misc
 *
 * Hull-side rules read the SUFFIX; accessory-side rules read the 2-letter
 * type code at positions 1-2 of the stem (e.g. `AGM034` → "GM" → gun).
 * Falls back to `tile` when nothing matches — matches WG runtime's
 * "default to hull mask" behaviour.
 *
 * NOTE: `plane` and `float` aren't in the classifier — those tags appear
 * in WG's `<Textures>` and `<UV>` but the runtime hits them via a
 * separate code branch (placement metadata, not stem prefix). Consumers
 * handle plane/float routing via accessory placement `category` /
 * `subcategory` fields, not via this function.
 */
export function classifyPartCategory(stem: string): string {
  const lower = stem.toLowerCase();
  if (lower.endsWith('_hull') || lower.endsWith('_hull_wire')) return 'tile';
  if (lower.endsWith('_deckhouse')) return 'deckhouse';
  if (lower.includes('_bulge')) return 'bulge';
  if (stem.length >= 4 && stem[0] >= 'A' && stem[0] <= 'Z') {
    const cat = stem.slice(1, 3);
    if (cat === 'GM' || cat === 'GS' || cat === 'GA') return 'gun';
    if (cat === 'D0' || cat === 'D1' || cat === 'F0' || cat === 'F1') return 'director';
    if (cat === 'RS') return 'misc';
  }
  if (lower.includes('_hull')) return 'tile';
  return 'tile';
}

/**
 * Map a placement's gameplay-category metadata onto a camo category.
 * Falls back to {@link classifyPartCategory} when placement metadata is
 * missing or doesn't match cleanly. Used for catapult-launched aircraft
 * (`plane` / `float`) where stem-prefix classification fails.
 */
export function classifyPlacementCategory(
  assetId: string,
  category: string | null | undefined,
  subcategory: string | null | undefined,
): string {
  const cat = (category ?? '').toLowerCase();
  const sub = (subcategory ?? '').toLowerCase();
  if (cat === 'gun') return 'gun';
  if (cat === 'director') return 'director';
  if (cat === 'finder') return 'director';
  if (cat === 'radar') return 'misc';
  if (cat === 'catapult') {
    if (sub === 'plane' || sub === 'float') return sub;
    return 'plane';
  }
  if (cat === 'torpedo') return 'misc';
  if (cat === 'misc') return 'misc';
  return classifyPartCategory(assetId);
}
