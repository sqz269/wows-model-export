// Turret-rig types. Mirrors the JSON shapes the pipeline produces:
//   <asset>.rig_pivots.json    — emitted by `wows-turret-autorig`
//   <asset>.rig_overrides.json — read/written by the rig editor
//   winding_audit.json         — emitted by `wows-build-accessory-library --audit-winding`
//   flip_overrides.json        — read/written by per-asset flip toggle

/** Per-asset auto-detect winding verdict.
 *  Mirrors the schema produced by
 *  `compose.accessory_library._audit_winding`.
 *  Keyed by GLB-relative path (matches `LibraryAsset.glb`). */
export interface WindingAuditEntry {
  path: string;
  /** `unscored` for pathological geometry where Signal A+B failed. */
  verdict: 'flip' | 'keep' | 'ambiguous' | 'manual' | 'unscored';
  /** 0..1, joint A+B score (>0.5 = correct, <0.5 = inverted). */
  correctness: number;
  signal_b: number;
  signal_a: number;
  n_prim: number;
  /** True iff asset has a row in `flip_overrides.json`. */
  in_overrides: boolean;
}

export interface WindingAuditDoc {
  schema: string;
  generated_at: string;
  asset_count: number;
  summary: Record<string, number>;
  assets: WindingAuditEntry[];
}

// ── Rig pivots (turret_autorig output) ──────────────────────────────────

/** Rig pivot data from `compose.turret_autorig.autorig_asset`.
 *  Coords are in the glTF frame the library GLB uses (metric, right-
 *  handed) and apply to the Three.js scene AS-IS — unlike Blender,
 *  three.js's GLTFLoader does not rotate the mesh on import. */
export interface RigPivots {
  shared_elev: boolean;
  barrel_count: number;
  pivots: {
    yaw: [number, number, number];
    elev: [number, number, number];
    muzzle_tips: Array<[number, number, number]>;
    muzzle_tips_alt?: Array<[number, number, number]>;
  };
  warnings?: string[];
  /** OI-6 auto-flip: `turret_autorig` validates the extracted muzzles
   *  against the alive library mesh and bakes a Ry(180°) into the
   *  emitted pivots when WG's pre-aim-rotation pose was extracted.
   *  True here means the pivots have already been corrected — a
   *  subsequent flip-180° toggle would un-correct them. */
  auto_flipped_180_around_yaw?: boolean;
  geometric_check?: {
    verdict: 'ok' | 'needs_flip' | 'ambiguous' | 'no_mesh';
    muzzle_dists?: number[];
    muzzle_dists_flip?: number[];
    votes?: { ok: number; flip: number; tie: number };
    error?: string;
  };
}

// ── Rig editor (debug-scene picker) ────────────────────────────────────

/** Category a debug-scene piece can be classified as. */
export type RigCategory = 'body' | 'elev' | 'skin';

/** Stable fingerprint the override-loader uses to re-find a piece across
 *  rebuilds. Centre = bbox centre; verts = vertex count. */
export interface PieceFingerprint {
  center: [number, number, number];
  verts: number;
}

/** One piece extracted from a `<asset>.rig.debug.glb`. */
export interface PieceInfo {
  /** Index into the loaded debug scene's pieces array. Stable until
   *  the next load. */
  index: number;
  /** glTF node name from the rigger ("piece_NNNN_<cat>"). */
  name: string;
  /** Auto-classified category from the rigger run that produced this scene. */
  autoCategory: RigCategory;
  /** Was this piece picked by the rigger as the face-plate reference? */
  autoFacePlate: boolean;
  fingerprint: PieceFingerprint;
}

/** Rig override JSON shape (matches the rigger's persisted schema). */
export interface RigOverridesDoc {
  schema: string;
  asset_id: string;
  authored_at?: string;
  category_overrides?: Array<{
    fingerprint: PieceFingerprint;
    category: RigCategory;
    note?: string;
  }>;
  face_plate?: {
    fingerprint: PieceFingerprint;
    note?: string;
  };
}
