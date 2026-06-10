// Ship-page types: summary list (`/api/ships`) and the placements JSON
// (`<Ship>_accessories.json`) the toolkit emits per ship.
//
// Keep these as local subsets of the full sidecar schema — only what the
// SPA actually reads — to avoid pulling the whole METADATA_SPEC.md
// vocabulary into every consumer. Full schema docs:
// `docs/contracts/sidecar-schema.md` (TODO).

import type { HullSectionKey } from './hull';

export interface ShipSectionCounts {
  turrets: number;
  secondaries: number;
  antiair: number;
  torpedoes: number;
  accessories: number;
}

export interface ShipSummary {
  name: string;
  display_name: string;
  nation: string | null;
  ship_class: string | null;
  tier: number | null;
  /** Workspace-relative POSIX path. */
  hull_glb: string;
  accessories_json: string;
  sidecar_json: string | null;
  hull_bytes: number;
  /** Unix seconds. */
  hull_mtime: number;
  section_counts: ShipSectionCounts;
}

/**
 * Raw GameParams dispersion scalars surfaced for downstream combat consumers.
 * Field names intentionally mirror GameParams until gameplay code normalizes
 * the exact radius / ellipse model.
 */
export interface PlacementDispersion {
  maxDist?: number;
  sigmaCount?: number;
  taperDist?: number;
  normalDistribution?: boolean;
  idealRadius?: number;
  idealDistance?: number;
  minRadius?: number;
  delim?: number;
  radiusOnZero?: number;
  radiusOnDelim?: number;
  radiusOnMax?: number;
  ellipseRangeMin?: number;
  ellipseRangeMax?: number;
  minEllipseRanging?: number;
  medEllipseRanging?: number;
  maxEllipseRanging?: number;
  aiMGminEllipseRanging?: number;
  aiMGmedEllipseRanging?: number;
  aiMGmaxEllipseRanging?: number;
}

/**
 * Per-placement entry in `<Ship>_accessories.json`. Same common shape
 * across every typed section (turrets/secondaries/antiair/torpedoes/
 * accessories); the typed sections may carry extra category-specific
 * fields (caliber, reload, etc.) that we ignore in the viewer.
 */
export interface ShipPlacement {
  instance_id: string;
  /** Joins to `LibraryIndex.assets`. */
  asset_id: string;
  hp_name: string;
  /**
   * Hull section the placement belongs to. Drives sectioning + sinking
   * parenting downstream. May be null on data pre-2026-04-27.
   */
  parent_section?: HullSectionKey | null;
  /**
   * Specific hull mesh the placement visually rests on, e.g.
   * `Bow_DeckHouseShape` or `Bow_patch_MidFront_DeckHouseShape`. Drives
   * per-variant visibility — when a damage state hides the named mesh,
   * any placement bound to it hides too. Resolved by `skel_ext_resolve`
   * via mesh-AABB overlap; null when the asset wasn't in the library or
   * the placement falls outside every hull mesh AABB.
   */
  parent_mesh?: string | null;
  scope: string;
  category: string;
  subcategory: string | null;
  species: string | null;
  transform: {
    /** 16 floats, column-major, metres. */
    matrix: number[];
    position: [number, number, number];
  };
  dead_asset_id?: string | null;
  /**
   * Placement provenance. `"skel_ext_hash"` marks hull skel_ext
   * decoratives (voice tubes, binoculars, searchlights…) — a layer the
   * engine reads from the LOADED hull model's `.skel_ext` files, so a
   * hull-swap exterior replaces it wholesale (see
   * `ExteriorHullDelta.decoratives`). Absent on HP_-mount placements.
   */
  source?: string | null;
  /**
   * Per-mount ammo list (gun mounts only). Joins to
   * `Sidecar.ballistics.shells[<id>]` for the per-shell profile. Empty
   * or absent on AA, torpedo, and accessory placements.
   */
  ammo_ids?: string[];
  dispersion?: PlacementDispersion;
  /**
   * Per-HP miscFilter — a WHITELIST of `MP_<…>` placement_ids the WG
   * runtime renders for this asset's bundled attached accessories.
   * Three-state semantics: null = render all; `[]` = drop all non-isStyle;
   * non-empty = whitelist. Sourced from
   * `Vehicle.<section>.<HP_name>.miscFilter` via the pipeline's
   * GameParams autofill.
   */
  misc_filter?: string[];
  /**
   * Set by the pipeline's variant accessory swap when bone-direction
   * mismatch was corrected by post-multiplying the host placement matrix
   * by Ry(180°). The toolkit's `<asset>.attached_accessories.json` sub
   * matrices already carry an unconditional Ry(180°) for the WG natural-
   * front convention; combined with the corrected host the children land
   * 180° off (Baltimore Azur AGM019→AGM622 main-turret rangefinder/boats
   * facing the wrong way). Consumer pre-multiplies each sub matrix by
   * Ry(180°) when this is true.
   *
   * NOTE for new code: schema_v6 attached_accessories bakes the convention-B
   * basis conjugation into the matrix, so the consumer should NOT pre-Ry(180°)
   * — this field stays as a diagnostic only.
   */
  attached_y_flip?: boolean;
}

export interface ShipPlacementsDoc {
  schema_version?: number;
  ship?: { display_name?: string; nation?: string; species?: string; tier?: number };
  pipeline?: { toolkit_version?: string; generated_at?: string };
  turrets: ShipPlacement[];
  secondaries: ShipPlacement[];
  antiair: ShipPlacement[];
  torpedoes: ShipPlacement[];
  accessories: ShipPlacement[];
}

export type ShipSectionKey = 'turrets' | 'secondaries' | 'antiair' | 'torpedoes' | 'accessories';

export const SHIP_SECTIONS: ShipSectionKey[] = [
  'turrets',
  'secondaries',
  'antiair',
  'torpedoes',
  'accessories',
];
