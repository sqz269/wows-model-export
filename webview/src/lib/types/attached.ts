// Schema for `<asset>.attached_accessories.json` (emitted by the pipeline's
// `asset_attachments_resolve`). Carries every WG-runtime-composed misc
// placement on a host accessory, keyed by `p0_hash → asset_id`.
//
// Two parallel lists: `attachments_live` (rendered when the host is
// intact) and `attachments_dead` (rendered when the host has been
// destroyed and its `_dead` mesh is shown).
//
// All transforms are metric, column-major, and **local to the host
// accessory's root** — the consumer applies them after positioning
// the host at its own world transform. Recursive composition is
// supported in principle (an attached child could itself have
// attached_accessories), but the WG corpus has only one level in practice.

export interface AttachedAccessory {
  asset_id: string;
  /** Raw `MP_<asset_id>[_INDEX_<n>]` string. */
  placement_id: string;
  /** 1-based, null for single-instance. */
  instance_index: number | null;
  p0_hash: string;
  p1_hash: string;
  transform: {
    /** 16 floats, column-major, host-local metric. */
    matrix: number[];
    position: [number, number, number];
  };
  source: {
    record_offset: string;
    matrix_index: number;
  };
  /** Resolver diagnostic; older sidecars omit it. */
  rotation_policy?:
    | 'as_emitted'
    | 'convention_b_host_space_position_only'
    | 'convention_b_external_prop_position_only'
    | 'convention_b_external_prop_y_conjugate'
    | 'convention_b_external_prop_post_ry180';
  host_space_child?: boolean | null;
}

export interface AttachedAccessoriesDoc {
  /** Currently "6" — see the schema_v6 baking notes in CLAUDE.md. */
  schema_version: string;
  /** Host asset (e.g. `AGM034_16in50_Mk7`). */
  asset_id: string;
  source: {
    skel_ext_candidates: string;
    candidates_total: number;
    kept_record_offsets: string[];
  };
  stats: {
    candidates_total: number;
    candidates_in_kept_records: number;
    unresolved_p0_hashes: number;
    filtered_skinned?: number;
    attachments_live: number;
    attachments_dead: number;
    distinct_assets: number;
    convention_b_external_y_conjugate?: number;
    convention_b_external_post_ry180?: number;
    convention_b_external_position_only?: number;
    convention_b_external_rotation_fixed?: number;
    convention_b_host_space_children?: number;
  };
  attachments_live: AttachedAccessory[];
  attachments_dead: AttachedAccessory[];
}
