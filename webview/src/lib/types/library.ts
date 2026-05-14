// Accessory library types. Mirrors the schema emitted by the pipeline's
// `wows-build-accessory-library` command (sidecar + index.json under
// `<workspace>/libraries/accessories/`).
//
// The library is fleet-wide: one GLB + DDS mip chain per `asset_id`,
// shared across every ship that places the asset.

export interface LibraryIndex {
  version: number;
  asset_count: number;
  assets: Record<string, LibraryAsset>;
}

export interface LibraryAsset {
  scope: string;
  category: string;
  subcategory: string | null;
  species: string | null;
  /** Path relative to `<workspace>/libraries/accessories/`. */
  glb: string;
  /** Directory path relative to `accessories/`, holding the raw WG DDS mip
   *  chain. `null` when the asset has no textures on disk. */
  textures_dds: string | null;
  glb_bytes: number;
  /**
   * Unix seconds (int) — when the asset's GLB was first written into the
   * library. Preserved across rebuilds; missing on legacy entries built
   * before the field was introduced.
   */
  built_at?: number | null;
  used_by_ships: string[];
  /** Optional destroyed-state variant. */
  glb_dead?: string;
  glb_dead_bytes?: number;
  /**
   * Variant → slot → mip-chain paths (relative to `accessories/`).
   * Variants include "main", "dead", "camo_<name>", "dead_camo_<name>".
   *
   * ASSET-LEVEL fallback: a flat merge of every stem's files in the dir
   * — works for single-material accessories but blurs together multi-
   * mesh assets (directors, complex radars). Per-material `texture_sets`
   * on `materials[*]` carry the precise per-mesh stem; consumers should
   * prefer those when present.
   */
  texture_sets?: Record<string, TextureSet>;
  /**
   * Declared materials (shader_intent, factors, per-material texture_sets
   * when material_mappings.json is available). `texture_sets` here uses
   * the sidecar shape `{<scheme>: {<slot>: {dds_mips: [...]}}}` (different
   * from the asset-level shape above which inlines the path arrays
   * directly).
   */
  materials?: Array<{
    material_id?: string;
    texture_sets?: Record<string, Record<string, { dds_mips: string[] }>>;
    [key: string]: unknown;
  }>;
  /**
   * Toolkit-emitted per-asset attached-accessories pointer. Present on
   * every asset whose source `.skel_ext` carries non-trivial bundled
   * mesh placements (rangefinders, periscopes, ammo boxes, boats, decks).
   * The JSON file at this relative path lists each bundled placement
   * with its asset_id + local transform; the ship composer fetches it
   * for every host placement and instantiates the bundled children at
   * the listed transforms, applying the host's HP miscFilter from
   * `ShipPlacement.misc_filter`.
   */
  attached_accessories?: string;
  attachments_live_count?: number;
  attachments_dead_count?: number;
}

export interface TextureSet {
  baseColor?: string[];
  metallicRoughness?: string[];
  normal?: string[];
  occlusion?: string[];
  emissive?: string[];
  /**
   * Toolkit-emitted BC4 single-channel mask carrying WG's categorical
   * "no-camo region" marker (originally packed in the normal map's B
   * channel). The camo shader gates on this independently of the
   * normal map.
   */
  camoMask?: string[];
  [slot: string]: string[] | undefined;
}

export interface LibraryFilter {
  scope: string | null;
  category: string | null;
  subcategory: string | null;
  ship: string | null;
  deadOnly: boolean;
  newOnly: boolean;
  untexturedOnly: boolean;
  query: string;
}
